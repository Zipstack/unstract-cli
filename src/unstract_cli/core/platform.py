"""Where the platform API lives, and the client that talks to it.

Both platform-key operations -- `whoami` and the deployment listing -- are
published in the OpenAPI spec and generated into `unstract-client`, so this
module only resolves configuration and builds `PlatformKeyClient`. Earlier
revisions subclassed `unstract.clone.PlatformClient`, the org-cloning tool's
hand-written admin client, and hand-built the whoami URL; that bypassed the
generated surface entirely, which is why neither command was covered by the
spec-derived contract tests.
"""

from __future__ import annotations

from unstract.api_deployments.client import PlatformKeyClient

from unstract_cli.config import (
    DOCSTUDIO,
    PLATFORM,
    ResolvedConfig,
)
from unstract_cli.core.errors import CLIError, ExitCode


def platform_base_url(config: ResolvedConfig) -> str:
    """Where the platform API lives.

    One deployment serves both the platform API and the deployments it manages,
    so a caller who has said where docstudio is has already said where this is.
    Resolving `platform.base_url` alone would ignore that: a profile written
    before the `platform` block existed, and every `docstudio --base-url`, would
    silently fall through to the built-in cloud default and send the key there.

    The two products are walked **tier by tier**, not one product at a time.
    Asking `platform` for all three tiers first would let a profile's
    `platform.base_url` beat a `docstudio --base-url` flag -- and `config init`
    writes `platform.base_url` into every profile it generates, so that would
    silently ignore the flag for every generated config, which is the majority
    of them.

    Each tier is read explicitly, stopping before the built-in defaults.
    Comparing `get`'s answer against the default instead would read a caller who
    named the SaaS host as one who named nothing -- the same `config init`
    profiles again, from the other direction.
    """
    for from_platform, from_docstudio in zip(
        config.explicit_tiers(PLATFORM, "base_url"),
        config.explicit_tiers(DOCSTUDIO, "base_url"),
        strict=True,
    ):
        # Within one tier the platform block wins: it is the specific answer to
        # this question, where docstudio's is the one inherited from the sibling.
        if from_platform is not None:
            return str(from_platform)
        if from_docstudio is not None:
            return str(from_docstudio)
    return str(config.require(PLATFORM, "base_url"))


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
        base_url=platform_base_url(config),
        api_key=config.require(PLATFORM, "api_key"),
        transport_timeout=timeout,
        logging_level="ERROR",
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


__all__ = [
    "organisation",
    "platform_base_url",
    "platform_client",
]
