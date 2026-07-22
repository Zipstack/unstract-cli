"""End-to-end CLI behaviour: the promises an agent actually depends on."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest
import respx

from unstract_cli.app import discover
from unstract_cli.endpoints import ALL_ENDPOINTS

from .conftest import FAKE_KEY, WHISPER_BASE


class TestWalkingSkeleton:
    """M1.3 - one endpoint, end to end, proving the whole spine."""

    @respx.mock
    def test_whisper_usage_succeeds(self, runner, cli, whisper_env):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(
                200, json={"subscription_plan": "free", "daily_quota": 100}
            )
        )
        result = runner.invoke(cli, ["whisper", "usage", "--output", "json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["subscription_plan"] == "free"

    @respx.mock
    def test_works_with_env_vars_only(self, runner, cli, whisper_env, isolated_env):
        """Zero-config operation: no config file exists at all here."""
        assert not (isolated_env / "config.toml").exists()
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(200, json={"subscription_plan": "free"})
        )
        assert runner.invoke(cli, ["whisper", "usage"]).exit_code == 0


class TestExitCodes:
    """SPEC §5.4 end-to-end, not just at the mapping layer."""

    @respx.mock
    @pytest.mark.parametrize(
        "status,expected",
        [(401, 3), (403, 3), (404, 4), (400, 5), (422, 5), (429, 6), (500, 8)],
    )
    def test_http_status_maps_to_exit_code(self, runner, cli, whisper_env, status, expected):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(status, json={"message": "failure"})
        )
        result = runner.invoke(cli, ["whisper", "usage", "--no-retry"])
        assert result.exit_code == expected

    def test_missing_credentials_is_usage_error(self, runner, cli):
        result = runner.invoke(cli, ["whisper", "usage"])
        assert result.exit_code == 2
        assert "LLMWHISPERER_API_KEY" in result.stderr

    def test_constraint_violation_is_usage_error(self, runner, cli, whisper_env, sample_file):
        result = runner.invoke(
            cli, ["whisper", "extract", "--file", str(sample_file), "--url", "http://x"]
        )
        assert result.exit_code == 2


class TestStdoutPurity:
    """SPEC §5.1 - `unstract ... | jq` must always parse."""

    @respx.mock
    def test_errors_go_to_stderr_only(self, runner, cli, whisper_env):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(403, json={"message": "Unauthorized"})
        )
        result = runner.invoke(cli, ["whisper", "usage", "--no-retry"])
        assert result.stdout.strip() == "", "stdout must stay empty on failure"
        assert json.loads(result.stderr)["error"]["code"] == "auth_error"

    @respx.mock
    def test_diagnostics_go_to_stderr(self, runner, cli, whisper_env):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        result = runner.invoke(cli, ["whisper", "usage", "-vv"])
        assert json.loads(result.stdout) == {"ok": True}

    @respx.mock
    def test_json_is_default_when_not_a_tty(self, runner, cli, whisper_env):
        """CliRunner's stdout is not a TTY, which is the agent's situation."""
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(200, json={"plan": "free"})
        )
        result = runner.invoke(cli, ["whisper", "usage"])
        assert json.loads(result.stdout) == {"plan": "free"}


class TestOneShotSemantics:
    """SPEC §5.6 - the footgun that silently loses an agent's data."""

    @respx.mock
    def test_save_writes_before_exit(self, runner, cli, whisper_env, tmp_path):
        respx.get(f"{WHISPER_BASE}/whisper-retrieve").mock(
            return_value=httpx.Response(200, json={"result_text": "IMPORTANT DATA"})
        )
        out = tmp_path / "result.txt"
        result = runner.invoke(
            cli, ["whisper", "retrieve", "--whisper-hash", "h1", "--save", str(out)]
        )
        assert result.exit_code == 0
        # raw_field means the saved artefact is the text itself, not the envelope.
        assert out.read_text() == "IMPORTANT DATA"

    @respx.mock
    def test_second_retrieve_exits_9(self, runner, cli, whisper_env):
        respx.get(f"{WHISPER_BASE}/whisper-retrieve").mock(
            return_value=httpx.Response(200, json={"message": "Whisper already delivered"})
        )
        result = runner.invoke(cli, ["whisper", "retrieve", "--whisper-hash", "h1"])
        assert result.exit_code == 9
        assert "once" in json.loads(result.stderr)["error"]["hint"].lower()

    @respx.mock
    def test_no_retry_on_consumed_result(self, runner, cli, whisper_env):
        """A retry here could consume a result the first call already delivered."""
        route = respx.get(f"{WHISPER_BASE}/whisper-retrieve").mock(
            return_value=httpx.Response(406, json={"message": "already delivered"})
        )
        runner.invoke(cli, ["whisper", "retrieve", "--whisper-hash", "h1"])
        assert route.call_count == 1


