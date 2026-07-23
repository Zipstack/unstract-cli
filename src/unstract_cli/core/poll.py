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


def extract_handle(payload: Any, field: str) -> str | None:
    """Read the job handle (whisper_hash / execution_id / file_hash)."""
    value = _dig(payload, field)
    return str(value) if value is not None else None


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

    handle = extract_handle(initial, spec.handle_field)
    if not handle:
        diagnostic(
            f"--wait: no {spec.handle_field} in response; returning immediately.",
            quiet=quiet,
            verbosity=verbosity,
        )
        return initial

    status_endpoint = get_endpoint(spec.status_endpoint)
    deadline = now() + timeout
    last_status: str | None = None

    # Path parameters from the original invocation must survive into the status
    # and retrieve calls: `deployment status` needs the same --api-name and
    # --org-id, and the handle alone cannot supply them.
    carried = _carry_path_values(values or {}, status_endpoint)

    while True:
        poll_values = {**carried, spec.handle_param: handle}
        plan = http.build_request(status_endpoint, config, poll_values)
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
                extra={spec.handle_field: handle, "last_status": status},
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
