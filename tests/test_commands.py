"""The product commands, with the clients replaced. No network.

The seam is the client factory, not the transport: what matters here is which
arguments a command hands the client, what it does with the reply, and what a
caller sees on stdout and in the exit code.
"""

from __future__ import annotations

import json
import os
import socket

import click
import httpx
import pytest
from requests.exceptions import ConnectionError, InvalidHeader, MissingSchema
from unstract.api_deployments.client import PlatformClientError
from unstract.clone.exceptions import CloneError, PlatformAPIError
from unstract.clone.report import CloneReport, Endpoint, PhaseResult
from unstract.llmwhisperer import client_v2
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)

from unstract_cli.__main__ import main
from unstract_cli.app import command_tree
from unstract_cli.commands import clone_cmd, docstudio_cmd, platform_cmd, whisper_cmd
from unstract_cli.config import DOCSTUDIO, LLMWHISPERER
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
    """The failure the pinned client raises when a host does not resolve.

    Built by putting a transport error through the client's own translation
    rather than assembled here: the client re-raises with only a message, so a
    hand-made stand-in can keep passing long after the client has stopped
    producing anything like it.
    """

    def fail():
        request = httpx.Request("GET", f"https://{host}/api/v2/get-usage-info")
        raise httpx.ConnectError(
            "[Errno -2] Name or service not known", request=request
        ) from socket.gaierror(-2, "Name or service not known")

    try:
        client_v2._translate_transport_errors(fail)
    except ConnectionError as exc:
        return exc
    raise AssertionError("the pinned client no longer translates a connect error")


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
            client.built_with["api_key"] = config.get(DOCSTUDIO, "platform_key")
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
    assert set(tree["auth"]["commands"]) == {"login", "whoami"}


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

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0.1")

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
        capsys, "-q", "-o", "raw", "whisper", "extract", str(doc), "--interval", "0.1"
    )
    assert out.strip() == "hello"


def test_raw_retrieve_of_an_empty_extraction_prints_nothing_and_succeeds(
    capsys, whisper_client
):
    whisper_client(whisper_retrieve={"extraction": {"result_text": ""}})

    code, out, _ = run(capsys, "-q", "-o", "raw", "whisper", "retrieve", "h1")

    assert code == int(ExitCode.SUCCESS)
    assert out == "\n"


def test_raw_extract_of_an_empty_document_prints_empty_text_not_the_hash(
    capsys, whisper_client, tmp_path
):
    doc = tmp_path / "blank.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1"},
        whisper_status={"status": "processed"},
        whisper_retrieve={"extraction": {"result_text": ""}},
    )

    _, out, _ = run(
        capsys, "-q", "-o", "raw", "whisper", "extract", str(doc), "--interval", "0.1"
    )

    assert out == "\n"


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

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0.1")
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

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0.1")
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

    code, out, _ = run(capsys, "-q", "whisper", "extract", str(doc), "--interval", "0.1")
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


def test_a_body_that_is_not_json_is_a_server_failure_not_a_crash(capsys, whisper_client):
    """A proxy or web-app host answers 200 with HTML, which the client parses
    itself; untranslated it reaches the entry point as a crash."""
    whisper_client(get_usage_info=json.JSONDecodeError("Expecting value", "<html>", 0))

    code, out, _ = run(capsys, "whisper", "usage")

    assert code == int(ExitCode.SERVER_ERROR)
    assert "base_url" in envelope(out)["error"]["hint"]


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


@pytest.mark.skipif(
    not os.environ.get("UNSTRACT_CLI_LIVE"),
    reason="asks the resolver about a host; set UNSTRACT_CLI_LIVE=1 to run it",
)
def test_a_real_resolver_failure_reaches_the_same_answer(capsys, monkeypatch):
    """The offline stand-in is built by hand, however carefully. This one asks
    the pinned client to reach a name no resolver will answer for."""
    monkeypatch.setenv("LLMWHISPERER_API_KEY", "k")
    monkeypatch.setenv("LLMWHISPERER_BASE_URL", "https://unresolvable.invalid/api/v2")
    code, out, _ = run(capsys, "whisper", "usage")
    assert code == int(ExitCode.SERVER_ERROR)
    error = envelope(out)["error"]
    assert error["retryable"] is False
    assert "unresolvable.invalid" in error["message"]


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
        "0.1",
    )

    assert code == int(ExitCode.SUCCESS)
    assert client.kwargs_for("structure_file")["timeout"] == 0
    assert envelope(out)["data"]["execution_status"] == "COMPLETED"


def test_a_run_can_name_its_documents_as_presigned_urls(
    capsys, deployment_client, tmp_path
):
    """The flag is derived from the spec and advertised by `--discover`, so an
    invocation that uses it and nothing else has to reach the client: a local
    path was once the only way to name a document, which made the flag
    unusable rather than merely unused.
    """
    client = deployment_client(
        structure_file={
            "status_code": 200,
            "pending": False,
            "execution_status": "COMPLETED",
            "extraction_result": [{"file": "doc.pdf"}],
        }
    )

    code, out, _ = run(
        capsys,
        "-q",
        "docstudio",
        "deployment",
        "run",
        "my-api",
        "--presigned-urls",
        "https://example.com/doc.pdf",
        "--interval",
        "0.1",
    )

    assert code == int(ExitCode.SUCCESS)
    sent = client.kwargs_for("structure_file")
    assert list(sent["presigned_urls"]) == ["https://example.com/doc.pdf"]
    assert envelope(out)["data"]["execution_status"] == "COMPLETED"


def test_a_run_naming_no_documents_at_all_is_refused(capsys, deployment_client):
    """Neither source is required on its own, so nothing in Click's own parsing
    catches a run that names no document; without this the request goes out
    empty and the server answers for us.
    """
    deployment_client(structure_file={"status_code": 200})

    code, out, _ = run(capsys, "docstudio", "deployment", "run", "my-api")

    assert code == int(ExitCode.USAGE)
    assert "at least one document" in envelope(out)["error"]["message"]


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ([], 120.0),
        (["--transport-timeout", "12.5"], 12.5),
        (["--transport-timeout", "0"], None),
    ],
)
def test_the_transport_timeout_flag_reaches_the_client(
    capsys, deployment_client, tmp_path, flag, expected
):
    """Unset means the default, and only zero means a stalled connection is
    never given up on."""
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


