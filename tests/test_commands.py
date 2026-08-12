"""The product commands, with the clients replaced. No network.

The seam is the client factory, not the transport: what matters here is which
arguments a command hands the client, what it does with the reply, and what a
caller sees on stdout and in the exit code.
"""

from __future__ import annotations

import json

import pytest
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)

from unstract_cli.__main__ import main
from unstract_cli.app import command_tree
from unstract_cli.commands import docstudio_cmd, whisper_cmd
from unstract_cli.core.errors import ExitCode


def run(capsys, *args):
    """Invoke the CLI as the console script does, returning (code, stdout, stderr)."""
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def envelope(out: str) -> dict:
    return json.loads(out)


class FakeWhisper:
    """Records calls; returns whatever the test queued."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls: list[tuple[str, tuple, dict]] = []

    def _reply(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        reply = self.replies.get(name)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, list):
            return reply.pop(0) if len(reply) > 1 else reply[0]
        return reply

    #: Pure geometry on a reply, so the real implementation is used rather than
    #: a queued answer.
    get_highlight_rect = LLMWhispererClientV2.get_highlight_rect

    def __getattr__(self, name):
        def call(*args, **kwargs):
            return self._reply(name, *args, **kwargs)

        return call

    def kwargs_for(self, name) -> dict:
        return next(kw for called, _, kw in self.calls if called == name)


@pytest.fixture
def whisper_client(monkeypatch):
    """Install a fake LLMWhisperer client and hand it back to the test."""

    def install(**replies):
        client = FakeWhisper(**replies)
        monkeypatch.setattr(whisper_cmd, "llmwhisperer", lambda _config: client)
        return client

    return install


@pytest.fixture
def deployment_client(monkeypatch):
    """Install a fake deployment client and hand it back to the test."""

    def install(**replies):
        client = FakeWhisper(**replies)
        client.api_url = "https://api.example.com/deployment/api/org/api-name/"
        monkeypatch.setattr(docstudio_cmd, "deployment", lambda _config, _t: client)
        return client

    return install


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #


def test_the_v1_commands_are_registered():
    tree = command_tree()
    assert set(tree["whisper"]["commands"]) == {
        "detail",
        "extract",
        "highlights",
        "retrieve",
        "status",
        "usage",
        "webhook",
    }
    assert set(tree["whisper"]["commands"]["webhook"]["commands"]) == {
        "create",
        "delete",
        "get",
        "update",
    }
    assert set(tree["docstudio"]["commands"]["deployment"]["commands"]) == {
        "run",
        "status",
    }


# --------------------------------------------------------------------------- #
# whisper extract
# --------------------------------------------------------------------------- #


def test_extract_without_wait_returns_the_handle(capsys, whisper_client, tmp_path):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = whisper_client(whisper={"whisper_hash": "h1", "status_code": 202})

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--no-wait")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["whisper_hash"] == "h1"
    assert client.kwargs_for("whisper")["file_path"] == str(doc)


def test_only_the_flags_that_were_passed_reach_the_client(
    capsys, whisper_client, tmp_path
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = whisper_client(whisper={"whisper_hash": "h1"})

    run(capsys, "whisper", "extract", str(doc), "--no-wait", "--mode", "table")

    sent = client.kwargs_for("whisper")
    assert sent["mode"] == "table"
    assert "lang" not in sent and "median_filter_size" not in sent


def test_a_falsy_flag_still_reaches_the_client(capsys, whisper_client, tmp_path):
    """`--median-filter-size 0` is a choice; a truthiness filter would drop it
    and silently leave the client's own default in place."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = whisper_client(whisper={"whisper_hash": "h1"})

    run(
        capsys,
        "whisper",
        "extract",
        str(doc),
        "--no-wait",
        "--median-filter-size",
        "0",
        "--no-add-line-nos",
    )

    sent = client.kwargs_for("whisper")
    assert sent["median_filter_size"] == 0
    assert sent["add_line_nos"] is False


def test_a_url_source_is_sent_as_a_url(capsys, whisper_client):
    client = whisper_client(whisper={"whisper_hash": "h1"})
    run(capsys, "whisper", "extract", "https://example.com/a.pdf", "--no-wait")
    sent = client.kwargs_for("whisper")
    assert sent["url"] == "https://example.com/a.pdf" and "file_path" not in sent


