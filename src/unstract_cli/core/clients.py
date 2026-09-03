"""Building the product clients, and turning their failures into CLI errors.

The entry point deliberately does not catch bare ``Exception``: an unexpected
crash should look like a crash. Everything a client raises on purpose is
expected, so it is translated here into a ``CLIError`` carrying an exit code, a
hint and the response detail.

The two clients report failure differently -- LLMWhisperer raises with a status
code attached, the deployment client returns a dict containing one -- so both
shapes converge here rather than in each command.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from requests.exceptions import ConnectionError, Timeout
from unstract.api_deployments.client import (
    APIDeploymentsClient,
    APIDeploymentsClientException,
)
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
            hint=_alias_hint(config, target)
            or (
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


def _alias_hint(config: ResolvedConfig, target: str) -> str | None:
    """What to say when a target is not one of the aliases that are configured.

    A bare API name is a supported way to name a deployment, so a target that is
    not an alias cannot be rejected outright. It can still be a misspelt one,
    and a caller who has defined aliases is likelier to have meant one of them
    than to have typed a raw name, so the ones that exist are worth naming.
    """
    if not (aliases := config.deployment_aliases()) or target in aliases:
        return None
    return (
        f"{target!r} is not one of the deployment aliases in the active profile "
        f"({', '.join(aliases)}), so it was sent as an API name."
    )


@contextmanager
def naming_aliases(config: ResolvedConfig, target: str) -> Iterator[None]:
    """Say which aliases exist when a bare API name is not found.

    Sending a misspelt alias as an API name is indistinguishable from sending a
    real one until the service answers, so the correction belongs on the answer.
    """
    try:
        yield
    except CLIError as exc:
        if exc.exit_code is ExitCode.NOT_FOUND and (hint := _alias_hint(config, target)):
            exc.hint = f"{exc.hint} {hint}" if exc.hint else hint
        raise


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


def _causes(exc: BaseException) -> Iterator[BaseException]:
    """One failure and everything it was raised from, outermost first."""
    seen: BaseException | None = exc
    while seen is not None:
        yield seen
        seen = seen.__cause__ or seen.__context__


def _unresolved_host(exc: BaseException) -> str | None:
    """The host a connection failed to resolve, or ``None`` if that is not why.

    A name that does not resolve is the one connection failure retrying cannot
    fix. Read from the chain rather than from the outermost exception: the
    clients re-raise transport failures as their ``requests`` equivalents
    carrying only a message, so nothing structural survives at the top -- but
    the original is still attached underneath, and `socket.gaierror` is the
    resolver's own answer whichever transport asked it.
    """
    if not any(isinstance(cause, socket.gaierror) for cause in _causes(exc)):
        return None
    for cause in _causes(exc):
        # httpx keeps the request on the error it raises; urllib3 keeps the
        # connection. Either names the host without parsing a message.
        url = getattr(getattr(cause, "request", None), "url", None)
        if host := getattr(url, "host", "") or getattr(
            getattr(cause, "conn", None), "host", ""
        ):
            return host
    return ""


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
    "naming_aliases",
    "raise_for_result",
    "translated",
    "translating",
]
