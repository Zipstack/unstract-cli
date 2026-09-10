"""Turning a client's failure into an exit code, a hint and a payload."""

from __future__ import annotations

import pytest
from requests.exceptions import ConnectionError, TooManyRedirects
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException

from unstract_cli.core.clients import raise_for_result, translated
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
