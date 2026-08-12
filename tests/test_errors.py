"""The exit-code table, retry policy and redaction."""

from __future__ import annotations

import pytest

from unstract_cli.core.errors import (
    REDACTED,
    ExitCode,
    error_from_status,
    exit_code_for_status,
    hint_for,
    is_retryable,
    redact_headers,
    redact_value,
    scrub,
    undeclared_status_error,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Only a failure ever reaches this map: a 2xx or an unfollowed 3xx here
        # means something answered outside the contract, which is not success.
        (200, ExitCode.GENERIC),
        (302, ExitCode.GENERIC),
        (400, ExitCode.VALIDATION),
        (401, ExitCode.AUTH),
        (403, ExitCode.AUTH),
        (404, ExitCode.NOT_FOUND),
        (406, ExitCode.ALREADY_CONSUMED),
        (408, ExitCode.TIMEOUT),
        (409, ExitCode.VALIDATION),
        (418, ExitCode.GENERIC),
        (422, ExitCode.VALIDATION),
        (429, ExitCode.RATE_LIMITED),
        (500, ExitCode.SERVER_ERROR),
        (503, ExitCode.SERVER_ERROR),
    ],
)
def test_status_to_exit_code(status, expected):
    assert exit_code_for_status(status) is expected


def test_exit_codes_are_stable_integers():
    # A caller branches on these numbers, so they are an API, not an enum detail.
    assert [int(c) for c in ExitCode] == [*range(11), 130]
    assert int(ExitCode.ALREADY_CONSUMED) == 9
    assert int(ExitCode.SAVE_FAILED) == 10
    # 128 + SIGINT, which every shell and job runner already reads as
    # "stopped", rather than the next number in this CLI's own sequence.
    assert int(ExitCode.INTERRUPTED) == 130


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retryable(status):
    assert is_retryable(status)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 406, 409, 422])
def test_not_retryable(status):
    # A 4xx retry re-sends what the server already rejected, and for a one-shot
    # read it can consume a result the first attempt already delivered.
    assert not is_retryable(status)


def test_one_shot_status_carries_its_own_hint():
    assert "already retrieved" in hint_for(406)
    assert "--save" in hint_for(406)


def test_error_from_status_fills_code_hint_and_retryability():
    err = error_from_status(429, "slow down", endpoint="POST /whisper")
    assert err.exit_code is ExitCode.RATE_LIMITED
    assert err.retryable is True
    assert err.to_dict()["endpoint"] == "POST /whisper"


def test_undeclared_status_is_reported_verbatim_never_guessed():
    err = undeclared_status_error(418, {"detail": "teapot"})
    assert "Undeclared status 418" in err.message
    assert "teapot" in err.message
    assert err.to_dict()["details"] == {"detail": "teapot"}


def test_redact_headers():
    out = redact_headers(
        {
            "unstract-key": "abc",
            "Authorization": "Bearer x",
            "X-Api-Key": "y",
            "Content-Type": "application/json",
        }
    )
    assert out["unstract-key"] == out["Authorization"] == out["X-Api-Key"] == REDACTED
    assert out["Content-Type"] == "application/json"


def test_redact_value_walks_nested_payloads():
    out = redact_value({"a": {"api_key": "secret", "n": 1}, "b": [{"token": "t"}]})
    assert out == {"a": {"api_key": REDACTED, "n": 1}, "b": [{"token": REDACTED}]}


def test_scrub_ignores_short_values():
    # Redacting a 3-character "key" would mangle unrelated text.
    assert scrub("the key is abc", ["abc"]) == "the key is abc"
    assert scrub("the key is abcdefghij", ["abcdefghij"]) == f"the key is {REDACTED}"
