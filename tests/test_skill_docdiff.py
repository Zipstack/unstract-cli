"""Phase 6 acceptance: the Skill's docs-vs-records diff.

The strongest signal available is the **zero-drift regression**: the records were
authored from exactly these documentation files, so running the diff against the
current docs must report no actionable drift. If it does, either a record is
wrong or the parser is.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from unstract_cli.endpoints import endpoints_for, get_endpoint
from unstract_cli.skill.docdiff import (
    DocEndpoint,
    DocParam,
    diff,
    parse_docs,
    parse_markdown_params,
    report,
)

#: Docs repos are siblings of this one; skip rather than fail when absent, so the
#: suite still runs in a checkout that has only unstract-cli.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LLMW_DOCS = _REPO_ROOT / "llmwhisperer-docs/docs/llm_whisperer/apis"
_DEPLOY_DOCS = _REPO_ROOT / "unstract-docs/docs/unstract_platform/api_deployment"

_PLATFORM_DOCS = (
    _REPO_ROOT / "unstract-docs/docs/unstract_platform/api_documentation/versions"
)

#: Set in CI. These tests are the only oracle in the suite that is not the
#: endpoint records themselves, so silently skipping them there would leave 125
#: hand-encoded routes with no drift protection at all while CI stayed green.
#: With this set, a missing docs checkout fails the build instead.
_REQUIRE_DOCS = bool(os.environ.get("UNSTRACT_REQUIRE_DOCS"))


if _REQUIRE_DOCS:
    missing = [
        str(p) for p in (_LLMW_DOCS, _PLATFORM_DOCS, _DEPLOY_DOCS) if not p.exists()
    ]
    if missing:
        raise RuntimeError(
            "UNSTRACT_REQUIRE_DOCS is set but the documentation repos are not "
            "checked out alongside this one: " + ", ".join(missing) + ". "
            "The drift tests would silently skip, leaving the endpoint records "
            "with no external oracle."
        )

requires_docs = pytest.mark.skipif(
    not _LLMW_DOCS.exists(), reason="documentation repos not checked out alongside"
)
requires_platform_docs = pytest.mark.skipif(
    not _PLATFORM_DOCS.exists(), reason="unstract-docs not checked out alongside"
)


class TestMarkdownParsing:
    def test_extracts_request_parameters(self):
        text = """
## Parameters

| Parameter | Type | Default | Required | Description |
| --- | --- | --- | --- | --- |
| mode | string | `form` | No | The processing mode |
| whisper_hash | string | | Yes | The whisper hash |
"""
        params = {p.name: p for p in parse_markdown_params(text)}
        assert params["mode"].default == "form"
        assert params["whisper_hash"].required
        assert not params["mode"].required

    def test_ignores_response_tables(self):
        """Response fields are documented in the same format as parameters.

        Without section scoping, every response field reads as a missing flag.
        """
        text = """
## Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| whisper_hash | string | Yes | The hash |

## Response

| Parameter | Type | Description |
| --- | --- | --- |
| result_text | string | The extracted text |
| confidence_metadata | array | Confidence scores |
"""
        names = {p.name for p in parse_markdown_params(text)}
        assert names == {"whisper_hash"}

    def test_ignores_status_code_tables(self):
        text = """
## Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| execution_id | string | Yes | Execution id |

### Possible Execution status

