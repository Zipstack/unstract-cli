"""Config resolution: flag > env > profile > built-in default."""

from __future__ import annotations

import stat

import pytest

from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    LLMWHISPERER,
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


def test_starter_profiles_hold_no_literal_secrets():
    for blocks in starter_profiles().values():
        for settings in blocks.values():
            key = settings.get("api_key")
            assert key is None or key.startswith("env:")
