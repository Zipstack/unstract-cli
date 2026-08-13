"""Config resolution: flag > env > profile > built-in default."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

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
