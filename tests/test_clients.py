"""Turning a client's failure into an exit code, a hint and a payload."""

from __future__ import annotations

import pytest
from requests.exceptions import (
    ConnectionError,
    ConnectTimeout,
    ReadTimeout,
    TooManyRedirects,
)
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException

from unstract_cli.config import ResolvedConfig, load_config
from unstract_cli.core.clients import (
    deployment,
    deployment_errors,
    deployment_url,
    raise_for_result,
    translated,
)
from unstract_cli.core.errors import CLIError, ExitCode, error_from_status


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


def test_a_request_that_timed_out_in_transit_says_the_job_may_still_run():
    err = _translate(ReadTimeout("read timed out"))
    assert err.exit_code is ExitCode.TIMEOUT
    assert err.retryable is True
    assert "still be running" in (err.hint or "")


def test_a_connect_timeout_is_a_timeout_rather_than_a_connection_failure():
    """`ConnectTimeout` is both, so which arm catches it is decided by their order."""
    err = _translate(ConnectTimeout("connect timed out"))
    assert err.exit_code is ExitCode.TIMEOUT


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

[profiles.p.deployments."invoice-parser"]
api_key = "entry-key"
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


def test_a_deployment_with_an_entry_runs_with_its_own_key(tmp_path):
    client = deployment(_config(tmp_path), "invoice-parser")
    assert client.api_url.endswith("/org_profile/invoice-parser/")
    assert client.api_key == "entry-key"


def test_a_deployment_client_is_built_with_a_socket_timeout_by_default(tmp_path):
    """The client sets none of its own, so without this a stalled connection
    is waited on forever."""
    client = deployment(_config(tmp_path), "some-api")
    assert client.transport_timeout == 120.0


def test_a_deployment_without_an_entry_runs_with_the_profile_key(tmp_path):
    client = deployment(_config(tmp_path), "some-api")
    assert client.api_url.endswith("/org_profile/some-api/")
    assert client.api_key == "profile-key"


@pytest.mark.parametrize(
    "ablate, winner",
    [
        ((), "flag-key"),
        (("flag",), "env-key"),
        (("flag", "env"), "entry-key"),
        (("flag", "env", "entry"), "profile-key"),
    ],
)
def test_the_key_for_a_run_resolves_flag_env_entry_profile(
    tmp_path, monkeypatch, ablate, winner
):
    """The per-deployment entry is the most specific value *within* the profile
    tier, not a tier above the environment: a file value that beat
    `$UNSTRACT_DEPLOYMENT_KEY` would let a stale entry hijack a run the caller
    set up for CI. Every source set, then removed one at a time from the top."""
    text = CONFIG if "entry" not in ablate else CONFIG.split("[profiles.p.deployments")[0]
    config = _config(tmp_path, text)
    if "env" not in ablate:
        monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "env-key")
    if "flag" not in ablate:
        config.overrides = {"docstudio.api_key": "flag-key"}

    assert deployment(config, "invoice-parser").api_key == winner


def test_a_deployment_with_no_key_anywhere_says_where_it_looked(tmp_path):
    """The chain walked, and the exact command for each remedy: a caller told
    only that a key is missing has to guess which of four places was read."""
    with pytest.raises(CLIError) as caught:
        deployment(
            _config(
                tmp_path,
                'default_profile = "p"\n[profiles.p.docstudio]\norg_id = "org_X"\n',
            ),
            "invoice-parser",
        )
    error = caught.value
    assert error.exit_code is ExitCode.USAGE
    assert "invoice-parser" in error.message
    for source in (
        "--api-key",
        "$UNSTRACT_DEPLOYMENT_KEY",
        '[profiles.p.deployments."invoice-parser"] api_key',
        "[profiles.p.docstudio] api_key",
    ):
        assert source in error.message, source
    assert "config set docstudio api_key <key> --deployment invoice-parser" in error.hint
    assert "API Key Manager" in error.hint


def test_a_deployment_with_no_organisation_fails_before_the_key_is_read(tmp_path):
    with pytest.raises(CLIError) as caught:
        deployment(_config(tmp_path, 'default_profile = "p"\n[profiles.p]\n'), "some-api")
    assert caught.value.exit_code is ExitCode.USAGE
    assert "organisation" in caught.value.message
    assert "auth login" in caught.value.hint


def test_a_rejected_key_is_reported_against_the_deployment():
    """The server's refusal is where "this deployment may need its own key"
    becomes true, so that is where it is said -- with the command that stores
    one."""
    with pytest.raises(CLIError) as caught, deployment_errors("invoice-parser"):
        raise error_from_status(401, "Unauthorized")
    error = caught.value
    assert error.exit_code is ExitCode.AUTH
    assert "invoice-parser" in error.message and "does not authorize" in error.message
    assert "config set docstudio api_key <key> --deployment invoice-parser" in error.hint


def test_an_unknown_deployment_is_pointed_at_the_listing():
    """An API name is editable server-side, so a name that was right can stop
    being right without anything local changing."""
    with pytest.raises(CLIError) as caught, deployment_errors("invoice-parser"):
        raise error_from_status(404, "Not found")
    assert "deployment ls" in caught.value.hint


def test_other_failures_pass_through_the_deployment_context_untouched():
    with pytest.raises(CLIError) as caught, deployment_errors("invoice-parser"):
        raise error_from_status(500, "boom")
    assert caught.value.message == "boom"
