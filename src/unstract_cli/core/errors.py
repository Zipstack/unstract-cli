"""Exit codes, structured errors, and secret redaction.

Exit codes are a stable API: a caller branches on them without parsing prose.
Every failure carries `retryable`, and a `hint` wherever one can be given, so
the caller can self-correct rather than retry blindly.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERIC = 1
    USAGE = 2
    AUTH = 3
    NOT_FOUND = 4
    VALIDATION = 5
    RATE_LIMITED = 6
    TIMEOUT = 7
    SERVER_ERROR = 8
    ALREADY_CONSUMED = 9
    SAVE_FAILED = 10
    #: 128 + SIGINT, the value a shell and every job runner already read as
    #: "the user stopped it" rather than as a failure of the command.
    INTERRUPTED = 130


#: HTTP status -> exit code. An in-progress 422 never reaches here: the poll
#: engine branches on the response body first.
_STATUS_MAP: dict[int, ExitCode] = {
    400: ExitCode.VALIDATION,
    401: ExitCode.AUTH,
    403: ExitCode.AUTH,
    404: ExitCode.NOT_FOUND,
    408: ExitCode.TIMEOUT,
    409: ExitCode.VALIDATION,
    422: ExitCode.VALIDATION,
    429: ExitCode.RATE_LIMITED,
}

_ERROR_CODES: dict[ExitCode, str] = {
    ExitCode.GENERIC: "error",
    ExitCode.USAGE: "usage_error",
    ExitCode.AUTH: "auth_error",
    ExitCode.NOT_FOUND: "not_found",
    ExitCode.VALIDATION: "validation_error",
    ExitCode.RATE_LIMITED: "rate_limited",
    ExitCode.TIMEOUT: "timeout",
    ExitCode.SERVER_ERROR: "server_error",
    ExitCode.ALREADY_CONSUMED: "already_consumed",
    ExitCode.SAVE_FAILED: "save_failed",
    ExitCode.INTERRUPTED: "interrupted",
}


def error_code_for(code: ExitCode) -> str:
    """The published error token for an exit code."""
    return _ERROR_CODES.get(code, "error")


def error_codes() -> dict[ExitCode, str]:
    """Every exit code that names an error, for publication."""
    return dict(_ERROR_CODES)


def exit_code_for_status(status: int, *, one_shot: bool = False) -> ExitCode:
    """Map an HTTP status onto its exit code.

    ``one_shot`` marks a read the service serves exactly once, which is the
    only place a 406 means the result is gone. Everywhere else it is content
    negotiation -- a proxy or a misaimed host -- and reporting that as a
    consumed result sends the reader after a command they never ran.
    """
    if status == 406 and one_shot:
        return ExitCode.ALREADY_CONSUMED
    if (code := _STATUS_MAP.get(status)) is not None:
        return code
    if 500 <= status < 600:
        return ExitCode.SERVER_ERROR
    # A 3xx that was not followed, or a status no spec declares, is still a
    # failure: never fall through to SUCCESS.
    return ExitCode.GENERIC


def is_retryable(status: int) -> bool:
    """Retry on rate limiting, server faults and 408 -- never on another 4xx.

    Retrying a 4xx re-sends a request the server already rejected on its merits,
    and for one-shot reads a blind retry can consume a result the first attempt
    already delivered. A 408 is the exception: the server abandoned the wait
    rather than judging the request, so the same request is still worth sending.
    """
    return status in (408, 429) or 500 <= status < 600


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

#: Words that mark a field as carrying a credential, matched as whole name
#: segments rather than as substrings.
_SECRET_KEY_HINTS = frozenset(
    {
        "bearer",
        "key",
        "keys",
        "apikey",
        "token",
        "tokens",
        "secret",
        "secrets",
        "passwd",
        "password",
        "credential",
        "credentials",
        "auth",
        "authorization",
    }
)
REDACTED = "***REDACTED***"

#: Below this, replacing a value would mangle unrelated text more often than it
#: would hide a credential.
_MIN_SECRET_LEN = 8

#: Credentials resolved during this run. Registered where they are resolved, so
#: no emitter has to remember to opt into scrubbing.
_KNOWN_SECRETS: set[str] = set()

#: Short credentials already warned about, so one key warns once per run.
_REPORTED_SHORT: set[str] = set()


def _to_stderr(message: str) -> None:
    print(message, file=sys.stderr)


#: Notes written before a sink was bound. The command tree is built at import,
#: so a warning about the overlay is raised before there is a run to ask whether
#: it was told to be quiet.
_HELD: list[str] = []

#: Where a note goes once a run owns the streams. Unbound until then.
_SINK: Callable[[str], None] | None = None


def warn(message: str) -> None:
    """A note from a module that cannot reach the output layer.

    The config, overlay and credential registries are all imported by it, so
    they cannot import it back; this is the seam that keeps their notes subject
    to the same `--quiet` as every other diagnostic.
    """
    if _SINK is None:
        _HELD.append(message)
        return
    _SINK(message)


def set_warning_sink(sink: Callable[[str], None] | None) -> None:
    """Route held and future notes. ``None`` sends them to stderr unfiltered."""
    global _SINK
    _SINK = sink or _to_stderr
    for message in _HELD:
        _SINK(message)
    _HELD.clear()


def forget_warning_sink() -> None:
    """Unbind the sink and drop anything held, so one run cannot leak into the next."""
    global _SINK
    _SINK = None
    _HELD.clear()


def remember_secret(value: Any) -> None:
    """Record a resolved credential so no stream can print it later."""
    if not isinstance(value, str) or not value:
        return
    if len(value) < _MIN_SECRET_LEN:
        # Say so rather than drop it silently: the caller has every reason to
        # believe registering a credential is what protects it. Once per value:
        # a key resolves several times in one run.
        if value not in _REPORTED_SHORT:
            _REPORTED_SHORT.add(value)
            warn(
                f"warning: a credential under {_MIN_SECRET_LEN} characters is too "
                "short to scrub for and will not be redacted"
            )
        return
    _KNOWN_SECRETS.add(value)


def forget_secrets() -> None:
    """Drop every credential registered so far.

    The registry is process-global, so a test that resolves one would otherwise
    leak it into every test that runs after it.
    """
    _KNOWN_SECRETS.clear()
    _REPORTED_SHORT.clear()


def known_secrets() -> list[str]:
    """Every credential resolved so far, longest first.

    Longest first so a key that contains another as a prefix is replaced whole
    rather than leaving its tail behind.
    """
    return sorted(_KNOWN_SECRETS, key=len, reverse=True)


#: Splits a field name into words on punctuation and on camelCase boundaries.
#: Case has to be read before it is folded away, or `accessToken` collapses to a
#: single unrecognisable word.
_NAME_SEGMENTS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def names_a_secret(key: Any) -> bool:
    """Whether a field name marks its value as a credential.

    Matched on whole words rather than as a substring, so `authors` is not read
    as `auth`. Any word counts, not just the last: `secretAccessKey` and
    `authorization_header` name credentials as surely as `api_key` does. That
    also redacts a `key_terms`, which is the side to err on -- an over-redacted
    field is an inconvenience, an under-redacted one is a leak.
    """
    segments = [p for p in _NAME_SEGMENTS.split(str(key)) if p]
    return any(part.lower() in _SECRET_KEY_HINTS for part in segments)


def redact_value(value: Any) -> Any:
    """Recursively redact secret-looking keys in a payload."""
    if isinstance(value, dict):
        return {
            # Collapsed whole rather than walked: nothing under a key that
            # names a credential is worth more than the risk of missing one.
            k: (REDACTED if names_a_secret(k) else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def scrub(text: str, secrets: list[str]) -> str:
    """Remove known secret literals from free text.

    Last line of defence: a credential that reaches a message body via an
    upstream error string still must not be printed. Short values are skipped --
    redacting a 3-character "key" would mangle unrelated text.
    """
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            text = re.sub(re.escape(secret), REDACTED, text)
    return text


def scrub_structure(value: Any, secrets: list[str]) -> Any:
    """Remove secret literals from every string in a payload.

    Rendering is what defeats a scrub applied afterwards: a table wraps a long
    cell across lines and JSON escapes quotes and non-ASCII, so a credential
    that was one literal in the payload is no longer one literal in the output.
    Replacing before rendering is what closes that; `scrub` stays as a backstop.
    """
    if not secrets:
        return value
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {
            scrub_structure(k, secrets): scrub_structure(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_structure(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_structure(v, secrets) for v in value)
    return value


# --------------------------------------------------------------------------- #
# CLIError
# --------------------------------------------------------------------------- #


@dataclass
class CLIError(Exception):
    """A failure that maps onto an exit code and a structured error payload."""

    message: str
    exit_code: ExitCode = ExitCode.GENERIC
    http_status: int | None = None
    details: Any = None
    endpoint: str | None = None
    hint: str | None = None
    retryable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    #: Set where `details` is the only surviving copy of something the service
    #: will not serve again. Redacting by field name would destroy the part of
    #: the result the caller most needs; the literal scrub of every resolved
    #: credential still applies on the way out.
    verbatim_details: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.exit_code is ExitCode.SUCCESS:
            raise ValueError("a CLIError cannot carry the success exit code")

    def to_dict(self) -> dict[str, Any]:
        # Written out whole, then thinned: one list of the names this owns, so
        # a field added here cannot be forgotten in the guard below.
        payload: dict[str, Any] = {
            "code": error_code_for(self.exit_code),
            "message": self.message,
            "exit_code": int(self.exit_code),
            "retryable": self.retryable,
            "http_status": self.http_status,
            # Redacted by default: the details come from a server body that can
            # echo the request, headers and key included. Only a rescued result
            # that would be destroyed by it opts out.
            "details": self.details
            if self.verbatim_details
            else redact_value(self.details),
            "endpoint": self.endpoint or None,
            "hint": self.hint or None,
        }
        # `extra` carries server-named keys (a poll handle, say), so it may not
        # be allowed to rewrite a field a caller branches on -- including one
        # omitted from this payload for being unset.
        reserved = payload.keys()
        extra = {k: v for k, v in self.extra.items() if k not in reserved}
        return {k: v for k, v in payload.items() if v is not None} | extra


def error_from_status(
    status: int,
    message: str,
    *,
    details: Any = None,
    endpoint: str | None = None,
    one_shot: bool = False,
) -> CLIError:
    """Build a CLIError from an HTTP status, with its exit code, hint and retryability."""
    return CLIError(
        message,
        exit_code_for_status(status, one_shot=one_shot),
        http_status=status,
        details=details,
        endpoint=endpoint,
        hint=hint_for(status, one_shot=one_shot),
        retryable=is_retryable(status),
    )


def undeclared_status_error(
    status: int, body: Any, endpoint: str | None = None
) -> CLIError:
    """Report a status the spec does not declare, verbatim.

    A guessed message for an unknown status is worse than none: it sends the
    reader after the wrong cause. The body is passed through untouched.
    """
    return CLIError(
        f"Undeclared status {status} with body {body!r}",
        exit_code_for_status(status),
        http_status=status,
        details=body,
        endpoint=endpoint,
        retryable=is_retryable(status),
    )


def hint_for(status: int, *, one_shot: bool = False) -> str | None:
    """A short, actionable next step for a common failure."""
    if status == 406:
        return (
            "This execution result was already retrieved. A deployment serves "
            "its result exactly once; re-running the status call cannot "
            "recover it. Pass --save to `deployment run` to keep the next one."
            if one_shot
            else "The service would not answer in the format asked for. Check "
            "that `base_url` names the API rather than a proxy or web app."
        )
    match status:
        case 400:
            return (
                "The service rejected the request. Check the ids and parameter "
                "values passed; `details` carries the service's own response. On "
                "a retrieve this can also mean the result was already read -- "
                "that read cannot be repeated, so pass --save to keep the next one."
            )
        case 401 | 403:
            # Wrong, revoked and not-permitted all arrive as the same response,
            # so the hint cannot settle on one of them. A key from another
            # organisation is not among them: the resource is resolved within
            # its organisation first, so that answers 404 instead.
            return (
                "The key was rejected. Keys are per-product: `unstract config "
                "doctor` reports which one resolved and from where. A key that "
                "works elsewhere can still be rejected here if it is the wrong "
                "kind for this command, or does not cover this deployment."
            )
        case 404:
            return (
                "Verify the resource id, and that the organisation matches the "
                "resource's own. For deployments, confirm the API name."
            )
        case 402:
            return (
                "Out of quota, or the licence does not cover this request. "
                "Check the subscription for this product."
            )
        case 409:
            return "The resource is in use, or conflicts with an existing one."
        case 413:
            return "The upload is larger than the service accepts."
        case 415:
            return "The file's type is not one this service extracts."
        case 429:
            return "Rate limited. Back off and retry."
    if 500 <= status < 600:
        return "Server-side failure. If it persists, check service status."
    return None


__all__ = [
    "REDACTED",
    "CLIError",
    "ExitCode",
    "error_code_for",
    "error_codes",
    "scrub_structure",
    "forget_secrets",
    "known_secrets",
    "remember_secret",
    "error_from_status",
    "exit_code_for_status",
    "hint_for",
    "is_retryable",
    "redact_value",
    "scrub",
    "undeclared_status_error",
]
