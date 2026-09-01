"""The product commands, with the clients replaced. No network.

The seam is the client factory, not the transport: what matters here is which
arguments a command hands the client, what it does with the reply, and what a
caller sees on stdout and in the exit code.
"""

from __future__ import annotations

import json
import socket

import pytest
from requests.exceptions import ConnectionError
from unstract.clone.exceptions import PlatformAPIError
from unstract.clone.report import CloneReport, Endpoint, PhaseResult
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)
from urllib3.connection import HTTPConnection
from urllib3.exceptions import MaxRetryError, NameResolutionError

from unstract_cli.__main__ import main
from unstract_cli.app import command_tree
from unstract_cli.commands import clone_cmd, docstudio_cmd, platform_cmd, whisper_cmd
from unstract_cli.config import LLMWHISPERER, PLATFORM
from unstract_cli.core.errors import CLIError, ExitCode


def run(capsys, *args):
    """Invoke the CLI as the console script does, returning (code, stdout, stderr).

    `-o json` explicitly: these assert on the parseable output, which is what a
    caller opts into rather than what an unflagged run happens to print.
    """
    code = main(["-o", "json", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def envelope(out: str) -> dict:
    return json.loads(out)


def _name_resolution_error(host: str) -> ConnectionError:
    """What requests raises when DNS has no answer, built rather than provoked:
    resolving a name for real would make this suite depend on the network."""
    conn = HTTPConnection(host)
    reason = NameResolutionError(host, conn, socket.gaierror(-2, "no answer"))
    return ConnectionError(MaxRetryError(pool=conn, url="/", reason=reason))


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
        # Resolving the credential is what registers it for scrubbing, so the
        # fake factory has to do it too or the seam hides a production path.
        monkeypatch.setattr(
            whisper_cmd,
            "llmwhisperer",
            lambda config: (config.get(LLMWHISPERER, "api_key"), client)[1],
        )
        return client

    return install


@pytest.fixture
def deployment_client(monkeypatch):
    """Install a fake deployment client and hand it back to the test."""

    def install(**replies):
        client = FakeWhisper(**replies)
        client.api_url = "https://api.example.com/deployment/api/org/api-name/"
        client.built_with = {}

        def build(_config, _target, transport_timeout=None):
            client.built_with["transport_timeout"] = transport_timeout
            return client

        monkeypatch.setattr(docstudio_cmd, "deployment", build)
        return client

    return install


@pytest.fixture
def platform_client(monkeypatch):
    """Install a fake Platform API client and hand it back to the test."""

    def install(**replies):
        client = FakeWhisper(**replies)
        client.built_with = {}

        def build(config, org_id=None, *, timeout=None):
            # Resolving the key is what registers it for scrubbing, so the fake
            # factory has to do it too or the seam hides a production path.
            # The signature tracks the real `platform_client` deliberately: a
            # fixture that drifts from it passes while testing nothing.
            client.built_with["api_key"] = config.get(PLATFORM, "api_key")
            client.built_with["org_id"] = org_id
            client.built_with["timeout"] = timeout
            return client

        monkeypatch.setattr(platform_cmd, "platform_client", build)
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
        "ls",
        "run",
        "status",
    }
    assert set(tree["auth"]["commands"]) == {"whoami"}


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


def test_a_transport_failure_mid_poll_carries_the_handle(
    capsys, whisper_client, tmp_path
):
    """The document is submitted and billed by this point. Without the handle the
    only way on is to send it again and pay for it twice."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status=ConnectionError("connection dropped"),
    )

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0")
    assert code == int(ExitCode.SERVER_ERROR)
    assert envelope(out)["error"]["whisper_hash"] == "h1"


def test_a_failed_retrieve_carries_the_handle(capsys, whisper_client, tmp_path):
    """Retrieve is the acknowledging read: a failure here can lose the text and
    the handle at once, and the handle is the only way back to either."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status={"status": "processed"},
        whisper_retrieve=ConnectionError("connection dropped"),
    )

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0")
    assert code == int(ExitCode.SERVER_ERROR)
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


def test_a_host_that_does_not_resolve_is_not_worth_retrying(capsys, whisper_client):
    """Every other connection failure is transient; a name that does not resolve
    is a typo, and a caller told to retry retries against it forever."""
    whisper_client(get_usage_info=_name_resolution_error("nope.invalid"))
    code, out, _ = run(capsys, "whisper", "usage")
    assert code == int(ExitCode.SERVER_ERROR)
    error = envelope(out)["error"]
    assert error["retryable"] is False
    assert "nope.invalid" in error["message"]


