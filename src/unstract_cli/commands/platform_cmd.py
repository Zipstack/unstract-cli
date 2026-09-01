"""`unstract auth whoami` and `unstract docstudio deployment ls`.

Both authenticate with a platform key rather than a deployment key. The two
credentials are not interchangeable and neither is going away: a deployment key
runs one deployment and cannot describe the account, a platform key describes
the account and lists what is in it but cannot run anything.

No OpenAPI spec is vendored for the platform API, so these declare their flags
by hand rather than through `spec_options`.
"""

from __future__ import annotations

from typing import Any

import click

from unstract_cli.app import Context, auth_group, deployment_group, pass_context
from unstract_cli.commands.common import finish
from unstract_cli.config import DOCSTUDIO, load_config, save_config
from unstract_cli.core.clients import translated
from unstract_cli.core.platform import organisation, platform_client

#: The fields a deployment listing shows. The server sends around fifteen per
#: row, including run histories; `--output table` wraps rather than truncates,
#: so the whole row is unreadable at a terminal. Narrowed here rather than
#: silently cut off downstream -- `--full` returns the rows as sent.
LISTING_FIELDS = ("api_name", "display_name", "id", "is_active", "api_endpoint")


def _store_organisation(obj: Any, org_id: str) -> dict[str, Any]:
    """Write the resolved organisation into the active profile.

    It lands on the docstudio block because that is where every consumer reads
    it from -- deployment URLs and aliases both -- and a second copy under the
    platform block would be one more thing to keep in agreement.
    """
    cfg = load_config()
    name = getattr(obj, "profile", None) or cfg.default_profile or "cloud-us"
    cfg.profiles.setdefault(name, {}).setdefault(DOCSTUDIO, {})["org_id"] = org_id
    if not cfg.default_profile:
        cfg.default_profile = name
    return {"profile": name, "path": str(save_config(cfg))}


@auth_group.command("whoami")
@click.option(
    "--save/--no-save",
    default=True,
    help="Write the resolved organisation into the active profile.",
)
@pass_context
def whoami(ctx: Context, save: bool) -> None:
    """Resolve which organisation your platform key belongs to.

    The organisation is otherwise only discoverable by reading it out of a
    web-app URL, and every other command needs it. Resolving it here and storing
    it means it is supplied once rather than pasted.

    \b
    Examples:
      export UNSTRACT_PLATFORM_KEY=...
      unstract auth whoami
      unstract auth whoami --no-save     # validate the key, change nothing

    Exits 3 when the key is rejected, so a setup script can branch on it without
    reading the message.
    """
    client = platform_client(ctx.config)
    with translated(endpoint="whoami"):
        identity = client.whoami()

    meta = {"saved": False}
    if save and (org_id := identity.get("organization_id")):
        meta = {"saved": True, **_store_organisation(ctx, str(org_id))}
    finish(ctx, identity, meta=meta)


@deployment_group.command("ls")
@click.option(
    "--api-name",
    default=None,
    help="Return only the deployment with this exact API name.",
)
@click.option(
    "--full/--no-full",
    default=False,
    help=f"Return every field the server sends, not just {', '.join(LISTING_FIELDS)}.",
)
@pass_context
def ls(ctx: Context, api_name: str | None, full: bool) -> None:
    """List the API deployments in your organisation.

    Answers what a deployment is called, which is the one thing `deployment run`
    needs and the UI is the only other place to find. Authenticates with the
    platform key, not the deployment key.

    \b
    Examples:
      unstract docstudio deployment ls
      unstract docstudio deployment ls --api-name invoice-parser
      unstract docstudio deployment ls --full
    """
    client = platform_client(ctx.config, organisation(ctx.config))
    with translated(endpoint="api/deployment/"):
        rows = client.list_api_deployments(api_name=api_name)

    if not full:
        rows = [{field: row.get(field) for field in LISTING_FIELDS} for row in rows]
    finish(ctx, {"results": rows}, meta={"count": len(rows)})


__all__ = ["ls", "whoami"]
