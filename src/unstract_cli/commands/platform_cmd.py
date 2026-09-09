"""`unstract auth whoami` and `unstract docstudio deployment ls`.

Both authenticate with a platform key rather than a deployment key. The two
credentials are not interchangeable and neither is going away: a deployment key
runs deployments and cannot describe the account, a platform key describes the
account and lists what is in it but cannot run anything.

No OpenAPI spec is vendored for the platform API, so these declare their flags
by hand rather than through `spec_options`.
"""

from __future__ import annotations

from typing import Any

import click

from unstract_cli.app import Context, auth_group, deployment_group, pass_context
from unstract_cli.commands.common import finish
from unstract_cli.config import DOCSTUDIO, ConfigError, load_config, save_config
from unstract_cli.core.clients import translated
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import diagnostic
from unstract_cli.core.platform import organisation, platform_client

#: The fields a deployment listing shows. The server sends fifteen per row,
#: including run histories; `--output table` wraps rather than truncates, so the
#: whole row is unreadable at a terminal. Narrowed here rather than silently cut
#: off downstream -- `--full` returns the rows as sent.
LISTING_FIELDS = ("api_name", "display_name", "id", "is_active", "api_endpoint")


class SaveDeclinedError(Exception):
    """The organisation resolved, and storing it was deliberately skipped.

    Distinct from a write that *failed*: there is nothing to retry and nothing
    is wrong. The call succeeded, so it exits 0 and reports the identity, with
    `meta.saved` false and `reason` saying which rule declined -- the same shape
    `--no-save` already produces.
    """

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