def test_an_unreachable_service_is_worth_retrying(capsys, whisper_client):
    whisper_client(get_usage_info=ConnectionError("connection refused"))
    code, out, _ = run(capsys, "whisper", "usage")
    assert code == int(ExitCode.SERVER_ERROR)
    assert envelope(out)["error"]["retryable"] is True


def test_highlights_needs_lines_or_all_of_them(capsys, whisper_client):
    """The API takes either; asking for neither is a usage error, not a call."""
    whisper_client(get_highlight_data={})
    code, out, _ = run(capsys, "whisper", "highlights", "h1")
    assert code == int(ExitCode.USAGE)
    assert "--extract-all-lines" in envelope(out)["error"]["message"]


def test_extract_all_lines_stands_in_for_a_line_range(capsys, whisper_client):
    """The client takes `lines` positionally even when the request does not need
    it, so omitting the flag would raise inside the client rather than answer."""
    client = whisper_client(get_highlight_data={"1": [1, 100, 20, 1000]})
    code, out, _ = run(capsys, "whisper", "highlights", "h1", "--extract-all-lines")
    assert code == int(ExitCode.SUCCESS)
    sent = client.kwargs_for("get_highlight_data")
    assert sent == {"lines": "", "extract_all_lines": True}


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


@pytest.mark.parametrize(
    ("flag", "expected"), [([], None), (["--transport-timeout", "12.5"], 12.5)]
)
def test_the_transport_timeout_flag_reaches_the_client(
    capsys, deployment_client, tmp_path, flag, expected
):
    """Unset means a stalled connection is never given up on, which is what
    the client has always done."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = deployment_client(
        structure_file={"status_code": 200, "execution_status": "COMPLETED"}
    )

    code, _out, _err = run(
        capsys, "-q", "docstudio", *flag, "deployment", "run", "my-api", str(doc)
    )

    assert code == int(ExitCode.SUCCESS)
    assert client.built_with["transport_timeout"] == expected


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


def test_a_queued_run_reports_the_handle_it_started(capsys, deployment_client, tmp_path):
    """Without --wait the answer is an acknowledgement, so the only thing worth
    printing is what the caller polls with."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={
            "status_code": 200,
            "execution_status": "PENDING",
            "execution_id": "e-1",
            "extraction_result": None,
        }
    )

    code, out, _ = run(
        capsys, "docstudio", "deployment", "run", "my-api", str(doc), "--no-wait"
    )
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["meta"]["execution_id"] == "e-1"

    code = main(
        [
            "-o",
            "raw",
            "docstudio",
            "deployment",
            "run",
            "my-api",
            str(doc),
            "--no-wait",
        ]
    )
    assert code == int(ExitCode.SUCCESS)
    assert capsys.readouterr().out.strip() == "e-1"


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


@pytest.mark.parametrize(
    ("flag", "name", "value"),
    [
        ("--include-metadata", "include_metadata", True),
        ("--no-include-metadata", "include_metadata", False),
        ("--include-metrics", "include_metrics", True),
        ("--no-include-metrics", "include_metrics", False),
        ("--include-extracted-text", "include_extracted_text", True),
        ("--no-include-extracted-text", "include_extracted_text", False),
    ],
)
def test_a_status_flag_reaches_the_client(capsys, deployment_client, flag, name, value):
    """A derived flag that is collected and never forwarded is indistinguishable
    from one that works: the command still succeeds and the payload still parses."""
    client = deployment_client(
        check_execution_status={"status_code": 200, "execution_status": "COMPLETED"}
    )
    run(capsys, "docstudio", "deployment", "status", flag, "my-api", "e1")
    assert client.kwargs_for("check_execution_status")[name] is value


def test_status_sends_only_the_flags_that_were_given(capsys, deployment_client):
    client = deployment_client(
        check_execution_status={"status_code": 200, "execution_status": "COMPLETED"}
    )
    run(capsys, "docstudio", "deployment", "status", "my-api", "e1")
    assert client.kwargs_for("check_execution_status") == {}


