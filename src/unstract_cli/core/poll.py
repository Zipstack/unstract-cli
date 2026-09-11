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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from unstract_cli.core.errors import CLIError, ExitCode

#: Consecutive transient poll failures tolerated before the wait gives up.
#: Retrying stops at whichever comes first, this count or the deadline -- at the
#: default interval the backoff reaches this count well inside the timeout.
MAX_TRANSIENT_POLLS = 5


class PollState(StrEnum):
    """What one poll response says about the job."""

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    UNKNOWN = "unknown"


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

    #: The terminal states case-folded, since every comparison against them is
    #: case-insensitive. Derived, not declared -- see `__post_init__`.
    failed: frozenset[str] = field(init=False, repr=False, compare=False)
    succeeded: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        succeeded = frozenset(s.lower() for s in self.terminal_success)
        failed = frozenset(s.lower() for s in self.terminal_failure)
        object.__setattr__(self, "succeeded", succeeded)
        object.__setattr__(self, "failed", failed)
        both = succeeded & failed
        if both:
            # A CLIError rather than a ValueError: specs are module-level, so
            # this fires during import, and only a CLIError renders an envelope
            # on the stream contracted to always carry one.
            raise CLIError(
                f"{sorted(both)} is named as both success and failure; "
                "classify tests failure first, so a success would be reported "
                "as an error.",
                ExitCode.GENERIC,
                hint="The poll spec is inconsistent; this needs a code change.",
            )


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
    # Saving here would replace the link itself, so it stops being a link and
    # whatever it stands for stops being updated.
    if target.is_symlink():
        raise CLIError(
            f"--save target {path!r} is a symlink to {os.readlink(target)}: the "
            "result would replace the link rather than update what it points at.",
            ExitCode.USAGE,
            hint=(
                "Pass the path of the real file; nothing has been read yet, so "
                "nothing is lost."
            ),
        )
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
    if target.is_symlink():
        raise CLIError(
            f"--save target {path!r} is a symlink to {os.readlink(target)}: writing "
            "here would replace the link rather than update what it points at.",
            ExitCode.SAVE_FAILED,
            details=payload,
            verbatim_details=True,
            hint=(
                "`details` carries the result. Pass the path of the real file and "
                "save it from there."
            ),
        )
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
            verbatim_details=True,
            hint=(
                "`details` carries the result. It has already been read from the "
                "service, which will not serve it again -- save it from here."
            ),
        ) from exc
    return target


