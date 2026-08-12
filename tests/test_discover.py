"""`--discover`, and the live half of `config doctor`.

Discovery is what an agent reads before it runs anything, so the tiers have to
stay cheap-then-detailed, and everything reported has to be read back from the
parser rather than described separately.
"""

from __future__ import annotations

import json

import pytest

from unstract_cli.__main__ import main
from unstract_cli.commands import config_cmd
from unstract_cli.core.errors import CLIError, ExitCode


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr().out
    return code, json.loads(out)["data"] if out.strip() else None


def test_groups_names_the_products_and_stops_there(capsys):
    """The cheap question stays cheap: no command list, no flags."""
    code, data = run(capsys, "--discover", "groups")
    assert code == int(ExitCode.SUCCESS)
    assert {g["name"] for g in data["groups"]} == {"config", "docstudio", "whisper"}
    assert all(g["help"] for g in data["groups"])
    assert "commands" not in data


def test_summary_lists_commands_without_their_flags(capsys):
    _, data = run(capsys, "--discover", "summary")
    whisper = data["commands"]["whisper"]["commands"]
    assert "extract" in whisper
    assert whisper["extract"]["help"]
    assert "params" not in whisper["extract"]


def test_full_carries_enough_to_build_a_call(capsys):
    _, data = run(capsys, "--discover", "full")
    extract = data["commands"]["whisper"]["commands"]["extract"]
    params = {p["name"]: p for p in extract["params"]}

    assert params["source"]["kind"] == "argument" and params["source"]["required"]
    assert params["mode"]["choices"] == [
        "form",
        "high_quality",
        "low_cost",
        "native_text",
        "table",
    ]
    assert params["wait"]["flags"] == ["--wait", "--no-wait"]
    assert params["interval"]["type"] == "float"
    assert extract["raw_field"] == "result_text"


def test_full_carries_the_exit_code_table(capsys):
    """A caller branches on these; they are part of the contract, not prose."""
    _, data = run(capsys, "--discover", "full")
    codes = {entry["name"]: entry["code"] for entry in data["exit_codes"]}
    assert codes["already_consumed"] == int(ExitCode.ALREADY_CONSUMED)
    assert codes["success"] == 0


def test_discovery_needs_no_configuration(capsys, tmp_path, monkeypatch):
    """It is how a caller finds out what to run, so it must work before anything
    is set up."""
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "nonexistent.toml"))
    code, data = run(capsys, "--discover", "summary")
    assert code == int(ExitCode.SUCCESS) and data["commands"]


def test_an_unknown_tier_is_a_usage_error(capsys):
    code = main(["--discover", "sideways"])
    capsys.readouterr()
    assert code == int(ExitCode.USAGE)


# --------------------------------------------------------------------------- #
# config doctor --probe
# --------------------------------------------------------------------------- #


@pytest.fixture
def probe_client(monkeypatch):
    def install(reply=None):
        class Fake:
            def get_usage_info(self):
                if isinstance(reply, Exception):
                    raise reply
                return reply or {}

        monkeypatch.setattr(config_cmd, "llmwhisperer", lambda _config: Fake())

    return install


def test_doctor_makes_no_call_without_probe(capsys, probe_client):
    probe_client(CLIError("must not be called"))
    code, data = run(capsys, "config", "doctor")
    assert code == int(ExitCode.SUCCESS)
    assert "probe" not in data


def test_probe_verifies_the_whisperer_key(capsys, probe_client):
    probe_client({"quota": 1})
    _, data = run(capsys, "config", "doctor", "--probe")
    assert data["probe"]["llmwhisperer"] == {
        "checked": True,
        "ok": True,
        "detail": "The key was accepted by the usage endpoint.",
    }


def test_a_rejected_key_reports_why(capsys, probe_client):
    probe_client(CLIError("bad key", ExitCode.AUTH))
    _, data = run(capsys, "config", "doctor", "--probe")
    entry = data["probe"]["llmwhisperer"]
    assert entry == {
        "checked": True,
        "ok": False,
        "detail": "bad key",
        "exit_code": int(ExitCode.AUTH),
    }


def test_the_deployment_probe_says_it_verified_nothing(capsys, probe_client, monkeypatch):
    """The only deployment endpoint is an execution, so there is nothing
    side-effect-free to call. Saying otherwise would be worse than not checking.
    """
    probe_client({})
    monkeypatch.setenv("UNSTRACT_ORG_ID", "org_A")
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "key")
    _, data = run(capsys, "config", "doctor", "--probe")
    entry = data["probe"]["docstudio"]
    assert entry["checked"] is False and entry["ok"] is True
    assert "not verified live" in entry["detail"]