| Status | Description |
| --- | --- |
| PENDING | Queued |
| COMPLETED | Done |
"""
        assert {p.name for p in parse_markdown_params(text)} == {"execution_id"}


class TestZeroDriftRegression:
    """Exit criterion 1: current docs must produce no actionable drift."""

    @requires_docs
    def test_llmwhisperer_has_no_drift(self):
        findings = [
            f
            for f in diff(parse_docs(_LLMW_DOCS), endpoints_for("llmwhisperer"))
            if f.severity == "action"
        ]
        assert not findings, "\n".join(f"{f.kind}: {f.message}" for f in findings)

    @requires_docs
    def test_deployment_has_no_drift(self):
        findings = [
            f
            for f in diff(parse_docs(_DEPLOY_DOCS), endpoints_for("deployment"))
            if f.severity == "action"
        ]
        assert not findings, "\n".join(f"{f.kind}: {f.message}" for f in findings)

    @requires_platform_docs
    def test_platform_mdx_parser_extracts_the_surface(self):
        """Assert positively before asserting absence.

        `missing_in_docs` is only informational, so a parser that silently
        extracted nothing would make a "no action findings" assertion pass while
        being completely broken. Pin the volume and a known parameter set first.
        """
        docs = parse_docs(_PLATFORM_DOCS)
        assert len(docs) > 90, f"MDX parser extracted only {len(docs)} endpoints"

        create = next(
            d for d in docs if d.method == "POST" and d.path.endswith("/workflow/")
        )
        names = {p.name for p in create.params}
        assert {"workflow_name", "description", "deployment_type"} <= names

        # responseBody fields must not be mistaken for request parameters.
        assert not names & {"id", "created_at", "created_by_email", "status"}

    @requires_platform_docs
    def test_platform_has_no_drift(self):
        findings = [
            f
            for f in diff(parse_docs(_PLATFORM_DOCS), endpoints_for("platform"))
            if f.severity == "action"
        ]
        assert not findings, "\n".join(f"{f.kind}: {f.message}" for f in findings)


class TestSyntheticDrift:
    """Exit criterion 2: a genuinely new parameter must be detected and cited."""

    def test_detects_new_parameter(self):
        doc = DocEndpoint(
            method="GET",
            path="/whisper-status",
            params=[DocParam(name="whisper_hash", required=True),
                    DocParam(name="include_progress", type="boolean")],
            source="whisper_status.md",
        )
        findings = diff([doc], endpoints_for("llmwhisperer"))
        drift = [f for f in findings if f.kind == "param_drift"]
        assert len(drift) == 1
        assert "include_progress" in drift[0].message
        assert drift[0].citation == "whisper_status.md"
        assert "Param(" in drift[0].suggestion

    def test_detects_new_endpoint(self):
        doc = DocEndpoint(method="POST", path="/whisper-cancel", source="new.md")
        findings = diff([doc], endpoints_for("llmwhisperer"))
        assert any(f.kind == "missing_in_cli" for f in findings)


class TestSafetyRules:
    """Exit criterion 3, plus the rules that prevent destructive 'fixes'."""

    def test_missing_from_docs_is_informational_only(self):
        """Docs lag implementation; absence is never grounds for deletion."""
        findings = diff([], endpoints_for("llmwhisperer"))
        removals = [f for f in findings if f.kind == "missing_in_docs"]
        assert removals, "expected the endpoints to be reported as undocumented"
        assert all(f.severity == "info" for f in removals)
        assert all("NEVER delete" in f.suggestion for f in removals)

    def test_doc_conflict_endpoints_are_exempt(self):
        """`/whisper-detail` diverges from the docs index deliberately.

        It must not be reported as removable, or a future run would 'correct' a
        decision that was verified against the official client.
        """
        assert get_endpoint("whisper.detail").doc_conflict
        reported = {
            f.command for f in diff([], endpoints_for("llmwhisperer")) if f.kind == "missing_in_docs"
        }
        assert "unstract whisper detail" not in reported

    def test_draft_pages_are_excluded(self, tmp_path):
        """`draft: true` marks an unstable contract; those endpoints stay out."""
        page = tmp_path / "chat.md"
        page.write_text(
            "---\nid: chat\ndraft: true\n---\n\n"
            "| Endpoint | `/md/chat` |\n| Method | `POST` |\n\n"
            "## Parameters\n\n| Parameter | Type | Required | Description |\n"
            "| --- | --- | --- | --- |\n| query | string | Yes | The question |\n"
        )
        assert parse_docs(tmp_path) == []


class TestReport:
    def test_report_is_valid_json(self):
        import json

        doc = DocEndpoint(method="POST", path="/whisper-cancel", source="new.md")
        payload = json.loads(report(diff([doc], endpoints_for("llmwhisperer"))))
        assert payload["total"] >= 1
        assert "missing_in_cli" in payload["summary"]
