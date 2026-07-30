"""`--wait` state machines (SPEC.md §3.1, §6.2).

Both LLMWhisperer and Unstract follow execute -> poll -> retrieve. Agents should
not have to script that loop, so `--wait` drives it to a terminal state.

**The load-bearing rule:** terminal state is decided by the ``status`` field in
the *response body*, never by the HTTP status code. The Unstract deployment API
currently returns HTTP 422 for the in-progress states ``PENDING`` and
``EXECUTING`` -- a documented server-side defect that is scheduled to be fixed.
Reading the body means the CLI behaves identically before and after that fix;
reading the status code would break in one direction or the other.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from unstract_cli.config.loader import ResolvedConfig
from unstract_cli.core import http
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.model import Endpoint
from unstract_cli.core.output import diagnostic


def _dig(payload: Any, field: str) -> Any:
    """Find a field, looking one level into common envelopes.

    Responses variously arrive bare, under ``message``, or under ``data``, and an
    agent shouldn't care which.
    """
    if not isinstance(payload, dict):
        return None
    if field in payload:
        return payload[field]
    for envelope in ("message", "data", "result"):
        inner = payload.get(envelope)
        if isinstance(inner, dict) and field in inner:
            return inner[field]
    return None


def extract_status(payload: Any, field: str | tuple[str, ...] = "status") -> str | None:
    """Read the status from a response body.

    ``field`` may be a single name or a tuple of candidate names tried in order,
    because the run POST and the status GET spell the state differently (the run
    nests ``execution_status`` under ``message``; the status GET returns a
    top-level ``status``). The first candidate that resolves wins.
    """
    fields = (field,) if isinstance(field, str) else field
    for candidate in fields:
        value = _dig(payload, candidate)
        if value is not None:
            return str(value)
    return None


def extract_handle(
    payload: Any, field: str, from_query: tuple[str, str] | None = None
) -> str | None:
    """Read the job handle (whisper_hash / execution_id / file_hash).

    ``from_query`` is a ``(field, param)`` fallback for responses that carry the
    handle only inside a URL. The deployment run POST is the live case: its body
    is ``{"execution_status", "status_api", "error", "result"}`` with no
    ``execution_id`` anywhere -- the id exists solely in the ``status_api`` query
    string. Without this, `--wait` silently returned the PENDING stub as if it
    were the finished result.
    """
    value = _dig(payload, field)
    if value is not None:
        return str(value)

    if from_query is not None:
        source_field, query_param = from_query
        url = _dig(payload, source_field)
        if isinstance(url, str) and url:
            found = parse_qs(urlparse(url).query).get(query_param)
            if found and found[0]:
                return str(found[0])
    return None


def _carry_path_values(values: dict[str, Any], target: Endpoint) -> dict[str, Any]:
    """Forward path parameters from the original call into a follow-up call."""
    return {
        param.py_name: values[param.py_name]
        for param in target.path_params()
        if values.get(param.py_name) is not None
    }


def wait_for_completion(
    *,
    endpoint: Endpoint,
    initial: Any,
    config: ResolvedConfig,
    values: dict[str, Any] | None = None,
    poll_interval: float = 3.0,
    timeout: float = 300.0,
    max_retries: int = 3,
    request_timeout: float = 60.0,
    quiet: bool = False,
    verbosity: int = 0,
    sleep=time.sleep,
    now=time.monotonic,
) -> Any:
    """Poll until terminal, then retrieve if the endpoint defines a retrieve step.

    On timeout, exits 7 *with the job handle*, so an agent can resume with a
    plain `status`/`retrieve` call rather than reprocessing the document.
    """
    spec = endpoint.poll
    if spec is None:  # pragma: no cover - guarded by the caller
        return initial

    from unstract_cli.endpoints import get_endpoint

    handle = extract_handle(initial, spec.handle_field, spec.handle_from_query)
    if not handle:
        # Returning `initial` here would be a silent success: exit 0 with a
        # PENDING stub that the caller records as the finished result. The user
        # asked to wait, and the CLI cannot -- say so and let them recover from
        # the payload, which is attached.
        raise CLIError(
            f"--wait: no {spec.handle_field} in the response; cannot poll.",
            ExitCode.SERVER_ERROR,
            details=initial if isinstance(initial, dict) else None,
            endpoint=f"{endpoint.method} {endpoint.path}",
            hint=(
                "The operation may still be running. The initial response is "
                "attached; re-run without --wait and poll with the status command."
            ),
            retryable=False,
        )

    status_endpoint = get_endpoint(spec.status_endpoint)
    deadline = now() + timeout
    last_status: str | None = None
    last_payload: Any = initial

    # Path parameters from the original invocation must survive into the status
    # and retrieve calls: `deployment status` needs the same --api-name and
    # --org-id, and the handle alone cannot supply them.
    carried = _carry_path_values(values or {}, status_endpoint)
    # Non-path values the user explicitly set that the status endpoint would
    # otherwise default away (notably --include-metadata, whose default of False
    # makes the server strip metadata and drop it from a one-shot store).
    carried.update(
        {
            name: (values or {})[name]
            for name in spec.poll_carry
            if (values or {}).get(name) is not None
        }
    )

    while True:
        poll_values = {**carried, spec.handle_param: handle}
        plan = http.build_request(status_endpoint, config, poll_values)
        try:
            response = http.execute(
                plan,
                endpoint=status_endpoint,
                timeout=request_timeout,
                max_retries=max_retries,
            )

            status = extract_status(response.payload, spec.status_field)

            # Only now consider the HTTP status: if the body carried no recognisable
            # state, a 4xx/5xx is a real failure rather than the 422 quirk.
            if status is None:
                http.raise_for_status(response, status_endpoint)
                status = extract_status(response.payload, spec.status_field)
        except CLIError as exc:
            # Every exit from the wait loop carries the resume handle, matching
            # what the timeout path does. Without this an agent gets exit 8 with
            # no handle anywhere -- and on a one-shot, already-billed execution
            # the only recovery would be to run the whole thing again.
            exc.extra.setdefault(spec.handle_field, handle)
            if last_status is not None:
                exc.extra.setdefault("last_status", last_status)
            raise
        last_payload = response.payload

        if status != last_status:
            diagnostic(f"--wait: status={status}", quiet=quiet, verbosity=verbosity, level=1)
            last_status = status

        normalised = (status or "").lower()
        if normalised in {s.lower() for s in spec.terminal_failure}:
            raise CLIError(
                f"Operation finished with status {status!r}.",
                ExitCode.VALIDATION,
                endpoint=f"{endpoint.method} {endpoint.path}",
                details=response.payload,
                hint="Inspect `details` for the per-file error, or check execution logs.",
                extra={spec.handle_field: handle},
            )

        if normalised in {s.lower() for s in spec.terminal_success}:
            break

        # A status we do not recognise is not "still running". Treating it as
        # in-progress polls until timeout, and on a one-shot store the first poll
        # already consumed the result -- so every later poll 406s and the data is
        # gone. Fail now, with the payload, while it is still in hand.
        #
        # Only enforced where `in_progress` is declared: several of these APIs
        # emit intermediate states that are not exhaustively documented (API Hub
        # alone has QUEUED_FOR_WHISPER, WHISPERING, ...), and guessing at that
        # list would turn a working poll into a hard failure.
        if (
            spec.in_progress
            and normalised
            and normalised
            not in {
                s.lower()
                for s in (*spec.terminal_success, *spec.terminal_failure, *spec.in_progress)
            }
        ):
            raise CLIError(
                f"Unrecognised status {status!r} from the status endpoint.",
                ExitCode.SERVER_ERROR,
                endpoint=f"{endpoint.method} {endpoint.path}",
                details=response.payload if isinstance(response.payload, dict) else None,
                hint=(
                    "The server reported a state this CLI does not model. The "
                    "response is attached; the result may already be consumed."
                ),
                extra={spec.handle_field: handle},
                retryable=False,
            )

        if now() >= deadline:
            raise CLIError(
                f"Timed out after {timeout:g}s waiting for completion "
                f"(last status: {status!r}).",
                ExitCode.TIMEOUT,
                endpoint=f"{endpoint.method} {endpoint.path}",
                hint=(
                    f"The job is still running. Resume with the {spec.handle_field} "
                    f"below rather than resubmitting the document."
                ),
                extra={
                    spec.handle_field: handle,
                    "last_status": status,
                    "last_payload": last_payload,
                },
            )

        sleep(poll_interval)

    if not spec.retrieve_endpoint:
        return response.payload

    retrieve_endpoint = get_endpoint(spec.retrieve_endpoint)
    retrieve_values: dict[str, Any] = _carry_path_values(values or {}, retrieve_endpoint)
    # Some result stores are keyed by an identifier from the *original* request,
    # not by the poll handle (prompt-studio's Output Manager is read by tool_id).
    for entry in spec.retrieve_carry:
        source, dest = entry if isinstance(entry, tuple) else (entry, entry)
        if (carried_value := (values or {}).get(source)) is not None:
            retrieve_values[dest] = carried_value
    for name, constant in spec.retrieve_extra:
        retrieve_values[name] = constant
    if not spec.retrieve_omits_handle:
        retrieve_values[spec.handle_param] = handle
    plan = http.build_request(retrieve_endpoint, config, retrieve_values)
    result = http.execute(
        plan, endpoint=retrieve_endpoint, timeout=request_timeout, max_retries=max_retries
    )
    http.raise_for_status(result, retrieve_endpoint)
    return result.payload


__all__ = ["extract_handle", "extract_status", "wait_for_completion"]
