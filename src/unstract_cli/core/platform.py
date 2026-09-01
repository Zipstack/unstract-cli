"""The Platform API client, and the one call the published client lacks.

`PlatformClient` ships in `unstract-client` for the clone subpackage, and its
list endpoints are exactly what `deployment ls` needs. It builds every URL as
``{base_url}/{prefix}/unstract/{organization_id}/<entity>/``, which is right for
every call that acts inside an organisation and wrong for the one call made
before the organisation is known.

`whoami` is that call, so it is added here rather than in the published client:
it needs no release to land, and the organisation-less shape is a CLI concern
until something else wants it.
"""

from __future__ import annotations

from typing import Any

from unstract.clone.client import PlatformClient
from unstract.clone.context import OrgEndpoint

from unstract_cli.config import DOCSTUDIO, PLATFORM, ResolvedConfig
from unstract_cli.core.errors import CLIError, ExitCode

#: The organisation `whoami` is called with. The endpoint carries no
#: organisation segment -- resolving it is the point of the call -- and
#: `OrgEndpoint` requires the field, so it is named rather than left as a bare
#: empty string at the call site.
NO_ORGANISATION = ""


class CLIPlatformClient(PlatformClient):
    """`PlatformClient` plus the organisation-less identity read."""

    def whoami(self) -> dict[str, Any]:
        """Describe the key: which organisation it belongs to, and its tier.

        The URL is built here instead of through ``_url`` because that method
        inserts the organisation this call exists to discover.
        """
        base = self.endpoint.base_url.rstrip("/")
        prefix = self.endpoint.api_path_prefix.strip("/")
        return self._send("GET", f"{base}/{prefix}/unstract/whoami/", "whoami/")


def platform_client(
    config: ResolvedConfig, org_id: str | None = None
) -> CLIPlatformClient:
    """Build a Platform API client from the resolved configuration.

    ``org_id`` is optional because `whoami` runs before one is known. Every
    other call needs it, and asks for it explicitly.
    """
    return CLIPlatformClient(
        OrgEndpoint(
            base_url=config.require(PLATFORM, "base_url"),
            organization_id=org_id if org_id is not None else NO_ORGANISATION,
            platform_key=config.require(PLATFORM, "api_key"),
        )
    )


def organisation(config: ResolvedConfig) -> str:
    """The organisation to act inside, or a usage error naming how to get one.

    It lives on the docstudio block: a platform key resolves it, and everything
    that consumes it -- deployment URLs, aliases -- reads it from there.
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


__all__ = ["CLIPlatformClient", "organisation", "platform_client"]