def test_the_cli_owns_the_wait_loop(capsys, whisper_client, tmp_path):
    """The client has a blocking loop of its own; using it would make --interval,
    --timeout and the handle-on-timeout behaviour product-specific."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status=[{"status": "processing"}, {"status": "processed"}],
        whisper_retrieve={"extraction": {"result_text": "hello"}},
    )

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0")

    assert code == int(ExitCode.SUCCESS)
    assert client.kwargs_for("whisper")["wait_for_completion"] is False
    assert envelope(out)["data"] == {"result_text": "hello"}


def test_raw_output_prints_the_extracted_text(capsys, whisper_client, tmp_path):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status={"status": "processed"},
        whisper_retrieve={"extraction": {"result_text": "hello"}},
    )

    _, out, _ = run(
        capsys, "-q", "-o", "raw", "whisper", "extract", str(doc), "--interval", "0"
    )
    assert out.strip() == "hello"


def test_wait_and_use_webhook_are_mutually_exclusive(capsys, whisper_client, tmp_path):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(whisper={"whisper_hash": "h1"})

    code, out, _ = run(
        capsys, "whisper", "extract", str(doc), "--use-webhook", "wh1", "--wait"
    )
    assert code == int(ExitCode.USAGE)
    assert "webhook" in envelope(out)["error"]["hint"]


def test_a_failed_extraction_carries_the_handle(capsys, whisper_client, tmp_path):
    """A caller can resume from the handle rather than resubmitting."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status={"status": "error", "message": "bad scan"},
    )

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0")
    assert code == int(ExitCode.VALIDATION)
    assert envelope(out)["error"]["whisper_hash"] == "h1"


# --------------------------------------------------------------------------- #
# Retrieval is one-shot
# --------------------------------------------------------------------------- #


def test_retrieve_saves_before_it_prints(capsys, whisper_client, tmp_path):
    """A result can be read once. Persisting after printing loses it to a broken
    pipe or a full terminal buffer."""
    target = tmp_path / "out" / "result.json"
    whisper_client(whisper_retrieve={"extraction": {"result_text": "hello"}})

    code, out, _ = run(capsys, "whisper", "retrieve", "h1", "--save", str(target))

    assert code == int(ExitCode.SUCCESS)
    assert json.loads(target.read_text())["result_text"] == "hello"
    assert envelope(out)["data"]["result_text"] == "hello"


def test_an_already_consumed_result_has_its_own_exit_code(capsys, whisper_client):
    whisper_client(whisper_retrieve=LLMWhispererClientException("already retrieved", 406))
    code, out, _ = run(capsys, "whisper", "retrieve", "h1")
    assert code == int(ExitCode.ALREADY_CONSUMED)
    assert "once" in envelope(out)["error"]["hint"]


# --------------------------------------------------------------------------- #
# Errors from the client
# --------------------------------------------------------------------------- #


def test_an_auth_failure_maps_onto_its_exit_code(capsys, whisper_client):
    whisper_client(get_usage_info=LLMWhispererClientException("bad key", 401))
    code, out, _ = run(capsys, "whisper", "usage")
    assert code == int(ExitCode.AUTH)
    assert envelope(out)["error"]["message"] == "bad key"


def test_an_error_body_keeps_its_own_wording(capsys, whisper_client):
    whisper_client(
        whisper_detail=LLMWhispererClientException(
            {"message": "no such hash", "status_code": 404}
        )
    )
    code, out, _ = run(capsys, "whisper", "detail", "h1")
    assert code == int(ExitCode.NOT_FOUND)
    error = envelope(out)["error"]
    assert error["message"] == "no such hash"
    assert error["details"]["status_code"] == 404


# --------------------------------------------------------------------------- #
# highlights
# --------------------------------------------------------------------------- #


def test_highlights_scales_line_metadata_when_a_page_size_is_given(
    capsys, whisper_client
):
    """Pure arithmetic on the reply, so it is folded into this command rather
    than being a command that makes no request."""
    whisper_client(get_highlight_data={"1": [1, 100, 20, 1000]})
    code, out, _ = run(
        capsys,
        "whisper",
        "highlights",
        "h1",
        "--lines",
        "1-5",
        "--target-width",
        "600",
        "--target-height",
        "800",
    )
    assert code == int(ExitCode.SUCCESS)
    data = envelope(out)["data"]
    assert data["rects"]["1"] == [1, 0, 64, 600, 80]


