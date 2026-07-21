"""Shared fixtures.

Every test runs against an isolated config path and a controlled environment, so
a developer's real `~/.config/unstract/config.toml` can never influence results
(or be written to).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from unstract_cli.app import build_cli

#: Recognisable so redaction tests can assert this exact string never appears.
FAKE_KEY = "sk-test-SECRETVALUE-0123456789"

WHISPER_BASE = "https://llmwhisperer-api.us-central.unstract.com/api/v2"
PLATFORM_BASE = "https://us-central.unstract.com"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point config at a temp path and clear inherited credentials."""
    for var in (
        "UNSTRACT_PROFILE", "LLMWHISPERER_API_KEY", "LLMWHISPERER_BASE_URL",
        "UNSTRACT_PLATFORM_KEY", "UNSTRACT_DEPLOYMENT_KEY", "UNSTRACT_ORG_ID",
        "UNSTRACT_BASE_URL", "UNSTRACT_APIHUB_KEY", "UNSTRACT_APIHUB_BASE_URL",
        "UNSTRACT_ANTHROPIC_API_KEY", "UNSTRACT_OUTPUT", "NO_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    return tmp_path


@pytest.fixture
def whisper_env(monkeypatch):
    """Credentials for LLMWhisperer, supplied only through the environment.

    Deliberately env-only: this is also the assertion that the CLI is fully
    usable with no config file, which is how it runs in CI and agent sandboxes.
    """
    monkeypatch.setenv("LLMWHISPERER_API_KEY", FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def platform_env(monkeypatch):
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", FAKE_KEY)
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", FAKE_KEY)
    monkeypatch.setenv("UNSTRACT_ORG_ID", "org_test123")
    return FAKE_KEY


@pytest.fixture
def runner():
    """Click runner that keeps stderr separate, so stdout purity is testable."""
    return CliRunner()


@pytest.fixture
def cli():
    return build_cli()


@pytest.fixture
def sample_file(tmp_path):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4 fake document for testing")
    return path
