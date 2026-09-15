"""The client that talks to the platform API.

Both platform-key operations -- `whoami` and the deployment listing -- are
published in the OpenAPI spec and generated into `unstract-client`, so this
module only resolves configuration and builds `PlatformKeyClient`.

The platform API is served by the same deployment as the API deployments it
manages, so the client reads docstudio's `base_url` and the `platform_key`
beside it: one host, two keys.
"""

from __future__ import annotations

from typing import Any

from unstract.api_deployments.client import PlatformClientError, PlatformKeyClient

from unstract_cli.config import DOCSTUDIO, ResolvedConfig
from unstract_cli.core.errors import CLIError, ExitCode


def platform_client(
    config: ResolvedConfig,
    org_id: str | None = None,
    *,
    timeout: float | None = None,
) -> PlatformKeyClient:
    """Build a Platform API client from the resolved configuration.

    ``org_id`` is accepted and ignored: the generated operations take it per
    call, not per client. It stays in the signature to document which calls
    need one -- `whoami` runs before an organisation is known, the listing does
    not -- so a caller passes it where it matters.
    """
    if timeout is not None and timeout <= 0:
        # A negative timeout is rejected at send time, by a bare ValueError
        # that matches no arm in `__main__` -- a traceback and no envelope.
        # Refused here, where it is still a usage error about a flag.
        raise CLIError(
            f"--transport-timeout must be greater than 0, not {timeout:g}.",
            ExitCode.USAGE,
            hint="Omit the flag to leave the connection unbounded.",
        )
    try:
        return PlatformKeyClient(
            base_url=config.require(DOCSTUDIO, "base_url"),
            api_key=config.require(DOCSTUDIO, "platform_key"),
            transport_timeout=timeout,
            logging_level="ERROR",
        )
    except PlatformClientError as exc:
        # The client validates the host and the key before it sends anything,
        # and that failure reaches no arm of the entry point: a traceback with
        # no envelope, where a script is parsing one. It is what the caller
        # configured, so it is a usage error.
        raise CLIError(
            str(exc),
            ExitCode.USAGE,
            hint="Check `base_url` and the platform key: the host needs a "
            "scheme, e.g. https://host, and the key cannot be blank.",
        ) from exc


def deployment_rows(page: Any, endpoint: str = "api/deployment/") -> list[Any]:
    """The rows of a deployment listing, or a protocol error naming the host.

    A listing whose `results` is not a list is not an empty account: it is a
    proxy, a login page or a web app answering in the API's place. Read as rows
    it would either crash on the first field or report deployments as gone.
    """
    rows = page.get("results") if isinstance(page, dict) else None
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise CLIError(
            "The deployment listing did not come back as a list of deployments.",
            ExitCode.SERVER_ERROR,
            details=page,
            endpoint=endpoint,
            hint="Check that `base_url` names the API rather than a proxy or "
            "web app; `details` carries what was received.",
        )
    return rows


def organisation(config: ResolvedConfig) -> str:
    """The organisation to act inside, or a usage error naming how to get one.

    It lives on the docstudio block: a platform key resolves it, and everything
    that consumes it -- deployment URLs above all -- reads it from there.
    """
    if org_id := str(config.get(DOCSTUDIO, "org_id") or "").strip():
        return org_id
    raise CLIError(
        "No organisation is configured.",
        ExitCode.USAGE,
        hint=(
            "Run `unstract auth whoami` to resolve it from your platform key, "
            "or set $UNSTRACT_ORG_ID."
        ),
    )


__all__ = ["deployment_rows", "organisation", "platform_client"]
