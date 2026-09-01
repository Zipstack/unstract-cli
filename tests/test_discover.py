"""`--discover`, and the live half of `config doctor`.

Discovery is what an agent reads before it runs anything, so the tiers have to
stay cheap-then-detailed, and everything reported has to be read back from the
parser rather than described separately.
"""

from __future__ import annotations

import json

import click
import pytest

from unstract_cli.__main__ import main
from unstract_cli.commands import config_cmd
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import CONTRACT_VERSION


def run(capsys, *args):
    code = main(["-o", "json", *args])
    out = capsys.readouterr().out
    return code, json.loads(out)["data"] if out.strip() else None


def test_groups_names_the_products_and_stops_there(capsys):
    """The cheap question stays cheap: no command list, no flags."""
    code, data = run(capsys, "--discover", "groups")
    assert code == int(ExitCode.SUCCESS)
    assert {g["name"] for g in data["groups"]} == {"config", "docstudio", "whisper"}
    # A leaf listed among the groups is a group a consumer finds empty.
    assert [c["name"] for c in data["commands"]] == ["clone"]
    assert all(entry["help"] for entry in [*data["groups"], *data["commands"]])
    assert all("commands" not in entry for entry in data["groups"])


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
        "document_insights",
        "excel",
        "form",
        "high_quality",
        "low_cost",
        "native_text",
        "table",
    ]
    assert params["wait"]["flags"] == ["--wait", "--no-wait"]
    assert params["interval"]["type"] == "float"
    assert extract["raw_field"] == "result_text"


def test_full_publishes_the_flags_that_are_not_on_the_command(capsys):
    """The connection settings live on the group and the format on the root, so
    a description of the leaves alone describes a call nobody can make."""
    _, data = run(capsys, "--discover", "full")

    root = {p["name"]: p for p in data["params"]}
    assert "-o" in root["output"]["flags"]
    assert "--profile" in root["profile"]["flags"]

    whisper = {p["name"]: p for p in data["commands"]["whisper"]["params"]}
    assert "--api-key" in whisper["api_key"]["flags"]
    assert "--base-url" in whisper["base_url"]["flags"]
    assert "org_id" in {p["name"] for p in data["commands"]["docstudio"]["params"]}


def test_no_flag_publishes_a_default_it_does_not_have(capsys):
    """Click marks "no default given" with a sentinel object, not None, and a
    serialised sentinel reads as a value the caller could send back."""
    _, data = run(capsys, "--discover", "full")

    def defaults(node):
        for param in node.get("params", []):
            if "default" in param:
                yield param["name"], param["default"]
        for child in node.get("commands", {}).values():
            yield from defaults(child)

    published = list(defaults(data))
    assert published
    for name, value in published:
        assert not isinstance(value, str) or "Sentinel" not in value, name
        assert not repr(value).startswith("<"), name


def test_a_bare_option_publishes_no_default():
    """The case the CLI's own flags do not cover: an option declared with no
    default at all, which is what a derived required flag is."""
    from unstract_cli.core.discover import _param

    assert "default" not in _param(click.Option(["--bare"]))


@pytest.mark.parametrize("fmt", ["table", "raw", "json"])
def test_discovery_answers_as_json_whatever_the_format_says(capsys, fmt):
    """It is the machine-readable description; a wrapped table is not one."""
    assert main(["-o", fmt, "--discover", "summary"]) == int(ExitCode.SUCCESS)
    assert json.loads(capsys.readouterr().out)["data"]["commands"]


def test_full_carries_the_exit_code_table(capsys):
    """A caller branches on these; they are part of the contract, not prose."""
    _, data = run(capsys, "--discover", "full")
    codes = {entry["name"]: entry["code"] for entry in data["exit_codes"]}
    assert codes["already_consumed"] == int(ExitCode.ALREADY_CONSUMED)
    assert codes["success"] == 0


def test_full_publishes_how_to_consume_the_output(capsys):
    """The compatibility bargain is only binding if the consumer can read it."""
    _, data = run(capsys, "--discover", "full")
    contract = data["contract"]
    assert contract["version"] == CONTRACT_VERSION
    assert contract["envelope"] == ["ok", "data", "error", "meta"]
    rules = " ".join(contract["rules"]).lower()
    assert "-o json" in rules
    assert "ignore fields you do not recognise" in rules
    assert "contract_version" in rules


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
    """A probe that failed exits non-zero: --probe is run from setup scripts,
    and a script branches on the exit code, not on the payload."""
    probe_client(CLIError("bad key", ExitCode.AUTH))
    code = main(["-o", "json", "config", "doctor", "--probe"])
    report = json.loads(capsys.readouterr().out)["error"]["details"]
    assert code == int(ExitCode.GENERIC)
    entry = report["probe"]["llmwhisperer"]
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
    # `ok` is null rather than true: a true beside `checked: false` is read as a
    # live check that passed, which is the one thing this probe cannot claim.
    assert entry["checked"] is False and entry["ok"] is None
    assert entry["resolved"] is True
    assert "NOT verified" in entry["detail"]