@pytest.mark.parametrize(
    "argv",
    [
        ("docstudio", "deployment", "status", "invoice-parser", "e-1"),
        ("docstudio", "deployment", "run", "invoice-parser", "DOC", "--no-wait"),
    ],
    ids=["status", "run"],
)
def test_a_rejected_key_names_the_deployment_and_how_to_give_it_its_own(
    capsys, deployment_client, tmp_path, argv
):
    """A 401 is the first moment "this deployment may need its own key" is
    known to be true, so the hint that says so has to be reachable from both
    commands that send one."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF")
    deployment_client(
        check_execution_status={"status_code": 401, "error": "Unauthorized"},
        structure_file={"status_code": 401, "error": "Unauthorized"},
    )
    code, out, _ = run(capsys, *(str(doc) if a == "DOC" else a for a in argv))
    assert code == int(ExitCode.AUTH)
    error = envelope(out)["error"]
    assert "invoice-parser" in error["message"]
    assert "does not authorize" in error["message"]
    assert (
        "config set docstudio api_key <key> --deployment invoice-parser"
        in (error["hint"])
    )


REJECTED_KEY_CONFIG = """
default_profile = "p"
[profiles.p.docstudio]
org_id = "org_X"
api_key = "dk-profile-000001"
[profiles.p.deployments."invoice-parser"]
api_key = "dk-entry-0000001"
"""


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ("docstudio", "deployment", "status", "invoice-parser", "e-1"),
            "The key stored for 'invoice-parser' was rejected",
            id="the deployment's own entry",
        ),
        pytest.param(
            ("docstudio", "deployment", "status", "receipt-parser", "e-1"),
            "may need a key of its own",
            id="the profile key",
        ),
        pytest.param(
            (
                "docstudio",
                "--api-key",
                "dk-flag-00000001",
                "deployment",
                "status",
                "invoice-parser",
                "e-1",
            ),
            "passed with --api-key",
            id="the flag",
        ),
    ],
)
def test_a_rejected_key_is_reported_where_it_came_from(
    capsys, deployment_client, monkeypatch, tmp_path, argv, expected
):
    """Telling the caller whose per-deployment key was just rejected to store a
    per-deployment key names the step that has already failed."""
    _config_with(tmp_path, monkeypatch, REJECTED_KEY_CONFIG)
    deployment_client(check_execution_status={"status_code": 401, "error": "no"})

    code, out, _ = run(capsys, *argv)

    assert code == int(ExitCode.AUTH)
    assert expected in envelope(out)["error"]["hint"]


def test_a_rejected_key_from_the_environment_names_the_variable(
    capsys, deployment_client, monkeypatch, tmp_path
):
    """The variable outranks both stored keys, so editing either changes
    nothing until it is unset."""
    _config_with(tmp_path, monkeypatch, REJECTED_KEY_CONFIG)
    monkeypatch.setenv("UNSTRACT_DEPLOYMENT_KEY", "dk-env-000000001")
    deployment_client(check_execution_status={"status_code": 401, "error": "no"})

    code, out, _ = run(
        capsys, "docstudio", "deployment", "status", "invoice-parser", "e-1"
    )

    assert code == int(ExitCode.AUTH)
    assert "$UNSTRACT_DEPLOYMENT_KEY was rejected" in envelope(out)["error"]["hint"]


def test_an_unknown_api_name_is_pointed_at_the_listing(capsys, deployment_client):
    """A misspelt or renamed API name comes back not-found, and the server is
    the only authority on what the current names are."""
    deployment_client(check_execution_status={"status_code": 404, "error": "not found"})
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "invoces", "e-1")
    assert code == int(ExitCode.NOT_FOUND)
    assert "deployment ls" in envelope(out)["error"]["hint"]


def test_a_status_read_of_a_consumed_result_has_its_own_exit_code(
    capsys, deployment_client
):
    """A deployment hands its result over once, so a 406 from the status read
    means it is gone rather than that the request was malformed."""
    deployment_client(
        check_execution_status={"status_code": 406, "error": "already retrieved"}
    )
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "my-api", "e-1")
    assert code == int(ExitCode.ALREADY_CONSUMED)
    assert "--save" in envelope(out)["error"]["hint"]


def test_highlights_on_an_extraction_without_line_numbers_says_where_to_fix_it(
    capsys, whisper_client
):
    """The call that can be fixed is the extract, which has already been paid
    for; a hint about this call sends the caller nowhere."""
    whisper_client(
        get_highlight_data=LLMWhispererClientException(
            {"message": "no line metadata", "status_code": 400}, 400
        )
    )
    code, out, _ = run(capsys, "whisper", "highlights", "h1", "--lines", "1-5")
    assert code == int(ExitCode.VALIDATION)
    assert "--add-line-nos" in envelope(out)["error"]["hint"]


ACK = {
    "status_code": 200,
    "execution_status": "PENDING",
    "extraction_result": "",
    "status_check_api_endpoint": "/deployment/api/status?execution_id=e-1",
}

PENDING_STATUS = {
    "status_code": 422,
    "pending": True,
    "execution_status": "EXECUTING",
    "extraction_result": "",
}

DONE_STATUS = {
    "status_code": 200,
    "execution_status": "COMPLETED",
    "extraction_result": "the answer",
}


def _raw(capsys, *args) -> str:
    assert main(["-o", "raw", *args]) == int(ExitCode.SUCCESS)
    return capsys.readouterr().out.strip()


def test_a_queued_run_renders_the_handle_it_had_to_derive(
    capsys, deployment_client, tmp_path
):
    """The ack names no execution of its own -- the id is only in the endpoint
    it hands back -- so raw would otherwise have nothing true to print."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(structure_file=ACK)

    code, out, _ = run(
        capsys, "docstudio", "deployment", "run", "my-api", str(doc), "--no-wait"
    )
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["meta"]["execution_id"] == "e-1"

    deployment_client(structure_file=ACK)
    assert (
        _raw(capsys, "docstudio", "deployment", "run", "my-api", str(doc), "--no-wait")
        == "e-1"
    )


def test_an_accepted_extraction_renders_its_handle_not_the_whole_ack(
    capsys, whisper_client, tmp_path
):
    """An accepted job carries no text, so raw prints the handle -- the one
    thing the caller can act on. Declaring no fields here would print the whole
    acknowledgement instead, which raw is precisely not for.
    """
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(whisper={"whisper_hash": "h1", "status_code": 202})

    assert _raw(capsys, "whisper", "extract", str(doc), "--no-wait") == "h1"


def test_a_still_running_status_never_renders_as_an_empty_result(
    capsys, deployment_client
):
    """`extraction_result` is present and empty while the job runs. Printing that
    tells a polling caller the same thing as a finished job with no output."""
    deployment_client(check_execution_status=PENDING_STATUS)
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "my-api", "e-1")
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["execution_status"] == "EXECUTING"

    deployment_client(check_execution_status=PENDING_STATUS)
    assert (
        _raw(capsys, "docstudio", "deployment", "status", "my-api", "e-1") == "EXECUTING"
    )


def test_a_finished_status_renders_its_result(capsys, deployment_client):
    deployment_client(check_execution_status=DONE_STATUS)
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "my-api", "e-1")
    assert envelope(out)["data"]["extraction_result"] == "the answer"

    deployment_client(check_execution_status=DONE_STATUS)
    assert (
        _raw(capsys, "docstudio", "deployment", "status", "my-api", "e-1") == "the answer"
    )


def test_raw_fails_rather_than_printing_something_else(capsys, deployment_client):
    """An answer carrying none of the declared fields has no raw form. Dumping
    the whole payload answers a question the caller did not ask."""
    deployment_client(check_execution_status={"status_code": 200, "unexpected": 1})
    code = main(["-o", "raw", "docstudio", "deployment", "status", "my-api", "e-1"])
    out, err = capsys.readouterr()
    assert code == int(ExitCode.GENERIC)
    assert "unexpected" not in out
    assert "extraction_result" in out or "extraction_result" in err


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
        "0.1",
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
        "0.1",
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
        "0.1",
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

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0.1")

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

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0.1")

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

    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0.1")

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


def test_a_clone_that_skipped_files_says_so_on_stderr(capsys, monkeypatch):
    """A skip is not a failure, so nothing else tells a caller it happened."""

    def fake_clone(source, target, options):
        return CloneReport(
            source=Endpoint(source.base_url, source.organization_id),
            target=Endpoint(target.base_url, target.organization_id),
            phases=[PhaseResult(name="files", created=1, skipped=3)],
            oversize_files=[{"name": "big.pdf"}],
        )

    monkeypatch.setattr(clone_cmd, "run_clone", fake_clone)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")
    args = (
        "clone",
        "--source-url",
        "https://dev.example.com",
        "--source-org",
        "org_dev",
        "--target-url",
        "https://qa.example.com",
        "--target-org",
        "org_qa",
    )

    code, out, err = run(capsys, *args)
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["ok"] is True
    assert "files 3" in err and "oversize files 1" in err

    assert "Skipped" not in run(capsys, "--quiet", *args)[2]


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
    platform_client(whoami=PlatformClientError("whoami failed with 401: nope"))

    code, out, _ = run(capsys, "auth", "whoami")

    assert code == int(ExitCode.AUTH)
    assert envelope(out)["ok"] is False


def test_whoami_stores_nothing_when_no_organisation_comes_back(
    capsys, platform_client, monkeypatch, tmp_path
):
    """The key was accepted but resolved nothing to store, which the next
    command fails on -- so it is said rather than reported as a save."""
    _platform_env(monkeypatch, tmp_path)
    platform_client(whoami={"organization_name": "Acme"})

    code, out, err = run(capsys, "auth", "whoami")
    meta = envelope(out)["meta"]

    assert code == int(ExitCode.SUCCESS)
    assert (meta["saved"], meta["reason"]) == (False, "no organization_id")
    assert "no organization_id" in err
    assert not (tmp_path / "config.toml").exists()


def test_whoami_without_a_key_is_a_usage_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    code, out, _ = run(capsys, "auth", "whoami")

    assert code == int(ExitCode.USAGE)
    assert "UNSTRACT_PLATFORM_KEY" in json.dumps(envelope(out)["error"])


# --------------------------------------------------------------------------- #
# docstudio deployment ls
# --------------------------------------------------------------------------- #


def _page(*rows, count=None, next_url=None) -> dict:
    """The paginated envelope `list_deployments` returns.

    The old `list_api_deployments` returned a flat list; the generated operation
    returns `{count, next, previous, results}`, and `ls` reports the server's
    total separately from what it shows.
    """
    return {
        "count": count if count is not None else len(rows),
        "next": next_url,
        "previous": None,
        "results": list(rows),
    }


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
    platform_client(list_deployments=_returns(_page(DEPLOYMENT_ROW)))

    _, out, _ = run(capsys, "docstudio", "deployment", "ls")

    (row,) = envelope(out)["data"]["results"]
    assert set(row) == set(platform_cmd.LISTING_FIELDS)
    assert row["api_name"] == "invoice-parser"


def test_ls_can_return_every_field_the_server_sent(
    capsys, platform_client, monkeypatch, tmp_path
):
    _listing_env(monkeypatch, tmp_path)
    platform_client(list_deployments=_returns(_page(DEPLOYMENT_ROW)))

    _, out, _ = run(capsys, "docstudio", "deployment", "ls", "--full")

    (row,) = envelope(out)["data"]["results"]
    assert row == DEPLOYMENT_ROW


