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
        url = f"{PLATFORM_BASE}/deployment/api/org_test/my-api/"
        respx.get(url).mock(
            side_effect=[
                httpx.Response(
                    in_progress_status_code,
                    json={"message": {"execution_status": "EXECUTING"}},
                ),
                httpx.Response(
                    200,
                    json={"message": {"execution_status": "COMPLETED",
                                      "result": [{"file": "a.pdf", "status": "Success"}]}},
                ),
            ]
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        return wait_for_completion(
            endpoint=get_endpoint("deployment.run"),
            initial={"message": {"execution_id": "e-1", "execution_status": "PENDING"}},
            config=cfg,
            values={"api_name": "my-api"},
            sleep=lambda _: None,
        )

    @respx.mock
    def test_current_behaviour_422_in_progress(self, monkeypatch):
        """Today: 422 + EXECUTING means 'still running', not 'failed'."""
        result = self._run(monkeypatch, 422)
        assert extract_status(result, "execution_status") == "COMPLETED"

    @respx.mock
    def test_future_behaviour_200_in_progress(self, monkeypatch):
        """After the fix: 200 + EXECUTING must behave exactly the same."""
        result = self._run(monkeypatch, 200)
        assert extract_status(result, "execution_status") == "COMPLETED"

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
                endpoint=get_endpoint("deployment.run"),
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
                422, json={"message": {"execution_status": "ERROR", "error": "tool failed"}}
            )
        )
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_test"
        )
        with pytest.raises(CLIError) as exc:
            wait_for_completion(
                endpoint=get_endpoint("deployment.run"),
                initial={"message": {"execution_id": "e-1"}},
                config=cfg,
                values={"api_name": "my-api"},
                sleep=lambda _: None,
            )
        assert exc.value.exit_code is ExitCode.VALIDATION


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
