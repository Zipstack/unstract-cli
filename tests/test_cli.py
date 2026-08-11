"""End-to-end through the entry point: exit codes reach the shell, stdout parses."""

from __future__ import annotations

import json

import pytest

from unstract_cli.__main__ import main
from unstract_cli.app import cli, command_tree
from unstract_cli.core.errors import ExitCode


def run(capsys, *args):
    """Invoke the CLI as the console script does, returning (code, stdout json)."""
    code = main(list(args))
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


def test_table_output_is_opt_in_and_json_is_the_default(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    # JSON even on a TTY: a caller never has to detect the terminal to parse.
    assert run(capsys, "config", "doctor")[1]["ok"] is True

    main(["--output", "table", "config", "doctor"])
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "active_profile" in out


def test_click_parameter_info_dict_keeps_the_keys_discovery_reads():
    # Discovery derives flags from Click's own introspection; a Click bump that
    # reshaped this dict would silently degrade it.
    param = next(p for p in cli.params if p.name == "output")
    info = param.to_info_dict()
    assert {"name", "opts", "help", "type", "required"} <= set(info)