class TestDryRun:
    def test_prints_request_and_sends_nothing(self, runner, cli, whisper_env, sample_file):
        with respx.mock:
            route = respx.post(f"{WHISPER_BASE}/whisper")
            result = runner.invoke(
                cli, ["whisper", "extract", "--file", str(sample_file), "--dry-run"]
            )
            assert result.exit_code == 0
            assert route.call_count == 0, "--dry-run must not send the request"
        payload = json.loads(result.stdout)
        assert payload["method"] == "POST"
        assert payload["query"]["mode"] == "form"

    def test_redacts_secrets(self, runner, cli, whisper_env, sample_file):
        result = runner.invoke(
            cli, ["whisper", "extract", "--file", str(sample_file), "--dry-run"]
        )
        assert FAKE_KEY not in result.stdout
        assert "***REDACTED***" in result.stdout


class TestNoSecretLeaks:
    """SPEC §5.7 - a credential must not reach any stream, ever."""

    @respx.mock
    def test_verbose_error_path_stays_clean(self, runner, cli, whisper_env):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(401, json={"message": f"bad key {FAKE_KEY}"})
        )
        result = runner.invoke(cli, ["whisper", "usage", "-vv", "--no-retry"])
        assert FAKE_KEY not in result.stdout
        assert FAKE_KEY not in result.stderr, "the key leaked through the API's own message"