def test_ls_reports_the_servers_total_apart_from_what_it_shows(
    capsys, platform_client, monkeypatch, tmp_path
):
    """The listing is paginated and this command does not follow the pages, so
    `count` (the server's total) and `shown` (this page) are different numbers.
    Reporting one as the other would tell a caller with more deployments than a
    page that they had seen everything.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    platform_client(
        list_deployments=_returns(
            _page(DEPLOYMENT_ROW, count=37, next_url="https://h/next?page=2")
        )
    )

    code, out, _ = run(capsys, "docstudio", "deployment", "ls")
    meta = envelope(out)["meta"]

    assert code == 0
    assert meta["shown"] == 1
    assert meta["count"] == 37
    assert meta["more"] is True


def test_ls_says_there_is_no_more_when_the_page_is_the_whole_set(
    capsys, platform_client, monkeypatch, tmp_path
):
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    platform_client(list_deployments=_returns(_page(DEPLOYMENT_ROW)))

    code, out, _ = run(capsys, "docstudio", "deployment", "ls")
    meta = envelope(out)["meta"]

    assert code == 0
    assert (meta["shown"], meta["count"], meta["more"]) == (1, 1, False)


def test_ls_survives_a_page_with_no_results_key(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`results` is declared required, but the facade returns whatever JSON the
    server sent. Absent, the projection used to raise `TypeError` on `None`.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    platform_client(list_deployments=_returns({"count": 0, "next": None}))

    code, out, _ = run(capsys, "docstudio", "deployment", "ls")

    assert code == 0
    assert envelope(out)["data"]["results"] == []


def test_a_listing_that_is_not_a_list_is_a_protocol_failure(
    capsys, platform_client, monkeypatch, tmp_path
):
    """A login page or proxy answering in the API's place is not an account
    with no deployments, and projecting its body would crash on the first row.
    """
    _listing_env(monkeypatch, tmp_path)
    platform_client(list_deployments=_returns({"results": "<html>Sign in</html>"}))

    code, out, _ = run(capsys, "docstudio", "deployment", "ls")
    error = envelope(out)["error"]

    assert code == int(ExitCode.SERVER_ERROR)
    assert "base_url" in error["hint"]
    assert error["details"]["results"] == "<html>Sign in</html>"


def test_ls_passes_the_name_filter_to_the_server(
    capsys, platform_client, monkeypatch, tmp_path
):
    """Filtering here rather than locally: the server has the exact-match
    filter, and a local one would still page the whole organisation."""
    _listing_env(monkeypatch, tmp_path)
    client = platform_client(list_deployments=_returns(_page(DEPLOYMENT_ROW)))

    run(capsys, "docstudio", "deployment", "ls", "--api-name", "invoice-parser")

    assert client.kwargs_for("list_deployments") == {"api_name": "invoice-parser"}


def test_ls_runs_inside_the_configured_organisation(
    capsys, platform_client, monkeypatch, tmp_path
):
    _listing_env(monkeypatch, tmp_path)
    client = platform_client(list_deployments=_returns(_page()))

    run(capsys, "docstudio", "deployment", "ls")

    _, args, _ = next(call for call in client.calls if call[0] == "list_deployments")
    # The factory takes an organisation and ignores it: the listing call is
    # where the wrong one would actually reach the server.
    assert args[0] == "org_ABC123"


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "--base-url", "localhost:8000", "whoami"],
        ["docstudio", "--base-url", "localhost:8000", "deployment", "ls"],
    ],
    ids=["whoami", "ls"],
)
def test_a_client_that_cannot_be_built_is_an_envelope_not_a_traceback(
    capsys, monkeypatch, tmp_path, argv
):
    """The real factory, not the fixture: the client validates the host before
    it sends anything, and every test that replaces the factory replaces that
    check with it."""
    _listing_env(monkeypatch, tmp_path)

    code, out, _ = run(capsys, *argv)

    assert code == int(ExitCode.USAGE)
    assert envelope(out)["ok"] is False


def test_ls_without_an_organisation_says_how_to_get_one(
    capsys, platform_client, monkeypatch, tmp_path
):
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-123")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    platform_client(list_deployments=_returns(_page()))

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
    identity to a full disk would report a working key as a total failure, on
    an exit code that means "you invoked it wrong".
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
    # Not full equality: `redact_value` masks any field whose name looks
    # secret, and `key_name` matches. The organisation is the part the caller
    # needs in order to carry on without the write.
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

    # Declining the write is not failing the call: a committed
    # `.unstract.toml` is supported, and this is the first command a new user
    # runs, so failing it would discard the identity with the write.
    assert code == 0
    assert body["data"]["organization_id"] == IDENTITY["organization_id"]
    assert body["meta"]["saved"] is False
    assert "project-local" in body["meta"]["reason"]
    assert "# hand written" in (project / ".unstract.toml").read_text()
    assert "org_TEAM" in (project / ".unstract.toml").read_text()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["auth", "whoami"], 120.0),
        (["auth", "--transport-timeout", "12.5", "whoami"], 12.5),
        (["auth", "--transport-timeout", "0", "whoami"], None),
        (["docstudio", "deployment", "ls"], 120.0),
        (["docstudio", "--transport-timeout", "12.5", "deployment", "ls"], 12.5),
        (["docstudio", "--transport-timeout", "0", "deployment", "ls"], None),
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
    client = platform_client(whoami=IDENTITY, list_deployments=_returns(_page()))

    code, _, _ = run(capsys, *argv)

    assert code == 0
    assert client.built_with["timeout"] == expected


@pytest.mark.parametrize("value", ["-1", "-0.5"])
def test_a_negative_transport_timeout_is_a_usage_error_not_a_traceback(
    capsys, monkeypatch, tmp_path, value
):
    """A bound below zero is refused at the flag, where it is still a usage
    error about something the caller typed."""
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
    platform_client(list_deployments=_returns(_page()))

    code, out, _ = run(
        capsys, "docstudio", "--api-key", "dk-FROM-FLAG", "deployment", "ls"
    )
    error = envelope(out)["error"]

    assert code == int(ExitCode.USAGE)
    assert "platform key" in error["message"]
    assert "UNSTRACT_PLATFORM_KEY" in error["hint"]
    assert "dk-FROM-FLAG" not in json.dumps(envelope(out))


@pytest.mark.parametrize(
    "args",
    [
        ("auth", "--platform-key", "pk-FROM-FLAG-0123", "whoami", "--no-save"),
        ("docstudio", "--platform-key", "pk-FROM-FLAG-0123", "deployment", "ls"),
    ],
)
def test_a_platform_key_flag_reaches_the_client_and_is_warned_about(
    capsys, platform_client, monkeypatch, tmp_path, args
):
    """Both groups that run platform-key commands take the key as a flag, and a
    key on the command line gets the same shell-history warning as `--api-key`.
    """
    _config_with(
        tmp_path,
        monkeypatch,
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\norg_id = "org_X"\n',
    )
    client = platform_client(whoami=IDENTITY, list_deployments=_returns(_page()))

    code, out, err = run(capsys, *args)

    assert code == int(ExitCode.SUCCESS)
    assert client.built_with["api_key"] == "pk-FROM-FLAG-0123"
    assert "shell history" in err
    assert "pk-FROM-FLAG-0123" not in out


def test_the_platform_key_never_reaches_a_stream(
    capsys, platform_client, monkeypatch, tmp_path
):
    """A refusal's reason comes from the server, so if the far end echoes the key
    back it travels to stdout inside `error.message`.

    `PlatformClientError` carries no body attribute -- the released client folds
    the reason into its message -- so that is now the only path, and the scrubber
    is the only thing standing on it.
    """
    _config_with(tmp_path, monkeypatch, 'default_profile = "cloud-us"\n')
    monkeypatch.setenv("UNSTRACT_PLATFORM_KEY", "pk-SUPERSECRET-0987654321")
    platform_client(
        whoami=PlatformClientError(
            'whoami failed with 401: {"echoed": "pk-SUPERSECRET-0987654321"}'
        )
    )

    _, out, err = run(capsys, "auth", "whoami")

    assert "pk-SUPERSECRET-0987654321" not in out
    assert "pk-SUPERSECRET-0987654321" not in err


def test_a_rejected_key_exits_auth_not_usage(
    capsys, platform_client, monkeypatch, tmp_path
):
    """`PlatformClientError` derives from `APIDeploymentsClientException`, whose
    arm maps everything to USAGE, and the released client carries no status --
    only the prose `"whoami failed with 401: ..."`. Caught by the base arm a
    rejected key would exit 2, contradicting the README's exit-code table and any
    setup script branching on 3.
    """
    _config_with(tmp_path, monkeypatch, 'default_profile = "cloud-us"\n')
    platform_client(whoami=PlatformClientError("whoami failed with 401: nope"))

    code, out, _ = run(capsys, "auth", "whoami")
    error = envelope(out)["error"]

    assert code == int(ExitCode.AUTH)
    assert error["exit_code"] == int(ExitCode.AUTH)
    assert "\n" not in error["message"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("whoami failed with 401: bad key", ExitCode.AUTH),
        ("whoami failed with 403: not yours", ExitCode.AUTH),
        ("list_deployments failed with 404: gone", ExitCode.NOT_FOUND),
        ("list_deployments failed with 429: slow down", ExitCode.RATE_LIMITED),
        ("whoami failed with 500: boom", ExitCode.SERVER_ERROR),
        # No status in the message: the failure is real and its status unknown,
        # so it is reported as server-side rather than as the caller's mistake.
        ("whoami returned something unreadable", ExitCode.SERVER_ERROR),
    ],
)
def test_the_platform_status_is_recovered_from_the_message(
    capsys, platform_client, monkeypatch, tmp_path, message, expected
):
    """Pins the parse. The released client embeds the status in prose, so an
    upstream wording change silently costs every one of these mappings -- this
    is what would catch it.
    """
    _config_with(tmp_path, monkeypatch, 'default_profile = "cloud-us"\n')
    platform_client(whoami=PlatformClientError(message))

    code, _, _ = run(capsys, "auth", "whoami")

    assert code == int(expected), message


# --------------------------------------------------------------------------- #
# auth login
# --------------------------------------------------------------------------- #

PK, DK, LK = "pk-platform-000001", "dk-deployment-0001", "lk-whisperer-00001"


@pytest.fixture
def login_seams(monkeypatch, platform_client, tmp_path):
    """Both client factories faked, prompts scripted, and a config file of our own.

    Returns a function that scripts the terminal: `answers` are what each prompt
    returns in order, `confirm` what the yes/no questions return (one value for
    all of them, or a list consumed in order), and `tty` whether stdin counts
    as a terminal at all.
    """
    monkeypatch.setenv("UNSTRACT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("UNSTRACT_PLATFORM_KEY", raising=False)
    state = {"prompts": [], "answers": []}

    def prompt(text, **kwargs):
        state["prompts"].append(text)
        return state["answers"].pop(0)

    def install(answers=(), *, confirm=False, tty=True, whoami=None, usage=None):
        state["answers"], state["prompts"] = list(answers), []
        monkeypatch.setattr(platform_cmd, "_interactive", lambda: tty)
        monkeypatch.setattr(platform_cmd, "_prompt", prompt)
        confirms = list(confirm) if isinstance(confirm, list) else None

        def confirm_answer(text, **kwargs):
            state["confirms"].append(text)
            return confirms.pop(0) if confirms is not None else confirm

        state["confirms"] = []
        monkeypatch.setattr(platform_cmd, "_confirm", confirm_answer)
        client = platform_client(whoami=whoami or IDENTITY)
        build_platform = platform_cmd.platform_client

        def build_recording_host(config, org_id=None, *, timeout=None):
            client.built_with["base_url"] = config.get(DOCSTUDIO, "base_url")
            return build_platform(config, org_id, timeout=timeout)

        monkeypatch.setattr(platform_cmd, "platform_client", build_recording_host)
        whisper = FakeWhisper(get_usage_info=usage if usage is not None else {"quota": 1})
        whisper.built_with = {}

        def build(config):
            whisper.built_with["api_key"] = config.get(LLMWHISPERER, "api_key")
            return whisper

        monkeypatch.setattr(platform_cmd, "llmwhisperer", build)
        state["platform"], state["whisper"] = client, whisper
        return state

    return install


def _written(tmp_path) -> str:
    return (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_login_asks_for_each_key_in_turn_and_stores_them_as_literals(
    capsys, login_seams, tmp_path
):
    """Interactive path: platform, deployment, LLMWhisperer, one hidden prompt
    each. The two keys with a read-only endpoint are checked; the deployment
    key is stored as given and said to be."""
    seams = login_seams([PK, DK, LK])

    code, out, err = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert [p.split(" (")[0] for p in seams["prompts"]] == [
        "Platform key",
        "Deployment key",
        "LLMWhisperer key",
    ]
    assert seams["platform"].built_with["api_key"] == PK
    assert seams["whisper"].built_with["api_key"] == LK
    data = envelope(out)["data"]
    assert data["profile"] == "cloud-us"
    assert (data["platform"], data["deployment"], data["llmwhisperer"]) == (
        "verified",
        "stored",
        "verified",
    )
    assert data["organization_id"] == "org_ABC123"
    assert "stored as given" in data["note"]
    text = _written(tmp_path)
    assert f'platform_key = "{PK}"' in text
    assert f'api_key = "{DK}"' in text
    assert f'api_key = "{LK}"' in text
    assert 'org_id = "org_ABC123"' in text
    assert oct((tmp_path / "config.toml").stat().st_mode & 0o777) == "0o600"
    # The keys reach the file and nowhere else.
    for key in (PK, DK, LK):
        assert key not in out and key not in err


def test_login_with_every_prompt_skipped_is_a_usage_error(capsys, login_seams, tmp_path):
    login_seams(["", "", ""])

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.USAGE)
    assert "at least one" in envelope(out)["error"]["message"]
    assert not (tmp_path / "config.toml").exists()


def test_login_with_only_a_deployment_key_calls_nothing(capsys, login_seams, tmp_path):
    seams = login_seams(["", DK, ""])

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert seams["platform"].calls == [] and seams["whisper"].calls == []
    data = envelope(out)["data"]
    assert (data["platform"], data["deployment"], data["llmwhisperer"]) == (
        "skipped",
        "stored",
        "skipped",
    )
    assert "org_id" not in _written(tmp_path)


def test_login_without_a_terminal_and_without_flags_fails_before_prompting(
    capsys, login_seams, tmp_path
):
    """A script that reaches a hidden prompt hangs; a script told which flags
    to pass does not."""
    seams = login_seams([], tty=False)

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.USAGE)
    assert seams["prompts"] == []
    error = envelope(out)["error"]
    assert "not a terminal" in error["message"]
    assert "--platform-key" in error["hint"] and "--llmwhisperer-key" in error["hint"]
    assert not (tmp_path / "config.toml").exists()


def test_login_flags_take_values_and_one_of_them_from_stdin(
    capsys, login_seams, monkeypatch, tmp_path
):
    """The non-interactive twin: zero prompts, even at a terminal."""
    import io

    seams = login_seams([], tty=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PK}\n"))

    code, out, _ = run(
        capsys, "auth", "login", "--platform-key", "-", "--llmwhisperer-key", LK
    )

    assert code == int(ExitCode.SUCCESS)
    assert seams["prompts"] == []
    assert seams["platform"].built_with["api_key"] == PK
    assert seams["whisper"].built_with["api_key"] == LK
    assert envelope(out)["data"]["deployment"] == "skipped"
    assert f'platform_key = "{PK}"' in _written(tmp_path)


def test_a_key_quoted_back_by_a_rejected_login_is_scrubbed(capsys, login_seams):
    """The deployment key is stored without being sent anywhere, so nothing
    else in the run would ever register it for scrubbing."""
    login_seams([], whoami=PlatformClientError(f"whoami failed with 400: sent {DK}"))

    code, out, err = run(
        capsys, "auth", "login", "--platform-key", PK, "--deployment-key", DK
    )

    assert code != int(ExitCode.SUCCESS)
    assert DK not in out and DK not in err
    assert "***REDACTED***" in out


def test_login_says_so_when_the_key_resolves_no_organisation(capsys, login_seams):
    """Every docstudio command needs one, so a login that stored none has to
    say it -- as `whoami` already does for the same answer."""
    login_seams([], whoami={"organization_name": "Acme"})

    code, out, err = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["organization_id"] is None
    assert "no organization_id" in err


def test_login_reads_at_most_one_key_from_stdin(capsys, login_seams, tmp_path):
    login_seams([])

    code, out, _ = run(
        capsys, "auth", "login", "--platform-key", "-", "--deployment-key", "-"
    )

    assert code == int(ExitCode.USAGE)
    assert "stdin" in envelope(out)["error"]["message"]
    assert not (tmp_path / "config.toml").exists()


def test_login_takes_the_platform_key_from_the_group_flag_too(
    capsys, login_seams, tmp_path
):
    seams = login_seams([])

    code, _, _ = run(capsys, "auth", "--platform-key", PK, "login")

    assert code == int(ExitCode.SUCCESS)
    assert seams["prompts"] == []
    assert f'platform_key = "{PK}"' in _written(tmp_path)


@pytest.mark.parametrize(
    "rejected",
    ["platform", "llmwhisperer"],
)
def test_login_writes_nothing_when_any_key_is_rejected(
    capsys, login_seams, tmp_path, rejected
):
    """Every check runs before the one write: a file holding one good key and
    one bad one would report the bad one as configured."""
    kwargs = {
        "whoami": PlatformClientError("whoami failed with 401: nope")
        if rejected == "platform"
        else None,
        "usage": LLMWhispererClientException(
            {"message": "bad key", "status_code": 401}, 401
        )
        if rejected == "llmwhisperer"
        else None,
    }
    login_seams([PK, DK, LK], **kwargs)

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.AUTH)
    assert envelope(out)["ok"] is False
    assert not (tmp_path / "config.toml").exists()


def test_login_again_replaces_the_keys_given_and_keeps_the_rest(
    capsys, login_seams, tmp_path
):
    """Rotation: the same profile, updated in place, and a same-organisation
    re-run asks nothing."""
    login_seams([PK, DK, LK])
    run(capsys, "auth", "login")
    seams = login_seams(["", "dk-rotated-000001", ""])

    code, _, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert seams["prompts"][-1].startswith("LLMWhisperer")
    text = _written(tmp_path)
    assert 'api_key = "dk-rotated-000001"' in text and DK not in text
    assert f'platform_key = "{PK}"' in text and f'api_key = "{LK}"' in text
    assert text.count("[profiles.") == 2


def test_login_refuses_to_repoint_a_profile_at_another_organisation(
    capsys, login_seams, tmp_path
):
    """Non-interactive: fail, name both organisations, name the way out."""
    login_seams([], whoami={**IDENTITY, "organization_id": "org_OLD"})
    run(capsys, "auth", "login", "--platform-key", PK)
    login_seams([], whoami={**IDENTITY, "organization_id": "org_NEW"})

    code, out, _ = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.USAGE)
    error = envelope(out)["error"]
    assert "org_OLD" in error["message"] and "org_NEW" in error["message"]
    assert "--force" in error["hint"] and "--profile" in error["hint"]
    assert 'org_id = "org_OLD"' in _written(tmp_path)


def test_login_overwrites_the_organisation_only_when_forced(
    capsys, login_seams, tmp_path
):
    login_seams([], whoami={**IDENTITY, "organization_id": "org_OLD"})
    run(capsys, "auth", "login", "--platform-key", PK)
    login_seams([], whoami={**IDENTITY, "organization_id": "org_NEW"})

    code, _, _ = run(capsys, "auth", "login", "--platform-key", PK, "--force")

    assert code == int(ExitCode.SUCCESS)
    assert 'org_id = "org_NEW"' in _written(tmp_path)
    assert "org_OLD" not in _written(tmp_path)


def test_login_offers_a_new_profile_named_after_the_organisation(
    capsys, login_seams, tmp_path
):
    """Interactive: the profile the key belongs to is a new one, suggested
    from the organisation's display name, and the old profile is untouched."""
    login_seams([PK, "", ""], whoami={**IDENTITY, "organization_id": "org_OLD"})
    run(capsys, "auth", "login")
    seams = login_seams(
        [PK, "", "", "beta-corp"],
        confirm=True,
        whoami={
            **IDENTITY,
            "organization_id": "org_NEW",
            "organization_name": "Beta Corp",
        },
    )

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["profile"] == "beta-corp"
    text = _written(tmp_path)
    assert "[profiles.beta-corp.docstudio]" in text and 'org_id = "org_NEW"' in text
    assert 'org_id = "org_OLD"' in text
    assert seams["prompts"][-1] == "Profile name"