def test_a_waited_run_reads_its_result_with_the_flags_it_was_given(
    capsys, deployment_client, tmp_path
):
    """Otherwise --wait silently returns less than the same flags return without
    it: the run is asked for metrics and the read that fetches them is not."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "execution_status": "PENDING",
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status={
            "status_code": 200,
            "pending": False,
            "execution_status": "COMPLETED",
        },
    )

    run(
        capsys,
        "-q",
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0",
        "--include-metrics",
        "--no-include-metadata",
    )

    polled = client.kwargs_for("check_execution_status")
    assert polled["include_metrics"] is True
    assert polled["include_metadata"] is False
    # `tags` is a run-time parameter the status endpoint does not accept.
    assert "tags" not in polled


def test_a_waited_run_reports_which_execution_it_was(capsys, deployment_client, tmp_path):
    """The waited payload names the execution nowhere, so without this a caller
    has no id to correlate the result against the service."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "execution_status": "PENDING",
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status={
            "status_code": 200,
            "pending": False,
            "execution_status": "COMPLETED",
        },
    )

    _, out, _ = run(
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
    assert envelope(out)["meta"]["execution_id"] == "e1"


def test_a_run_only_parameter_is_not_forwarded_to_the_status_read(
    capsys, deployment_client, tmp_path
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    client = deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "execution_status": "PENDING",
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status={
            "status_code": 200,
            "pending": False,
            "execution_status": "COMPLETED",
        },
    )

    run(
        capsys,
        "-q",
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0",
        "--tags",
        "a,b",
    )

    assert client.kwargs_for("structure_file")["tags"] == "a,b"
    assert client.kwargs_for("check_execution_status") == {}


# --------------------------------------------------------------------------- #
# The flag tier of flag > env > profile > default
# --------------------------------------------------------------------------- #


def test_a_connection_flag_beats_the_environment(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://from-env.test")
    monkeypatch.setenv("LLMWHISPERER_API_KEY", "env-key")
    seen = {}
    monkeypatch.setattr(
        whisper_cmd,
        "llmwhisperer",
        lambda config: (
            seen.update(
                base_url=config.get("llmwhisperer", "base_url"),
                api_key=config.get("llmwhisperer", "api_key"),
            )
            or FakeWhisper(get_usage_info={})
        ),
    )

    code, _, err = run(
        capsys,
        "whisper",
        "--base-url",
        "https://from-flag.test",
        "--api-key",
        "flag-key",
        "usage",
    )

    assert code == int(ExitCode.SUCCESS)
    assert seen == {"base_url": "https://from-flag.test", "api_key": "flag-key"}
    # A key on the command line lands in shell history and the process list.
    assert "shell history" in err


def test_the_environment_still_wins_over_a_profile(capsys, monkeypatch, write_config):
    write_config(
        """
        default_profile = "p"
        [profiles.p.llmwhisperer]
        base_url = "https://from-profile.test"
        """
    )
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://from-env.test")
    seen = {}
    monkeypatch.setattr(
        whisper_cmd,
        "llmwhisperer",
        lambda config: (
            seen.update(base_url=config.get("llmwhisperer", "base_url"))
            or FakeWhisper(get_usage_info={})
        ),
    )

    run(capsys, "whisper", "usage")
    assert seen == {"base_url": "https://from-env.test"}


def test_a_deployment_org_can_come_from_a_flag(capsys, monkeypatch):
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "key")
    seen = {}
    monkeypatch.setattr(
        docstudio_cmd,
        "deployment",
        lambda config, target, transport_timeout=None: (
            seen.update(org=config.get("docstudio", "org_id")) or _deployment_fake()
        ),
    )
    run(capsys, "docstudio", "--org-id", "org_A", "deployment", "status", "api", "e1")
    assert seen == {"org": "org_A"}


def _deployment_fake():
    client = FakeWhisper(
        check_execution_status={"status_code": 200, "execution_status": "COMPLETED"}
    )
    client.api_url = "https://api.example.com/deployment/api/org/api-name/"
    return client


# --------------------------------------------------------------------------- #
# The one-shot data path
# --------------------------------------------------------------------------- #


def test_a_waited_extract_keeps_a_result_that_is_not_wrapped(
    capsys, whisper_client, tmp_path
):
    """A bare `.get("extraction")` returned None here and printed
    `ok: true, data: null` for a document that had been processed and billed."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1", "status_code": 202},
        whisper_status={"status": "processed"},
        # No `extraction` key -- the shape the sibling command already tolerated.
        whisper_retrieve={"status_code": 200, "result_text": "THE REAL TEXT"},
    )

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["result_text"] == "THE REAL TEXT"


def test_a_waited_extract_calls_an_empty_result_a_failure(
    capsys, whisper_client, tmp_path
):
    """The read is acknowledged either way, so an empty result is a consumed
    document with nothing to show for it."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1", "status_code": 202},
        whisper_status={"status": "processed"},
        whisper_retrieve={"extraction": {}},
    )

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0")

    assert code == int(ExitCode.SERVER_ERROR)
    assert envelope(out)["ok"] is False


