"""Config resolution: flag > env > profile > built-in default."""

from __future__ import annotations

import errno
import os
import stat
import tomllib
from pathlib import Path

import pytest

from unstract_cli import config as config_module
from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    LLMWHISPERER,
    PROJECT_CONFIG_NAME,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    config_path,
    find_project_config,
    init_path,
    load_config,
    save_config,
    set_config_path,
    starter_profiles,
)

PROFILE_TOML = """
default_profile = "p"

[profiles.p.llmwhisperer]
base_url = "https://profile.example/api/v2"
api_key = "profile-key"

[profiles.p.docstudio]
org_id = "org_from_profile"
api_key = "env:UNSTRACT_DEPLOYMENT_KEY"

[profiles.p.deployments.invoices]
api_name = "invoice-parser"

[profiles.p.deployments.receipts]
api_name = "receipt-parser"
org_id = "org_alias"
api_key = "alias-key"
"""


def resolved(overrides=None, profile=None):
    return ResolvedConfig(
        file=load_config(), profile_name=profile, overrides=overrides or {}
    )


def test_default_when_nothing_configured():
    assert resolved().get(LLMWHISPERER, "base_url") == DEFAULT_BASE_URLS[LLMWHISPERER]
    assert resolved().get(LLMWHISPERER, "api_key") is None


def test_profile_beats_default(write_config):
    write_config(PROFILE_TOML)
    assert resolved().get(LLMWHISPERER, "base_url") == "https://profile.example/api/v2"


def test_env_beats_profile(write_config, monkeypatch):
    write_config(PROFILE_TOML)
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://env.example/api/v2")
    assert resolved().get(LLMWHISPERER, "base_url") == "https://env.example/api/v2"


@pytest.mark.parametrize(
    ("product", "key", "var", "value"),
    [
        (
            LLMWHISPERER,
            "base_url",
            "LLMWHISPERER_BASE_URL_V2",
            "https://staging.example/api/v2",
        ),
        (DOCSTUDIO, "api_key", "UNSTRACT_API_DEPLOYMENT_KEY", "deployment-key"),
    ],
)
def test_the_env_names_the_clients_read_are_honoured(
    monkeypatch, product, key, var, value
):
    """An environment set up for the published client must not leave the CLI on
    its built-in default, which points at production."""
    monkeypatch.setenv(var, value)
    assert resolved().get(product, key) == value


def test_the_cli_s_own_env_name_wins_over_the_client_s(monkeypatch):
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://first.example/api/v2")
    monkeypatch.setenv("LLMWHISPERER_BASE_URL_V2", "https://second.example/api/v2")
    assert resolved().get(LLMWHISPERER, "base_url") == "https://first.example/api/v2"


def test_override_beats_env(write_config, monkeypatch):
    write_config(PROFILE_TOML)
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://env.example/api/v2")
    cfg = resolved(overrides={"llmwhisperer.base_url": "https://flag.example"})
    assert cfg.get(LLMWHISPERER, "base_url") == "https://flag.example"


def test_env_indirection_resolves_and_missing_var_reads_as_unset(
    write_config, monkeypatch
):
    write_config(PROFILE_TOML)
    assert resolved().get(DOCSTUDIO, "api_key") is None
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "secret-value")
    assert resolved().get(DOCSTUDIO, "api_key") == "secret-value"


def test_require_names_every_way_to_supply_the_setting():
    with pytest.raises(ConfigError) as excinfo:
        resolved().require(DOCSTUDIO, "api_key")
    message = str(excinfo.value)
    assert "UNSTRACT_DEPLOYMENT_KEY" in message
    assert "[profiles.<name>.docstudio]" in message
    # Credentials get no flag, so none may be suggested.
    assert "--api-key" not in message


def test_placeholder_is_not_a_value(write_config):
    """`config init` writes `org_id = ""`, and that must not satisfy `require`."""
    write_config('default_profile = "p"\n\n[profiles.p.docstudio]\norg_id = ""\n')
    assert resolved().get(DOCSTUDIO, "org_id") is None
    assert resolved().resolution_source(DOCSTUDIO, "org_id")["resolved"] is False
    with pytest.raises(ConfigError):
        resolved().require(DOCSTUDIO, "org_id")


def test_starter_profile_org_id_does_not_satisfy_require(write_config):
    path = write_config("")
    save_config(ConfigFile(default_profile="cloud-us", profiles=starter_profiles()), path)
    with pytest.raises(ConfigError):
        resolved().require(DOCSTUDIO, "org_id")


