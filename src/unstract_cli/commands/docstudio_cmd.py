"""`unstract docstudio deployment ...` -- running a deployed API.

The deployment client reports failure by returning a status code rather than
raising, and it has no polling loop of its own, so both are handled here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import click
from unstract.api_deployments.client import APIDeploymentsClient

from unstract_cli.app import Context, deployment_group, pass_context
from unstract_cli.commands.common import finish, raw_field, wait_options
from unstract_cli.core.clients import (
    deployment,
    raise_for_result,
    translated,
    translating,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.params import requested, spec_options
from unstract_cli.core.poll import PollSpec, classify, preflight, wait_for_completion

PRODUCT = "docstudio"

#: The run POST and the status GET spell the state under different names, and
#: the API answers HTTP 422 while still executing -- only the body decides.
RUN_POLL = PollSpec(
    handle_field="status_check_api_endpoint",
    terminal_success=("COMPLETED", "SUCCESS"),
    terminal_failure=("ERROR", "ERROR_EXCEPTION", "FAILED", "STOPPED"),
    status_field=("execution_status", "status"),
)

#: `--output raw` prints one field rather than the whole payload.
RAW_FIELD = "extraction_result"

#: Parameters the run POST and the status GET share. What a caller asked to be
#: included in the result has to be asked for again when the result is read, or a
#: waited run returns less than the same flags returned without --wait.
_SHARED_WITH_STATUS = ("include_metadata", "include_metrics", "include_extracted_text")


@raw_field(RAW_FIELD)
@deployment_group.command("run")
@click.argument("target")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
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

    TARGET is a deployment alias or an API name. With --wait (the default) this
    polls until the execution finishes and returns its result.
    """
    client = deployment(ctx.config, target, ctx.transport_timeout)
    sent = requested(params)
    if save:
        preflight(save)
    with translated(endpoint=client.api_url):
        # Queued execution, so the request returns a handle instead of holding
        # the connection open for the length of the job.
        started = client.structure_file(list(files), timeout=0, **sent)
        raise_for_result(started, endpoint=client.api_url)

        if not wait:
            finish(ctx, started, raw_field=RAW_FIELD)
            return

        result = wait_for_completion(
            initial=started,
            spec=RUN_POLL,
            poll=_status_poller(
                client, {k: v for k, v in sent.items() if k in _SHARED_WITH_STATUS}
            ),
            save=save,
            interval=interval,
            timeout=wait_timeout,
            on_status=lambda status: (
                click.echo(f"status: {status}", err=True) if not ctx.quiet else None
            ),
        )
    # The waited result identifies the execution nowhere at the top level, so a
    # caller has nothing to correlate against the service. --no-wait returns the
    # handle as data; waiting returns it as meta.
    finish(ctx, result, raw_field=RAW_FIELD, meta=_handle_meta(started))


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


@raw_field(RAW_FIELD)
@deployment_group.command("status")
@click.argument("target")
@click.argument("execution_id")
@spec_options(
    PRODUCT,
    "status",
    client_method=APIDeploymentsClient.check_execution_status,
    exclude=("execution_id",),
)
@pass_context
def status(ctx: Context, target: str, execution_id: str, **params: Any) -> None:
    """Report the state of a running or finished execution."""
    client = deployment(ctx.config, target, ctx.transport_timeout)
    endpoint = f"{client.api_url}?execution_id={execution_id}"
    with translated(endpoint=client.api_url):
        result = client.check_execution_status(endpoint, **requested(params))
        if not result.get("pending"):
            raise_for_result(result, endpoint=client.api_url)
    # A finished-and-failed execution is reported inside an HTTP 200, so the
    # status code alone would call this a success.
    if classify(result, RUN_POLL) == "failure":
        raise CLIError(
            f"Execution {execution_id} finished with status "
            f"{result.get('execution_status')!r}.",
            ExitCode.VALIDATION,
            details=result,
            endpoint=client.api_url,
            hint="Inspect `details` for the per-file error, or check the execution logs.",
            extra={"execution_id": execution_id},
        )
    finish(ctx, result, raw_field=RAW_FIELD)


__all__ = ["run", "status"]
