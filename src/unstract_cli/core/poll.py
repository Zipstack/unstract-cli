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
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unstract_cli.core.errors import CLIError, ExitCode


@dataclass(frozen=True)
class PollSpec:
    """How to read progress out of one operation's responses."""

    #: Where the job handle lives in the initial response. Echoed back on
    #: timeout so a caller can resume rather than reprocess the document.
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


def preflight(path: str | Path) -> Path:
    """Prove the save target is writable, before anything destructive runs.

    `--save` exists to protect a read the server serves exactly once, so
    discovering an unwritable path *after* that read is the one failure the
    flag must not have.
    """
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        with target.open("a", encoding="utf-8"):
            pass
        if not existed:
            target.unlink()
    except OSError as exc:
        raise CLIError(
            f"Cannot write to --save target {path!r}: {exc}.",
            ExitCode.USAGE,
            hint="Pick a writable path; nothing has been read yet, so nothing is lost.",
        ) from exc
    return target


def persist(path: str | Path, payload: Any) -> Path:
    """Write a result to disk and return where it landed.

    Some results can be read exactly once. Callers must persist **before** the
    read is acknowledged to the user, so a crash between the two cannot destroy
    a result the server will not serve again.

    Written through a temporary file so a full disk leaves the previous copy
    intact rather than a truncated one. A failure here raises with the payload
    attached: by this point the only surviving copy is in memory, and it has to
    reach stdout somehow.
    """
    target = Path(path).expanduser()
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )
    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A predictable sibling in a directory someone else can write is a
        # symlink waiting to be planted, and the write would follow it. `mkstemp`
        # names it unpredictably and creates it exclusively; the 0600 it opens
        # with is what `os.replace` then gives the result.
        handle_fd, name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        tmp = Path(name)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        if tmp is not None:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise CLIError(
            f"The result could not be written to {path!r}: {exc}.",
            ExitCode.SAVE_FAILED,
            details=payload,
            hint=(
                "`details` carries the result. It has already been read from the "
                "service, which will not serve it again -- save it from here."
            ),
        ) from exc
    return target


def classify(payload: Any, spec: PollSpec) -> str:
    """`success`, `failure`, `pending` or `unknown` for one poll response.

    Shared with the standalone status commands: a finished-and-failed execution
    is reported inside an HTTP 200, so a command that only checks the status
    code calls it a success.
    """
    status = (extract_status(payload, spec.status_field) or "").lower()
    if status in {state.lower() for state in spec.terminal_failure}:
        return "failure"
    if status in {state.lower() for state in spec.terminal_success}:
        return "success"
    if not status or _dig(payload, "error"):
        # Not progress: polling on regardless reports a server fault as "still
        # running" until the deadline.
        return "unknown"
    return "pending"


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
    #: Called with the path once a result is on disk, before the caller sees
    #: anything. The ordering it observes is the whole point of --save.
    on_saved: Callable[[Path], None] | None = None,
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

    deadline = now() + timeout
    last_status: str | None = None
    payload: Any = initial

    def naming_the_job(call: Callable[[str], Any]) -> Any:
        """Run one step of the loop, ensuring any failure names the job.

        The handle is the difference between resuming and paying to process the
        document a second time, so it is attached here rather than left to
        whatever the caller wrapped the loop in.
        """
        try:
            return call(handle)
        except CLIError as exc:
            exc.extra.setdefault(spec.handle_field, handle)
            raise
        except Exception as exc:
            raise CLIError(
                str(exc) or type(exc).__name__,
                ExitCode.SERVER_ERROR,
                retryable=True,
                extra={spec.handle_field: handle},
            ) from exc

    while True:
        payload = naming_the_job(poll)
        status = extract_status(payload, spec.status_field)

        if status != last_status:
            if on_status is not None:
                on_status(status)
            last_status = status

        state = classify(payload, spec)
        if state == "failure":
            raise CLIError(
                f"Operation finished with status {status!r}.",
                ExitCode.VALIDATION,
                details=payload,
                hint="Inspect `details` for the per-file error, or check the execution logs.",
                extra={spec.handle_field: handle},
            )
        if state == "unknown":
            raise CLIError(
                "The service answered with neither a status nor progress.",
                ExitCode.SERVER_ERROR,
                details=payload,
                retryable=True,
                hint=(
                    "The response carries no usable state, so polling on would "
                    "only repeat it. Retry with the handle below."
                ),
                extra={spec.handle_field: handle},
            )
        if state == "success":
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
        payload = naming_the_job(retrieve)
    if save is not None:
        written = persist(save, payload)
        if on_saved is not None:
            on_saved(written)
    return payload


__all__ = [
    "PollSpec",
    "classify",
    "extract_handle",
    "extract_status",
    "persist",
    "preflight",
    "wait_for_completion",
]
