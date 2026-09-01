"""Building the product clients, and turning their failures into CLI errors.

The entry point deliberately does not catch bare ``Exception``: an unexpected
crash should look like a crash. Everything a client raises on purpose is
expected, so it is translated here into a ``CLIError`` carrying an exit code, a
hint and the response detail.

The clients report failure differently -- LLMWhisperer raises with a status
code attached, the deployment client returns a dict containing one, the Platform
API client raises with the body attached -- so every shape converges here rather
than in each command.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from requests.exceptions import (
    ConnectionError,
    InvalidSchema,
    InvalidURL,
    MissingSchema,
    RequestException,
    Timeout,
)
from unstract.api_deployments.client import (
    APIDeploymentsClient,
    APIDeploymentsClientException,
)
from unstract.clone.exceptions import PlatformAPIError
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)

from unstract_cli.config import DOCSTUDIO, LLMWHISPERER, ResolvedConfig
from unstract_cli.core.errors import CLIError, ExitCode, error_from_status
from unstract_cli.core.params import find_operation


def llmwhisperer(config: ResolvedConfig) -> LLMWhispererClientV2:
    """Build the LLMWhisperer client from the resolved configuration."""
    return LLMWhispererClientV2(
        base_url=config.require(LLMWHISPERER, "base_url"),
        api_key=config.require(LLMWHISPERER, "api_key"),
        logging_level="ERROR",
    )


def deployment_url(base_url: str, org_id: str, api_name: str) -> str:
    """The deployment's full URL, laid out as the spec declares the route.

    The client takes the whole URL and reads the organisation and API name back
    out of its last two segments, so the route is built from the spec rather
    than from a format string that can disagree with it.
    """
    path = find_operation(DOCSTUDIO, "execute")["path"]
    path = path.format(org_name=org_id, api_name=api_name)
    return base_url.rstrip("/") + path


def deployment(
    config: ResolvedConfig, target: str, transport_timeout: float | None = None
) -> APIDeploymentsClient:
    """Build a deployment client for an alias, or for a bare API name.

    An alias carries its own organisation and key; a bare name falls back to the
    profile's, so an unconfigured caller can still name a deployment directly.
    """
    if target in config.deployment_aliases():
        entry = config.deployment(target)
        api_name, org_id, api_key = (
            entry["api_name"],
            entry["org_id"],
            entry["api_key"],
        )
    else:
        api_name = target
        org_id = config.get(DOCSTUDIO, "org_id")
        api_key = config.get(DOCSTUDIO, "api_key")

    missing = [
        name for name, value in (("org_id", org_id), ("api_key", api_key)) if not value
    ]
    if missing:
        raise CLIError(
            f"Deployment {target!r} is missing {' and '.join(missing)}.",
            ExitCode.USAGE,
            hint=(
                "Define the deployment as an alias in the active profile, or set "
                "$UNSTRACT_ORG_ID and $UNSTRACT_DEPLOYMENT_KEY."
            ),
        )

    return APIDeploymentsClient(
        api_url=deployment_url(config.require(DOCSTUDIO, "base_url"), org_id, api_name),
        api_key=api_key,
        logging_level="ERROR",
        transport_timeout=transport_timeout,
    )


def _message_and_details(value: Any) -> tuple[str, Any]:
    """Split a client's error value into a one-line message and the raw detail.

    LLMWhisperer raises with either a string or the decoded error body, and the
    body's own wording is better than anything invented here.
    """
    if isinstance(value, dict):
        for key in ("message", "error", "detail", "reason"):
            if text := value.get(key):
                return str(text), value
        return str(value), value
    return str(value), None


def _unresolved_host(exc: BaseException) -> str | None:
    """The host a connection failed to resolve, or ``None`` if that is not why.

    A name that does not resolve is the one connection failure retrying cannot
    fix. Matched by type name rather than by import: the exception belongs to a
    transitive dependency of the clients, not to anything declared here.
    """
    reason = getattr(exc.args[0] if exc.args else None, "reason", None)
    if type(reason).__name__ != "NameResolutionError":
        return None
    return getattr(getattr(reason, "conn", None), "host", "") or ""


@contextmanager
def translated(endpoint: str | None = None) -> Iterator[None]:
    """Turn a client failure into a CLIError with an exit code and a hint."""
    try:
        yield
    except LLMWhispererClientException as exc:
        message, details = _message_and_details(exc.value)
        status = exc.status_code or (
            details.get("status_code") if isinstance(details, dict) else None
        )
        if status:
            raise error_from_status(
                int(status), message, details=details, endpoint=endpoint
            ) from exc
        raise CLIError(message, details=details, endpoint=endpoint) from exc
    except APIDeploymentsClientException as exc:
        raise CLIError(str(exc), ExitCode.USAGE, endpoint=endpoint) from exc
    except PlatformAPIError as exc:
        # `PlatformAPIError.__init__` appends "\n  body: <resp.text[:2000]>" to
        # its own message, so `str(exc)` would put up to 2KB of server body into
        # `error.message` -- which `emit_error` documents as a one-line summary
        # -- and duplicate it into `details`.
        message = str(exc).split("\n  body:", 1)[0]
        # The Platform API client raises rather than returning a status, and
        # carries the response body on the exception. Untranslated it would
        # reach the entry point as an unexpected crash and print a traceback.
        if exc.status_code:
            raise error_from_status(
                int(exc.status_code),
                message,
                details=exc.body,
                endpoint=endpoint,
            ) from exc
        raise CLIError(message, details=exc.body, endpoint=endpoint) from exc
    except Timeout as exc:
        raise CLIError(
            str(exc),
            ExitCode.TIMEOUT,
            endpoint=endpoint,
            retryable=True,
            hint="The request timed out in transit; the job may still be running.",
        ) from exc
    except ConnectionError as exc:
        if (host := _unresolved_host(exc)) is not None:
            raise CLIError(
                f"Could not resolve the host {host or endpoint or 'in the base URL'}.",
                ExitCode.SERVER_ERROR,
                endpoint=endpoint,
                hint="Check the base URL for a typo. Retrying will not help.",
            ) from exc
        raise CLIError(
            str(exc),
            ExitCode.SERVER_ERROR,
            endpoint=endpoint,
            retryable=True,
            hint="Could not reach the service. Check the base URL and connectivity.",
        ) from exc
    except RequestException as exc:
        # Must sit after Timeout and ConnectionError, which are subclasses.
        #
        # The Platform client is a bare `requests.Session`: unlike the other two
        # it wraps nothing itself, so a malformed base URL (`MissingSchema`,
        # `InvalidURL`) or a 2xx carrying HTML from a proxy or SPA host
        # (`JSONDecodeError`) arrives here raw. Every one of those subclasses
        # `OSError`, so untranslated they were caught by the entry point's
        # full-disk handler and rendered "Check the path and disk." -- the wrong
        # subsystem, on the one message a user with a bad base URL most needs to
        # be right.
        usage = isinstance(exc, (MissingSchema, InvalidSchema, InvalidURL))
        raise CLIError(
            str(exc),
            ExitCode.USAGE if usage else ExitCode.SERVER_ERROR,
            endpoint=endpoint,
            hint=(
                "Check `base_url` -- it needs a scheme, e.g. https://host."
                if usage
                else "The service answered, but not with JSON. Check that "
                "`base_url` names the API rather than a proxy or web app."
            ),
        ) from exc


def translating(
    call: Callable[..., Any], endpoint: str | None = None
) -> Callable[..., Any]:
    """Wrap one call so its failures are CLIErrors where they happen.

    A ``with translated(...)`` around a loop converts nothing until the loop is
    left, by which point what the loop knew -- the job handle above all -- is out
    of scope.
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with translated(endpoint=endpoint):
            return call(*args, **kwargs)

    return wrapped


def raise_for_result(result: dict[str, Any], endpoint: str | None = None) -> None:
    """Fail on a deployment response that reports an error status.

    The deployment client returns its status code instead of raising, so a
    failure would otherwise be reported as a successful run whose payload
    happens to contain an error.
    """
    status = int(result.get("status_code") or 0)
    reported = result.get("error")
    if status and not 200 <= status < 300:
        raise error_from_status(
            status,
            str(reported or f"Request failed with status {status}"),
            details=result,
            endpoint=endpoint,
        )
    if reported:
        # HTTP success carrying a failure in the body. Not retryable: a re-run
        # starts a second billed execution rather than retrying the first.
        raise CLIError(
            str(reported),
            ExitCode.VALIDATION,
            http_status=status or None,
            details=result,
            endpoint=endpoint,
            hint="The request was accepted and the work was not done; `details` "
            "carries the service's own report.",
        )


__all__ = [
    "deployment",
    "deployment_url",
    "llmwhisperer",
    "raise_for_result",
    "translated",
    "translating",
]