def test_unknown_profile_is_an_error_not_a_silent_empty_block(write_config):
    write_config(PROFILE_TOML)
    with pytest.raises(ConfigError, match="not found"):
        resolved(profile="nope").get(DOCSTUDIO, "org_id")


def test_profile_selected_by_env_var(write_config, monkeypatch):
    write_config(PROFILE_TOML.replace('default_profile = "p"', ""))
    monkeypatch.setenv("UNSTRACT_PROFILE", "p")
    assert resolved().get(DOCSTUDIO, "org_id") == "org_from_profile"


def test_deployment_alias_falls_back_to_the_product_block(write_config, monkeypatch):
    write_config(PROFILE_TOML)
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "secret-value")
    alias = resolved().deployment("invoices")
    assert alias == {
        "api_name": "invoice-parser",
        "org_id": "org_from_profile",
        "api_key": "secret-value",
    }


def test_deployment_alias_overrides_win(write_config):
    write_config(PROFILE_TOML)
    alias = resolved().deployment("receipts")
    assert alias["org_id"] == "org_alias"
    assert alias["api_key"] == "alias-key"


def test_unknown_deployment_alias_lists_the_known_ones(write_config):
    write_config(PROFILE_TOML)
    with pytest.raises(ConfigError, match="invoices, receipts"):
        resolved().deployment("nope")


def test_resolution_source_reports_the_winner(write_config, monkeypatch):
    write_config(PROFILE_TOML)
    cfg = resolved()
    assert (
        cfg.resolution_source(LLMWHISPERER, "base_url")["source"] == "profile (literal)"
    )
    assert cfg.resolution_source(DOCSTUDIO, "base_url")["source"] == "built-in default"
    assert cfg.resolution_source(DOCSTUDIO, "api_key") == {
        "resolved": False,
        "source": "profile -> env:UNSTRACT_DEPLOYMENT_KEY",
        "detail": "$UNSTRACT_DEPLOYMENT_KEY is not set in this process's environment",
    }
    monkeypatch.setenv("LLMWHISPERER_API_KEY", "k")
    assert resolved().resolution_source(LLMWHISPERER, "api_key") == {
        "resolved": True,
        "source": "env:LLMWHISPERER_API_KEY",
    }


# --------------------------------------------------------------------------- #
# File discovery and writing
# --------------------------------------------------------------------------- #


def test_discovery_order(tmp_path, monkeypatch):
    from unstract_cli import config as config_mod

    home_default = config_mod.HOME_CONFIG
    assert config_path() == home_default

    project = tmp_path / "proj" / "nested"
    project.mkdir(parents=True)
    (tmp_path / "proj" / ".unstract.toml").touch()
    monkeypatch.chdir(project)
    assert config_path() == tmp_path / "proj" / ".unstract.toml"

    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "env.toml"))
    assert config_path() == tmp_path / "env.toml"

    set_config_path(tmp_path / "flag.toml")
    assert config_path() == tmp_path / "flag.toml"


def test_project_search_stops_at_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    # Above $HOME, so it must not be picked up.
    (tmp_path / ".unstract.toml").touch()
    assert find_project_config(work) is None


def test_missing_file_is_not_an_error():
    cfg = load_config()
    assert cfg.exists is False and cfg.profiles == {}


def test_saved_config_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    written = save_config(
        ConfigFile(default_profile="cloud-us", profiles=starter_profiles()), path
    )
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert load_config(written).default_profile == "cloud-us"


def test_an_existing_file_is_narrowed_before_the_secret_is_written(tmp_path, monkeypatch):
    """The mode passed to `os.open` applies only on creation, so rewriting a
    world-readable file would otherwise publish the new key while it is written."""
    path = tmp_path / "config.toml"
    path.write_text("")
    path.chmod(0o644)

    seen = []
    real = config_module.tomli_w.dump
    monkeypatch.setattr(
        config_module.tomli_w,
        "dump",
        lambda doc, fh: (
            seen.append(stat.S_IMODE(os.fstat(fh.fileno()).st_mode)),
            real(doc, fh),
        )[1],
    )
    written = save_config(ConfigFile(profiles=starter_profiles()), path)

    assert seen == [0o600]
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_previous_config_intact(tmp_path, monkeypatch):
    """Truncating the real file first would trade a working config for an empty
    one whenever anything after the truncate failed."""
    path = tmp_path / "config.toml"
    path.write_text('default_profile = "keep"\n', encoding="utf-8")

    monkeypatch.setattr(
        config_module.tomli_w,
        "dump",
        lambda doc, fh: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    with pytest.raises(OSError):
        save_config(ConfigFile(profiles=starter_profiles()), path)

    assert path.read_text(encoding="utf-8") == 'default_profile = "keep"\n'
    # And nothing half-written left behind next to it.
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_the_replacement_is_synced_before_it_is_renamed(tmp_path, monkeypatch):
    """A rename that outruns its own bytes survives a crash while the content
    does not, which turns a working config into an empty one."""
    path = tmp_path / "config.toml"
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        config_module.os,
        "fsync",
        lambda fd: (order.append("fsync"), real_fsync(fd))[1],
    )
    monkeypatch.setattr(
        config_module.os,
        "replace",
        lambda src, dst: (order.append("replace"), real_replace(src, dst))[1],
    )

    save_config(ConfigFile(profiles=starter_profiles()), path)

    assert order[: order.index("replace")] == ["fsync"]
    # The last one is the directory, so the rename itself is on the disk too.
    assert order[-1] == "fsync"