def test_a_new_profile_offered_by_the_guard_keeps_the_host_the_key_was_checked_on(
    capsys, login_seams, tmp_path
):
    """The key was verified against the profile the login started from; a new
    profile that does not record that host would send it to the built-in
    default next time."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "onprem"\n[profiles.onprem.docstudio]\n'
        'base_url = "https://onprem.example/"\norg_id = "org_OLD"\n',
        encoding="utf-8",
    )
    seams = login_seams(
        [PK, "", "", "beta"],
        confirm=True,
        whoami={**IDENTITY, "organization_id": "org_NEW"},
    )

    code, _, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert seams["platform"].built_with["base_url"] == "https://onprem.example/"
    text = _written(tmp_path)
    assert text.count('base_url = "https://onprem.example/"') == 2
    assert "[profiles.beta.docstudio]" in text


def test_a_profile_chosen_at_the_guard_is_replaced_not_merged_into(
    capsys, login_seams, tmp_path
):
    """The name typed at the guard may be an existing profile with a host and
    keys of its own; none of those were checked against this key's host, so
    the profile is confirmed and then rebuilt from what this login verified."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "onprem"\n[profiles.onprem.docstudio]\n'
        'base_url = "https://onprem.example/"\norg_id = "org_OLD"\n'
        '[profiles.beta.docstudio]\nbase_url = "https://stale.example/"\n'
        'api_key = "sk-stale"\n'
        '[profiles.beta.deployments."invoice-parser"]\napi_key = "sk-stale-entry"\n',
        encoding="utf-8",
    )
    seams = login_seams(
        [PK, "", "", "beta"],
        confirm=True,
        whoami={**IDENTITY, "organization_id": "org_NEW"},
    )

    code, _, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert seams["confirms"][-1] == "Profile 'beta' already exists. Replace it?"
    text = _written(tmp_path)
    assert "stale" not in text
    assert text.count('base_url = "https://onprem.example/"') == 2
    assert "[profiles.beta.docstudio]" in text and 'org_id = "org_NEW"' in text


