"""End-to-end through the entry point: exit codes reach the shell, stdout parses."""

from __future__ import annotations

import json
import os
import sys
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


def test_help_exits_zero():
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


def test_a_reader_that_went_away_does_not_raise_on_the_way_out(capfd, monkeypatch):
    """`... | head` closes the pipe mid-write; Python flushes stdout again at
    exit, so stdout has to be pointed somewhere that write can go.

    `capfd` rather than `capsys`: the redirect asks stdout for its descriptor,
    which a `capsys` stream refuses, and the refusal is swallowed by the same
    guard that swallows a failed redirect.
    """
    redirected = []

    def gone():
        raise BrokenPipeError

    monkeypatch.setattr("unstract_cli.commands.config_cmd.load_config", gone)
    monkeypatch.setattr(os, "dup2", lambda src, dst: redirected.append(dst))

    assert main(["-o", "json", "config", "doctor"]) == int(ExitCode.GENERIC)
    assert redirected == [sys.stdout.fileno()]


def test_an_unwritable_stream_is_an_envelope_rather_than_a_traceback(capsys, monkeypatch):
    def full_disk():
        raise OSError("No space left on device")

    monkeypatch.setattr("unstract_cli.commands.config_cmd.load_config", full_disk)

    code, payload, _ = run(capsys, "config", "doctor")

    assert code == int(ExitCode.GENERIC)
    assert payload["error"]["message"] == "No space left on device"
    assert "disk" in payload["error"]["hint"]


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


def test_set_refuses_a_setting_the_product_does_not_have(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    code, payload, _ = run(capsys, "config", "set", "llmwhisperer", "org_id", "org_A")

    assert code == int(ExitCode.USAGE)
    assert "org_id" in payload["error"]["message"]
    assert "base_url" in payload["error"]["hint"]
    assert not (tmp_path / "c.toml").exists()


def test_set_can_store_a_key_for_one_deployment(capsys, tmp_path, monkeypatch):
    """The entry is where a run looks after the environment and before the
    profile's key, so the write lands under the API name, not the product."""
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    code, payload, _ = run(
        capsys, "config", "set", "docstudio", "api_key", "dk-1", "--deployment", "inv"
    )
    assert code == 0 and payload["data"]["deployment"] == "inv"
    text = (tmp_path / "c.toml").read_text(encoding="utf-8")
    assert "[profiles.cloud-us.deployments.inv]" in text
    assert "[profiles.cloud-us.docstudio]" not in text


def test_set_refuses_to_store_anything_but_a_key_per_deployment(
    capsys, tmp_path, monkeypatch
):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "c.toml"))
    code, payload, _ = run(
        capsys, "config", "set", "docstudio", "org_id", "org_A", "--deployment", "inv"
    )
    assert code == int(ExitCode.USAGE)
    assert "--deployment" in payload["error"]["message"]
    assert not (tmp_path / "c.toml").exists()


def test_doctor_reports_a_setting_nothing_reads(capsys, write_config):
    write_config('default_profile = "p"\n\n[profiles.p.llmwhisperer]\norg_id = "org_A"\n')
    code, payload, _ = run(capsys, "config", "doctor")

    assert code != 0
    problems = payload["error"]["details"]["problems"]
    assert any("llmwhisperer.org_id" in problem for problem in problems)


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


def test_doctor_reports_the_platform_key_beside_the_deployment_key(capsys, monkeypatch):
    """Two keys on one block, each with its own row: a platform key is
    optional, so its absence is a report and not a problem."""
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-super-secret-value")
    code, payload, _ = run(capsys, "config", "doctor")
    assert code == 0
    docstudio = payload["data"]["products"]["docstudio"]
    assert docstudio["platform_key"] == {
        "resolved": True,
        "source": "env:UNSTRACT_PLATFORM_KEY",
    }
    assert "platform" not in payload["data"]["products"]
    assert "pk-super-secret-value" not in json.dumps(payload)


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
        how it was launched. `auth login` asks the question once, to decide
        whether it may prompt for keys -- never what it prints in."""
        source = Path(app.__file__).parent
        offenders = [
            path.name
            for path in source.rglob("*.py")
            if "isatty" in path.read_text(encoding="utf-8")
        ]
        assert offenders == ["platform_cmd.py"]
        text = (source / "commands" / "platform_cmd.py").read_text(encoding="utf-8")
        assert text.count("isatty") == 1 and "sys.stdin.isatty()" in text

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


def test_the_joined_output_form_is_accepted(capsys):
    """`--output=json` is the same request as `--output json`, and the pre-parse
    read of it decides what a parse failure is rendered in."""
    assert main(["--output=json", "--discover", "groups"]) == int(ExitCode.SUCCESS)
    assert json.loads(capsys.readouterr().out)["data"]["tier"] == "groups"


def test_an_unknown_output_format_is_reported_as_an_envelope(capsys):
    code, payload, _ = run(capsys, "--output", "yaml", "config", "list")
    assert code == int(ExitCode.USAGE)
    assert payload["error"]["exit_code"] == int(ExitCode.USAGE)


def test_a_clustered_short_option_still_selects_the_failure_format(capsys):
    """`-ojson` and `-o json` are the same option to Click, so a failure has to
    render the same way under both -- reading argv by hand only sees one."""
    assert main(["-ojson", "docstudio", "deployment", "status"]) == int(ExitCode.USAGE)
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"


def test_a_format_named_after_other_short_options_is_still_read(capsys):
    assert main(["-qojson", "whisper", "status"]) == int(ExitCode.USAGE)
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_a_bare_invocation_is_a_usage_error_in_a_parseable_format(capsys):
    """Help on stdout with exit 0 tells a parser the run succeeded, then hands
    it a page of prose where the envelope should be."""
    assert main(["-o", "json"]) == int(ExitCode.USAGE)
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"


def test_a_bare_invocation_still_prints_help_for_a_person(capsys):
    assert main(["-o", "table"]) == int(ExitCode.SUCCESS)
    assert "Commands:" in capsys.readouterr().out


def test_quiet_silences_a_note_from_below_the_output_layer(capsys, tmp_path, monkeypatch):
    """The config and credential registries cannot import the output layer, and
    went straight to stderr -- so `--quiet` reached everything except them."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".unstract.toml").write_text(
        '[profiles.p.docstudio]\norg_id = "env:CI_DEPLOY_TOKEN"\napi_key = "k-0123456789"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(work)
    monkeypatch.setenv("CI_DEPLOY_TOKEN", "tkn-never-read")

    args = ["-o", "json", "-p", "p", "docstudio", "deployment", "status", "a", "b"]
    main(args)
    assert "may not choose which environment variable" in capsys.readouterr().err

    main(["-q", *args])
    assert "may not choose which environment variable" not in capsys.readouterr().err