class TestDiscover:
    """SPEC §5.3 - the machine-readable index agents discover the CLI through."""

    def test_valid_json_covering_every_endpoint(self, runner, cli):
        result = runner.invoke(cli, ["--discover"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        endpoints = [c for c in data["commands"] if c["kind"] == "endpoint"]
        assert len(endpoints) == len(ALL_ENDPOINTS)

    def test_click_to_info_dict_contract(self):
        """R3's retirement rests on this exact shape.

        If a Click upgrade reshapes `to_info_dict()`, discovery would silently
        degrade -- so the dependency bump must fail here instead.
        """
        import click

        info = click.Option(
            ["--mode"], type=click.Choice(["a", "b"]), default="a", help="h"
        ).to_info_dict()
        for key in ("name", "opts", "type", "required", "multiple", "default", "help"):
            assert key in info, f"Click no longer reports {key!r}"
        assert info["type"]["choices"] == ("a", "b")

    def test_flags_carry_full_metadata(self):
        extract = next(
            c for c in discover(detail="full")["commands"]
            if c["command"] == "unstract whisper extract"
        )
        mode = next(f for f in extract["flags"] if f["name"] == "mode")
        assert mode["choices"] == ["native_text", "low_cost", "high_quality", "form", "table"]
        assert mode["default"] == "form"
        assert extract["api"] == {"method": "POST", "path": "/whisper"}
        assert extract["supports_wait"] and extract["one_shot"]

    def test_local_commands_flagged(self):
        """An agent must be able to tell local operations from API calls."""
        data = discover(detail="full")
        local = [c for c in data["commands"] if c["kind"] == "local"]
        assert {c["command"] for c in local} >= {"unstract config init", "unstract config use"}
        assert all("api" not in c for c in local)

    def test_documents_exit_codes_and_conventions(self):
        data = discover()
        assert data["exit_codes"]["9"].startswith("result already consumed")
        assert "never_interactive" in data["conventions"]

    def test_flag_is_discover_only(self, runner, cli):
        """`--dump-commands` was the original name and is fully removed.

        Pinned by a test so it cannot creep back in via a copied example.
        """
        assert "--discover" in runner.invoke(cli, ["--help"]).stdout
        result = runner.invoke(cli, ["--dump-commands"])
        assert result.exit_code != 0

    def test_advertised_params_match_the_real_parser(self, cli):
        """The index must describe what the parser actually accepts.

        This is the guarantee `--discover` exists to provide. It once failed for
        the hand-authored `config` group, which was described by a parallel data
        structure rather than introspected: it advertised `--product/--key/--value`
        for `config set`, whose parameters are positional. An agent following
        that index would build a command line the parser rejects.
        """
        mismatches = []
        for entry in discover(detail="full")["commands"]:
            node = cli
            for part in entry["path"]:
                node = node.commands.get(part) if hasattr(node, "commands") else None
                if node is None:
                    break
            assert node is not None, f"{entry['command']} is advertised but not registered"

            real = {
                p.to_info_dict()["name"]: (
                    "argument"
                    if p.to_info_dict()["param_type_name"] == "argument"
                    else "option"
                )
                for p in node.params
            }
            for flag in entry["flags"]:
                if real.get(flag["name"]) != flag["kind"]:
                    mismatches.append(
                        f"{entry['command']}: {flag['name']} advertised as "
                        f"{flag['kind']}, parser says {real.get(flag['name'])}"
                    )
        assert not mismatches, "\n".join(mismatches)

    def test_positional_args_are_not_advertised_as_flags(self):
        """A positional must never carry a `--flag` spelling."""
        entry = next(
            c for c in discover(command="config set", detail="full")["commands"]
        )
        arg = next(f for f in entry["flags"] if f["kind"] == "argument")
        assert "flags" not in arg, "a positional is not a flag"
        # The metavar spells out the real shape; the bare name ("ARGS") would not.
        assert entry["usage"] == "unstract config set TARGET... KEY VALUE"

    def test_usage_line_present_for_every_command(self):
        """`usage` shows the invocation form, which flags alone cannot convey."""
        for entry in discover(detail="full")["commands"]:
            assert entry["usage"].startswith("unstract "), entry["command"]

    def test_choices_reflect_the_real_enum(self):
        """Advertised choices must come from the parser, not a copy of it."""
        entry = next(
            c for c in discover(command="whisper extract", detail="full")["commands"]
        )
        mode = next(f for f in entry["flags"] if f["name"] == "mode")
        assert "form" in mode["choices"] and "high_quality" in mode["choices"]

    def test_summary_is_the_default(self):
        """Full detail for 143 commands is ~50k tokens -- too much to read
        speculatively, so the cheap index is what an unadorned call returns."""
        data = discover()
        assert data["detail"] == "summary"
        entry = data["commands"][0]
        assert set(entry) <= {"command", "kind", "summary"}
        assert "flags" not in entry

    def test_summary_explains_how_to_drill_down(self):
        data = discover()
        assert "--group" in data["drill_down"]["one_group"]
        assert set(data["groups"]) >= {"whisper", "docstudio", "apihub", "config"}

    def test_filter_by_group(self):
        data = discover(group="whisper")
        assert data["count"] == 11
        assert all(c["command"].startswith("unstract whisper") for c in data["commands"])

    def test_filter_by_exact_command(self):
        data = discover(command="whisper extract", detail="full")
        assert data["count"] == 1
        assert data["commands"][0]["api"] == {"method": "POST", "path": "/whisper"}

    def test_filter_by_command_prefix(self):
        """A prefix selects a subtree, so `whisper webhook` gets all four."""
        assert discover(command="whisper webhook")["count"] == 4

    def test_filter_tolerates_unstract_prefix(self):
        """`command` values are copied from output, which includes 'unstract '."""
        assert discover(command="unstract whisper extract")["count"] == 1

    def test_group_and_command_combine(self):
        assert discover(group="docstudio", command="docstudio platform adapter")["count"] == 13
        assert discover(group="whisper", command="docstudio platform adapter")["count"] == 0

    def test_detail_is_independent_of_selection(self):
        """Both axes are orthogonal: any combination must work."""
        for group in (None, "whisper"):
            for detail in ("summary", "full"):
                data = discover(group=group, detail=detail)
                assert data["count"] > 0
                assert data["detail"] == detail

    def test_narrow_queries_omit_global_boilerplate(self):
        """On a filtered view the exit-code table would outweigh the answer."""
        narrow = discover(group="whisper")
        assert "exit_codes" not in narrow
        assert "exit_codes" in discover()

    def test_no_match_exits_2_with_clean_stdout(self, runner, cli):
        result = runner.invoke(cli, ["--discover", "--group", "nope"])
        assert result.exit_code == 2
        assert result.stdout.strip() == ""
        assert "hint" in json.loads(result.stderr)["error"]

    def test_compact_when_piped(self, runner, cli):
        """Pretty-printing costs ~36% more tokens for a consumer that parses it."""
        out = runner.invoke(cli, ["--discover", "--group", "whisper"]).stdout
        assert json.loads(out)["count"] == 11
        assert "\n  " not in out, "output should be compact when stdout is not a TTY"

    def test_doc_conflict_recorded(self):
        """The whisper-detail divergence must be visible, so it is not 'fixed'."""
        detail = next(
            c for c in discover(detail="full")["commands"]
            if c["command"] == "unstract whisper detail"
        )
        assert "singular" in detail["doc_conflict"]


class TestHelp:
    def test_every_group_renders_help(self, runner, cli):
        for group in ("whisper", "docstudio", "apihub", "config"):
            result = runner.invoke(cli, [group, "--help"])
            assert result.exit_code == 0, f"{group} --help failed"

    def test_leaf_help_shows_api_and_examples(self, runner, cli):
        result = runner.invoke(cli, ["whisper", "extract", "--help"])
        assert "POST /whisper" in result.stdout
        assert "Examples:" in result.stdout

    def test_destructive_command_states_permission(self, runner, cli):
        result = runner.invoke(cli, ["docstudio", "platform", "workflow", "delete", "--help"])
        assert "full_access" in result.stdout

    def test_one_shot_help_warns(self, runner, cli):
        result = runner.invoke(cli, ["whisper", "retrieve", "--help"])
        assert "ONE-SHOT" in result.stdout

    def test_unknown_command_suggests(self, runner, cli):
        result = runner.invoke(cli, ["whisperr"])
        assert result.exit_code != 0
        assert "whisper" in result.stderr


class TestFlagCoverage:
    """SPEC §1.1.3 - every documented parameter must be reachable as a flag."""

    def test_all_params_surface_as_flags(self, cli):
        data = discover(detail="full")
        by_command = {c["command"]: c for c in data["commands"]}
        for endpoint in ALL_ENDPOINTS:
            entry = by_command["unstract " + " ".join(endpoint.command_path)]
            exposed = {f["name"] for f in entry["flags"]}
            for param in endpoint.params:
                assert param.py_name in exposed, (
                    f"{endpoint.dotted_name}: {param.name} is not reachable as a flag"
                )


class TestConfigCommands:
    """M1.5 - hand-authored commands, and never a prompt."""

    def test_init_writes_0600(self, runner, cli, isolated_env):
        result = runner.invoke(cli, ["config", "init"])
        assert result.exit_code == 0
        path = isolated_env / "config.toml"
        assert path.exists()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_init_refuses_to_clobber_without_force(self, runner, cli):
        runner.invoke(cli, ["config", "init"])
        result = runner.invoke(cli, ["config", "init"])
        assert result.exit_code == 2
        assert "--force" in json.loads(result.stderr)["error"]["hint"]

    def test_init_force_overwrites(self, runner, cli):
        runner.invoke(cli, ["config", "init"])
        assert runner.invoke(cli, ["config", "init", "--force"]).exit_code == 0

    def test_use_switches_default_profile(self, runner, cli):
        runner.invoke(cli, ["config", "init"])
        assert runner.invoke(cli, ["config", "use", "cloud-eu"]).exit_code == 0
        current = json.loads(runner.invoke(cli, ["config", "current"]).stdout)
        assert current["active_profile"] == "cloud-eu"

    def test_use_rejects_unknown_profile(self, runner, cli):
        runner.invoke(cli, ["config", "init"])
        result = runner.invoke(cli, ["config", "use", "nope"])
        assert result.exit_code == 2

    def test_starter_config_contains_no_secrets(self, runner, cli, isolated_env):
        runner.invoke(cli, ["config", "init"])
        content = (isolated_env / "config.toml").read_text()
        assert "env:" in content
        assert "sk-" not in content

    def test_get_never_echoes_a_secret(self, runner, cli, whisper_env):
        result = runner.invoke(cli, ["config", "get", "llmwhisperer", "api_key"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["configured"] is True
        assert FAKE_KEY not in result.stdout

    @pytest.mark.parametrize(
        "target", [["docstudio.platform"], ["docstudio", "platform"]]
    )
    def test_both_separators_address_the_same_group(self, runner, cli, target):
        """`docstudio platform` is what a shell user types; the dot is canonical."""
        runner.invoke(cli, ["config", "init"])
        assert runner.invoke(
            cli, ["config", "set", *target, "org_id", "org_X"]
        ).exit_code == 0
        shown = json.loads(
            runner.invoke(cli, ["config", "get", *target, "org_id"]).stdout
        )
        assert shown["value"] == "org_X"

    @pytest.mark.parametrize("bare", ["platform", "deployment", "hitl", "whisper"])
    def test_unqualified_target_is_rejected(self, runner, cli, bare):
        """A group owned by a product must be named through it.

        A bare `platform` hides which product the setting belongs to, so it is
        an error rather than a silent guess.
        """
        runner.invoke(cli, ["config", "init"])
        result = runner.invoke(cli, ["config", "set", bare, "org_id", "x"])
        assert result.exit_code == 2
        error = json.loads(result.stderr)["error"]
        assert "docstudio.platform" in error["hint"], "the hint must list valid targets"

    @pytest.mark.parametrize(
        "block", ["[profiles.p.whisper]", "[profiles.p.platform]", "[profiles.p.hitl]"]
    )
    def test_non_canonical_blocks_are_ignored(self, runner, cli, isolated_env, block):
        """There is exactly one accepted layout -- no aliases, no flat fallback.

        A block written any other way must NOT be silently picked up: a config
        that looks applied but is not surfaces later as a missing-credential
        error with no obvious cause.
        """
        (isolated_env / "config.toml").write_text(
            f'default_profile = "p"\n\n{block}\nbase_url = "https://wrong.example/"\n'
        )
        current = json.loads(runner.invoke(cli, ["config", "current"]).stdout)
        for settings in current["settings"].values():
            assert settings.get("base_url") != "https://wrong.example/"

    def test_canonical_block_is_read(self, runner, cli, isolated_env):
        (isolated_env / "config.toml").write_text(
            'default_profile = "p"\n\n'
            "[profiles.p.docstudio.platform]\n"
            'org_id = "org_CANON"\n'
        )
        shown = json.loads(
            runner.invoke(cli, ["config", "get", "docstudio.platform", "org_id"]).stdout
        )
        assert shown["value"] == "org_CANON"

    def test_set_writes_where_get_reads(self, runner, cli, isolated_env):
        """A write must land in the block the reader consults.

        Config blocks nest by product ([profiles.X.docstudio.platform]); writing
        a flat block instead would be silently ignored on read.
        """
        runner.invoke(cli, ["config", "init"])
        assert runner.invoke(
            cli, ["config", "set", "docstudio.platform", "org_id", "org_ROUNDTRIP"]
        ).exit_code == 0

        shown = json.loads(runner.invoke(cli, ["config", "get", "docstudio.platform", "org_id"]).stdout)
        assert shown["value"] == "org_ROUNDTRIP"

        text = (isolated_env / "config.toml").read_text()
        assert "[profiles.cloud-us.docstudio.platform]" in text
        assert "[profiles.cloud-us.platform]" not in text, "stray flat block written"

    def test_docstudio_owns_three_api_groups(self, runner, cli, isolated_env):
        """Document Studio's groups keep separate credentials and hosts."""
        runner.invoke(cli, ["config", "init"])
        text = (isolated_env / "config.toml").read_text()
        for api in ("platform", "deployment", "hitl"):
            assert f"[profiles.cloud-us.docstudio.{api}]" in text

    def test_config_flag_selects_a_file(self, runner, cli, tmp_path):
        """Several config files can coexist; --config picks one per invocation."""
        alt = tmp_path / "alt.toml"
        assert runner.invoke(cli, ["--config", str(alt), "config", "init"]).exit_code == 0
        assert alt.exists()

        shown = json.loads(
            runner.invoke(cli, ["--config", str(alt), "config", "path"]).stdout
        )
        assert shown["path"] == str(alt)

    def test_config_flag_outranks_env(self, runner, cli, monkeypatch, tmp_path):
        env_file, flag_file = tmp_path / "env.toml", tmp_path / "flag.toml"
        runner.invoke(cli, ["--config", str(flag_file), "config", "init"])
        monkeypatch.setenv("UNSTRACT_CONFIG", str(env_file))

        shown = json.loads(
            runner.invoke(cli, ["--config", str(flag_file), "config", "path"]).stdout
        )
        assert shown["path"] == str(flag_file), "flag must outrank $UNSTRACT_CONFIG"

    def test_project_config_found_from_subdirectory(self, runner, cli, tmp_path, monkeypatch):
        """A project's `.unstract.toml` applies to anyone working inside it.

        Found by walking upward, the way git and ruff locate their settings, so a
        repo can commit its own hosts and org without every caller passing flags.
        """
        monkeypatch.delenv("UNSTRACT_CONFIG", raising=False)
        project = tmp_path / "proj"
        (project / "deep" / "nested").mkdir(parents=True)
        (project / ".unstract.toml").write_text(
            'default_profile = "proj"\n\n'
            '[profiles.proj.llmwhisperer]\n'
            'base_url = "https://project.example/api/v2"\n'
        )
        monkeypatch.chdir(project / "deep" / "nested")

        shown = json.loads(runner.invoke(cli, ["config", "path"]).stdout)
        assert shown["path"] == str(project / ".unstract.toml")

        current = json.loads(runner.invoke(cli, ["config", "current"]).stdout)
        assert current["active_profile"] == "proj"
        assert current["settings"]["llmwhisperer"]["base_url"].startswith(
            "https://project.example"
        )

    def test_project_config_search_stops_at_home(self, monkeypatch, tmp_path):
        """A stray file above $HOME must not capture every invocation."""
        from unstract_cli.config.loader import find_project_config

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path.parent / ".unstract.toml").write_text("")
        work = tmp_path / "work"
        work.mkdir()
        try:
            assert find_project_config(work) is None
        finally:
            (tmp_path.parent / ".unstract.toml").unlink(missing_ok=True)

    def test_profile_selects_region(self, runner, cli, whisper_env):
        runner.invoke(cli, ["config", "init"])
        with respx.mock:
            route = respx.get(
                "https://llmwhisperer-api.eu-west.unstract.com/api/v2/get-usage-info"
            ).mock(return_value=httpx.Response(200, json={"plan": "eu"}))
            result = runner.invoke(cli, ["whisper", "usage", "--profile", "cloud-eu"])
            assert result.exit_code == 0, result.stderr
            assert route.call_count == 1
