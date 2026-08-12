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

from collections.abc import Iterator
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


def deployment(config: ResolvedConfig, target: str) -> APIDeploymentsClient:
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
        raise CLIError(
            str(exc),
            ExitCode.SERVER_ERROR,
            endpoint=endpoint,
            retryable=True,
            hint="Could not reach the service. Check the base URL and connectivity.",
        ) from exc


def raise_for_result(result: dict[str, Any], endpoint: str | None = None) -> None:
    """Fail on a deployment response that reports an error status.

    The deployment client returns its status code instead of raising, so a
    failure would otherwise be reported as a successful run whose payload
    happens to contain an error.
    """
    status = int(result.get("status_code") or 0)
    reported = result.get("error")
    if (status and not 200 <= status < 300) or reported:
        raise error_from_status(
            status or 500,
            str(reported or f"Request failed with status {status}"),
            details=result,
            endpoint=endpoint,
        )


__all__ = [
    "deployment",
    "deployment_url",
    "llmwhisperer",
    "raise_for_result",
    "translated",
]