def classify(payload: Any, spec: PollSpec) -> PollState:
    """What one poll response says about the job.

    Shared with the standalone status commands: a finished-and-failed execution
    is reported inside an HTTP 200, so a command that only checks the status
    code calls it a success.
    """
    status = (extract_status(payload, spec.status_field) or "").lower()
    if status in spec.failed:
        return PollState.FAILURE
    if status in spec.succeeded:
        return PollState.SUCCESS
    if not status or _dig(payload, "error"):
        # Not progress: polling on regardless reports a server fault as "still
        # running" until the deadline.
        return PollState.UNKNOWN
    return PollState.PENDING


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
    #: Called with the failure being retried. Separate from `on_status` so a
    #: server-authored error is never rendered as a job status, and so the
    #: caller can scrub it the way it scrubs any other untrusted text.
    on_retry: Callable[[CLIError], None] | None = None,
    #: Called with the path once a result is on disk, before the caller sees
    #: anything. The ordering it observes is the whole point of --save.
    on_saved: Callable[[Path], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    """Poll until terminal, then retrieve if the operation has a retrieve step.

    On timeout, raises with the job handle attached, so a caller can resume with
    a plain status/retrieve call rather than resubmitting the document. A
    response carrying no handle is judged on the spot, since there is nothing to
    poll: a terminal success is the whole answer and is delivered, anything else
    raises.
    """

    def deliver(payload: Any) -> Any:
        """Save the result before the caller is told it exists."""
        if save is not None:
            written = persist(save, payload)
            if on_saved is not None:
                on_saved(written)
        return payload

    handle = extract_handle(initial, spec.handle_field)
    if not handle:
        # No handle means nothing can be polled, so this response is the whole
        # answer -- it still has to be judged, and saved if it is a result.
        state = classify(initial, spec)
        if state is PollState.SUCCESS:
            return deliver(initial)
        if state is PollState.FAILURE:
            raise CLIError(
                f"Operation finished with status "
                f"{extract_status(initial, spec.status_field)!r}.",
                ExitCode.VALIDATION,
                details=initial,
                hint="Inspect `details` for the per-file error, or check the execution logs.",
            )
        raise CLIError(
            f"The service accepted the request without a {spec.handle_field}, so "
            "there is nothing to poll and no result to return.",
            ExitCode.SERVER_ERROR,
            details=initial,
            retryable=True,
            hint=(
                "`details` carries the response. Resubmitting is the only way "
                f"forward, since no {spec.handle_field} was issued."
            ),
        )

    deadline = now() + timeout
    last_status: str | None = None
    payload: Any = initial

    def naming_the_job(call: Callable[[str], Any], *, retryable: bool) -> Any:
        """Run one step of the loop, ensuring any failure names the job.

        The handle is the difference between resuming and paying to process the
        document a second time, so it is attached here rather than left to
        whatever the caller wrapped the loop in.
        """
        try:
            return call(handle)
        except CLIError as exc:
            exc.extra.setdefault(spec.handle_field, handle)
            if not retryable and exc.http_status not in (408, 429):
                # A step that must not be repeated has to be un-marked here or
                # the envelope invites the retry. Refusals are the exception:
                # they mean the request was never served, so the one-shot read
                # is still there to collect.
                exc.retryable = False
            raise
        except Exception as exc:
            raise CLIError(
                str(exc) or type(exc).__name__,
                ExitCode.SERVER_ERROR,
                retryable=retryable,
                extra={spec.handle_field: handle},
            ) from exc

    transient = 0
    while True:
        try:
            payload = naming_the_job(poll, retryable=True)
        except CLIError as exc:
            remaining = deadline - now()
            if not exc.retryable or remaining <= 0 or transient >= MAX_TRANSIENT_POLLS:
                raise
            transient += 1
            if on_retry is not None:
                on_retry(exc)
            # Back off so a rate limit is not answered at the same rate that
            # earned it, but never past the deadline the caller set.
            sleep(min(interval * 2**transient, remaining))
            continue
        transient = 0
        status = extract_status(payload, spec.status_field)

        if status != last_status:
            if on_status is not None:
                on_status(status)
            last_status = status

        state = classify(payload, spec)
        if state is PollState.FAILURE:
            raise CLIError(
                f"Operation finished with status {status!r}.",
                ExitCode.VALIDATION,
                details=payload,
                hint="Inspect `details` for the per-file error, or check the execution logs.",
                extra={spec.handle_field: handle},
            )
        if state is PollState.UNKNOWN:
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
        if state is PollState.SUCCESS:
            break

        remaining = deadline - now()
        if remaining <= 0:
            raise CLIError(
                f"Timed out after {timeout:g}s waiting for completion "
                f"(last status: {status!r}).",
                ExitCode.TIMEOUT,
                retryable=True,
                hint=(
                    f"Resume with the {spec.handle_field} below rather than "
                    "resubmitting the document."
                ),
                extra={spec.handle_field: handle, "last_status": status},
            )

        # Never sleep past the deadline: --wait 30 that returns at 35s has lied,
        # and the last poll should land on the deadline, not after it.
        sleep(min(interval, remaining))

    if retrieve is not None:
        payload = naming_the_job(retrieve, retryable=False)
    return deliver(payload)


__all__ = [
    "MAX_TRANSIENT_POLLS",
    "PollSpec",
    "PollState",
    "classify",
    "extract_handle",
    "extract_status",
    "persist",
    "preflight",
    "wait_for_completion",
]
