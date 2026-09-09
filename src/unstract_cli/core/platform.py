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

from unstract_cli.config import (
    DOCSTUDIO,
    PLATFORM,
    ResolvedConfig,
)
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
        body = self._send("GET", f"{base}/{prefix}/unstract/whoami/", "whoami/")
        if not isinstance(body, dict):
            # `_send` returns None on a 204 or an empty 2xx body, and whatever
            # `resp.json()` decoded otherwise -- a list, for a misrouted host.
            # Guarded here, where the shape is known, rather than at each
            # consumer: unguarded, `body.get(...)` raises an AttributeError that
            # matches no arm in `__main__`, so the caller gets a traceback and
            # no envelope at all.
            raise CLIError(
                "The platform API did not return an identity.",
                ExitCode.SERVER_ERROR,
                details=body,
                endpoint="whoami",
                hint="Check that `base_url` names an Unstract deployment that "
                "serves /unstract/whoami/.",
            )
        return body


def platform_base_url(config: ResolvedConfig) -> str:
    """Where the platform API lives.

    One deployment serves both the platform API and the deployments it manages,
    so a caller who has said where docstudio is has already said where this is.
    Resolving `platform.base_url` alone would ignore that: a profile written
    before the `platform` block existed, and every `docstudio --base-url`, would
    silently fall through to the built-in cloud default and send the key there.

    Both tiers are read with `get_explicit`, which stops before the built-in
    defaults. Comparing `get`'s answer against the default instead would read a
    caller who named the SaaS host as one who named nothing -- and `config init`
    writes that exact host into every generated profile, so that is the common
    case, not a corner of it.
    """
    if (named := config.get_explicit(PLATFORM, "base_url")) is not None:
        return str(named)
    if (shared := config.get_explicit(DOCSTUDIO, "base_url")) is not None:
        return str(shared)
    return str(config.require(PLATFORM, "base_url"))


def platform_client(
    config: ResolvedConfig,
    org_id: str | None = None,
    *,
    timeout: float | None = None,
) -> CLIPlatformClient:
    """Build a Platform API client from the resolved configuration.

    ``org_id`` is optional because `whoami` runs before one is known. Every
    other call needs it, and asks for it explicitly.
    """
    endpoint = OrgEndpoint(
        base_url=platform_base_url(config),
        organization_id=org_id if org_id is not None else NO_ORGANISATION,
        platform_key=config.require(PLATFORM, "api_key"),
        **(
            {"api_path_prefix": prefix}
            if (prefix := config.get(PLATFORM, "api_prefix")) is not None
            else {}
        ),
    )
    # `PlatformClient`'s own default is 60s per request, which `_paginate`
    # spends per page. An interactive caller who asked for a bound gets it.
    if timeout is None:
        return CLIPlatformClient(endpoint)
    if timeout <= 0:
        # urllib3 raises a bare ValueError for a non-positive timeout, which
        # matches no arm in `__main__` -- a traceback and no envelope. Rejected
        # here, where it is still a usage error about a flag.
        raise CLIError(
            f"--transport-timeout must be greater than 0, not {timeout:g}.",
            ExitCode.USAGE,
            hint="Omit the flag to use the client's own 60s default.",
        )
    # Passed as the float it was parsed as. `PlatformClient` annotates this
    # `int`, but the annotation is not enforced and `requests` takes floats;
    # truncating instead would send `--transport-timeout 0.5` as 0, which is
    # the ValueError above, and silently round 1.9 down to 1.
    return CLIPlatformClient(endpoint, timeout=timeout)


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


__all__ = [
    "CLIPlatformClient",
    "organisation",
    "platform_base_url",
    "platform_client",
]
