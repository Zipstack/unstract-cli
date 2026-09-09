"""Exit codes, structured errors, and secret redaction.

Exit codes are a stable API: a caller branches on them without parsing prose.
Every failure also carries `hint` and `retryable` so the caller can self-correct
rather than retry blindly.
"""

from __future__ import annotations

import re
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
    # The deployment status endpoint is the one that answers 406 meaningfully;
    # the whisper equivalent is a 400 whose body says so, which is prose we do
    # not translate on. A 406 from anywhere else -- DRF returns one on content
    # negotiation failure -- lands on this code too, and reads as a one-shot
    # read that was already consumed.
    406: ExitCode.ALREADY_CONSUMED,
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


def exit_code_for_status(status: int) -> ExitCode:
    """Map an HTTP status onto its exit code."""
    if code := _STATUS_MAP.get(status):
        return code
    if 500 <= status < 600:
        return ExitCode.SERVER_ERROR
    # A 3xx that was not followed, or a status no spec declares, is still a
    # failure: never fall through to SUCCESS.
    return ExitCode.GENERIC


def is_retryable(status: int) -> bool:
    """Retry only on rate limiting and server faults -- never on 4xx.

    Retrying a 4xx re-sends a request the server already rejected on its merits,
    and for one-shot reads a blind retry can consume a result the first attempt
    already delivered.
    """
    return status == 429 or 500 <= status < 600


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SECRET_HEADERS = {"unstract-key", "authorization", "apikey"}
_SECRET_HEADER_PREFIXES = ("x-",)
_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "credential", "auth")
REDACTED = "***REDACTED***"

#: Credentials resolved during this run. Registered where they are resolved, so
#: no emitter has to remember to opt into scrubbing.
_KNOWN_SECRETS: set[str] = set()


def remember_secret(value: Any) -> None:
    """Record a resolved credential so no stream can print it later."""
    if isinstance(value, str) and len(value) >= 8:
        _KNOWN_SECRETS.add(value)


def known_secrets() -> list[str]:
    """Every credential resolved so far, longest first.

    Longest first so a key that contains another as a prefix is replaced whole
    rather than leaving its tail behind.
    """
    return sorted(_KNOWN_SECRETS, key=len, reverse=True)


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-bearing headers."""
    out: dict[str, Any] = {}
    for key, value in headers.items():
        low = key.lower()
        secret = low in _SECRET_HEADERS or (
            low.startswith(_SECRET_HEADER_PREFIXES)
            and any(hint in low for hint in _SECRET_KEY_HINTS)
        )
        out[key] = REDACTED if secret else value
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact secret-looking keys in a payload."""
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if any(hint in str(k).lower() for hint in _SECRET_KEY_HINTS)
                and isinstance(v, str)
                else redact_value(v)
            )
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
        if secret and len(secret) >= 8:
            text = re.sub(re.escape(secret), REDACTED, text)
    return text


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
    code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.exit_code is ExitCode.SUCCESS:
            raise ValueError("a CLIError cannot carry the success exit code")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code or _ERROR_CODES.get(self.exit_code, "error"),
            "message": self.message,
            "exit_code": int(self.exit_code),
            "retryable": self.retryable,
        }
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.details is not None:
            # Structural, not opt-in: the details come from a server body that
            # can echo the request, headers and key included.
            payload["details"] = redact_value(self.details)
        if self.endpoint:
            payload["endpoint"] = self.endpoint
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return payload


def error_from_status(
    status: int, message: str, *, details: Any = None, endpoint: str | None = None
) -> CLIError:
    """Build a CLIError from an HTTP status, with its exit code, hint and retryability."""
    return CLIError(
        message,
        exit_code_for_status(status),
        http_status=status,
        details=details,
        endpoint=endpoint,
        hint=hint_for(status),
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


def hint_for(status: int) -> str | None:
    """A short, actionable next step for a common failure."""
    match status:
        case 400:
            return (
                "The service rejected the request. Check the ids and parameter "
                "values passed; `details` carries the service's own response."
            )
        case 401 | 403:
            # Wrong, revoked, foreign-organisation and not-permitted all arrive
            # as the same response, so the hint cannot settle on one of them.
            return (
                "The key was rejected. Keys are per-product: `unstract config "
                "doctor` reports which one resolved and from where. A key that "
                "works elsewhere can still be rejected here -- it may be the "
                "wrong kind for this command, or belong to another organisation."
            )
        case 404:
            return (
                "Verify the resource id, and that the organisation matches the "
                "resource's own. For deployments, confirm the API name."
            )
        case 406:
            return (
                "This execution result was already retrieved. A deployment serves "
                "its result exactly once; re-running the status call cannot "
                "recover it. Pass --save to `deployment run` to keep the next one."
            )
        case 409:
            return "The resource is in use, or conflicts with an existing one."
        case 429:
            return "Rate limited. Back off and retry."
    if 500 <= status < 600:
        return "Server-side failure. If it persists, check service status."
    return None


__all__ = [
    "REDACTED",
    "CLIError",
    "ExitCode",
    "known_secrets",
    "remember_secret",
    "error_from_status",
    "exit_code_for_status",
    "hint_for",
    "is_retryable",
    "redact_headers",
    "redact_value",
    "scrub",
    "undeclared_status_error",
]