def test_a_directory_that_will_not_sync_is_warned_about_not_hidden(
    tmp_path, monkeypatch, warnings_seen
):
    """The config is already renamed into place by then, so the write is a
    success with a caveat, not a failure -- but not a silent success either."""
    path = tmp_path / "config.toml"
    real_fsync = os.fsync

    def fsync(fd):
        if os.fstat(fd).st_mode & stat.S_IFDIR:
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(config_module.os, "fsync", fsync)

    assert save_config(ConfigFile(profiles=starter_profiles()), path) == path
    assert path.exists()
    assert any(
        "could not be synced" in note and str(path) in note for note in warnings_seen
    )


def test_an_unwritable_directory_is_reported_rather_than_raised(tmp_path):
    """Replacing the file needs the directory, which overwriting it did not, so
    the case says what is wrong instead of surfacing a bare PermissionError."""
    nested = tmp_path / "locked"
    nested.mkdir()
    path = nested / "config.toml"
    path.write_text("", encoding="utf-8")
    nested.chmod(0o500)
    try:
        with pytest.raises(ConfigError, match="not writable"):
            save_config(ConfigFile(profiles=starter_profiles()), path)
    finally:
        nested.chmod(0o700)


def test_loose_permissions_warn_rather_than_fail(write_config):
    path = write_config(PROFILE_TOML)
    path.chmod(0o644)
    assert any("readable by other users" in w for w in load_config().warnings)


#: What a repository could commit: a host of its own choosing, and a key.
PROJECT_TOML = """
default_profile = "p"

[profiles.p.llmwhisperer]
base_url = "https://elsewhere.example/api/v2"
api_key = "project-literal-key"

[profiles.p.docstudio]
org_id = "org_from_project"

[profiles.p.deployments.invoices]
api_name = "invoice-parser"
api_key = "alias-literal-key"
"""


def _plant_project_config(tmp_path, monkeypatch):
    work = tmp_path / "checkout"
    work.mkdir()
    path = work / ".unstract.toml"
    path.write_text(PROJECT_TOML, encoding="utf-8")
    monkeypatch.chdir(work)
    return path


def test_a_discovered_project_config_supplies_no_key_and_no_host(tmp_path, monkeypatch):
    path = _plant_project_config(tmp_path, monkeypatch)
    cfg = resolved()

    assert cfg.get(LLMWHISPERER, "base_url") == DEFAULT_BASE_URLS[LLMWHISPERER]
    assert cfg.get(LLMWHISPERER, "api_key") is None
    assert cfg.deployment("invoices")["api_key"] is None
    # Everything the file is legitimately for still applies.
    assert cfg.get(DOCSTUDIO, "org_id") == "org_from_project"
    assert cfg.deployment("invoices")["api_name"] == "invoice-parser"
    assert any(str(path) in w and "Ignoring" in w for w in cfg.file.warnings)
    assert cfg.resolution_source(LLMWHISPERER, "api_key")["detail"]


def test_the_same_file_named_explicitly_is_honoured(tmp_path, monkeypatch):
    path = _plant_project_config(tmp_path, monkeypatch)
    monkeypatch.setenv("UNSTRACT_CONFIG", str(path))
    cfg = resolved()

    assert cfg.get(LLMWHISPERER, "base_url") == "https://elsewhere.example/api/v2"
    assert cfg.get(LLMWHISPERER, "api_key") == "project-literal-key"
    assert not any("Ignoring" in w for w in cfg.file.warnings)


