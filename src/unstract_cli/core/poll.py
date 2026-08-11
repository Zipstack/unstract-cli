"""`--wait` state machine and one-shot result persistence.

Both products follow execute -> poll -> retrieve, and a caller should not have to
script that loop.

**The load-bearing rule:** terminal state is decided by the ``status`` field in
the *response body*, never by the HTTP status code. The deployment API returns
HTTP 422 for the in-progress states, so reading the body means this behaves
identically before and after that is fixed server-side.

The engine takes callables rather than owning any transport: the clients issue
every request, and the clock is injected so the whole thing tests offline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unstract_cli.core.errors import CLIError, ExitCode


@dataclass(frozen=True)
class PollSpec:
    """How to read progress out of one operation's responses."""

    #: Where the job handle lives in the initial response (whisper_hash,
    #: execution_id, ...). It is echoed back on timeout so a caller can resume
    #: rather than reprocess the document.
    handle_field: str
    terminal_success: tuple[str, ...]
    terminal_failure: tuple[str, ...]
    #: One name, or candidates tried in order: the run POST and the status GET
    #: spell the state differently.
    status_field: str | tuple[str, ...] = "status"


def _dig(payload: Any, field: str) -> Any:
    """Find a field, looking one level into the common envelopes."""
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
    """Read the status from a response body; first candidate that resolves wins."""
    fields = (field,) if isinstance(field, str) else field
    for candidate in fields:
        value = _dig(payload, candidate)
        if value is not None:
            return str(value)
    return None


def extract_handle(payload: Any, field: str) -> str | None:
    """Read the job handle out of a response body."""
    value = _dig(payload, field)
    return str(value) if value is not None else None


def persist(path: str | Path, payload: Any) -> Path:
    """Write a result to disk and return where it landed.

    Some results can be read exactly once. Callers must persist **before** the
    read is acknowledged to the user, so a crash between the two cannot destroy
    a result the server will not serve again.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )
    target.write_text(text, encoding="utf-8")
    return target


def wait_for_completion(
    *,
    initial: Any,
    spec: PollSpec,
    poll: Callable[[str], Any],
    retrieve: Callable[[str], Any] | None = None,
    save: str | Path | None = None,
    interval: float = 3.0,
    timeout: float = 300.0,
    on_status: Callable[[str | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    """Poll until terminal, then retrieve if the operation has a retrieve step.

    On timeout, raises with the job handle attached, so a caller can resume with
    a plain status/retrieve call rather than resubmitting the document.
    """
    handle = extract_handle(initial, spec.handle_field)
    if not handle:
        return initial

    success = {state.lower() for state in spec.terminal_success}
    failure = {state.lower() for state in spec.terminal_failure}
    deadline = now() + timeout
    last_status: str | None = None
    payload: Any = initial

    while True:
        payload = poll(handle)
        status = extract_status(payload, spec.status_field)

        if status != last_status:
            if on_status is not None:
                on_status(status)
            last_status = status

        normalised = (status or "").lower()
        if normalised in failure:
            raise CLIError(
                f"Operation finished with status {status!r}.",
                ExitCode.VALIDATION,
                details=payload,
                hint="Inspect `details` for the per-file error, or check the execution logs.",
                extra={spec.handle_field: handle},
            )
        if normalised in success:
            break

        remaining = deadline - now()
        if remaining <= 0:
            raise CLIError(
                f"Timed out after {timeout:g}s waiting for completion "
                f"(last status: {status!r}).",
                ExitCode.TIMEOUT,
                hint=(
                    f"The job is still running. Resume with the {spec.handle_field} "
                    f"below rather than resubmitting the document."
                ),
                extra={spec.handle_field: handle, "last_status": status},
            )

        # Never sleep past the deadline: --wait 30 that returns at 35s has lied,
        # and the last poll should land on the deadline, not after it.
        sleep(min(interval, remaining))

    if retrieve is not None:
        payload = retrieve(handle)
    if save is not None:
        persist(save, payload)
    return payload


__all__ = [
    "PollSpec",
    "extract_handle",
    "extract_status",
    "persist",
    "wait_for_completion",
]