def test_a_waited_extract_reads_the_result_when_it_is_not_wrapped(
    capsys, whisper_client, tmp_path
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1", "status_code": 202},
        whisper_status={"status": "processed"},
        whisper_retrieve={"extraction": {"result_text": "hello"}},
    )

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["result_text"] == "hello"


def test_retrieve_writes_the_result_before_it_prints(
    capsys, whisper_client, tmp_path, monkeypatch
):
    """Ordering, not outcome: asserting after the command returns passes for
    either order, which is how this went unnoticed."""
    order: list[str] = []
    target = tmp_path / "out" / "result.json"
    whisper_client(whisper_retrieve={"extraction": {"result_text": "hello"}})

    real_persist = whisper_cmd.persist
    monkeypatch.setattr(
        whisper_cmd,
        "persist",
        lambda path, payload: (order.append("persist"), real_persist(path, payload))[1],
    )
    real_finish = whisper_cmd.finish
    monkeypatch.setattr(
        whisper_cmd,
        "finish",
        lambda *a, **kw: (order.append("finish"), real_finish(*a, **kw))[1],
    )

    run(capsys, "whisper", "retrieve", "h1", "--save", str(target))

    assert order == ["persist", "finish"]


def test_retrieve_refuses_an_unwritable_target_before_reading(
    capsys, whisper_client, tmp_path
):
    """Nothing has been consumed yet at this point, so this failure is cheap --
    the same failure after the read is not recoverable at all."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    client = whisper_client(whisper_retrieve={"extraction": {"result_text": "hello"}})

    code, out, _ = run(
        capsys, "whisper", "retrieve", "h1", "--save", str(blocker / "r.json")
    )

    assert code == int(ExitCode.USAGE)
    assert client.calls == []


def test_a_save_failure_after_the_read_still_emits_the_result(
    capsys, whisper_client, tmp_path, monkeypatch
):
    target = tmp_path / "result.json"
    whisper_client(whisper_retrieve={"extraction": {"result_text": "IRREPLACEABLE"}})

    def explode(path, payload):
        # What `persist` itself raises when the write fails: the payload rides
        # out on the error because there is no other copy left.
        raise CLIError(
            "The result could not be written.",
            ExitCode.SAVE_FAILED,
            details=payload,
        )

    monkeypatch.setattr(whisper_cmd, "persist", explode)

    code, out, _ = run(capsys, "whisper", "retrieve", "h1", "--save", str(target))

    assert code == int(ExitCode.SAVE_FAILED)
    assert envelope(out)["error"]["details"]["result_text"] == "IRREPLACEABLE"


def test_a_failed_execution_inside_a_200_is_not_a_success(capsys, deployment_client):
    deployment_client(
        check_execution_status={
            "status_code": 200,
            "pending": False,
            "execution_status": "ERROR",
            "error": "tool crashed",
        }
    )

    code, out, _ = run(capsys, "docstudio", "deployment", "status", "api", "e1")

    assert code != int(ExitCode.SUCCESS)
    assert envelope(out)["ok"] is False


def test_the_key_never_reaches_stdout_or_stderr(capsys, whisper_client, monkeypatch):
    """Scrubbing is not a keyword argument a call site can forget."""
    key = "lw-live-ABCDEF0123456789"
    monkeypatch.setenv("LLMWHISPERER_API_KEY", key)
    whisper_client(
        whisper_retrieve=LLMWhispererClientException(
            {"message": f"invalid key {key}", "status_code": 401}, 401
        )
    )

    code, out, err = run(capsys, "whisper", "retrieve", "h1")

    assert code == int(ExitCode.AUTH)
    assert key not in out
    assert key not in err


def test_clone_maps_its_flags_and_reports_a_partial_failure(capsys, monkeypatch):
    """Migration flags decide what is copied where, with two admin keys in play."""
    captured: dict = {}

    def fake_clone(source, target, options):
        captured.update(source=source, target=target, options=options)
        return CloneReport(
            source=Endpoint(source.base_url, source.organization_id),
            target=Endpoint(target.base_url, target.organization_id),
            phases=[
                PhaseResult(name="adapters", created=1, failed=2),
                PhaseResult(name="files", created=1, skipped=3),
            ],
            oversize_files=[{"name": "big.pdf"}, {"name": "bigger.pdf"}],
        )

    monkeypatch.setattr(clone_cmd, "run_clone", fake_clone)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    code, out, err = run(
        capsys,
        "clone",
        "--source-url",
        "https://dev.example.com",
        "--source-org",
        "org_dev",
        "--target-url",
        "https://qa.example.com",
        "--target-org",
        "org_qa",
        "--dry-run",
        "--exclude",
        "files, groups",
        "--skip-files",
        "--max-file-size",
        "2MB",
        "--api-prefix",
        "api/v2",
        "--on-name-conflict",
        "abort",
    )

    assert captured["source"].platform_key == "src-key-0123456789"
    assert captured["target"].organization_id == "org_qa"
    assert captured["target"].api_path_prefix == "api/v2"
    assert captured["options"].dry_run is True
    assert captured["options"].exclude == ("files", "groups")
    assert captured["options"].file_strategy == "skip"
    assert captured["options"].max_file_size == 2 * 1024 * 1024
    # adopt and abort decide what is written into a live target organisation.
    assert captured["options"].on_name_conflict == "abort"

    # A phase that failed is not a successful migration, whatever else worked.
    assert code == int(ExitCode.GENERIC)
    body = envelope(out)
    assert body["ok"] is False
    assert "adapters" in body["error"]["message"]
    # Documents that never arrived are counted where a consumer reads first.
    assert body["error"]["details"]["skipped"] == {
        "total": 3,
        "by_phase": {"files": 3},
        "oversize_files": 2,
        "unsupported_files": 0,
    }
    for key in ("src-key-0123456789", "tgt-key-0123456789"):
        assert key not in out and key not in err


def test_a_key_quoted_in_a_clone_report_does_not_survive_the_table(capsys, monkeypatch):
    """The table is the output a person gets, and the report renders itself.

    A platform key quoted back by a failing service lands in a terminal buffer
    and in whatever scrapes one, so the rendered report is scrubbed on the same
    path as every envelope rather than by hand.
    """
    key = "src-key-0123456789"

    def fake_clone(source, target, options):
        return CloneReport(
            source=Endpoint(source.base_url, source.organization_id),
            target=Endpoint(target.base_url, target.organization_id),
            phases=[PhaseResult(name="adapters", created=1)],
            warnings=[f"target refused the request for {key}"],
        )

    monkeypatch.setattr(clone_cmd, "run_clone", fake_clone)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", key)
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    code = main(
        [
            "-o",
            "table",
            "clone",
            "--source-url",
            "https://dev.example.com",
            "--source-org",
            "org_dev",
            "--target-url",
            "https://qa.example.com",
            "--target-org",
            "org_qa",
        ]
    )
    captured = capsys.readouterr()

    assert code == int(ExitCode.SUCCESS)
    assert "adapters" in captured.out
    assert key not in captured.out and key not in captured.err


# --------------------------------------------------------------------------- #
# auth whoami
# --------------------------------------------------------------------------- #

IDENTITY = {
    "organization_id": "org_ABC123",
    "organization_name": "Acme",
    "permission": "read",
    "key_name": "ci",
}


def _platform_env(monkeypatch, tmp_path):
    """A resolvable platform key, and a config file of our own to write into."""
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))


def test_whoami_reports_the_identity_the_service_returned(
    capsys, platform_client, monkeypatch, tmp_path
):
    _platform_env(monkeypatch, tmp_path)
    platform_client(whoami=IDENTITY)

    code, out, _ = run(capsys, "auth", "whoami")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"] == IDENTITY


def test_whoami_is_called_with_no_organisation(
    capsys, platform_client, monkeypatch, tmp_path
):
    """Resolving the organisation is the point, so requiring one would be
    circular."""
    _platform_env(monkeypatch, tmp_path)
    client = platform_client(whoami=IDENTITY)

    run(capsys, "auth", "whoami")

    assert client.built_with["org_id"] is None
    assert client.built_with["api_key"] == "pk-123"


def test_whoami_stores_the_organisation_where_everything_else_reads_it(
    capsys, platform_client, monkeypatch, tmp_path
):
    _platform_env(monkeypatch, tmp_path)
    platform_client(whoami=IDENTITY)

    _, out, _ = run(capsys, "auth", "whoami")

    assert envelope(out)["meta"]["saved"] is True
    # Read back through the CLI rather than out of the file: what matters is
    # that the next command resolves it, not where the bytes landed.
    _, out, _ = run(capsys, "config", "get", "docstudio", "org_id")
    assert envelope(out)["data"]["value"] == "org_ABC123"


def test_whoami_can_validate_without_writing_anything(
    capsys, platform_client, monkeypatch, tmp_path
):
    _platform_env(monkeypatch, tmp_path)
    platform_client(whoami=IDENTITY)

    _, out, _ = run(capsys, "auth", "whoami", "--no-save")

    assert envelope(out)["meta"]["saved"] is False
    assert not (tmp_path / "config.toml").exists()


def test_a_rejected_platform_key_exits_on_the_auth_code(
    capsys, platform_client, monkeypatch, tmp_path
):
    """A traceback here would mean the Platform API's own exception type never
    reached the translator."""
    _platform_env(monkeypatch, tmp_path)
    platform_client(whoami=PlatformAPIError("nope", status_code=401, body="{}"))

    code, out, _ = run(capsys, "auth", "whoami")

    assert code == int(ExitCode.AUTH)
    assert envelope(out)["ok"] is False


def test_whoami_without_a_key_is_a_usage_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    code, out, _ = run(capsys, "auth", "whoami")

    assert code == int(ExitCode.USAGE)
    assert "UNSTRACT_PLATFORM_KEY" in json.dumps(envelope(out)["error"])


# --------------------------------------------------------------------------- #
# docstudio deployment ls
# --------------------------------------------------------------------------- #

DEPLOYMENT_ROW = {
    "api_name": "invoice-parser",
    "display_name": "Invoices",
    "id": "dep-1",
    "is_active": True,
    "api_endpoint": "https://example.com/deployment/api/org/invoice-parser/",
    "created_by_email": "someone@example.com",
    "last_5_run_statuses": [],
}


def _returns(value):
    """Queue one reply whose value is itself a list.

    `FakeWhisper` reads a list reply as a queue of replies, so a bare list would
    hand back its first row rather than the listing.
    """
    return [value]


def _listing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    monkeypatch.setenv("UNSTRACT_ORG_ID", "org_ABC123")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))


def test_ls_narrows_the_row_to_what_a_caller_can_read(
    capsys, platform_client, monkeypatch, tmp_path
):
    _listing_env(monkeypatch, tmp_path)
    platform_client(list_api_deployments=_returns([DEPLOYMENT_ROW]))

    _, out, _ = run(capsys, "docstudio", "deployment", "ls")

    (row,) = envelope(out)["data"]["results"]
    assert set(row) == set(platform_cmd.LISTING_FIELDS)
    assert row["api_name"] == "invoice-parser"


def test_ls_can_return_every_field_the_server_sent(
    capsys, platform_client, monkeypatch, tmp_path
):
    _listing_env(monkeypatch, tmp_path)
    platform_client(list_api_deployments=_returns([DEPLOYMENT_ROW]))

    _, out, _ = run(capsys, "docstudio", "deployment", "ls", "--full")

    (row,) = envelope(out)["data"]["results"]
    assert row == DEPLOYMENT_ROW


def test_ls_passes_the_name_filter_to_the_server(
    capsys, platform_client, monkeypatch, tmp_path
):
    """Filtering here rather than locally: the server has the exact-match
    filter, and a local one would still page the whole organisation."""
    _listing_env(monkeypatch, tmp_path)
    client = platform_client(list_api_deployments=_returns([DEPLOYMENT_ROW]))

    run(capsys, "docstudio", "deployment", "ls", "--api-name", "invoice-parser")

    assert client.kwargs_for("list_api_deployments") == {"api_name": "invoice-parser"}


def test_ls_runs_inside_the_configured_organisation(
    capsys, platform_client, monkeypatch, tmp_path
):
    _listing_env(monkeypatch, tmp_path)
    client = platform_client(list_api_deployments=_returns([]))

    run(capsys, "docstudio", "deployment", "ls")

    assert client.built_with["org_id"] == "org_ABC123"


def test_ls_without_an_organisation_says_how_to_get_one(
    capsys, platform_client, monkeypatch, tmp_path
):
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    platform_client(list_api_deployments=_returns([]))

    code, out, _ = run(capsys, "docstudio", "deployment", "ls")

    assert code == int(ExitCode.USAGE)
    assert "whoami" in json.dumps(envelope(out)["error"])


# --------------------------------------------------------------------------- #
# auth whoami — where it writes, and what happens when it cannot
# --------------------------------------------------------------------------- #


def _config_with(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(path))
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    return path


def test_whoami_writes_to_the_profile_the_run_is_actually_using(
    capsys, platform_client, monkeypatch, tmp_path
):
    """Reads resolve through `active_profile` (flag > env > file default).
    Re-deriving that chain here dropped the env tier, so the organisation was
    written into a profile no later command reads -- and `deployment ls` then
    failed immediately after a `whoami` reporting `saved: true`.
    """
    path = _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n'
        '[profiles.cloud-us.docstudio]\norg_id = ""\n'
        '[profiles.cloud-eu.docstudio]\norg_id = ""\n',
    )
    monkeypatch.setenv("UNSTRACT_PROFILE", "cloud-eu")
    platform_client(whoami=IDENTITY)

    _, out, _ = run(capsys, "auth", "whoami")

    assert envelope(out)["meta"]["profile"] == "cloud-eu"
    assert 'org_id = "org_ABC123"' in path.read_text().split("[profiles.cloud-eu")[1]


def test_whoami_refuses_to_invent_a_profile_that_does_not_exist(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`setdefault` created it. That silently disarmed the "Profile not found"
    guard for every later command, which then resolved the built-in production
    defaults instead -- from a single typo, permanently.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = ""\n',
    )
    platform_client(whoami=IDENTITY)

    code, out, _ = run(capsys, "-p", "cloud-uss", "auth", "whoami")

    # SAVE_FAILED, not USAGE: the key resolved and only the note-taking failed,
    # so the identity comes back in `details` rather than being discarded.
    error = envelope(out)["error"]
    assert code == int(ExitCode.SAVE_FAILED)
    assert "cloud-uss" in json.dumps(error)
    assert error["details"]["organization_id"] == IDENTITY["organization_id"]


def test_whoami_writes_to_the_only_profile_when_no_default_is_named(
    capsys, platform_client, monkeypatch, tmp_path
):
    """The unknown-profile guard fired on the literal "cloud-us" fallback -- a
    name the caller never typed -- for any file with profiles and no
    `default_profile`, and advised creating a third that would shadow theirs.
    """
    path = _config_with(
        tmp_path,
        monkeypatch,
        '[profiles.work.docstudio]\norg_id = ""\n',
    )
    platform_client(whoami=IDENTITY)

    code, out, _ = run(capsys, "auth", "whoami")

    assert code == 0
    assert envelope(out)["meta"]["profile"] == "work"
    written = path.read_text(encoding="utf-8")
    assert f'org_id = "{IDENTITY["organization_id"]}"' in written
    assert 'default_profile = "work"' in written


def test_whoami_will_not_guess_between_several_unselected_profiles(
    capsys, platform_client, monkeypatch, tmp_path
):
    """Two profiles and no default: writing into either would be a guess. It
    says so and hands the identity back, rather than naming `cloud-us`.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        '[profiles.work.docstudio]\norg_id = ""\n[profiles.home.docstudio]\norg_id = ""\n',
    )
    platform_client(whoami=IDENTITY)

    code, out, _ = run(capsys, "auth", "whoami")
    error = envelope(out)["error"]

    assert code == int(ExitCode.SAVE_FAILED)
    assert "cloud-us" not in json.dumps(error)
    assert "-p <name>" in json.dumps(error)
    assert error["details"]["organization_id"] == IDENTITY["organization_id"]


