"""The stdout envelope and its renderings."""

from __future__ import annotations

import json

from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import (
    OutputFormat,
    emit_error,
    emit_result,
    envelope,
    render,
    render_table,
)

ENVELOPE_KEYS = {"ok", "data", "error", "meta"}


def test_success_envelope_shape():
    env = envelope(data={"a": 1}, meta={"took": 2})
    assert set(env) == ENVELOPE_KEYS
    assert env == {"ok": True, "data": {"a": 1}, "error": None, "meta": {"took": 2}}


def test_error_envelope_shape():
    err = CLIError("boom", ExitCode.AUTH, http_status=401, hint="check the key")
    env = envelope(error=err.to_dict())
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is False and env["data"] is None
    assert env["error"] == {
        "code": "auth_error",
        "message": "boom",
        "exit_code": 3,
        "retryable": False,
        "http_status": 401,
        "hint": "check the key",
    }


def test_meta_defaults_to_an_object_not_null():
    # A caller reading meta.<x> should not have to null-check the container.
    assert envelope(data=1)["meta"] == {}


def test_stdout_carries_the_envelope_on_success(capsys):
    emit_result({"text": "hello"}, OutputFormat.JSON)
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "ok": True,
        "data": {"text": "hello"},
        "error": None,
        "meta": {},
    }
    assert out.err == ""


def test_stdout_carries_the_envelope_on_failure_and_stderr_gets_a_summary(capsys):
    code = emit_error(CLIError("nope", ExitCode.NOT_FOUND))
    out = capsys.readouterr()
    parsed = json.loads(out.out)
    assert parsed["ok"] is False and parsed["error"]["code"] == "not_found"
    assert out.err.strip() == "error: nope"
    assert code == ExitCode.NOT_FOUND


def test_secrets_are_scrubbed_from_both_streams(capsys):
    secret = "sk-supersecret-value"
    emit_error(CLIError(f"rejected token {secret}"), secrets=[secret])
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    assert "***REDACTED***" in out.out


def test_table_and_raw_render_the_payload_not_the_envelope():
    env = envelope(data={"text": "hello"})
    assert "hello" in render(env, OutputFormat.TABLE)
    assert "ok" not in render(env, OutputFormat.TABLE)
    assert render(env, OutputFormat.RAW, raw_field="text") == "hello"


def test_raw_renders_the_error_when_the_run_failed():
    env = envelope(error=CLIError("boom").to_dict())
    assert "boom" in render(env, OutputFormat.RAW)


def test_table_wraps_long_cells_rather_than_truncating():
    long = "word " * 40
    rendered = render_table([{"text": long.strip()}], max_width=40)
    assert rendered.count("\n") > 2
    assert "".join(rendered.split()).count("word") == 40


def test_table_of_an_empty_list_says_so():
    assert render_table([]) == "(no results)"
