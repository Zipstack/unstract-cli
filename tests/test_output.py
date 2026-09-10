"""The stdout envelope and its renderings."""

from __future__ import annotations

import json

import pytest

from unstract_cli.core.errors import REDACTED, CLIError, ExitCode, remember_secret
from unstract_cli.core.output import (
    CONTRACT_VERSION,
    AgentMode,
    OutputFormat,
    diagnostic,
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


# --------------------------------------------------------------------------- #
# Redaction has to survive the rendering, not follow it
# --------------------------------------------------------------------------- #

SECRET = "sk-live-éabcdefghijklmnopqrstuvwxyz012345"


def test_a_secret_is_scrubbed_before_the_table_can_wrap_it(capsys, monkeypatch):
    """The scrub has to run on the structure, not on the rendered text: a
    narrow terminal splits a long cell across lines, and a literal broken over
    a newline has nothing left for a text scrub to match."""
    monkeypatch.setattr("unstract_cli.core.output._terminal_width", lambda *a, **k: 30)
    # A value this long does wrap at this width -- so had the secret reached the
    # renderer intact, it would have been split rather than replaced.
    emit_result({"answer": "z" * len(SECRET)}, OutputFormat.TABLE)
    assert len(capsys.readouterr().out.strip().splitlines()) > 3

    emit_result({"answer": SECRET}, OutputFormat.TABLE, secrets=[SECRET])
    out = capsys.readouterr().out
    assert SECRET[:20] not in "".join(out.split())
    assert REDACTED in out


def test_a_secret_escaped_by_json_is_still_redacted(capsys):
    """`json.dumps` escapes non-ASCII, so the key in the output is not the key
    that was registered."""
    emit_result({"answer": SECRET}, OutputFormat.JSON, secrets=[SECRET])
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "abcdefghij" not in out
    assert json.loads(out)["data"]["answer"] == "***REDACTED***"


def test_an_agent_variable_set_to_zero_is_not_an_agent():
    assert resolve_format(None, env={"CLAUDECODE": "0"}) is OutputFormat.TABLE
    assert resolve_format(None, env={"CLAUDECODE": "1"}) is OutputFormat.JSON


def test_an_unknown_output_format_is_a_usage_error():
    with pytest.raises(CLIError) as caught:
        resolve_format("yaml")
    assert caught.value.exit_code is ExitCode.USAGE


def test_raw_prints_an_empty_answer_rather_than_the_next_field():
    """An empty result is a real answer, and printing the next field instead
    would hand back a handle where the caller expects text."""
    env = envelope(data={"extraction_result": "", "execution_id": "e-1"})
    assert (
        render(env, OutputFormat.RAW, raw_fields=("extraction_result", "execution_id"))
        == ""
    )


def test_a_wide_table_is_shrunk_in_one_pass():
    """Shaving one character per iteration is O(total width). The cap chosen has
    to be the widest one that fits, or the table is narrower than it need be."""
    wide = render_table([{"a": "x" * 4000, "b": "y" * 4000}], max_width=60)
    assert max(len(line) for line in wide.splitlines()) <= 60


def test_a_diagnostic_note_is_scrubbed_like_stdout(capsys):
    """A note can carry server-authored text, and a credential is no less
    leaked for arriving on the other stream."""
    remember_secret("sk-live-abcdef123456")
    diagnostic("retrying: rejected key sk-live-abcdef123456")
    err = capsys.readouterr().err
    assert "sk-live-abcdef123456" not in err
    assert REDACTED in err
