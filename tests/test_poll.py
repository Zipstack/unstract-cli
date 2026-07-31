"""`--wait` polling, including the 422 defect (R4).

The single most important test in this suite is
`TestDeployment422Defect::test_422_and_200_resolve_identically`.

The Unstract deployment API currently returns **HTTP 422** for the in-progress
states `PENDING` and `EXECUTING` -- a documented server-side defect that is
scheduled to be fixed. If the CLI branched on the HTTP status code it would
either break today (reading in-progress as failure) or break the day the defect
is fixed. Branching on the response body's `status` field makes both behave
identically, and that is what these tests pin down.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from unstract_cli.config.loader import ConfigFile, ResolvedConfig
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.poll import extract_handle, extract_status, wait_for_completion
from unstract_cli.endpoints import get_endpoint

from .conftest import FAKE_KEY, PLATFORM_BASE, WHISPER_BASE


def _config(monkeypatch, **env) -> ResolvedConfig:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ResolvedConfig(file=ConfigFile(exists=False))


class TestFieldExtraction:
    def test_reads_bare_field(self):
        assert extract_status({"status": "processed"}) == "processed"

    def test_reads_through_message_envelope(self):
        """The deployment API nests its payload under `message`."""
        payload = {"message": {"execution_status": "COMPLETED"}}
        assert extract_status(payload, "execution_status") == "COMPLETED"

    def test_reads_handle_through_envelope(self):
        payload = {"message": {"execution_id": "e-123"}}
        assert extract_handle(payload, "execution_id") == "e-123"


class TestWhisperWait:
    @respx.mock
    def test_polls_then_retrieves(self, monkeypatch):
        respx.get(f"{WHISPER_BASE}/whisper-status").mock(
            side_effect=[
                httpx.Response(200, json={"status": "processing"}),
                httpx.Response(200, json={"status": "processed"}),
            ]
        )
        respx.get(f"{WHISPER_BASE}/whisper-retrieve").mock(
            return_value=httpx.Response(200, json={"result_text": "EXTRACTED"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        result = wait_for_completion(
            endpoint=get_endpoint("whisper.extract"),
            initial={"whisper_hash": "h1", "status": "processing"},
            config=cfg,
            sleep=lambda _: None,
        )
        assert result["result_text"] == "EXTRACTED"

    @respx.mock
    def test_error_status_raises(self, monkeypatch):
        respx.get(f"{WHISPER_BASE}/whisper-status").mock(
            return_value=httpx.Response(200, json={"status": "error", "message": "bad scan"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("whisper.extract"),
                initial={"whisper_hash": "h1"},
                config=cfg,
                sleep=lambda _: None,
            )
        assert exc.value.exit_code is ExitCode.VALIDATION

    @respx.mock
    def test_timeout_exits_7_and_returns_handle(self, monkeypatch):
        """On timeout an agent must be able to resume, not reprocess."""
        respx.get(f"{WHISPER_BASE}/whisper-status").mock(
            return_value=httpx.Response(200, json={"status": "processing"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        clock = iter([0.0, 0.0, 100.0, 200.0, 400.0, 500.0, 600.0])

        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("whisper.extract"),
                initial={"whisper_hash": "h-resume-me"},
                config=cfg,
                timeout=300.0,
                sleep=lambda _: None,
                now=lambda: next(clock),
            )
        assert exc.value.exit_code is ExitCode.TIMEOUT
        assert exc.value.to_dict()["error"]["whisper_hash"] == "h-resume-me"


class TestDeployment422Defect:
    """R4 - the highest-value test in the suite.

    `EXECUTING`/`PENDING` currently arrive with HTTP 422. When that defect is
    fixed they will arrive with HTTP 200. Both must behave identically.
    """

    @staticmethod
    def _run(monkeypatch, in_progress_status_code: int):
        # The real status GET returns a TOP-LEVEL `status`, and its `message` holds
        # the result -- NOT the nested `{message: {execution_status}}` shape the run
        # POST uses. Testing the true shape is what protects CAPTURE2 BUG 2.
        url = f"{PLATFORM_BASE}/deployment/api/org_test/my-api/"
        respx.get(url).mock(
            side_effect=[
                httpx.Response(
                    in_progress_status_code,
                    json={"status": "EXECUTING", "message": None},
                ),
                httpx.Response(
                    200,
                    json={"status": "COMPLETED",
                          "message": [{"file": "a.pdf", "status": "Success"}]},
                ),
            ]
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        return wait_for_completion(
            endpoint=get_endpoint("docstudio.deployment.run"),
            initial={"message": {"execution_id": "e-1", "execution_status": "PENDING"}},
            config=cfg,
            values={"api_name": "my-api"},
            sleep=lambda _: None,
        )

    @respx.mock
    def test_current_behaviour_422_in_progress(self, monkeypatch):
        """Today: 422 + EXECUTING means 'still running', not 'failed'."""
        result = self._run(monkeypatch, 422)
        assert extract_status(result, ("status", "execution_status")) == "COMPLETED"

    @respx.mock
    def test_future_behaviour_200_in_progress(self, monkeypatch):
        """After the fix: 200 + EXECUTING must behave exactly the same."""
        result = self._run(monkeypatch, 200)
        assert extract_status(result, ("status", "execution_status")) == "COMPLETED"

    @respx.mock
    def test_both_paths_agree(self, monkeypatch):
        a = self._run(monkeypatch, 422)
        respx.reset()
        b = self._run(monkeypatch, 200)
        assert a == b, "the 422 defect must not change the observable outcome"

    @respx.mock
    def test_genuine_422_without_status_still_fails(self, monkeypatch):
        """A 422 carrying no recognisable state is a real validation failure.

        Reading the body must not become a way to swallow genuine errors.
        """
        url = f"{PLATFORM_BASE}/deployment/api/org_test/my-api/"
        respx.get(url).mock(
            return_value=httpx.Response(
                422, json={"type": "client_error",
                           "errors": [{"detail": "Pipeline is inactive"}]}
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("docstudio.deployment.run"),
                initial={"message": {"execution_id": "e-1"}},
                config=cfg,
                values={"api_name": "my-api"},
                sleep=lambda _: None,
            )
        assert exc.value.exit_code is ExitCode.VALIDATION
        assert "inactive" in exc.value.message

    @respx.mock
    def test_error_status_is_terminal_failure(self, monkeypatch):
        url = f"{PLATFORM_BASE}/deployment/api/org_test/my-api/"
        respx.get(url).mock(
            return_value=httpx.Response(
                422, json={"status": "ERROR", "message": "tool failed"}
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("docstudio.deployment.run"),
                initial={"message": {"execution_id": "e-1"}},
                config=cfg,
                values={"api_name": "my-api"},
                sleep=lambda _: None,
            )
        assert exc.value.exit_code is ExitCode.VALIDATION

    @respx.mock
    def test_first_poll_terminal_returns_result_not_406(self, monkeypatch):
        """CAPTURE2 BUG 2 - fast completion: the very first status poll is already
        COMPLETED. The status endpoint is the one-shot store, so that read consumes
        the result. The loop must recognise the top-level `status` on that read and
        return its body (the result) -- not fail to recognise it, discard it, and
        406 on a second poll. A single mocked read enforces 'no second read'."""
        url = f"{PLATFORM_BASE}/deployment/api/org_test/my-api/"
        route = respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json={"status": "COMPLETED",
                      "message": [{"file": "bill.pdf", "result": {"invoice_no": "X-1"}}]},
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        result = wait_for_completion(
            endpoint=get_endpoint("docstudio.deployment.run"),
            initial={"message": {"execution_id": "e-1", "execution_status": "PENDING"}},
            config=cfg,
            values={"api_name": "my-api"},
            sleep=lambda _: None,
        )
        assert route.call_count == 1, "must not re-read a one-shot result"
        assert result["message"][0]["result"]["invoice_no"] == "X-1"


class TestPromptStudioWait:
    """IMPROVEMENT 3 - fetch-response is fire-and-forget; --wait must poll
    task-status to completion and then read the result from the Output Manager,
    which is keyed by the *original request's* tool_id, not the poll handle."""

    _PS_BASE = f"{PLATFORM_BASE}/api/v1/unstract/org_test/prompt-studio"

    @respx.mock
    def test_polls_task_status_then_reads_output(self, monkeypatch):
        respx.get(f"{self._PS_BASE}/the-tool/task-status/task-9").mock(
            side_effect=[
                httpx.Response(200, json={"task_id": "task-9", "status": "processing"}),
                httpx.Response(200, json={"task_id": "task-9", "status": "completed"}),
            ]
        )
        output = respx.get(f"{self._PS_BASE}/prompt-output/").mock(
            return_value=httpx.Response(
                200, json=[{"prompt_id": "p1", "output": "42", "modified_at": "t"}]
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        result = wait_for_completion(
            endpoint=get_endpoint("docstudio.platform.prompt-studio.fetch-response"),
            initial={"task_id": "task-9", "run_id": "r1", "status": "accepted"},
            config=cfg,
            values={"tool_id": "the-tool", "document_id": "d1", "id": "p1"},
            sleep=lambda _: None,
        )
        assert result[0]["output"] == "42"
        # The retrieve is narrowed to the exact prompt + document this call ran
        # (id->prompt_id, document_id->document_manager), keyed by the original
        # tool_id, and does NOT leak the task_id handle into the output-list call.
        url = str(output.calls.last.request.url)
        assert "tool_id=the-tool" in url
        assert "prompt_id=p1" in url
        assert "document_manager=d1" in url
        assert "task_id" not in url

    @respx.mock
    def test_failed_task_is_terminal_failure(self, monkeypatch):
        respx.get(f"{self._PS_BASE}/the-tool/task-status/task-9").mock(
            return_value=httpx.Response(
                500, json={"task_id": "task-9", "status": "failed", "error": "boom"}
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("docstudio.platform.prompt-studio.fetch-response"),
                initial={"task_id": "task-9", "status": "accepted"},
                config=cfg,
                values={"tool_id": "the-tool"},
                sleep=lambda _: None,
            )
        assert exc.value.exit_code is ExitCode.VALIDATION

    @respx.mock
    def test_single_pass_retrieve_asks_for_single_pass_rows(self, monkeypatch):
        respx.get(f"{self._PS_BASE}/the-tool/task-status/task-9").mock(
            return_value=httpx.Response(200, json={"status": "completed"})
        )
        output = respx.get(f"{self._PS_BASE}/prompt-output/").mock(
            return_value=httpx.Response(200, json=[{"output": "sp"}])
        )
        cfg = _config(
            monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        wait_for_completion(
            endpoint=get_endpoint("docstudio.platform.prompt-studio.single-pass"),
            initial={"task_id": "task-9", "status": "accepted"},
            config=cfg,
            values={"tool_id": "the-tool"},
            sleep=lambda _: None,
        )
        assert "is_single_pass_extract=true" in str(output.calls.last.request.url)


class TestApiHubWait:
    @respx.mock
    def test_traverses_three_stage_progression(self, monkeypatch):
        base = "https://hub.test"
        respx.get(f"{base}/api/v1/status").mock(
            side_effect=[
                httpx.Response(200, json={"status": "QUEUED_FOR_WHISPER"}),
                httpx.Response(200, json={"status": "QUEUED_FOR_EXTRACTION"}),
                httpx.Response(200, json={"status": "COMPLETED"}),
            ]
        )
        respx.get(f"{base}/api/v1/retrieve").mock(
            return_value=httpx.Response(200, json={"tables": [{"rows": 3}]})
        )
        cfg = _config(
            monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY, UNSTRACT_APIHUB_BASE_URL=base
        )
        result = wait_for_completion(
            endpoint=get_endpoint("apihub.extract"),
            initial={"file_hash": "fh-1", "status": "QUEUED_FOR_WHISPER"},
            config=cfg,
            sleep=lambda _: None,
        )
        assert result["tables"][0]["rows"] == 3


class TestOneShotDataLoss:
    """Regressions for the ways `--wait` could lose an unrepeatable result."""

    def test_handle_is_recovered_from_status_api_query(self):
        """The deployment run POST carries no `execution_id` field at all.

        Its real body is {execution_status, status_api, error, result}; the id
        exists only inside the status_api query string. Reading only the field
        meant `--wait` found no handle and returned the PENDING stub as success.
        """
        body = {
            "message": {
                "execution_status": "PENDING",
                "status_api": "/deployment/api/org/inv?execution_id=abc-123",
                "error": None,
                "result": None,
            }
        }
        assert extract_handle(body, "execution_id") is None, "field genuinely absent"
        assert extract_handle(body, "execution_id", ("status_api", "execution_id")) == "abc-123"

    def test_deployment_run_declares_the_query_fallback(self):
        spec = get_endpoint("docstudio.deployment.run").poll
        assert spec.handle_from_query == ("status_api", "execution_id")

    @respx.mock
    def test_missing_handle_fails_loudly_instead_of_faking_success(self, monkeypatch):
        """Exit 0 with a PENDING body is indistinguishable from a real result."""
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        initial = {"message": {"execution_status": "PENDING"}}
        with pytest.raises(CLIError) as excinfo:
            wait_for_completion(
                endpoint=get_endpoint("docstudio.deployment.run"),
                initial=initial,
                config=cfg,
                values={"api_name": "inv", "org_id": "org_test"},
                sleep=lambda _: None,
            )
        assert excinfo.value.exit_code != 0
        assert excinfo.value.details == initial, "the payload must survive the failure"

    @respx.mock
    def test_mid_poll_failure_carries_the_resume_handle(self, monkeypatch):
        """A 500 mid-poll must not strand an already-billed execution."""
        base = "https://us-central.unstract.com"
        respx.get(f"{base}/deployment/api/org_test/inv/").mock(
            return_value=httpx.Response(500, json={"error": "gateway blew up"})
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        with pytest.raises(CLIError) as excinfo:
            wait_for_completion(
                endpoint=get_endpoint("docstudio.deployment.run"),
                initial={"message": {"execution_id": "e-77", "execution_status": "PENDING"}},
                config=cfg,
                values={"api_name": "inv", "org_id": "org_test"},
                max_retries=0,
                sleep=lambda _: None,
            )
        assert excinfo.value.extra.get("execution_id") == "e-77"

    @respx.mock
    def test_poll_carries_include_metadata(self, monkeypatch):
        """The status record defaults it to False; the server then drops it."""
        base = "https://us-central.unstract.com"
        route = respx.get(f"{base}/deployment/api/org_test/inv/").mock(
            return_value=httpx.Response(200, json={"status": "COMPLETED", "message": "done"})
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        wait_for_completion(
            endpoint=get_endpoint("docstudio.deployment.run"),
            initial={"message": {"execution_id": "e-1", "execution_status": "PENDING"}},
            config=cfg,
            values={"api_name": "inv", "org_id": "org_test", "include_metadata": True},
            sleep=lambda _: None,
        )
        sent = str(route.calls[0].request.url)
        assert "include_metadata=true" in sent.lower()


class TestOneShotInvariants:
    def test_every_one_shot_endpoint_exposes_save(self):
        """A one-shot result can be read exactly once, and `--save` is the only
        way to keep it. `whisper extract` shipped without it while the CLI's own
        output told users to pass it."""
        from unstract_cli.endpoints import ALL_ENDPOINTS

        missing = [
            f"{ep.product.value}.{ep.group}.{ep.name}"
            for ep in ALL_ENDPOINTS
            if ep.poll and ep.poll.one_shot and "save" not in {p.py_name for p in ep.params}
        ]
        assert missing == []


class TestConsumedGuardWiring:
    """The success gate must be wired INTO the poll loop, not merely exist.

    A unit test calling raise_for_status directly cannot prove the loop passes
    the spec through; dropping that argument leaves such a test green while the
    guard goes inert in production.
    """

    @respx.mock
    def test_completed_result_containing_the_phrase_survives_a_real_poll(self, monkeypatch):
        base = "https://us-central.unstract.com"
        respx.get(f"{base}/deployment/api/org_test/inv/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "COMPLETED",
                    # Ordinary delivery-note wording inside the actual result.
                    "message": "Goods were already delivered to depot 4",
                },
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        result = wait_for_completion(
            endpoint=get_endpoint("docstudio.deployment.run"),
            initial={"message": {"execution_id": "e-1", "execution_status": "PENDING"}},
            config=cfg,
            values={"api_name": "inv", "org_id": "org_test"},
            sleep=lambda _: None,
        )
        assert "already delivered" in str(result), "the completed result must survive"
