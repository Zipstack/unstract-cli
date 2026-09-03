"""End-to-end through the entry point: exit codes reach the shell, stdout parses."""

from __future__ import annotations

import json
from pathlib import Path

from unstract_cli import app
from unstract_cli.__main__ import main
from unstract_cli.app import cli, command_tree
from unstract_cli.core.errors import ExitCode


def run(capsys, *args):
    """Invoke the CLI as the console script does, returning (code, stdout json).

    `-o json` is passed the way any consumer has to pass it: the default format
    is human-facing, and a test that relied on it would be pinning the wrong
    thing.
    """
    code = main(["-o", "json", *args])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def test_v1_groups_are_registered():
    tree = command_tree()
    assert set(tree) >= {"config", "whisper", "docstudio"}
    assert "deployment" in tree["docstudio"]["commands"]
    assert set(tree["config"]["commands"]) == {"doctor", "get", "init", "list", "set"}


def test_help_exits_zero(capsys):
    assert main(["--help"]) == int(ExitCode.SUCCESS)


def test_unknown_command_is_a_usage_error_with_an_envelope(capsys):
    code, payload, err = run(capsys, "nope")
    assert code == int(ExitCode.USAGE)
    assert payload["ok"] is False
    assert payload["error"]["exit_code"] == int(ExitCode.USAGE)
    assert err.startswith("error:")


def test_an_interrupt_exits_one_thirty_with_an_envelope(capsys, monkeypatch):
    """Ctrl-C is not a failure of the command. Reporting it as a generic error
    tells a supervisor to retry what the user deliberately stopped."""

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr("unstract_cli.commands.config_cmd.load_config", interrupted)

    code, payload, _ = run(capsys, "config", "doctor")

    assert code == int(ExitCode.INTERRUPTED) == 130
    assert payload["ok"] is False
    assert payload["error"]["code"] == "interrupted"


def test_unknown_config_target_exits_two(capsys):
    code, payload, _ = run(capsys, "config", "get", "nosuchproduct", "base_url")
    assert code == int(ExitCode.USAGE)
    assert "llmwhisperer" in payload["error"]["hint"]


def test_set_then_get_round_trip(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))

    code, payload, _ = run(capsys, "config", "set", "docstudio", "org_id", "org_A")
    assert code == 0 and payload["ok"] is True

    code, payload, _ = run(capsys, "config", "get", "docstudio", "org_id")
    assert code == 0
    assert payload["data"]["value"] == "org_A"


def test_set_warns_when_a_credential_is_stored_literally(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    _, payload, _ = run(capsys, "config", "set", "llmwhisperer", "api_key", "literal-key")
    assert "env:VAR_NAME" in payload["data"]["warning"]


def test_get_never_echoes_a_credential(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    run(capsys, "config", "set", "llmwhisperer", "api_key", "super-secret-value")
    _, payload, _ = run(capsys, "config", "get", "llmwhisperer", "api_key")
    assert payload["data"]["value"] == "***SET***"
    assert "super-secret-value" not in json.dumps(payload)


def test_init_refuses_to_clobber_without_force(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    assert run(capsys, "config", "init")[0] == 0

    code, payload, _ = run(capsys, "config", "init")
    assert code == int(ExitCode.USAGE)
    assert "--force" in payload["error"]["hint"]

    assert run(capsys, "config", "init", "--force")[0] == 0


def test_doctor_reports_sources_without_leaking_values(capsys, monkeypatch):
    monkeypatch.setenv("LLMWHISPERER_API_KEY", "super-secret-value")
    code, payload, _ = run(capsys, "config", "doctor")
    assert code == 0
    products = payload["data"]["products"]
    assert products["llmwhisperer"]["api_key"] == {
        "resolved": True,
        "source": "env:LLMWHISPERER_API_KEY",
    }
    assert products["docstudio"]["api_key"]["resolved"] is False
    assert "super-secret-value" not in json.dumps(payload)


def doctor(capsys, *args) -> str:
    """`config doctor` -- a command with no network -- and its raw stdout."""
    main([*args, "config", "doctor"])
    return capsys.readouterr().out


def is_table(out: str) -> bool:
    try:
        json.loads(out)
    except json.JSONDecodeError:
        return "active_profile" in out
    return False


class TestOutputFormatEndToEnd:
    """One rule: `-o` decides, and where it is absent the environment picks the
    default only. Everything here is a way of getting that wrong."""

    def test_the_default_is_a_table_in_a_terminal_and_in_a_pipe(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        assert is_table(doctor(capsys))
        monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
        assert is_table(doctor(capsys))

    def test_no_isatty_call_decides_a_format(self):
        """A format that depends on a terminal makes a script's output depend on
        how it was launched."""
        source = Path(app.__file__).parent
        offenders = [
            path.name
            for path in source.rglob("*.py")
            if "isatty" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_an_agent_environment_makes_json_the_default(self, capsys, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        assert json.loads(doctor(capsys))["ok"] is True

    def test_an_explicit_format_wins_over_a_detected_agent(self, capsys, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        assert is_table(doctor(capsys, "-o", "table"))

    def test_agent_no_forces_the_human_default(self, capsys, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        assert is_table(doctor(capsys, "--agent", "no"))

    def test_json_is_byte_identical_however_it_was_asked_for(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        on_a_tty = doctor(capsys, "-o", "json")

        monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
        monkeypatch.setenv("CLAUDECODE", "1")
        piped_under_an_agent = doctor(capsys, "-o", "json")

        assert on_a_tty == piped_under_an_agent

    def test_every_envelope_carries_the_contract_version(self, capsys):
        assert run(capsys, "config", "doctor")[1]["meta"]["contract_version"] == 1
        assert run(capsys, "nope")[1]["meta"]["contract_version"] == 1


def test_the_config_group_says_what_it_withheld(capsys, tmp_path, monkeypatch):
    """`config list` is one of the commands run *to understand* the config.

    It loads the file itself rather than through the root context, so it has to
    report the file's warnings on its own or stay silent about its own subject.
    """
    work = tmp_path / "checkout"
    work.mkdir()
    (work / ".unstract.toml").write_text(
        '[profiles.p.llmwhisperer]\napi_key = "planted"\n', encoding="utf-8"
    )
    monkeypatch.chdir(work)

    _, _, err = run(capsys, "config", "list")
    assert err.count("Ignoring p.llmwhisperer.api_key") == 1


def test_click_parameter_info_dict_keeps_the_keys_discovery_reads():
    # Discovery derives flags from Click's own introspection; a Click bump that
    # reshaped this dict would silently degrade it.
    param = next(p for p in cli.params if p.name == "output")
    info = param.to_info_dict()
    assert {"name", "opts", "help", "type", "required"} <= set(info)
