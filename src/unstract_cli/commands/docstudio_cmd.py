"""`unstract docstudio deployment ...` -- running a deployed API.

The deployment client reports failure by returning a status code rather than
raising, and it has no polling loop of its own, so both are handled here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import click
from unstract.api_deployments.client import APIDeploymentsClient

from unstract_cli.app import Context, deployment_group, pass_context
from unstract_cli.commands.common import finish, raw_fields, wait_options
from unstract_cli.core.clients import (
    deployment,
    deployment_errors,
    raise_for_result,
    translated,
    translating,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import diagnostic
from unstract_cli.core.params import requested, spec_options
from unstract_cli.core.poll import (
    PollSpec,
    PollState,
    classify,
    persist,
    preflight,
    wait_for_completion,
)

PRODUCT = "docstudio"

#: The run POST and the status GET spell the state under different names, and
#: the API answers HTTP 422 while still executing -- only the body decides.
RUN_POLL = PollSpec(
    handle_field="status_check_api_endpoint",
    terminal_success=("COMPLETED", "SUCCESS"),
    terminal_failure=("ERROR", "ERROR_EXCEPTION", "FAILED", "STOPPED"),
    status_field=("execution_status", "status"),
)

#: What `--output raw` prints, best answer first. A queued run answers with a
#: handle and no result, and a status read answers with a state until there is
#: one, so a single field would be wrong for two of the three shapes.
RUN_RAW = ("extraction_result", "execution_id")
STATUS_RAW = ("extraction_result", "execution_status")

#: Parameters the run POST and the status GET share: what was asked for in the
#: run has to be asked for again when the result is read.
_SHARED_WITH_STATUS = ("include_metadata", "include_metrics", "include_extracted_text")


@raw_fields(*RUN_RAW)
@deployment_group.command("run")
@click.argument("target")
# Optional because a run can name its documents as `--presigned-urls`
# instead; the two are checked together below, since neither alone is
# required and a run naming no documents at all is the real error.
@click.argument("files", nargs=-1, type=click.Path(exists=True))
@wait_options()
@spec_options(
    PRODUCT,
    "execute",
    client_method=APIDeploymentsClient.structure_file,
    # `files` is the FILES argument; `timeout` selects the server's own
    # execution mode and would fight the CLI's polling for the same job.
    exclude=("files", "timeout"),
)
@pass_context
def run(
    ctx: Context,
    target: str,
    files: tuple[str, ...],
    wait: bool,
    interval: float,
    wait_timeout: float,
    save: str | None,
    **params: Any,
) -> None:
    """Run a deployment against one or more documents.

    TARGET is the deployment's API name, as `deployment ls` prints it. Name the
    documents as local FILES, as --presigned-urls, or both. With --wait (the
    default) this polls until the execution finishes and returns its result.
    """
    sent = requested(params)
    # Before the client is built: what the caller typed is wrong whatever the
    # config resolves to, and a credential error here would name the wrong fault.
    if not files and not sent.get("presigned_urls"):
        raise CLIError(
            "A run needs at least one document.",
            ExitCode.USAGE,
            hint=(
                "Name local files as arguments, or pass --presigned-urls with "
                "one or more HTTPS URLs."
            ),
        )
    client = deployment(ctx.config, target, ctx.transport_timeout)
    if save and not wait:
        raise CLIError(
            "--save has nothing to write with --no-wait.",
            ExitCode.USAGE,
            hint=(
                "Drop --no-wait, or start now and save later with "
                "`deployment status --save`."
            ),
        )
    if save:
        preflight(save)
    with deployment_errors(target), translated(endpoint=client.api_url):
        # Queued execution, so the request returns a handle instead of holding
        # the connection open for the length of the job.
        started = client.structure_file(list(files), timeout=0, **sent)
        raise_for_result(started, endpoint=client.api_url)

        if not wait:
            # The ack names no execution of its own: the handle has to be read
            # back out of the endpoint it hands you, and `meta` is where the
            # CLI puts what it had to derive.
            finish(ctx, started, raw_fields=RUN_RAW, meta=_handle_meta(started))
            return

        try:
            result = wait_for_completion(
                initial=started,
                spec=RUN_POLL,
                poll=_status_poller(
                    client, {k: v for k, v in sent.items() if k in _SHARED_WITH_STATUS}
                ),
                save=save,
                interval=interval,
                timeout=wait_timeout,
                on_status=lambda status: diagnostic(
                    f"status: {status}", quiet=ctx.quiet, verbosity=ctx.verbosity
                ),
                on_retry=lambda exc: diagnostic(
                    f"retrying: {exc.message}", quiet=ctx.quiet, verbosity=ctx.verbosity
                ),
                on_saved=lambda path: diagnostic(
                    f"saved: {path}", quiet=ctx.quiet, verbosity=ctx.verbosity
                ),
            )
        except CLIError as exc:
            # This job polls on a status URL, which is not what the status
            # command takes; without the id the caller is told to resume with
            # something they cannot pass to it.
            handle = _handle_meta(started)
            exc.extra = {**exc.extra, **handle}
            if exc.exit_code is ExitCode.TIMEOUT and (
                found := handle.get("execution_id")
            ):
                exc.hint = (
                    f"Resume with `unstract docstudio deployment status {target} "
                    f"{found}` rather than resubmitting the document."
                )
            raise
    handle = _handle_meta(started)
    _raise_for_failed_files(result, endpoint=client.api_url, extra=handle)
    # A waited result names no execution, so the handle is returned as meta for
    # correlation.
    finish(ctx, result, raw_fields=RUN_RAW, meta=handle)


def _failed_files(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-file entries of a completed execution that produced no result."""
    entries = result.get("extraction_result")
    if not isinstance(entries, list):
        return []
    failed = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status is None:
            if entry.get("error"):
                failed.append(entry)
        elif str(status).casefold() != "success":
            failed.append(entry)
    return failed