def test_highlights_reads_the_named_metadata_object(capsys, whisper_client):
    """The service returns the list inside an object; the client's geometry takes
    the bare list."""
    whisper_client(get_highlight_data={"1": {"raw": [1, 100, 20, 1000], "page": 1}})
    _, out, _ = run(
        capsys,
        "whisper",
        "highlights",
        "h1",
        "--lines",
        "1-5",
        "--target-width",
        "600",
        "--target-height",
        "800",
    )
    assert envelope(out)["data"]["rects"]["1"] == [1, 0, 64, 600, 80]


def test_a_line_without_geometry_gets_no_box(capsys, whisper_client):
    """The service reports a line it has no geometry for as all zeros, and the
    page height is a divisor in the scaling."""
    whisper_client(
        get_highlight_data={"1": {"raw": [0, 0, 0, 0]}, "2": {"raw": [1, 100, 20, 1000]}}
    )
    code, out, _ = run(
        capsys,
        "whisper",
        "highlights",
        "h1",
        "--lines",
        "1-5",
        "--target-width",
        "600",
        "--target-height",
        "800",
    )
    assert code == int(ExitCode.SUCCESS)
    assert set(envelope(out)["data"]["rects"]) == {"2"}


def test_highlights_returns_the_metadata_alone_without_a_page_size(
    capsys, whisper_client
):
    whisper_client(get_highlight_data={"1": [1, 100, 20, 1000]})
    _, out, _ = run(capsys, "whisper", "highlights", "h1", "--lines", "1-5")
    assert envelope(out)["data"] == {"1": [1, 100, 20, 1000]}


# --------------------------------------------------------------------------- #
# Deployments
# --------------------------------------------------------------------------- #


def test_run_queues_the_execution_and_polls_it(capsys, deployment_client, tmp_path):
    """`timeout=0` queues, so the CLI holds the poll loop instead of the request
    holding a connection open for the length of the job."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "execution_status": "PENDING",
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status=[
            {"status_code": 200, "pending": True, "execution_status": "EXECUTING"},
            {
                "status_code": 200,
                "pending": False,
                "execution_status": "COMPLETED",
                "extraction_result": [{"file": "doc.pdf"}],
            },
        ],
    )

    code, out, _ = run(
        capsys,
        "-q",
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0",
    )

    assert code == int(ExitCode.SUCCESS)
    assert client.kwargs_for("structure_file")["timeout"] == 0
    assert envelope(out)["data"]["execution_status"] == "COMPLETED"


def test_run_passes_only_the_flags_that_were_given(capsys, deployment_client, tmp_path):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = deployment_client(
        structure_file={"status_code": 200, "execution_status": "COMPLETED"}
    )

    run(
        capsys,
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--no-wait",
        "--tags",
        "a,b",
        "--no-include-metrics",
    )

    sent = client.kwargs_for("structure_file")
    assert sent["tags"] == "a,b"
    assert sent["include_metrics"] is False
    assert "llm_profile_id" not in sent


def test_an_error_status_from_a_run_is_a_failure(capsys, deployment_client, tmp_path):
    """The client reports the status code instead of raising, so an error would
    otherwise be reported as a successful run with an error inside it."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={
            "status_code": 422,
            "pending": False,
            "execution_status": "ERROR",
            "error": "no such API",
        }
    )

    code, out, _ = run(
        capsys, "docstudio", "deployment", "run", "my-api", str(doc), "--no-wait"
    )
    assert code == int(ExitCode.VALIDATION)
    assert envelope(out)["error"]["message"] == "no such API"


def test_deployment_status_reports_a_running_execution(capsys, deployment_client):
    client = deployment_client(
        check_execution_status={
            "status_code": 200,
            "pending": True,
            "execution_status": "EXECUTING",
        }
    )
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "my-api", "e1")
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["execution_status"] == "EXECUTING"
    assert "execution_id=e1" in client.calls[0][1][0]
