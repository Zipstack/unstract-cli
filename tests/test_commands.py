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
from unstract.clone.exceptions import CloneError, PlatformAPIError
from unstract.clone.report import CloneReport, Endpoint, PhaseResult
from unstract.llmwhisperer import client_v2
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)

from unstract_cli.__main__ import main
from unstract_cli.app import command_tree
from unstract_cli.commands import clone_cmd, docstudio_cmd, whisper_cmd
from unstract_cli.config import LLMWHISPERER
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


def test_a_target_that_is_not_a_configured_alias_names_the_ones_that_are(
    capsys, deployment_client, write_config
):
    """A misspelt alias is sent as an API name and comes back not-found, which
    says nothing about the aliases sitting in the profile."""
    write_config(
        'default_profile = "p"\n'
        "[profiles.p.docstudio]\n"
        'org_id = "org"\n'
        'api_key = "k"\n'
        "[profiles.p.deployments.invoices]\n"
        'api_name = "invoice-parser"\n'
    )
    deployment_client(check_execution_status={"status_code": 404, "error": "not found"})
    code, out, _ = run(capsys, "docstudio", "deployment", "status", "invoces", "e-1")
    assert code == int(ExitCode.NOT_FOUND)
    hint = envelope(out)["error"]["hint"]
    assert "invoces" in hint and "invoices" in hint


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


def test_a_zero_poll_interval_is_refused(capsys, whisper_client, tmp_path):
    """Zero seconds between polls is a busy loop against a metered service."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-")
    code, out, _ = run(capsys, "whisper", "extract", str(doc), "--interval", "0")
    assert code == int(ExitCode.USAGE)
    assert "interval" in envelope(out)["error"]["message"].lower()
