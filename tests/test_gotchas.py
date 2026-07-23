"""Regressions for the friction points recorded in GOTCHAS.md.

Each test pins the *behaviour a user hit*, not the implementation that fixes it,
so a later refactor is free to change the mechanism but cannot quietly restore
the friction. Numbers refer to GOTCHAS.md sections.
"""

from __future__ import annotations

import pytest

from unstract_cli.config.loader import ConfigFile, ResolvedConfig
from unstract_cli.core.errors import hint_for
from unstract_cli.core.http import build_request
from unstract_cli.core.model import RequiredUnless
from unstract_cli.endpoints import get_endpoint

from .conftest import FAKE_KEY


def _config(monkeypatch, **env) -> ResolvedConfig:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ResolvedConfig(file=ConfigFile(exists=False))


class TestProfileManagerOnPromptCreate:
    """#1 - `fetch-response` reads the PROMPT's profile, never the project default."""

    def test_prompt_create_accepts_a_profile(self):
        param = get_endpoint("docstudio.platform.prompt-studio.prompt.create").param(
            "profile_manager"
        )
        assert param is not None, "prompt create must be able to set the LLM profile"
        assert param.location.value == "body"

    def test_profile_reaches_the_body(self, monkeypatch):
        config = _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_1")
        plan = build_request(
            get_endpoint("docstudio.platform.prompt-studio.prompt.create"),
            config,
            {"tool_id": "t-1", "prompt_key": "invoice_no", "profile_manager": "p-9"},
        )
        assert plan.json_body["profile_manager"] == "p-9"
        # The tool_id mirror must survive alongside it (the older BUG 2 fix).
        assert plan.json_body["tool_id"] == "t-1"

    def test_the_misleading_error_gets_a_corrective_hint(self):
        hint = hint_for(500, message="Default LLM profile is not configured.")
        assert hint and "profile_manager" in hint
        # The whole point: stop the reader chasing `profile set-default`.
        assert "does NOT fall back" in hint or "not fall back" in hint.lower()


class TestChallengeLlm:
    """#2 - the 422 that only surfaced at the final `deployment run`."""

    def test_project_can_set_challenge_llm_before_export(self):
        for command in (
            "docstudio.platform.prompt-studio.create",
            "docstudio.platform.prompt-studio.patch",
        ):
            assert get_endpoint(command).param("challenge_llm") is not None

    def test_tool_validation_failure_explains_the_fix(self):
        hint = hint_for(422, message="Tool validation failed")
        assert hint and "challenge_llm" in hint
        assert "set-metadata" in hint


class TestVectorStoreStaysRequired:
    """#3 - asked for `--vector-store` to be optional at `--chunk-size 0`.

    It cannot be: `ProfileManager.vector_store` and `.embedding_model` are
    `null=False` and the serializer is `fields = "__all__"`, so DRF derives
    required=True and the server rejects the profile whatever chunk_size says.
    Relaxing the local rule would only trade a fast exit-2 for a slow remote 400,
    so the requirement stays and the help explains what to pass. These tests pin
    that decision so it is not "re-fixed" into a regression.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "docstudio.platform.prompt-studio.profile.create",
            "docstudio.platform.prompt-studio.profile.update",
        ],
    )
    def test_both_adapters_stay_required(self, command):
        for name in ("vector_store", "embedding_model"):
            assert get_endpoint(command).param(name).required, (
                f"{name} must stay required: the server rejects a profile without "
                "it even when chunk_size=0"
            )

    def test_help_explains_the_chunk_size_zero_case(self):
        endpoint = get_endpoint("docstudio.platform.prompt-studio.profile.create")
        assert "chunk-size 0" in endpoint.description
        # The caller must learn that the value is stored but unused, so that
        # supplying "any valid adapter id" reads as intended rather than a bodge.
        assert "REQUIRES" in endpoint.description


class TestRequiredUnlessPrimitive:
    """The conditional-required constraint itself, kept for cases that need it."""

    def test_sentinel_value_relaxes_the_requirement(self):
        constraint = RequiredUnless(("vector_store",), unless="chunk_size", unless_values=(0,))
        assert constraint.check({"chunk_size": 0}) is None
        assert constraint.check({"chunk_size": 1024}) is not None

    def test_string_and_int_sentinels_agree(self):
        # Click hands ints through typed, but config defaults and tests may not.
        constraint = RequiredUnless(("vector_store",), unless="chunk_size", unless_values=(0,))
        assert constraint.check({"chunk_size": "0"}) is None

    def test_supplying_the_param_always_satisfies_it(self):
        constraint = RequiredUnless(("vector_store",), unless="chunk_size", unless_values=(0,))
        assert constraint.check({"chunk_size": 1024, "vector_store": "v-1"}) is None


class TestSingleJsonDocumentOnStdout:
    """#4 - reported as duplicate output; stdout must carry exactly one document."""

    def test_stdout_parses_as_one_json_object(self, runner, cli, monkeypatch):
        import json

        monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", FAKE_KEY)
        monkeypatch.setenv("UNSTRACT_ORG_ID", "org_1")
        result = runner.invoke(
            cli,
            ["docstudio", "platform", "prompt-studio", "list", "--dry-run", "-o", "json"],
        )
        assert result.exit_code == 0
        json.loads(result.stdout)  # raises "Extra data" if the payload were doubled