def test_a_new_profile_name_that_belongs_to_a_third_organisation_is_confirmed(
    capsys, login_seams, tmp_path
):
    """The guard's own remedy must not be the overwrite it exists to prevent:
    a typed name that is another organisation's profile is asked about again,
    and declining re-prompts."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "cloud-us"\n'
        '[profiles.cloud-us.docstudio]\norg_id = "org_OLD"\n'
        '[profiles.partner.docstudio]\norg_id = "org_PARTNER"\n',
        encoding="utf-8",
    )
    seams = login_seams(
        [PK, "", "", "partner", "fresh"],
        confirm=[True, False],
        whoami={**IDENTITY, "organization_id": "org_NEW"},
    )

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["profile"] == "fresh"
    assert "partner" in seams["confirms"][1] and "org_PARTNER" in seams["confirms"][1]
    text = _written(tmp_path)
    assert 'org_id = "org_PARTNER"' in text and 'org_id = "org_OLD"' in text
    assert "[profiles.fresh.docstudio]" in text and 'org_id = "org_NEW"' in text


def test_login_overwrites_when_a_new_profile_is_declined(capsys, login_seams, tmp_path):
    login_seams([PK, "", ""], whoami={**IDENTITY, "organization_id": "org_OLD"})
    run(capsys, "auth", "login")
    seams = login_seams(
        [PK, "", ""], confirm=False, whoami={**IDENTITY, "organization_id": "org_NEW"}
    )

    code, out, _ = run(capsys, "auth", "login")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["profile"] == "cloud-us"
    assert len(seams["prompts"]) == 3
    assert 'org_id = "org_NEW"' in _written(tmp_path)
    assert "org_OLD" not in _written(tmp_path)


STRANDING_CONFIG = (
    'default_profile = "p"\n[profiles.p.docstudio]\n'
    'base_url = "https://stored.example/"\norg_id = "org_ABC123"\n'
    'api_key = "DEPLOYMENT-KEY-AAAA"\n'
    '[profiles.p.deployments.invoices]\napi_key = "ENTRY-KEY-BBBB"\n'
)


def test_login_drops_the_keys_a_host_change_leaves_unchecked(
    capsys, login_seams, tmp_path
):
    """The deployment keys were checked against the host the profile held. A
    login that stores another one would send them somewhere they were never
    accepted."""
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    seams = login_seams([PK, "", ""], confirm=True)

    code, _, _ = run(capsys, "auth", "--base-url", "https://moved.example/", "login")

    assert code == int(ExitCode.SUCCESS)
    asked = " ".join(seams["confirms"])
    assert "docstudio api_key" in asked and "deployment invoices" in asked
    text = _written(tmp_path)
    assert 'base_url = "https://moved.example/"' in text
    assert "DEPLOYMENT-KEY-AAAA" not in text
    assert "ENTRY-KEY-BBBB" not in text
    assert "invoices" not in text


def test_login_aborts_rather_than_drop_keys_the_answer_declined(
    capsys, login_seams, tmp_path
):
    """Declining is a decision about the whole login: the keys are worth more
    than the host change, so nothing is written at all."""
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    login_seams([PK, "", ""], confirm=False)

    code, _, _ = run(capsys, "auth", "--base-url", "https://moved.example/", "login")

    assert code == int(ExitCode.USAGE)
    assert _written(tmp_path) == STRANDING_CONFIG


def test_login_without_a_terminal_refuses_to_strand_keys_until_forced(
    capsys, login_seams, tmp_path
):
    """Nothing can be asked, so the keys are kept and the run fails; --force is
    how a script says it accepts losing them."""
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    login_seams([], tty=False)

    code, _, err = run(
        capsys,
        "auth",
        "--base-url",
        "https://moved.example/",
        "login",
        "--platform-key",
        PK,
    )

    assert code == int(ExitCode.USAGE)
    assert "docstudio api_key" in err and "deployment invoices" in err
    assert _written(tmp_path) == STRANDING_CONFIG

    code, _, _ = run(
        capsys,
        "auth",
        "--base-url",
        "https://moved.example/",
        "login",
        "--platform-key",
        PK,
        "--force",
    )

    assert code == int(ExitCode.SUCCESS)
    text = _written(tmp_path)
    assert "DEPLOYMENT-KEY-AAAA" not in text and "ENTRY-KEY-BBBB" not in text


@pytest.mark.parametrize(
    "base_url",
    ["https://stored.example/", "https://stored.example", "HTTPS://Stored.Example/"],
)
def test_a_rotation_against_the_same_host_keeps_the_other_keys(
    capsys, login_seams, tmp_path, base_url
):
    """Re-logging in against the host the profile already names checks nothing
    new, so there is nothing to ask about and nothing to drop; a trailing slash
    or letter case is the same host spelt differently."""
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    seams = login_seams([])

    code, _, _ = run(
        capsys, "auth", "--base-url", base_url, "login", "--platform-key", PK
    )

    assert code == int(ExitCode.SUCCESS)
    assert seams["confirms"] == []
    text = _written(tmp_path)
    assert "DEPLOYMENT-KEY-AAAA" in text and "ENTRY-KEY-BBBB" in text
    assert f'base_url = "{base_url}"' in text


def test_a_host_the_environment_selects_strands_the_keys_too(
    capsys, login_seams, tmp_path, monkeypatch
):
    """The environment outranks the profile on both the host being checked and
    the host the profile is read back with; only the file says where the keys
    were going before."""
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    login_seams([], tty=False)
    monkeypatch.setenv("UNSTRACT_BASE_URL", "https://moved.example/")

    code, _, err = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.USAGE)
    assert "deployment invoices" in err
    assert _written(tmp_path) == STRANDING_CONFIG


def test_a_host_that_differs_beyond_spelling_still_strands_the_keys(
    capsys, login_seams, tmp_path
):
    (tmp_path / "config.toml").write_text(STRANDING_CONFIG, encoding="utf-8")
    login_seams([], tty=False)

    code, _, err = run(
        capsys,
        "auth",
        "--base-url",
        "https://stored.example.org/",
        "login",
        "--platform-key",
        PK,
    )

    assert code == int(ExitCode.USAGE)
    assert "deployment invoices" in err


def test_login_writes_the_profile_named_and_checks_against_its_own_host(
    capsys, login_seams, tmp_path
):
    """A profile that does not exist yet must not borrow the default profile's
    host for the check: the key would be verified against a server the new
    profile will never talk to."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "cloud-us"\n[profiles.cloud-us.docstudio]\n'
        'base_url = "https://elsewhere.example/"\norg_id = "org_X"\n',
        encoding="utf-8",
    )
    seams = login_seams([])

    code, out, _ = run(
        capsys,
        "auth",
        "--base-url",
        "https://staging.example/",
        "login",
        "--profile",
        "staging",
        "--platform-key",
        PK,
    )

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["profile"] == "staging"
    assert seams["platform"].built_with["base_url"] == "https://staging.example/"
    text = _written(tmp_path)
    assert "[profiles.staging.docstudio]" in text
    assert 'base_url = "https://staging.example/"' in text
    assert 'org_id = "org_X"' in text  # the other profile is untouched