def _store_organisation(ctx: Context, org_id: str) -> dict[str, Any]:
    """Write the resolved organisation into the profile the run is using.

    The profile name comes from `ResolvedConfig.active_profile`, which is the
    same flag > env > file-default ladder every read uses. Re-deriving it here
    is what dropped the `$UNSTRACT_PROFILE` tier, so the organisation was
    written into a profile no later command read.

    It lands on the docstudio block because that is where every consumer reads
    it from -- deployment URLs and aliases both -- and a second copy under the
    platform block would be one more thing to keep in agreement.
    """
    cfg = load_config()
    if cfg.is_project_local:
        # A `.unstract.toml` found by walking up from the working directory is
        # very likely committed. Rewriting it would replace a teammate's
        # `org_id` with this caller's, drop every comment (the file is
        # re-serialised, not patched) and narrow its mode to 0600 -- a dirty,
        # mode-changed, semantically different tracked file, from a command
        # named `whoami`. The config layer already declines to *trust* this
        # file for credentials; declining to *write* it is the same judgement.
        raise SaveDeclinedError(
            f"the config at {cfg.path} is project-local",
            hint="Nothing was written. Rerun with --no-save to silence this, "
            "or store it elsewhere: `unstract --config <path> config set "
            f"docstudio org_id {org_id}`.",
        )

    selected = ctx.config.active_profile or cfg.default_profile
    if selected is None and cfg.exists and cfg.profiles:
        # Neither the caller nor the file named one, so the "cloud-us" literal
        # below is this function's own invention -- refusing under that name
        # would quote a profile the caller never typed, and advising `config
        # set` would create a third one that shadows theirs as the new default.
        if len(cfg.profiles) == 1:
            selected = next(iter(cfg.profiles))
        else:
            known = ", ".join(sorted(cfg.profiles))
            raise ConfigError(
                f"no profile is selected and {cfg.path} names no default "
                f"(known profiles: {known}); rerun with `-p <name>`"
            )

    name = selected or "cloud-us"
    if cfg.exists and cfg.profiles and name not in cfg.profiles:
        # `setdefault` would create it. That is not a convenience: the profile
        # lookup raises "Profile not found" for a typo today, and materialising
        # the name silently disarms that check for every later command, which
        # then resolves the built-in production defaults instead.
        #
        # Raised as `ConfigError` so the caller's SAVE_FAILED wrapper carries
        # the identity back: the key was resolved, only the note-taking failed.
        known = ", ".join(sorted(cfg.profiles)) or "none"
        raise ConfigError(
            f"profile {name!r} is not in {cfg.path} "
            f"(known profiles: {known}); create it with `config set` first"
        )

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
    client = platform_client(ctx.config, timeout=getattr(ctx, "transport_timeout", None))
    with translated(endpoint="whoami"):
        identity = client.whoami()

    if not save:
        finish(ctx, identity, meta={"saved": False, "reason": "--no-save"})
        return

    org_id = identity.get("organization_id")
    if not org_id:
        # Distinguished from --no-save: the caller asked to store and there was
        # nothing to store, which the next command will fail on.
        diagnostic(
            "warning: the platform API returned no organization_id; nothing was stored.",
            quiet=ctx.quiet,
            verbosity=ctx.verbosity,
        )
        finish(ctx, identity, meta={"saved": False, "reason": "no organization_id"})
        return

    try:
        written = _store_organisation(ctx, str(org_id))
    except SaveDeclinedError as exc:
        # The identity is what was asked for; the write was a convenience this
        # config layout declines. Reporting the whole command as a usage error
        # would fail the CLI's documented first command in any checkout holding
        # a committed `.unstract.toml`, and throw the identity away with it.
        diagnostic(
            f"note: org_id was not stored -- {exc.reason}. {exc.hint}",
            quiet=ctx.quiet,
            verbosity=ctx.verbosity,
        )
        finish(ctx, identity, meta={"saved": False, "reason": exc.reason})
        return
    except (OSError, ConfigError) as exc:
        # The read succeeded; only the convenience write failed. Losing the
        # identity to a full disk would report a working key as a total failure,
        # and SAVE_FAILED exists for exactly this shape.
        raise CLIError(
            f"Resolved the organisation but could not write it: {exc}",
            ExitCode.SAVE_FAILED,
            details=identity,
            hint="`details` carries the identity; set $UNSTRACT_ORG_ID or run "
            f"`unstract config set docstudio org_id {org_id}`.",
        ) from exc

    # `meta` is not rendered by `-o table` or `-o raw`, so a human would
    # otherwise see nothing about a file this command just wrote.
    diagnostic(
        f"wrote org_id={org_id} to profile {written['profile']!r} in {written['path']}",
        quiet=ctx.quiet,
        verbosity=ctx.verbosity,
    )
    finish(ctx, identity, meta={"saved": True, **written})


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
    platform key, not the deployment key -- but takes the same `--base-url` as
    its sibling commands, since one deployment serves both.

    \b
    Examples:
      unstract docstudio deployment ls
      unstract docstudio deployment ls --api-name invoice-parser
      unstract docstudio deployment ls --full
    """
    if ctx.config.overrides.get(f"{DOCSTUDIO}.api_key") is not None:
        # `--api-key` on the docstudio group means a *deployment* key, and this
        # command authenticates with a platform key. Honouring it would send a
        # deployment key to the platform API; ignoring it silently and then
        # reporting the platform key as missing is what shipped, and reads as a
        # broken flag rather than the wrong credential.
        raise CLIError(
            "`--api-key` on `docstudio` is a deployment key; "
            "`deployment ls` authenticates with a platform key.",
            ExitCode.USAGE,
            hint="Drop the flag and set $UNSTRACT_PLATFORM_KEY, or add "
            "`api_key` to the [profiles.<name>.platform] block. A deployment "
            "key runs a deployment; a platform key describes the account.",
        )

    client = platform_client(
        ctx.config,
        organisation(ctx.config),
        timeout=getattr(ctx, "transport_timeout", None),
    )
    with translated(endpoint="api/deployment/"):
        rows = client.list_api_deployments(api_name=api_name)

    if not full:
        rows = [{field: row.get(field) for field in LISTING_FIELDS} for row in rows]
    finish(ctx, {"results": rows}, meta={"count": len(rows)})


__all__ = ["SaveDeclinedError", "ls", "whoami"]
