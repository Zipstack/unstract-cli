"""The stdout envelope and its renderings."""

from __future__ import annotations

import json

from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import (
    CONTRACT_VERSION,
    AgentMode,
    OutputFormat,
    emit_error,
    emit_result,
    envelope,
    render,
    render_table,
    resolve_format,
)

ENVELOPE_KEYS = {"ok", "data", "error", "meta"}


def test_success_envelope_shape():
    env = envelope(data={"a": 1}, meta={"took": 2})
    assert set(env) == ENVELOPE_KEYS
    assert env == {
        "ok": True,
        "data": {"a": 1},
        "error": None,
        "meta": {"took": 2, "contract_version": CONTRACT_VERSION},
    }


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
    assert envelope(data=1)["meta"] == {"contract_version": CONTRACT_VERSION}


def test_every_envelope_is_versioned():
    """A consumer cannot refuse a shape it was not written for without this."""
    for env in (envelope(data=1, meta={"job": "x"}), envelope(error={"code": "x"})):
        assert env["meta"]["contract_version"] == CONTRACT_VERSION


def test_stdout_carries_the_envelope_on_success(capsys):
    emit_result({"text": "hello"}, OutputFormat.JSON)
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "ok": True,
        "data": {"text": "hello"},
        "error": None,
        "meta": {"contract_version": CONTRACT_VERSION},
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
    assert render(env, OutputFormat.RAW, raw_fields=("text",)) == "hello"


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


class TestFormatSelection:
    """Which rendering a run gets, and what is allowed to influence it."""

    AGENT = {"CLAUDECODE": "1"}

    def test_the_default_is_a_table(self):
        assert resolve_format(None, env={}) is OutputFormat.TABLE

    def test_an_agent_environment_moves_the_default_to_json(self):
        for var in ("CLAUDECODE", "CURSOR_AGENT", "CODEX_SANDBOX", "AI_AGENT"):
            assert resolve_format(None, env={var: "1"}) is OutputFormat.JSON

    def test_an_unset_marker_is_not_an_agent(self):
        """An exported-but-empty variable is how a shell spells 'no'."""
        assert resolve_format(None, env={"CLAUDECODE": ""}) is OutputFormat.TABLE

    def test_an_explicit_format_beats_detection_in_both_directions(self):
        assert resolve_format("table", env=self.AGENT) is OutputFormat.TABLE
        assert resolve_format("json", env={}) is OutputFormat.JSON

    def test_the_agent_flag_overrides_what_the_environment_says(self):
        assert resolve_format(None, AgentMode.NO, self.AGENT) is OutputFormat.TABLE
        assert resolve_format(None, AgentMode.YES, {}) is OutputFormat.JSON

    def test_json_renders_the_same_bytes_wherever_it_is_asked_for(self):
        env = envelope(data={"text": "hello"})
        one = render(env, resolve_format("json", AgentMode.NO, {}))
        two = render(env, resolve_format("json", AgentMode.YES, self.AGENT))
        assert one == two
