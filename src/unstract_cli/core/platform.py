"""The client that talks to the platform API.

Both platform-key operations -- `whoami` and the deployment listing -- are
published in the OpenAPI spec and generated into `unstract-client`, so this
module only resolves configuration and builds `PlatformKeyClient`.

The platform API is served by the same deployment as the API deployments it
manages, so the client reads docstudio's `base_url` and the `platform_key`
beside it: one host, two keys.
"""

from __future__ import annotations

from unstract.api_deployments.client import PlatformKeyClient

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
    call, not per client. It stays in the signature because every command site
    already passes it and the parameter documents which calls need one --
    `whoami` runs before an organisation is known, everything else does not.
    """
    if timeout is not None and timeout <= 0:
        # httpx rejects a non-positive timeout deep in the connection layer,
        # with an error that matches no arm in `__main__` -- a traceback and no
        # envelope. Refused here, where it is still a usage error about a flag.
        raise CLIError(
            f"--transport-timeout must be greater than 0, not {timeout:g}.",
            ExitCode.USAGE,
            hint="Omit the flag to leave the connection unbounded.",
        )
    return PlatformKeyClient(
        base_url=config.require(DOCSTUDIO, "base_url"),
        api_key=config.require(DOCSTUDIO, "platform_key"),
        transport_timeout=timeout,
        logging_level="ERROR",
    )


def organisation(config: ResolvedConfig) -> str:
    """The organisation to act inside, or a usage error naming how to get one.

    It lives on the docstudio block: a platform key resolves it, and everything
    that consumes it -- deployment URLs above all -- reads it from there.
    """
    if org_id := config.get(DOCSTUDIO, "org_id"):
        return str(org_id)
    raise CLIError(
        "No organisation is configured.",
        ExitCode.USAGE,
        hint=(
            "Run `unstract auth whoami` to resolve it from your platform key, "
            "or set $UNSTRACT_ORG_ID."
        ),
    )


__all__ = ["organisation", "platform_client"]