def test_login_that_cannot_write_exits_on_the_save_code(
    capsys, login_seams, monkeypatch, tmp_path
):
    """The keys were checked and accepted; only the write failed, and a setup
    script branching on the exit code needs to tell that from a bad key."""
    login_seams([])
    monkeypatch.setattr(
        platform_cmd,
        "save_config",
        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "no space left on device")),
    )

    code, out, _ = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.SAVE_FAILED)
    assert "no space left on device" in envelope(out)["error"]["message"]


def test_login_moves_an_existing_profile_to_the_host_it_checked_against(
    capsys, login_seams, tmp_path
):
    """The key was verified against the flag's host; leaving the old one in
    place would send it to a server it was never checked against."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "p"\n[profiles.p.docstudio]\n'
        'base_url = "https://old.example/"\norg_id = "org_ABC123"\n',
        encoding="utf-8",
    )
    login_seams([])

    code, _, _ = run(
        capsys,
        "auth",
        "--base-url",
        "https://new.example/",
        "login",
        "--platform-key",
        PK,
    )

    assert code == int(ExitCode.SUCCESS)
    text = _written(tmp_path)
    assert 'base_url = "https://new.example/"' in text
    assert "old.example" not in text


def test_login_moves_an_existing_profile_to_the_host_the_environment_chose(
    capsys, login_seams, monkeypatch, tmp_path
):
    """Same as with a flag: the host the key was checked against is the one
    stored, whatever the profile said before."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "p"\n[profiles.p.docstudio]\n'
        'base_url = "https://stored.example/"\norg_id = "org_ABC123"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UNSTRACT_BASE_URL", "https://from-env.example/")
    login_seams([])

    code, _, _ = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.SUCCESS)
    text = _written(tmp_path)
    assert 'base_url = "https://from-env.example/"' in text
    assert "stored.example" not in text


