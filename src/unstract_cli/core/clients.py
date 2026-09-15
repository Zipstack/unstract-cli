"""Building the product clients, and turning their failures into CLI errors.

The entry point deliberately does not catch bare ``Exception``: an unexpected
crash should look like a crash. Everything a client raises on purpose is
expected, so it is translated here into a ``CLIError`` carrying an exit code, a
hint and the response detail.

The clients report failure differently -- LLMWhisperer raises with a status
code attached, the deployment client returns a dict containing one, the platform
client spells one into its message -- so every shape converges here rather than
in each command. The deployment client also raises for a request it will not
send at all, which is always a usage error.
"""

from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from requests.exceptions import (
    ConnectionError,
    InvalidHeader,
    InvalidSchema,
    InvalidURL,
    MissingSchema,
    RequestException,
    Timeout,
    URLRequired,
)
from unstract.api_deployments.client import (
    APIDeploymentsClient,
    APIDeploymentsClientException,
    PlatformClientError,
)
from unstract.llmwhisperer.client_v2 import (
    LLMWhispererClientException,
    LLMWhispererClientV2,
)

from unstract_cli.config import (
    DOCSTUDIO,
    ENV_VARS,
    KEY_SOURCES,
    LLMWHISPERER,
    ResolvedConfig,
)
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


#: Transport timeout for the clients that set none of their own. The same
#: figure the LLMWhisperer client applies, so a stalled connection is given up
#: on the same way on every path.
DEFAULT_TRANSPORT_TIMEOUT = 120.0


def deployment(
    config: ResolvedConfig,
    api_name: str,
    transport_timeout: float | None = DEFAULT_TRANSPORT_TIMEOUT,
) -> APIDeploymentsClient:
    """Build a client for one deployment, named by its API name.

    Fails before any request when no key or organisation resolves: a credential
    error from the server would name the wrong fault, and the caller is told
    every place the CLI looked rather than only that it found nothing.
    """
    org_id = config.get(DOCSTUDIO, "org_id")
    if not org_id:
        raise CLIError(
            f"No organisation is configured to run {api_name!r} in.",
            ExitCode.USAGE,
            hint="Run `unstract auth login`, set $UNSTRACT_ORG_ID, or pass --org-id.",
        )
    api_key = config.deployment_key(api_name)
    if not api_key:
        looked_in = ", ".join(config.deployment_key_sources(api_name))
        raise CLIError(
            f"No key resolves for deployment {api_name!r}. Looked in: {looked_in} "
            "-- all unset.",
            ExitCode.USAGE,
            hint=(
                f"Set ${ENV_VARS[(DOCSTUDIO, 'api_key')][0]}, or store a key: "
                "`unstract config set docstudio api_key <key>` for one that covers "
                "the organisation, or `unstract config set docstudio api_key "
                f"<key> --deployment {api_name}` for this deployment alone. "
                f"{KEY_SOURCES}"
            ),
        )

    return APIDeploymentsClient(
        api_url=deployment_url(config.require(DOCSTUDIO, "base_url"), org_id, api_name),
        api_key=api_key,
        logging_level="ERROR",
        transport_timeout=transport_timeout,
    )


@contextmanager
def deployment_errors(api_name: str) -> Iterator[None]:
    """Say what a refusal means for *this* deployment, once the server answers.

    A rejected key and an unknown name are both indistinguishable from success
    until the service answers, so the correction belongs on the answer: a key
    that works elsewhere may not cover this deployment, and a name that was
    valid may have been renamed since it was written down.
    """
    try:
        yield
    except CLIError as exc:
        if exc.exit_code is ExitCode.AUTH:
            exc.message = (
                f"The key supplied for deployment {api_name!r} does not authorize "
                f"it: {exc.message}"
            )
            exc.hint = (
                "This deployment may need a key of its own: "
                "`unstract config set docstudio api_key <key> --deployment "
                f"{api_name}`."
            )
        elif exc.exit_code is ExitCode.NOT_FOUND:
            more = (
                "Run `unstract docstudio deployment ls` for the current API names; "
                "`unstract config doctor --probe` reports profile entries the "
                "organisation no longer has."
            )
            exc.hint = f"{exc.hint} {more}" if exc.hint else more
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


#: Failures that mean the request was never sendable, so the fault is in the
#: caller's configuration rather than in the service. `InvalidHeader` is not
#: among them: every handler takes it first, to keep the credential it quotes
#: out of the message. `InvalidProxyURL` is an `InvalidURL`.
UNSENDABLE = (
    MissingSchema,
    InvalidSchema,
    InvalidURL,
    URLRequired,
)


#: The status inside a `PlatformClientError` message. The released client embeds
#: it in prose rather than carrying it, so this is the only route from a refused
#: platform call to the right exit code. Deletable once the exception carries one.
_PLATFORM_STATUS = re.compile(r"failed with (\d{3})\b")