def test_whoami_keeps_the_identity_when_the_write_fails(
    capsys, platform_client, monkeypatch, tmp_path
):
    """The read succeeded and only the convenience write failed. Losing the
    identity to a full disk reported a working key as a total failure, on an
    exit code that means "you invoked it wrong".
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = ""\n',
    )
    platform_client(whoami=IDENTITY)
    monkeypatch.setattr(
        platform_cmd,
        "save_config",
        lambda *a, **k: (_ for _ in ()).throw(OSError(13, "nope")),
    )

    code, out, _ = run(capsys, "auth", "whoami")
    error = envelope(out)["error"]

    assert code == int(ExitCode.SAVE_FAILED)
    # Not full equality: `redact_value` masks any field whose *name* looks
    # secret, and `key_name` matches -- so the identity reaches `details` with
    # that one field starred out. The organisation is the part the caller needs
    # in order to carry on without the write.
    assert error["details"]["organization_id"] == IDENTITY["organization_id"]
    assert error["details"]["key_name"] == "***REDACTED***"


def test_whoami_does_not_rewrite_a_discovered_project_config(
    capsys, platform_client, monkeypatch, tmp_path
):
    """A `.unstract.toml` found by walking up is very likely committed. Writing
    it replaced a teammate's org_id, dropped every comment and narrowed the mode
    -- from a command named `whoami`, with no flag asked for.
    """
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".unstract.toml").write_text(
        '# hand written\ndefault_profile = "team"\n[profiles.team.docstudio]\norg_id = "org_TEAM"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("UNSTRACT_CONFIG", raising=False)
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    platform_client(whoami=IDENTITY)

    code, out, envelope_err = run(capsys, "auth", "whoami")
    body = envelope(out)

    # Declining the write is not failing the call: README blesses a committed
    # `.unstract.toml`, and `auth whoami` is the documented first command, so
    # exiting 2 broke the quickstart and discarded the identity with it.
    assert code == 0
    assert body["data"]["organization_id"] == IDENTITY["organization_id"]
    assert body["meta"]["saved"] is False
    assert "project-local" in body["meta"]["reason"]
    assert "# hand written" in (project / ".unstract.toml").read_text()
    assert "org_TEAM" in (project / ".unstract.toml").read_text()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["auth", "whoami"], None),
        (["auth", "--transport-timeout", "12.5", "whoami"], 12.5),
        (["docstudio", "deployment", "ls"], None),
        (["docstudio", "--transport-timeout", "12.5", "deployment", "ls"], 12.5),
    ],
)
def test_the_transport_timeout_flag_reaches_the_platform_client(
    capsys, platform_client, monkeypatch, tmp_path, argv, expected
):
    """The flag was accepted on both groups and threaded through the factory,
    but nothing asserted the commands passed it: deleting either call site left
    the suite green. The fixture recorded the value and no test read it.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    client = platform_client(whoami=IDENTITY, list_api_deployments=_returns([]))

    code, _, _ = run(capsys, *argv)

    assert code == 0
    assert client.built_with["timeout"] == expected


