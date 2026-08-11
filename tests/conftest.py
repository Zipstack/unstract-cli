from __future__ import annotations

import pytest

from unstract_cli import config as config_mod

#: Every variable the loader consults. Cleared per test so a developer's real
#: shell environment cannot change a result.
_ENV_VARS = sorted(
    {var for vars_ in config_mod.ENV_VARS.values() for var in vars_}
    | {"UNSTRACT_CONFIG", "UNSTRACT_PROFILE"}
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config_mod.set_config_path(None)
    # Both discovery fallbacks are redirected into the tmp dir: an upward search
    # from a real cwd could otherwise find a developer's own .unstract.toml.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod, "HOME_CONFIG", tmp_path / "home" / "config.toml")
    yield
    config_mod.set_config_path(None)


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a config file and point the CLI at it."""

    def _write(text: str):
        path = tmp_path / "config.toml"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("UNSTRACT_CONFIG", str(path))
        return path

    return _write