def test_login_says_when_the_host_it_checked_against_came_from_a_reference(
    capsys, login_seams, monkeypatch, tmp_path
):
    """A profile holding `env:VAR` keeps holding it; the keys are stored beside
    a host that can change without the file changing, and that is worth saying."""
    (tmp_path / "config.toml").write_text(
        'default_profile = "p"\n[profiles.p.docstudio]\n'
        'base_url = "env:DOCSTUDIO_HOST"\norg_id = "org_ABC123"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCSTUDIO_HOST", "https://referenced.example/")
    login_seams([])

    code, _, err = run(capsys, "auth", "login", "--platform-key", PK)

    assert code == int(ExitCode.SUCCESS)
    assert "$DOCSTUDIO_HOST" in err
    assert "https://referenced.example/" in err
    assert 'base_url = "env:DOCSTUDIO_HOST"' in _written(tmp_path)


def test_login_does_not_write_a_discovered_project_config(
    capsys, login_seams, monkeypatch, tmp_path
):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".unstract.toml").write_text(
        "[profiles.team.docstudio]\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("UNSTRACT_CONFIG", raising=False)
    login_seams([])

    code, out, _ = run(capsys, "auth", "login", "--deployment-key", DK)

    assert code == int(ExitCode.USAGE)
    assert "project-local" in envelope(out)["error"]["message"]
    assert DK not in (project / ".unstract.toml").read_text()


# --------------------------------------------------------------------------- #
# --save: the flag that exists to protect a one-shot read
# --------------------------------------------------------------------------- #


def test_save_with_no_wait_is_a_usage_error(capsys, whisper_client, tmp_path):
    """--no-wait returns before there is a result, so --save would write
    nothing while reporting success."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(whisper={"whisper_hash": "h1", "status_code": 202})

    code, out, _ = run(
        capsys,
        "whisper",
        "extract",
        str(doc),
        "--no-wait",
        "--save",
        str(tmp_path / "out.json"),
    )

    assert code == int(ExitCode.USAGE)
    assert not (tmp_path / "out.json").exists()
    assert "retrieve" in envelope(out)["error"]["hint"]


#: The flattened shape the pinned client hands back for a finished batch: the
#: execution completed, one document inside it did not.
PARTIAL_FAILURE = {
    "status_code": 200,
    "pending": False,
    "execution_status": "COMPLETED",
    "error": "",
    "extraction_result": [
        {
            "file": "a.pdf",
            "file_execution_id": "f1",
            "status": "Success",
            # A field the redactor would blank: the assertion on the rescued
            # payload is only a check if redaction would have changed it.
            "result": {"policy_key": "PK-1", "total": 1},
            "error": None,
            "metadata": {},
        },
        {
            "file": "bad.pdf",
            "file_execution_id": "f2",
            "status": "Failed",
            "result": None,
            "error": "Structure tool failed: 415 not supported",
            "metadata": {},
        },
        {
            "file": "c.pdf",
            "file_execution_id": "f3",
            "status": "Success",
            "result": {"total": 3},
            "error": None,
            "metadata": {},
        },
        # No status at all, only an error: the shape a tool that died before
        # reporting leaves behind.
        {"file": "noStatus.pdf", "file_execution_id": "f4", "error": "tool died"},
    ],
}


def _partial_run(capsys, deployment_client, tmp_path, *extra):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "execution_status": "PENDING",
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status=PARTIAL_FAILURE,
    )
    return run(
        capsys,
        "-q",
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0.1",
        *extra,
    )


def test_a_completed_run_with_a_failed_document_is_not_a_success(
    capsys, deployment_client, tmp_path
):
    """The batch status says the job ran, not that every document came out, so
    the exit code has to read the per-file results."""
    code, out, _ = _partial_run(capsys, deployment_client, tmp_path)

    assert code == int(ExitCode.VALIDATION)
    error = envelope(out)["error"]
    assert error["failed_files"] == ["bad.pdf", "noStatus.pdf"]
    assert error["execution_id"] == "e1"
    assert error["details"]["extraction_result"][0]["result"]["policy_key"] == "PK-1"
    assert "bad.pdf" in error["message"]
    # One-shot read: the successful documents survive only here, unredacted.
    assert error["details"] == PARTIAL_FAILURE


def test_a_run_whose_documents_all_succeeded_is_still_a_success(
    capsys, deployment_client, tmp_path
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={
            "status_code": 200,
            "pending": True,
            "status_check_api_endpoint": "/status?execution_id=e1",
        },
        check_execution_status={
            **PARTIAL_FAILURE,
            "extraction_result": [
                {**PARTIAL_FAILURE["extraction_result"][0]},
                {**PARTIAL_FAILURE["extraction_result"][2], "status": "SUCCESS"},
            ],
        },
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
        "0.1",
    )

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["ok"] is True


def test_a_failed_document_is_saved_before_the_run_is_failed(
    capsys, deployment_client, tmp_path
):
    target = tmp_path / "result.json"
    code, _, _ = _partial_run(capsys, deployment_client, tmp_path, "--save", str(target))

    assert code == int(ExitCode.VALIDATION)
    saved = json.loads(target.read_text())
    assert [e["file"] for e in saved["extraction_result"]] == [
        "a.pdf",
        "bad.pdf",
        "c.pdf",
        "noStatus.pdf",
    ]


def test_a_status_read_with_a_failed_document_is_not_a_success(
    capsys, deployment_client, tmp_path
):
    target = tmp_path / "result.json"
    deployment_client(check_execution_status=PARTIAL_FAILURE)

    code, out, _ = run(
        capsys,
        "docstudio",
        "deployment",
        "status",
        "my-api",
        "e-1",
        "--save",
        str(target),
    )

    assert code == int(ExitCode.VALIDATION)
    error = envelope(out)["error"]
    assert error["failed_files"] == ["bad.pdf", "noStatus.pdf"]
    assert error["execution_id"] == "e-1"
    assert target.exists()


def test_deployment_status_can_save_the_result(capsys, deployment_client, tmp_path):
    """`deployment status` is the documented way to resume after a timeout, so
    it is where a result has to be savable."""
    target = tmp_path / "result.json"
    deployment_client(
        check_execution_status={
            "status_code": 200,
            "execution_status": "COMPLETED",
            "extraction_result": {"text": "done"},
        }
    )

    code, out, _ = run(
        capsys,
        "docstudio",
        "deployment",
        "status",
        "my-api",
        "e-1",
        "--save",
        str(target),
    )

    assert code == int(ExitCode.SUCCESS)
    assert json.loads(target.read_text())["execution_status"] == "COMPLETED"
    assert envelope(out)["ok"] is True


def test_an_execution_id_cannot_carry_query_syntax(capsys, deployment_client):
    client = deployment_client(
        check_execution_status={"status_code": 200, "execution_status": "COMPLETED"}
    )
    run(capsys, "docstudio", "deployment", "status", "my-api", "e-1&admin=1")
    endpoint = client.calls[0][1][0]
    assert endpoint.endswith("?execution_id=e-1%26admin%3D1")


# --------------------------------------------------------------------------- #
# whisper status: a failure inside an HTTP 200
# --------------------------------------------------------------------------- #


def test_whisper_status_fails_on_a_failed_extraction(capsys, whisper_client):
    whisper_client(whisper_status={"status": "error", "message": "bad scan"})

    code, out, _ = run(capsys, "whisper", "status", "h1")

    assert code == int(ExitCode.VALIDATION)
    error = envelope(out)["error"]
    assert error["details"]["message"] == "bad scan"
    assert error["whisper_hash"] == "h1"


def test_whisper_status_reports_a_hash_the_service_forgot(capsys, whisper_client):
    """`unknown` is terminal: the service no longer holds the hash, and no
    amount of polling changes that."""
    whisper_client(whisper_status={"status": "unknown"})

    code, _, _ = run(capsys, "whisper", "status", "h1")

    assert code == int(ExitCode.VALIDATION)


def test_whisper_status_passes_a_running_extraction_through(capsys, whisper_client):
    whisper_client(whisper_status={"status": "processing"})

    code, out, _ = run(capsys, "whisper", "status", "h1")

    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["status"] == "processing"


def test_a_webhook_token_is_not_printed_back(capsys, whisper_client):
    token = "wh-secret-abcdefghijklmnop"
    whisper_client(get_webhook_details={"name": "n", "auth_token": token, "url": "u"})

    code, out, _ = run(capsys, "whisper", "webhook", "get", "n")

    assert code == int(ExitCode.SUCCESS)
    assert token not in out


def test_a_webhook_can_be_updated_and_removed(capsys, whisper_client):
    """The token reaches the client and never the output, on both commands."""
    token = "wh-token-abcdefghijk"
    client = whisper_client(
        update_webhook_details={"message": "updated"},
        delete_webhook={"message": "deleted"},
    )

    code, out, err = run(
        capsys,
        "whisper",
        "webhook",
        "update",
        "hook1",
        "--url",
        "https://example.com/hook",
        "--auth-token",
        token,
    )
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["message"] == "updated"
    assert client.calls[0][1] == ("hook1", "https://example.com/hook", token)
    assert token not in out and token not in err

    code, out, _ = run(capsys, "whisper", "webhook", "delete", "hook1")
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["message"] == "deleted"
    assert client.calls[-1][1] == ("hook1",)


def test_a_token_quoted_back_while_updating_a_webhook_is_scrubbed(capsys, whisper_client):
    """`remember_secret` runs before the call, so a failure quoting it is covered."""
    token = "wh-token-abcdefghijk"
    whisper_client(
        update_webhook_details=LLMWhispererClientException(
            f"rejected token {token}", status_code=400
        )
    )

    code, out, err = run(
        capsys,
        "whisper",
        "webhook",
        "update",
        "hook1",
        "--url",
        "https://example.com/hook",
        "--auth-token",
        token,
    )

    assert code == int(ExitCode.VALIDATION)
    assert token not in out and token not in err


def test_a_rate_limited_call_exits_six(capsys, whisper_client):
    whisper_client(
        get_usage_info=LLMWhispererClientException("slow down", status_code=429)
    )

    code, out, _ = run(capsys, "whisper", "usage")

    assert code == int(ExitCode.RATE_LIMITED) == 6
    assert envelope(out)["error"]["retryable"] is True


def test_a_wait_that_runs_out_exits_seven_naming_the_handle(
    capsys, whisper_client, tmp_path, monkeypatch
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    whisper_client(
        whisper={"whisper_hash": "h1", "status_code": 202},
        whisper_status={"status": "processing"},
    )
    monkeypatch.setattr("unstract_cli.core.poll.time.sleep", lambda _seconds: None)

    code, out, _ = run(
        capsys, "whisper", "extract", str(doc), "--interval", "0.1", "--timeout", "0"
    )

    assert code == int(ExitCode.TIMEOUT) == 7
    error = envelope(out)["error"]
    assert error["whisper_hash"] == "h1"
    assert error["retryable"] is True


def test_deployment_save_with_no_wait_is_a_usage_error(
    capsys, deployment_client, tmp_path
):
    """The deployment path needs the same guard as the whisper one: --no-wait
    returns before there is a result, so --save would write nothing."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file={"status_code": 200, "pending": True, "execution_status": "P"}
    )

    code, out, _ = run(
        capsys,
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--no-wait",
        "--save",
        str(tmp_path / "out.json"),
    )

    assert code == int(ExitCode.USAGE)
    assert not (tmp_path / "out.json").exists()


def test_a_webhook_token_can_come_from_the_environment(
    capsys, whisper_client, monkeypatch
):
    """Passing a credential as an argument puts it in the process list, so the
    envvar is the supported way to supply it."""
    monkeypatch.setenv("UNSTRACT_WEBHOOK_AUTH_TOKEN", "wh-token-0123456789")
    client = whisper_client(register_webhook={"status_code": 201, "message": "ok"})

    code, _, _ = run(
        capsys,
        "whisper",
        "webhook",
        "create",
        "hook1",
        "--url",
        "https://example.com/hook",
    )

    assert code == int(ExitCode.SUCCESS)
    assert "wh-token-0123456789" in client.calls[0][1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("25", 25), ("2K", 2048), ("500M", 500 * 1024**2), ("1.5GB", int(1.5 * 1024**3))],
)
def test_clone_accepts_every_size_spelling_the_client_does(value, expected):
    """Both spellings of this command have to accept the same strings."""
    assert clone_cmd._parse_size(value) == expected


@pytest.mark.parametrize("value", ["1.2.3", ".", "5X", ""])
def test_a_malformed_size_is_a_usage_error_not_a_crash(value):
    """`float()` raising here would escape as a traceback with no envelope."""
    with pytest.raises(click.BadParameter):
        clone_cmd._parse_size(value)


def test_splitting_a_csv_drops_blanks_and_trims():
    assert clone_cmd._split_csv(" a , b ,, c ") == ("a", "b", "c")
    assert clone_cmd._split_csv("") is None
    assert clone_cmd._split_csv(None) is None


def test_an_unusable_output_format_is_still_reported_as_an_envelope(capsys):
    """The format is resolved before the handler that renders envelopes exists,
    so a failure there has to fall back rather than raise past it."""
    code = main(["-o", "bogus", "whisper", "status", "h1"])
    out = capsys.readouterr().out
    assert code == int(ExitCode.USAGE)
    # Rendered in whatever the fallback resolves to, but on stdout and shaped
    # like a report: raising here would leave stdout empty instead.
    assert "bogus" in out


