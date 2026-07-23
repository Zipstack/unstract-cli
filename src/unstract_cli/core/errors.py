"""Exit codes, structured errors, and secret redaction (SPEC.md §5.4, §5.5, §5.7).

Exit codes are a stable API: an agent branches on them without parsing prose.
Every failure also emits a JSON object on **stderr**, carrying `hint` and
`retryable` so an agent can self-correct rather than retry blindly.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Stable exit codes (SPEC.md §5.4)."""

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


#: HTTP status -> exit code. 422 maps to VALIDATION, which is correct for a real
#: validation failure; the deployment API's misuse of 422 for in-progress states
#: is handled by the poller *before* reaching here, by branching on the response
#: body rather than the status code (SPEC.md §6.2).
_STATUS_MAP: dict[int, ExitCode] = {
    400: ExitCode.VALIDATION,
    401: ExitCode.AUTH,
    403: ExitCode.AUTH,
    404: ExitCode.NOT_FOUND,
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
}


def exit_code_for_status(status: int) -> ExitCode:
    """Map an HTTP status onto its exit code."""
    if code := _STATUS_MAP.get(status):
        return code
    if 500 <= status < 600:
        return ExitCode.SERVER_ERROR
    if 400 <= status < 500:
        return ExitCode.GENERIC
    return ExitCode.SUCCESS


def is_retryable(status: int) -> bool:
    """Retry only on rate limiting and server faults -- never on 4xx.

    Retrying a 4xx re-sends a request the server has already rejected on its
    merits. Worse, for one-shot reads a blind retry can consume a result that
    the first attempt already delivered (SPEC.md §5.6).
    """
    return status == 429 or 500 <= status < 600


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SECRET_HEADERS = {"unstract-key", "authorization", "apikey"}
_SECRET_HEADER_PREFIXES = ("x-", )
_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "credential", "auth")
REDACTED = "***REDACTED***"


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-bearing headers."""
    out: dict[str, Any] = {}
    for key, value in headers.items():
        low = key.lower()
        secret = low in _SECRET_HEADERS or (
            low.startswith(_SECRET_HEADER_PREFIXES)
            and any(h in low for h in _SECRET_KEY_HINTS)
        )
        out[key] = REDACTED if secret else value
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact secret-looking keys in a payload."""
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if any(h in str(k).lower() for h in _SECRET_KEY_HINTS)
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

    The last line of defence: a credential that reaches a message body via an
    upstream error string still must not be printed.
    """
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, REDACTED)
            text = re.sub(re.escape(secret), REDACTED, text)
    return text


# --------------------------------------------------------------------------- #
# CLIError
# --------------------------------------------------------------------------- #


@dataclass
class CLIError(Exception):
    """A failure that maps onto an exit code and a structured stderr payload."""

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
            payload["details"] = self.details
        if self.endpoint:
            payload["endpoint"] = self.endpoint
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return {"error": payload}

    def emit(self, secrets: list[str] | None = None) -> None:
        """Write the structured error to stderr, and to stdout when piped.

        Humans always get it on stderr. When stdout is not a TTY -- the agent /
        wrapper case -- the same envelope is *also* written to stdout, so a
        pipeline that feeds stdout to a JSON parser sees a valid object instead of
        an empty stream (DOC 9). A result and an error never share an invocation's
        stdout: emit runs only on the error path, which produces no result output.
        """
        text = json.dumps(self.to_dict(), indent=2, default=str)
        if secrets:
            text = scrub(text, secrets)
        print(text, file=sys.stderr)
        if not sys.stdout.isatty():
            print(text, file=sys.stdout)

    def emit_stdout_only(self, secrets: list[str] | None = None) -> None:
        """Write the envelope to stdout when piped, leaving stderr untouched.

        For Click's own usage errors, whose human message Click has already
        printed to stderr: this adds only the machine-readable copy on stdout
        (DOC 9), without duplicating the human line.
        """
        if sys.stdout.isatty():
            return
        text = json.dumps(self.to_dict(), indent=2, default=str)
        if secrets:
            text = scrub(text, secrets)
        print(text, file=sys.stdout)


def hint_for(status: int, endpoint: str | None = None, message: str | None = None) -> str | None:
    """A short, actionable next step for common failures.

    ``message`` lets a hint target a specific server error where the status code
    alone is ambiguous -- an adapter-permission 403 and a dead-vector-DB 500 both
    have a much more useful next step than the generic status hint.
    """
    low = (message or "").lower()

    # Adapter permission (IMPROVEMENT 4): the API key identity is distinct from the
    # human account of the same name, so an adapter can be "available" yet unusable.
    if "permission error" in low and "adapter" in low:
        return (
            "One or more adapters are not shared with your API key. The API user "
            "(e.g. *-api-rw-*@platform.internal) is a DIFFERENT identity from your "
            "human account. Share the adapters to the org in the web console, or set "
            "a default triad, then retry. `adapter list` shows created_by_email."
        )
    # The message names the *project* default, but the server never reads it: it
    # resolves the profile from the prompt's own profile_manager FK. Chasing the
    # project default is the wrong fix and cost real time (GOTCHAS #1).
    if "default llm profile is not configured" in low:
        return (
            "Misleading message: the server resolves the LLM profile from the "
            "PROMPT's own profile_manager field and does NOT fall back to the "
            "project default -- so `profile set-default` cannot fix this. Set it "
            "on the prompt: `prompt patch --prompt-id <id> --profile-manager "
            "<profile-id>` (or pass --profile-manager on this call). Create "
            "prompts with --profile-manager to avoid it entirely."
        )
    # Deploy-time tool validation (GOTCHAS #2). The 422 surfaces only at
    # `deployment run`, long after the tool instance was attached.
    if "tool validation failed" in low or ("challenge_llm" in low or "challenge llm" in low):
        return (
            "The attached tool instance likely has an empty metadata.challenge_llm, "
            "which fails validation even when enable_challenge is false. Fix: "
            "`workflow tool get --id <instance>` to read the CURRENT metadata, set "
            "challenge_llm to a valid LLM adapter id, and send the COMPLETE object "
            "back with `workflow tool set-metadata` (it replaces, never merges). "
            "To avoid it next time, set the project's --challenge-llm before "
            "`export-tool`."
        )
    # Dead vector DB (IMPROVEMENT 6): short docs don't need RAG at all.
    if "vectordb" in low or "vector db" in low or "qdrant" in low:
        return (
            "The vector DB is unreachable. For short documents you can skip RAG "
            "entirely: set the profile's chunk_size=0 (and chunk_overlap=0), which "
            "bypasses embedding and the vector DB, then re-index."
        )

    match status:
        case 401 | 403:
            return (
                "Check the API key for this product. Keys are per-product: see "
                "`unstract config list` and SPEC §4.4 for which env var applies."
            )
        case 404:
            return (
                "Verify the resource id and that --org-id matches the resource's "
                "organization. For deployments, confirm --api-name is correct."
            )
        case 406:
            return (
                "This result was already retrieved. Results can be read exactly "
                "once; re-running the request cannot recover them. Use --save next "
                "time to persist on first read."
            )
        case 409:
            return "The resource is in use or conflicts with an existing one."
        case 429:
            return "Rate limited. The CLI retries automatically; raise --max-retries if needed."
    if 500 <= status < 600:
        return "Server-side failure. Retried automatically; if it persists, check service status."
    return None


__all__ = [
    "REDACTED",
    "CLIError",
    "ExitCode",
    "exit_code_for_status",
    "hint_for",
    "is_retryable",
    "redact_headers",
    "redact_value",
    "scrub",
]