def test_writing_back_a_project_config_keeps_the_keys_it_withheld(tmp_path, monkeypatch):
    path = _plant_project_config(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.profiles["p"]["docstudio"]["org_id"] = "org_edited"
    save_config(cfg)

    monkeypatch.setenv("UNSTRACT_CONFIG", str(path))
    reloaded = load_config()
    assert reloaded.profiles["p"]["docstudio"]["org_id"] == "org_edited"
    assert reloaded.profiles["p"]["llmwhisperer"]["api_key"] == "project-literal-key"
    assert reloaded.profiles["p"]["deployments"]["invoices"]["api_key"] == (
        "alias-literal-key"
    )


def test_a_table_this_cli_does_not_own_survives_a_write(write_config):
    path = write_config(PROFILE_TOML + "\n[telemetry]\nenabled = false\n")
    cfg = load_config()
    cfg.profiles["p"]["docstudio"]["org_id"] = "org_edited"
    save_config(cfg, path)

    assert load_config().profiles["p"]["docstudio"]["org_id"] == "org_edited"
    assert tomllib.loads(path.read_text(encoding="utf-8"))["telemetry"] == {
        "enabled": False
    }


def test_withheld_keys_are_not_carried_into_a_file_the_user_names(tmp_path, monkeypatch):
    _plant_project_config(tmp_path, monkeypatch)
    elsewhere = tmp_path / "named.toml"
    save_config(load_config(), elsewhere)

    monkeypatch.setenv("UNSTRACT_CONFIG", str(elsewhere))
    assert "api_key" not in load_config().profiles["p"]["llmwhisperer"]


def test_naming_the_discovered_file_does_not_make_it_trusted(tmp_path, monkeypatch):
    path = _plant_project_config(tmp_path, monkeypatch)
    work = path.parent
    (work / "sub").mkdir()
    (tmp_path / "link").symlink_to(work)

    # The outcome first: the flag is only the mechanism, withholding is the point.
    cfg = ResolvedConfig(file=load_config(path))
    assert cfg.get(LLMWHISPERER, "api_key") is None
    assert cfg.get(LLMWHISPERER, "base_url") == DEFAULT_BASE_URLS[LLMWHISPERER]

    # However the same file is spelled, it is the same file.
    for spelling in (
        Path(PROJECT_CONFIG_NAME),
        path,
        work / "sub" / ".." / PROJECT_CONFIG_NAME,
        tmp_path / "link" / PROJECT_CONFIG_NAME,
    ):
        assert load_config(spelling).is_project_local is True, spelling

    other = tmp_path / "elsewhere.toml"
    other.write_text(PROJECT_TOML, encoding="utf-8")
    assert load_config(other).is_project_local is False


def test_a_symlinked_project_candidate_is_not_discovered(tmp_path, monkeypatch):
    work = tmp_path / "checkout"
    work.mkdir()
    victim = tmp_path / "victim.toml"
    victim.write_text("keep = true\n", encoding="utf-8")
    (work / ".unstract.toml").symlink_to(victim)
    monkeypatch.chdir(work)

    assert find_project_config(work) is None
    assert config_path() != work / ".unstract.toml"


def test_a_write_through_a_symlink_fails_without_touching_its_target(tmp_path):
    victim = tmp_path / "victim.toml"
    victim.write_text("keep = true\n", encoding="utf-8")
    link = tmp_path / "config.toml"
    link.symlink_to(victim)

    with pytest.raises(ConfigError, match="symlink"):
        save_config(ConfigFile(profiles=starter_profiles()), link)
    assert victim.read_text(encoding="utf-8") == "keep = true\n"


def test_a_withheld_alias_key_is_reported_against_the_alias(tmp_path, monkeypatch):
    _plant_project_config(tmp_path, monkeypatch)
    cfg = resolved()
    assert cfg.withheld_detail("deployments", "invoices", "api_key")
    assert cfg.withheld_detail("deployments", "invoices", "org_id") is None


def test_starter_profiles_hold_no_literal_secrets():
    for blocks in starter_profiles().values():
        for settings in blocks.values():
            key = settings.get("api_key")
            assert key is None or key.startswith("env:")


def test_a_discovered_file_cannot_choose_which_env_var_is_read(
    tmp_path, monkeypatch, warnings_seen
):
    """`org_id` is not withheld from a project file, and it is spliced into the
    deployment URL and echoed back in any error about it. Letting a checkout
    the user did not write name the variable makes that an exfiltration path."""
    work = tmp_path / "work"
    work.mkdir()
    (work / PROJECT_CONFIG_NAME).write_text(
        '[profiles.p.docstudio]\norg_id = "env:CI_DEPLOY_TOKEN"\n', encoding="utf-8"
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("CI_DEPLOY_TOKEN", "tkn-should-never-be-read")

    cfg = ResolvedConfig(file=load_config(), profile_name="p")

    assert cfg.get(DOCSTUDIO, "org_id") is None
    assert any("may not choose which environment variable" in n for n in warnings_seen)


def test_a_named_file_may_still_use_env_indirection(tmp_path, monkeypatch):
    path = tmp_path / "named.toml"
    path.write_text('[profiles.p.docstudio]\norg_id = "env:MY_ORG"\n', encoding="utf-8")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(path))
    monkeypatch.setenv("MY_ORG", "org_ABC")

    cfg = ResolvedConfig(file=load_config(), profile_name="p")
    assert cfg.get(DOCSTUDIO, "org_id") == "org_ABC"


def test_doctor_reports_a_refused_env_reference_as_unresolved(tmp_path, monkeypatch):
    """Doctor exists to answer "I set it -- where is it looking?", so reporting
    a value the resolver refuses as resolved is the one answer it must not give."""
    work = tmp_path / "work"
    work.mkdir()
    (work / PROJECT_CONFIG_NAME).write_text(
        '[profiles.p.docstudio]\norg_id = "env:MY_ORG"\n', encoding="utf-8"
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("MY_ORG", "org_ABC")

    cfg = ResolvedConfig(file=load_config(), profile_name="p")
    report = cfg.resolution_source(DOCSTUDIO, "org_id")

    assert cfg.get(DOCSTUDIO, "org_id") is None
    assert report["resolved"] is False
    assert "may not choose which environment variable" in report["detail"]


def test_a_refused_alias_reference_names_the_trust_rule_not_a_missing_var(
    tmp_path, monkeypatch
):
    """Blaming an unset variable sends the user to export one that is set."""
    work = tmp_path / "work"
    work.mkdir()
    (work / PROJECT_CONFIG_NAME).write_text(
        '[profiles.p.docstudio]\napi_key = "k"\n'
        '[profiles.p.deployments.inv]\napi_name = "n"\norg_id = "env:MY_ORG"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("MY_ORG", "org_ABC")

    cfg = ResolvedConfig(file=load_config(), profile_name="p")
    with pytest.raises(ConfigError) as caught:
        cfg.deployment("inv")
    assert "may not choose which environment variable" in str(caught.value)


def test_an_override_is_only_read_under_the_key_it_is_written_with(tmp_path):
    """`resolution_source` and `get` have to look in the same place, or doctor
    reports a value resolved that the CLI never reads."""
    cfg = ResolvedConfig(
        file=load_config(), profile_name="p", overrides={"org_id": "bare"}
    )
    assert cfg.get(DOCSTUDIO, "org_id") is None
    assert cfg.resolution_source(DOCSTUDIO, "org_id")["resolved"] is False


def test_init_writes_the_home_config_even_inside_a_project(tmp_path, monkeypatch):
    """A discovered file is read-only as far as `init` is concerned.

    Walking up from the working directory is how a project config is *found*.
    Creating one that way writes a file the caller never named -- and one whose
    credentials the loader then refuses, because a discovered file is not
    trusted to supply them. The starter config has to land where it works.
    """
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / PROJECT_CONFIG_NAME).write_text(PROFILE_TOML)
    monkeypatch.chdir(project / "sub")

    assert find_project_config() == project / PROJECT_CONFIG_NAME
    assert init_path() == config_module.HOME_CONFIG.expanduser()


@pytest.mark.parametrize("named_by", ["flag", "env"])
def test_init_writes_the_file_the_caller_named(tmp_path, monkeypatch, named_by):
    """Naming a path is the trusted case, and it stays the target wherever it
    points -- including at a project file, which the caller has then chosen."""
    chosen = tmp_path / "chosen.toml"
    if named_by == "flag":
        set_config_path(chosen)
    else:
        monkeypatch.setenv("UNSTRACT_CONFIG", str(chosen))

    assert init_path() == chosen


def test_the_starter_config_is_one_the_loader_will_honour(tmp_path, monkeypatch):
    """The round trip the bug broke: init, then read it back and resolve a
    credential from it. Written to a discovered project file this fails, since
    `env:` indirection is refused there.
    """
    monkeypatch.setenv("LLMWHISPERER_API_KEY", "k-1")
    written = save_config(
        ConfigFile(default_profile="cloud-us", profiles=starter_profiles()),
        init_path(),
    )

    resolved = ResolvedConfig(file=load_config(written), profile_name="cloud-us")

    assert resolved.get(LLMWHISPERER, "api_key") == "k-1"