def test_a_base_url_without_a_scheme_is_a_usage_error(capsys, whisper_client):
    """The request was never sendable, so the fault is the caller's config and
    retrying it is the wrong advice."""
    whisper_client(get_usage_info=MissingSchema("Invalid URL 'example.com'"))
    code, out, _ = run(capsys, "whisper", "usage")
    assert code == int(ExitCode.USAGE)
    assert envelope(out)["error"]["retryable"] is False


def test_a_clone_url_without_a_scheme_is_a_usage_error(capsys, monkeypatch):
    def fail(*_args, **_kwargs):
        raise MissingSchema("Invalid URL 'dev.example.com'")

    monkeypatch.setattr(clone_cmd, "run_clone", fail)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")
    code, out, _ = run(
        capsys,
        "clone",
        "--source-url",
        "dev.example.com",
        "--source-org",
        "a",
        "--target-url",
        "https://prod.example.com",
        "--target-org",
        "b",
    )
    assert code == int(ExitCode.USAGE)
    assert envelope(out)["error"]["retryable"] is False


CLONE_ARGS = (
    "clone",
    "--source-url",
    "https://dev.example.com",
    "--source-org",
    "a",
    "--target-url",
    "https://prod.example.com",
    "--target-org",
    "b",
)


@pytest.fixture
def clone_raising(monkeypatch):
    """Run `clone` against an orchestrator that fails the way the test names."""

    def install(exc):
        def fail(*_args, **_kwargs):
            raise exc

        monkeypatch.setattr(clone_cmd, "run_clone", fail)
        monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
        monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    return install


def test_a_platform_api_status_decides_the_clone_exit_code(capsys, clone_raising):
    clone_raising(PlatformAPIError("forbidden", status_code=403, body="no access"))
    code, out, _ = run(capsys, *CLONE_ARGS)

    assert code == int(ExitCode.AUTH)
    assert "no access" in json.dumps(envelope(out)["error"]["details"])


def test_a_clone_failure_keeps_the_response_body_out_of_the_message(
    capsys, clone_raising
):
    """`error.message` is published as a one-line summary, and the exception
    appends the response body to its own; `details` carries it either way."""
    clone_raising(PlatformAPIError("forbidden", status_code=403, body="x" * 3000))
    code, out, _ = run(capsys, *CLONE_ARGS)
    error = envelope(out)["error"]

    assert code == int(ExitCode.AUTH)
    assert error["message"] == "forbidden"
    assert error["details"] == "x" * 3000


def test_a_platform_api_that_never_answered_is_retryable(capsys, clone_raising):
    """No status means no response, which is the case retrying can still fix."""
    clone_raising(PlatformAPIError("connection reset"))
    code, out, _ = run(capsys, *CLONE_ARGS)

    assert code == int(ExitCode.SERVER_ERROR)
    assert envelope(out)["error"]["retryable"] is True


def test_a_clone_that_could_not_start_is_a_usage_error(capsys, clone_raising):
    clone_raising(CloneError("source and target are the same organization"))
    code, out, _ = run(capsys, *CLONE_ARGS)

    assert code == int(ExitCode.USAGE)
    assert "same organization" in envelope(out)["error"]["message"]


def test_an_aborted_clone_reports_why_it_stopped(capsys, monkeypatch):
    def fake_clone(source, target, options):
        return CloneReport(
            source=Endpoint(source.base_url, source.organization_id),
            target=Endpoint(target.base_url, target.organization_id),
            phases=[PhaseResult(name="adapters", created=1)],
            aborted=True,
            abort_reason="a name already exists on the target",
        )

    monkeypatch.setattr(clone_cmd, "run_clone", fake_clone)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    code, out, _ = run(capsys, *CLONE_ARGS)

    assert code == int(ExitCode.GENERIC)
    assert "already exists on the target" in envelope(out)["error"]["message"]


def test_the_clone_size_grammar_matches_the_client_it_mirrors():
    """Both spellings of this command have to accept the same strings, and the
    table is copied rather than imported, so nothing else notices a drift."""
    from unstract.clone import cli as upstream

    assert clone_cmd._SIZE_UNITS == upstream._SIZE_UNITS
    assert clone_cmd._SIZE_RE.pattern == upstream._SIZE_RE.pattern


def test_setting_an_env_reference_in_a_discovered_file_says_it_is_ignored(
    capsys, tmp_path, monkeypatch
):
    """The refusal applies to every key, not only the withheld ones, so writing
    one without a word would report success for a setting that never resolves."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".unstract.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(work)
    _, out, _ = run(capsys, "config", "set", "docstudio", "org_id", "env:MY_ORG")
    assert "ignored when the config is loaded" in envelope(out)["data"]["warning"]


@pytest.mark.parametrize(
    ("argv", "setup"),
    [
        (("whisper", "usage"), "whisper"),
        (
            (
                "clone",
                "--source-url",
                "https://dev.example.com",
                "--source-org",
                "a",
                "--target-url",
                "https://prod.example.com",
                "--target-org",
                "b",
            ),
            "clone",
        ),
    ],
)
def test_a_header_that_will_not_build_does_not_quote_the_credential(
    capsys, monkeypatch, whisper_client, argv, setup
):
    """The only way to reach this is a credential carrying a control character,
    and the exception quotes it `repr`-escaped -- past what the scrub matches."""
    # The control character sits inside the key, not after it: `repr` then
    # splits the literal, which is precisely what the scrub cannot match.
    key = f"sk-live{chr(10)}0123456789"
    failure = InvalidHeader(
        f"Invalid return character or leading space in header: {key!r}"
    )
    escaped = repr(key)[1:-1]
    if setup == "whisper":
        whisper_client(get_usage_info=failure)
    else:

        def fail(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(clone_cmd, "run_clone", fail)
        monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", key)
        monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    code, out, err = run(capsys, *argv)
    assert code == int(ExitCode.USAGE)
    assert escaped not in out and escaped not in err


def test_a_finished_clone_still_reports_when_the_config_is_unreadable(
    capsys, monkeypatch, tmp_path
):
    """Clone takes both endpoints as flags, so an unreadable config file has no
    bearing on it. Scrubbing consults the config for keys to hide, and failing
    there would discard a report describing work already done."""
    broken = tmp_path / "broken.toml"
    broken.write_text("this is not = = toml", encoding="utf-8")
    monkeypatch.setenv("UNSTRACT_CONFIG", str(broken))

    def fake_clone(source, target, options):
        return CloneReport(
            source=Endpoint(source.base_url, source.organization_id),
            target=Endpoint(target.base_url, target.organization_id),
            phases=[PhaseResult(name="adapters", created=1)],
        )

    monkeypatch.setattr(clone_cmd, "run_clone", fake_clone)
    monkeypatch.setenv("UNSTRACT_SRC_PLATFORM_KEY", "src-key-0123456789")
    monkeypatch.setenv("UNSTRACT_TGT_PLATFORM_KEY", "tgt-key-0123456789")

    code, out, _ = run(
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
    )
    assert code == int(ExitCode.SUCCESS)
    assert envelope(out)["data"]["skipped"]["total"] == 0


def test_a_run_that_times_out_names_the_id_its_status_command_takes(
    capsys, deployment_client, tmp_path, monkeypatch
):
    """The poll handle is a status URL. Told to resume with that, a caller has
    nothing to pass to `deployment status`, which takes an execution id."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    deployment_client(
        structure_file=ACK,
        check_execution_status={
            "status_code": 200,
            "execution_status": "EXECUTING",
            "extraction_result": "",
        },
    )
    monkeypatch.setattr("unstract_cli.core.poll.time.sleep", lambda _seconds: None)

    code, out, _ = run(
        capsys,
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0.1",
        "--timeout",
        "0",
    )

    assert code == int(ExitCode.TIMEOUT)
    error = envelope(out)["error"]
    assert error["execution_id"] == "e-1"
    assert "deployment status my-api e-1" in error["hint"]


def test_a_timed_out_run_asked_to_save_says_to_save_on_resume(
    capsys, deployment_client, tmp_path, monkeypatch
):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    target = tmp_path / "out.json"
    deployment_client(
        structure_file=ACK,
        check_execution_status={
            "status_code": 200,
            "execution_status": "EXECUTING",
            "extraction_result": "",
        },
    )
    monkeypatch.setattr("unstract_cli.core.poll.time.sleep", lambda _seconds: None)

    code, out, _ = run(
        capsys,
        "docstudio",
        "deployment",
        "run",
        "my-api",
        str(doc),
        "--interval",
        "0.1",
        "--timeout",
        "0",
        "--save",
        str(target),
    )

    assert code == int(ExitCode.TIMEOUT)
    assert (
        f"deployment status my-api e-1 --save {target}`" in envelope(out)["error"]["hint"]
    )


def test_a_zero_poll_interval_is_refused(capsys, whisper_client, tmp_path):
    """Zero seconds between polls is a busy loop against a metered service."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0")
    assert code == int(ExitCode.USAGE)
    assert "interval" in envelope(out)["error"]["message"].lower()
