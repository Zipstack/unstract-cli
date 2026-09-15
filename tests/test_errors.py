"""The exit-code table, retry policy and redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unstract_cli.core.discover import exit_codes
from unstract_cli.core.errors import (
    REDACTED,
    CLIError,
    ExitCode,
    error_code_for,
    error_codes,
    error_from_status,
    exit_code_for_status,
    hint_for,
    is_retryable,
    known_secrets,
    redact_value,
    remember_secret,
    scrub,
    scrub_structure,
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
        (406, ExitCode.GENERIC),
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
    assert exit_code_for_status(406, one_shot=True) is ExitCode.ALREADY_CONSUMED
    assert "already retrieved" in hint_for(406, one_shot=True)
    assert "--save" in hint_for(406, one_shot=True)


def test_a_406_outside_a_one_shot_read_is_not_reported_as_consumed():
    # Every other endpoint answers a 406 when it cannot serve the format asked
    # for, which no resend of the same request will fix and no --save averts.
    err = error_from_status(406, "Not Acceptable")
    assert err.exit_code is ExitCode.GENERIC
    assert "already retrieved" not in (err.hint or "")
    assert "base_url" in (err.hint or "")


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


def test_redact_value_walks_nested_payloads():
    out = redact_value({"a": {"api_key": "secret", "n": 1}, "b": [{"token": "t"}]})
    assert out == {"a": {"api_key": REDACTED, "n": 1}, "b": [{"token": REDACTED}]}


def test_scrub_ignores_short_values():
    # Redacting a 3-character "key" would mangle unrelated text.
    assert scrub("the key is abc", ["abc"]) == "the key is abc"
    assert scrub("the key is abcdefghij", ["abcdefghij"]) == f"the key is {REDACTED}"


def test_the_readme_table_lists_every_exit_code():
    """The README table is a copy of the enum, and the only one users read."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    documented = {
        int(row.split("|")[1]) for row in readme.splitlines() if _is_code_row(row)
    }

    assert documented == {int(code) for code in ExitCode}


def _is_code_row(row: str) -> bool:
    cells = row.split("|")
    return len(cells) > 2 and cells[1].strip().isdigit()


def test_the_rejected_key_hint_does_not_blame_the_organisation():
    """A key from another organisation cannot produce this: the resource is
    resolved within its own organisation first, so that answers 404."""
    hint = hint_for(401)
    assert "organisation" not in hint
    assert "does not cover" in hint
    assert "organisation" in hint_for(404)


# --------------------------------------------------------------------------- #
# The published table, and what may not silently escape redaction
# --------------------------------------------------------------------------- #


def test_every_exit_code_but_success_has_an_error_code():
    """`--discover full` publishes this table, so a code with no name is a hole
    in a contract rather than a missing string."""
    named = set(error_codes())
    assert named | {ExitCode.SUCCESS} == set(ExitCode)


def test_an_exit_code_with_no_token_falls_back_to_the_generic_one():
    """SUCCESS names no error, so the table published by `--discover` has to
    special-case it rather than trust this fallback."""
    assert error_code_for(ExitCode.SUCCESS) == "error"
    assert exit_codes()[0] == {"code": 0, "name": "success", "error_code": ""}


def test_a_request_timeout_is_retryable():
    """408 exits as TIMEOUT, which the CLI documents as worth retrying; saying
    otherwise here contradicts the exit code the same status produces."""
    assert is_retryable(408) is True
    assert is_retryable(400) is False


def test_extra_cannot_overwrite_a_field_callers_branch_on():
    err = CLIError(
        "boom",
        ExitCode.VALIDATION,
        extra={"exit_code": 0, "code": "ok", "whisper_hash": "h1"},
    )
    payload = err.to_dict()
    assert payload["exit_code"] == int(ExitCode.VALIDATION)
    assert payload["code"] == "validation_error"
    assert payload["whisper_hash"] == "h1"


def test_a_secret_named_key_is_redacted_whatever_type_it_holds():
    """A header echo arrives as a list as readily as as a string."""
    out = redact_value({"authorization": ["Bearer sk-live-1234567890"], "n": 1})
    assert out["authorization"] == REDACTED
    assert out["n"] == 1


def test_a_credential_too_short_to_scrub_for_says_so(warnings_seen):
    remember_secret("short")
    assert "short" not in known_secrets()
    assert any("will not be redacted" in note for note in warnings_seen)


def test_scrub_structure_replaces_before_anything_renders():
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz012345"
    out = scrub_structure({"a": [secret], "b": {secret: secret}}, [secret])
    assert secret not in json.dumps(out)


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "x-api-key",
        "Authorization",
        "authorization_header",
        "accessToken",
        "secretAccessKey",
        "refreshToken",
        "bearer_token",
        "credentials_json",
        "private_key_pem",
        "passwd",
        "token_value",
    ],
)
def test_a_credential_name_is_redacted_however_it_is_spelled(name):
    """camelCase is how a JSON body spells these, and the word marking a
    credential is not always the last one."""
    assert redact_value({name: "abcdef123456"})[name] == REDACTED


@pytest.mark.parametrize("name", ["authors", "rows", "execution_id", "message"])
def test_a_field_that_only_looks_like_a_credential_is_left_alone(name):
    assert redact_value({name: "abcdef123456"})[name] == "abcdef123456"


@pytest.mark.parametrize(
    "value",
    [
        "sk-live-1",
        ["live-key-1", None],
        {"value": "sk-live-1"},
        12345678,
    ],
)
def test_a_credential_is_collapsed_whatever_shape_it_arrives_in(value):
    """A token endpoint answers with any of these, and walking into one leaves
    the credential on the leaf it was reached through."""
    assert redact_value({"secret": value})["secret"] == REDACTED


def test_a_short_credential_warns_once_per_run(warnings_seen):
    remember_secret("short")
    remember_secret("short")
    assert sum("too short to scrub" in note for note in warnings_seen) == 1