@contextmanager
def translated(endpoint: str | None = None, *, one_shot: bool = False) -> Iterator[None]:
    """Turn a client failure into a CLIError with an exit code and a hint.

    ``one_shot`` marks a read the service serves exactly once, which changes
    what a 406 from it means.
    """
    try:
        yield
    except LLMWhispererClientException as exc:
        message, details = _message_and_details(exc.value)
        status = exc.status_code or (
            details.get("status_code") if isinstance(details, dict) else None
        )
        if status:
            raise error_from_status(
                int(status),
                message,
                details=details,
                endpoint=endpoint,
                one_shot=one_shot,
            ) from exc
        raise CLIError(message, details=details, endpoint=endpoint) from exc
    except PlatformClientError as exc:
        # Ordered before `APIDeploymentsClientException`, which it derives from:
        # caught there, every platform failure would exit USAGE, and a rejected
        # key has to exit AUTH -- the exit-code table in the README promises it
        # and a setup script branches on it.
        #
        # The status is recovered from the message because the exception does
        # not carry one: the client spells it into the text as "failed with
        # <status>". A wording change upstream silently costs the mapping,
        # which is what the `_PLATFORM_STATUS` test pins.
        if match := _PLATFORM_STATUS.search(str(exc)):
            raise error_from_status(
                int(match.group(1)), str(exc), endpoint=endpoint, one_shot=one_shot
            ) from exc
        # Unparseable: the failure is real and the status is unknown, so report
        # it as a server-side failure rather than blaming the caller's usage.
        raise CLIError(str(exc), ExitCode.SERVER_ERROR, endpoint=endpoint) from exc
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
    except InvalidHeader as exc:
        # The message quotes the offending header value, and that value is the
        # credential. It arrives `repr`-escaped, so the literal scrub cannot
        # match it either -- say what happened instead of quoting it.
        raise CLIError(
            "A request header could not be built.",
            ExitCode.USAGE,
            endpoint=endpoint,
            hint=(
                "A credential most likely carries a newline or a control "
                "character. Check how it is stored."
            ),
        ) from exc
    except UNSENDABLE as exc:
        # These say the request could never be sent -- a base URL without a
        # scheme is the usual one. Retrying is the wrong advice, and the fault
        # is in the caller's configuration rather than in the service.
        raise CLIError(
            str(exc) or type(exc).__name__,
            ExitCode.USAGE,
            endpoint=endpoint,
            hint=(
                "The request could not be built. Check the base URL, and any "
                "proxy variables, for a typo."
            ),
        ) from exc
    except json.JSONDecodeError as exc:
        # A client that parses the body itself raises this on a 2xx carrying
        # HTML from a proxy or SPA host. The base class, not the `requests`
        # subclass: only one of the clients raises that one.
        raise CLIError(
            str(exc),
            ExitCode.SERVER_ERROR,
            endpoint=endpoint,
            hint=(
                "The service answered, but not with JSON. Check that "
                "`base_url` names the API rather than a proxy or web app."
            ),
        ) from exc
    except RequestException as exc:
        raise CLIError(
            str(exc) or type(exc).__name__,
            ExitCode.SERVER_ERROR,
            endpoint=endpoint,
            retryable=True,
            hint="The request failed in transit rather than being answered.",
        ) from exc


def translating(
    call: Callable[..., Any], endpoint: str | None = None, *, one_shot: bool = False
) -> Callable[..., Any]:
    """Wrap one call so its failures are CLIErrors where they happen.

    A ``with translated(...)`` around a loop converts nothing until the loop is
    left, by which point what the loop knew -- the job handle above all -- is out
    of scope.
    """

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with translated(endpoint=endpoint, one_shot=one_shot):
            return call(*args, **kwargs)

    return wrapped


def raise_for_result(
    result: dict[str, Any], endpoint: str | None = None, *, one_shot: bool = False
) -> None:
    """Fail on a deployment response that reports an error status.

    The deployment client returns its status code instead of raising, so a
    failure would otherwise be reported as a successful run whose payload
    happens to contain an error.
    """
    raw_status = result.get("status_code")
    try:
        status = int(raw_status) if raw_status is not None else 0
    except (TypeError, ValueError):
        raise CLIError(
            f"The service reported a status code of {raw_status!r}.",
            ExitCode.SERVER_ERROR,
            details=result,
            endpoint=endpoint,
            hint="`details` carries the response exactly as it arrived.",
        ) from None
    reported = result.get("error")
    if not status:
        raise CLIError(
            "The service answered without a status code.",
            ExitCode.SERVER_ERROR,
            details=result,
            endpoint=endpoint,
            retryable=True,
            hint="`details` carries the response exactly as it arrived.",
        )
    if not 200 <= status < 300:
        raise error_from_status(
            status,
            str(reported or f"Request failed with status {status}"),
            details=result,
            endpoint=endpoint,
            one_shot=one_shot,
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
    "DEFAULT_TRANSPORT_TIMEOUT",
    "UNSENDABLE",
    "deployment",
    "deployment_errors",
    "deployment_url",
    "llmwhisperer",
    "raise_for_result",
    "translated",
    "translating",
]