@pytest.mark.parametrize("value", ["0", "0.0", "-1"])
def test_a_non_positive_transport_timeout_is_a_usage_error_not_a_traceback(
    capsys, monkeypatch, tmp_path, value
):
    """urllib3 raises a bare `ValueError` for a non-positive timeout, which
    matches no arm in `__main__` -- a traceback and no envelope. The flag is
    `type=float`, so anything in (0, 1) truncated to that same 0.
    """
    _config_with(tmp_path, monkeypatch, "")

    code, out, _ = run(capsys, "auth", "--transport-timeout", value, "whoami")

    assert code == int(ExitCode.USAGE)
    assert "transport-timeout" in envelope(out)["error"]["message"]


def test_a_deployment_key_flag_is_refused_rather_than_ignored_by_ls(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`--api-key` on the docstudio group is a *deployment* key and `ls`
    authenticates with a platform key. It was accepted, dropped, and the
    platform key then reported missing -- which reads as a broken flag rather
    than the wrong credential.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    platform_client(list_api_deployments=_returns([]))

    code, out, _ = run(
        capsys, "docstudio", "--api-key", "dk-FROM-FLAG", "deployment", "ls"
    )
    error = envelope(out)["error"]

    assert code == int(ExitCode.USAGE)
    assert "platform key" in error["message"]
    assert "UNSTRACT_PLATFORM_KEY" in error["hint"]
    assert "dk-FROM-FLAG" not in json.dumps(envelope(out))


def test_the_platform_key_never_reaches_a_stream(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`translated()` attaches `PlatformAPIError.body` -- the server's own
    response -- as `details`. If the far end echoes the key, that is the path it
    would travel to stdout.
    """
    _config_with(tmp_path, monkeypatch, 'default_profile = "cloud-us"\n')
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-SUPERSECRET-0987654321")
    platform_client(
        whoami=PlatformAPIError(
            "GET whoami/ returned 401",
            status_code=401,
            body='{"echoed": "pk-SUPERSECRET-0987654321"}',
        )
    )

    _, out, err = run(capsys, "auth", "whoami")

    assert "pk-SUPERSECRET-0987654321" not in out
    assert "pk-SUPERSECRET-0987654321" not in err


def test_a_rejected_key_keeps_its_message_on_one_line(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`PlatformAPIError` folds the body into its own string, so `str(exc)` put
    up to 2KB of server response into `error.message` -- which `emit_error`
    documents as a one-line summary -- and duplicated it into `details`.
    """
    _config_with(tmp_path, monkeypatch, 'default_profile = "cloud-us"\n')
    platform_client(
        whoami=PlatformAPIError(
            "GET whoami/ returned 401", status_code=401, body='{"m": "x"}'
        )
    )

    _, out, _ = run(capsys, "auth", "whoami")
    error = envelope(out)["error"]

    assert "\n" not in error["message"]
    assert error["details"] == '{"m": "x"}'
