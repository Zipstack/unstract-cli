"""Turning a client's failure into an exit code, a hint and a payload."""

from __future__ import annotations

import pytest
from requests.exceptions import ConnectionError, TooManyRedirects
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException

from unstract_cli.config import ResolvedConfig, load_config
from unstract_cli.core.clients import (
    deployment,
    deployment_url,
    raise_for_result,
    translated,
)
from unstract_cli.core.errors import CLIError, ExitCode


def _translate(exc: Exception) -> CLIError:
    with pytest.raises(CLIError) as caught, translated(endpoint="whisper"):
        raise exc
    return caught.value


def test_a_status_carrying_client_error_keeps_its_exit_code():
    err = _translate(LLMWhispererClientException("rate limited", status_code=429))
    assert err.exit_code is ExitCode.RATE_LIMITED
    assert err.retryable is True


def test_a_transport_failure_is_not_reported_as_a_local_disk_problem():
    """Every `requests` exception is an OSError, so anything left untranslated
    reaches the entry point's OSError handler and is blamed on the filesystem."""
    err = _translate(TooManyRedirects("too many redirects"))
    assert err.exit_code is ExitCode.SERVER_ERROR
    assert err.retryable is True
    assert "disk" not in (err.hint or "")


def test_an_unreachable_service_is_retryable():
    err = _translate(ConnectionError("connection refused"))
    assert err.exit_code is ExitCode.SERVER_ERROR
    assert err.retryable is True


def test_a_deployment_error_status_becomes_its_exit_code():
    with pytest.raises(CLIError) as caught:
        raise_for_result({"status_code": 404, "error": "no such API"})
    assert caught.value.exit_code is ExitCode.NOT_FOUND


def test_a_non_numeric_status_is_reported_rather_than_crashing():
    with pytest.raises(CLIError) as caught:
        raise_for_result({"status_code": "gateway"})
    assert caught.value.exit_code is ExitCode.SERVER_ERROR
    assert caught.value.details == {"status_code": "gateway"}


def test_a_missing_status_is_not_read_as_success():
    with pytest.raises(CLIError) as caught:
        raise_for_result({"result": "something"})
    assert caught.value.exit_code is ExitCode.SERVER_ERROR


def test_a_success_status_carrying_an_error_is_still_a_failure():
    with pytest.raises(CLIError) as caught:
        raise_for_result({"status_code": 200, "error": "the work was not done"})
    assert caught.value.exit_code is ExitCode.VALIDATION
    assert caught.value.retryable is False


def test_a_clean_success_raises_nothing():
    raise_for_result({"status_code": 200, "execution_status": "COMPLETED"})


CONFIG = """
default_profile = "p"

[profiles.p.docstudio]
base_url = "https://h/"
org_id = "org_profile"
api_key = "profile-key"

[profiles.p.deployments.invoices]
api_name = "invoice-parser"
org_id = "org_alias"
api_key = "alias-key"
"""


def _config(tmp_path, text: str = CONFIG):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return ResolvedConfig(file=load_config(path), profile_name="p")


def test_a_deployment_url_is_built_from_the_route_the_spec_declares():
    """The client reads the organisation and API name back out of the last two
    segments, so a URL that disagrees with the route fails inside the client."""
    url = deployment_url("https://h/", "org_A", "api-B")
    assert url.startswith("https://h/")
    assert url.endswith("/org_A/api-B/")
    assert "//" not in url.removeprefix("https://")


def test_an_alias_is_built_from_its_own_organisation_and_key(tmp_path):
    client = deployment(_config(tmp_path), "invoices")
    assert client.api_url.endswith("/org_alias/invoice-parser/")
    assert client.api_key == "alias-key"


def test_a_bare_api_name_falls_back_to_the_profile(tmp_path):
    client = deployment(_config(tmp_path), "some-api")
    assert client.api_url.endswith("/org_profile/some-api/")
    assert client.api_key == "profile-key"


def test_a_deployment_with_nothing_configured_names_everything_missing(tmp_path):
    """Both are required, and reporting one at a time costs a round trip each."""
    with pytest.raises(CLIError) as caught:
        deployment(_config(tmp_path, 'default_profile = "p"\n[profiles.p]\n'), "some-api")
    assert caught.value.exit_code is ExitCode.USAGE
    assert "org_id" in caught.value.message and "api_key" in caught.value.message


def test_a_target_that_is_not_an_alias_is_told_which_ones_are(tmp_path):
    """A bare API name is legal, so a misspelt alias cannot be rejected -- but
    a caller who defined aliases most likely meant one of them."""
    with pytest.raises(CLIError) as caught:
        deployment(
            _config(tmp_path, CONFIG.replace('org_id = "org_profile"\n', "")), "invoic"
        )
    assert "invoices" in (caught.value.hint or "")


def test_a_flag_fills_in_what_an_alias_leaves_out_and_no_more(tmp_path):
    """The precedence the README states: an alias owns the settings it names,
    and the connection flags reach only the ones it leaves to the profile."""
    config = _config(
        tmp_path,
        CONFIG + '\n[profiles.p.deployments.plain]\napi_name = "plain-api"\n',
    )
    config.overrides = {
        "docstudio.org_id": "org_flag",
        "docstudio.api_key": "flag-key",
        "docstudio.base_url": "https://flag-host",
    }

    stated = deployment(config, "invoices")
    assert stated.api_key == "alias-key"
    assert "/org_alias/" in stated.api_url

    silent = deployment(config, "plain")
    assert silent.api_key == "flag-key"
    assert "/org_flag/" in silent.api_url

    # base_url is not a per-alias setting, so the flag reaches both.
    assert stated.api_url.startswith("https://flag-host")