def _raise_for_failed_files(
    result: dict[str, Any], *, endpoint: str, extra: dict[str, Any]
) -> None:
    # A completed execution says the batch ran, not that every document in it
    # came out: a failed file is reported inside the success shape.
    failed = _failed_files(result)
    if not failed:
        return
    named = "; ".join(
        f"{entry.get('file') or '?'}: {entry.get('error') or entry.get('status')}"
        for entry in failed
    )
    raise CLIError(
        f"{len(failed)} of {len(result['extraction_result'])} documents failed: {named}",
        ExitCode.VALIDATION,
        details=result,
        endpoint=endpoint,
        # The status read is one-shot, so the successful documents in this
        # payload survive nowhere else.
        verbatim_details=True,
        hint=(
            "`error.details` carries the full result, successful documents "
            "included. Resubmit only the files named in `failed_files`."
        ),
        extra={**extra, "failed_files": [entry.get("file") for entry in failed]},
    )


def _handle_meta(started: dict[str, Any]) -> dict[str, Any]:
    """The execution's identity, from wherever the run response carries it."""
    if execution_id := started.get("execution_id"):
        return {"execution_id": execution_id}
    endpoint = str(started.get("status_check_api_endpoint") or "")
    found = parse_qs(urlparse(endpoint).query).get("execution_id")
    return {"execution_id": found[0]} if found else {}


def _status_poller(
    client: APIDeploymentsClient, params: dict[str, Any]
) -> Callable[[str], dict[str, Any]]:
    """Poll one execution, failing on a status code the poll loop cannot use."""

    def poll(endpoint: str) -> dict[str, Any]:
        result = client.check_execution_status(endpoint, **params)
        # A retryable status is left to the client's own retry policy, which has
        # already run; the client reports those as still pending.
        if not result.get("pending"):
            raise_for_result(result, endpoint=client.api_url)
        return result

    return translating(poll, client.api_url)


@raw_fields(*STATUS_RAW)
@deployment_group.command("status")
@click.argument("target")
@click.argument("execution_id")
@spec_options(
    PRODUCT,
    "status",
    client_method=APIDeploymentsClient.check_execution_status,
    exclude=("execution_id",),
)
@click.option(
    "--save",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the result here before printing it.",
)
@pass_context
def status(
    ctx: Context, target: str, execution_id: str, save: str | None, **params: Any
) -> None:
    """Report the state of a running or finished execution."""
    client = deployment(ctx.config, target, ctx.transport_timeout)
    if save:
        preflight(save)
    # Quoted rather than trusted: the id comes from the caller and would
    # otherwise be able to carry query syntax of its own.
    endpoint = f"{client.api_url}?execution_id={quote(execution_id, safe='')}"
    with deployment_errors(target), translated(endpoint=client.api_url):
        result = client.check_execution_status(endpoint, **requested(params))
        if not result.get("pending"):
            raise_for_result(result, endpoint=client.api_url)
    # A finished-and-failed execution is reported inside an HTTP 200, so the
    # status code alone would call this a success.
    if classify(result, RUN_POLL) is PollState.FAILURE:
        raise CLIError(
            f"Execution {execution_id} finished with status "
            f"{result.get('execution_status')!r}.",
            ExitCode.VALIDATION,
            details=result,
            endpoint=client.api_url,
            hint="Inspect `details` for the per-file error, or check the execution logs.",
            extra={"execution_id": execution_id},
        )
    if save:
        written = persist(save, result)
        diagnostic(f"saved: {written}", quiet=ctx.quiet, verbosity=ctx.verbosity)
    _raise_for_failed_files(
        result, endpoint=client.api_url, extra={"execution_id": execution_id}
    )
    finish(ctx, result, raw_fields=STATUS_RAW)


__all__ = ["run", "status"]