class TestKeyCreateNeedsOneIdentifier:
    """#6 - `--api-id` alone must suffice; the body's `api` is the same value."""

    def test_api_key_create_mirrors_the_path_id(self, monkeypatch):
        config = _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_1")
        plan = build_request(
            get_endpoint("docstudio.platform.api-deployment.key.create"),
            config,
            {"api_id": "dep-1"},
        )
        assert plan.json_body["api"] == "dep-1"
        assert "dep-1" in plan.url

    def test_pipeline_key_create_mirrors_the_path_id(self, monkeypatch):
        config = _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_1")
        plan = build_request(
            get_endpoint("docstudio.platform.pipeline.key.create"),
            config,
            {"pipeline_id": "pipe-1"},
        )
        assert plan.json_body["pipeline"] == "pipe-1"

    def test_no_second_flag_is_demanded(self):
        # The old MutuallyExclusive(api, pipeline) rejected `--api-id` on its own.
        assert get_endpoint("docstudio.platform.api-deployment.key.create").validate(
            {"api_id": "dep-1"}
        ) == []


class TestOrgIdFallsBackToPlatform:
    """#7 - the deployment/hitl config blocks start empty, but the org is the same."""

    def test_deployment_run_uses_the_platform_org_id(self, monkeypatch):
        config = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_platform"
        )
        plan = build_request(
            get_endpoint("docstudio.deployment.run"),
            config,
            {"api_name": "invoice-api"},
        )
        assert "org_platform" in plan.url

    def test_an_explicit_org_id_still_wins(self, monkeypatch):
        config = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_platform"
        )
        plan = build_request(
            get_endpoint("docstudio.deployment.run"),
            config,
            {"api_name": "invoice-api", "org_id": "org_explicit"},
        )
        assert "org_explicit" in plan.url


class TestIndexDocumentSupportsWait:
    """#8 - index-document was the only async command without --wait."""

    def test_it_declares_a_poll_spec(self):
        endpoint = get_endpoint("docstudio.platform.prompt-studio.index-document")
        assert endpoint.poll is not None
        assert endpoint.poll.handle_field == "task_id"

    def test_indexing_has_nothing_to_retrieve(self):
        # Indexing writes no Output Manager row, so the terminal status IS the
        # result; a retrieve step would fetch an unrelated prompt's output.
        assert get_endpoint(
            "docstudio.platform.prompt-studio.index-document"
        ).poll.retrieve_endpoint is None

    def test_wait_flag_is_exposed(self, runner, cli):
        result = runner.invoke(
            cli,
            ["docstudio", "platform", "prompt-studio", "index-document", "--help"],
        )
        assert "--wait" in result.output


class TestDocumentedServerLimitations:
    """#5, #9, #10 - not CLI-fixable; the help must say so rather than mislead."""

    def test_export_tool_explains_the_new_registry_id(self):
        description = get_endpoint(
            "docstudio.platform.prompt-studio.export-tool"
        ).description
        assert "function_name" in description and "not" in description.lower()

    def test_project_delete_documents_the_registry_cascade(self):
        description = get_endpoint("docstudio.platform.prompt-studio.delete").description
        assert "cascade" in description.lower() or "registry" in description.lower()

    def test_default_triad_explains_an_empty_object(self):
        description = get_endpoint(
            "docstudio.platform.adapter.default-triad.get"
        ).description
        assert "{}" in description

    def test_adapter_choices_points_at_the_working_alternative(self):
        description = get_endpoint(
            "docstudio.platform.prompt-studio.adapter-choices"
        ).description
        assert "adapter list" in description

    def test_task_status_says_it_needs_the_tool_id(self):
        description = get_endpoint(
            "docstudio.platform.prompt-studio.task-status"
        ).description
        assert "--tool-id" in description
