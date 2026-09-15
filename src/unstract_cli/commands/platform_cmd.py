"""`unstract auth login`, `unstract auth whoami` and `unstract docstudio deployment ls`.

`whoami` and `ls` authenticate with a platform key rather than a deployment
key. The two credentials are not interchangeable and neither is going away: a
deployment key runs deployments and cannot describe the account, a platform key
describes the account and lists what is in it but cannot run anything. `login`
stores either, and the LLMWhisperer key, into one profile.

No OpenAPI spec is vendored for the platform API, so these declare their flags
by hand rather than through `spec_options`.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import click

from unstract_cli.app import Context, auth_group, deployment_group, pass_context
from unstract_cli.commands.common import finish
from unstract_cli.config import (
    DOCSTUDIO,
    KEY_SOURCES,
    LLMWHISPERER,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    load_config,
    save_config,
)
from unstract_cli.core.clients import llmwhisperer, translated
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


def _writable_config() -> ConfigFile:
    """The config file a command may write to, or a refusal saying why not."""
    cfg = load_config()
    if cfg.is_project_local:
        # A `.unstract.toml` found by walking up from the working directory is
        # very likely committed. Rewriting it would replace a teammate's
        # `org_id` with this caller's, drop every comment (the file is
        # re-serialised, not patched) and narrow its mode to 0600 -- a dirty,
        # mode-changed, semantically different tracked file. The config layer
        # already declines to *trust* this file for credentials; declining to
        # *write* it is the same judgement.
        raise SaveDeclinedError(
            f"the config at {cfg.path} is project-local",
            hint="Nothing was written. Name the file to write instead: "
            "`unstract --config <path> ...`.",
        )
    return cfg


def _profile_to_write(
    ctx: Context, cfg: ConfigFile, name: str | None = None, *, create: bool = False
) -> str:
    """The profile a write lands in: the one named, else the one the run is using.

    The fallback is `ResolvedConfig.active_profile`, the same flag > env >
    file-default ladder every read uses. Re-deriving it here is what dropped the
    `$UNSTRACT_PROFILE` tier once, so the organisation was written into a
    profile no later command read.
    """
    selected = name or ctx.config.active_profile or cfg.default_profile
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

    selected = selected or "cloud-us"
    if not create and cfg.exists and cfg.profiles and selected not in cfg.profiles:
        # `setdefault` would create it. That is not a convenience: the profile
        # lookup raises "Profile not found" for a typo today, and materialising
        # the name silently disarms that check for every later command, which
        # then resolves the built-in production defaults instead.
        #
        # Raised as `ConfigError` so the caller's SAVE_FAILED wrapper carries
        # the identity back: the key was resolved, only the note-taking failed.
        known = ", ".join(sorted(cfg.profiles)) or "none"
        raise ConfigError(
            f"profile {selected!r} is not in {cfg.path} "
            f"(known profiles: {known}); create it with `config set` first"
        )
    return selected


def _store_organisation(ctx: Context, org_id: str) -> dict[str, Any]:
    """Write the resolved organisation into the profile the run is using.

    It lands on the docstudio block because that is where every consumer reads
    it from.
    """
    cfg = _writable_config()
    name = _profile_to_write(ctx, cfg)
    cfg.profiles.setdefault(name, {}).setdefault(DOCSTUDIO, {})["org_id"] = org_id
    if not cfg.default_profile:
        cfg.default_profile = name
    return {"profile": name, "path": str(save_config(cfg))}


# --------------------------------------------------------------------------- #
# auth login
# --------------------------------------------------------------------------- #

#: Each credential `login` takes, in the order it asks for them: its flag, the
#: prompt a terminal sees, and where in a profile it is stored.
_CREDENTIALS = (
    ("platform", "--platform-key", "Platform key", (DOCSTUDIO, "platform_key")),
    ("deployment", "--deployment-key", "Deployment key", (DOCSTUDIO, "api_key")),
    ("llmwhisperer", "--llmwhisperer-key", "LLMWhisperer key", (LLMWHISPERER, "api_key")),
)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _prompt(text: str, **kwargs: Any) -> Any:
    # Prompts go to stderr so `-o json` output on stdout stays parseable.
    return click.prompt(text, err=True, **kwargs)


def _confirm(text: str, **kwargs: Any) -> bool:
    return click.confirm(text, err=True, **kwargs)


def _keys_from_flags(given: dict[str, str | None]) -> dict[str, str | None]:
    """Flag values as keys, with `-` read from stdin -- at most one of them."""
    from_stdin = [name for name, value in given.items() if value == "-"]
    if len(from_stdin) > 1:
        flags = ", ".join(flag for name, flag, *_ in _CREDENTIALS if name in from_stdin)
        raise CLIError(
            f"Only one key can be read from stdin, and {flags} each ask for it.",
            ExitCode.USAGE,
            hint="Pass the others as values.",
        )
    keys = dict(given)
    if from_stdin:
        keys[from_stdin[0]] = sys.stdin.read().strip()
    return keys


def _keys_from_prompts() -> dict[str, str | None]:
    """One hidden, skippable prompt per credential, in the documented order."""
    keys: dict[str, str | None] = {}
    for name, _flag, label, _setting in _CREDENTIALS:
        keys[name] = (
            _prompt(
                f"{label} (Enter to skip)",
                hide_input=True,
                default="",
                show_default=False,
            ).strip()
            or None
        )
    return keys


def _validation_config(
    ctx: Context, cfg: ConfigFile, name: str, keys: dict[str, str | None]
) -> ResolvedConfig:
    """The keys being stored, resolved as the profile they will land in.

    Validating through the ordinary config layer means the same base URL the
    profile will run against is the one the keys are checked against. A
    profile that does not exist yet resolves against nothing but the flags and
    the environment, so a stranger's host cannot be the one that answers.
    """
    overrides = dict(ctx.overrides)
    for credential, _flag, _label, (product, key) in _CREDENTIALS:
        if keys.get(credential):
            overrides[f"{product}.{key}"] = keys[credential]
    if name in cfg.profiles:
        return ResolvedConfig(file=cfg, profile_name=name, overrides=overrides)
    return ResolvedConfig(file=ConfigFile(), profile_name=None, overrides=overrides)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _new_profile_name(cfg: ConfigFile, org_id: str, org_name: Any) -> str:
    """Ask for the profile to hold this organisation, until the answer is safe.

    A typed name that already belongs to another organisation would let the
    guard's own remedy do the overwrite it exists to prevent, so such a name
    needs a second confirmation of its own.
    """
    suggested = _slug(str(org_name or "")) or org_id
    while True:
        name = _prompt("Profile name", default=suggested)
        other = cfg.profiles.get(name, {}).get(DOCSTUDIO, {}).get("org_id")
        if not other or other == org_id:
            return name
        if _confirm(
            f"Profile {name!r} belongs to organisation {other}. Overwrite it?",
            default=False,
        ):
            return name


@auth_group.command("login")
@click.option("--profile", "-p", "profile", default=None, help="Profile to write.")
@click.option(
    "--platform-key",
    "platform",
    default=None,
    metavar="KEY",
    help="The platform key to store; `-` reads it from stdin.",
)
@click.option(
    "--deployment-key",
    "deployment",
    default=None,
    metavar="KEY",
    help="The deployment key to store; `-` reads it from stdin.",
)
@click.option(
    "--llmwhisperer-key",
    "llmwhisperer",
    default=None,
    metavar="KEY",
    help="The LLMWhisperer key to store; `-` reads it from stdin.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite a profile that belongs to a different organisation.",
)
@pass_context
def login(ctx: Context, profile: str | None, force: bool, **given: str | None) -> None:
    """Store your keys in a profile, checking each one that can be checked.

    At a terminal this asks for each key in turn -- platform, deployment,
    LLMWhisperer -- and Enter skips one; at least one is needed. Without a
    terminal, pass the keys as flags, one of them as `-` to read it from stdin.

    \b
    Examples:
      unstract auth login
      unstract auth login --profile staging
      echo "$PLATFORM_KEY" | unstract auth login --platform-key -
      unstract auth login --deployment-key "$DEPLOYMENT_KEY" --llmwhisperer-key -

    The platform key is checked with `whoami` and the organisation it resolves
    is stored beside it; the LLMWhisperer key is checked against the usage
    endpoint. A deployment key has no side-effect-free endpoint, so it is stored
    as given and reported as unverified. Nothing is written until every check
    has passed. Running it again replaces the keys given and keeps the rest.

    Exits 3 when a key is rejected and 2 when no key was given.
    """
    if given["platform"] is None:
        given["platform"] = ctx.overrides.get(f"{DOCSTUDIO}.platform_key")
    if any(value is not None for value in given.values()):
        keys = _keys_from_flags(given)
        interactive = False
    elif _interactive():
        keys = _keys_from_prompts()
        interactive = True
    else:
        raise CLIError(
            "No keys were given and stdin is not a terminal, so there is nothing "
            "to ask for them with.",
            ExitCode.USAGE,
            hint="Pass --platform-key, --deployment-key or --llmwhisperer-key, as a "
            "value or as `-` to read one of them from stdin.",
        )
    if not any(keys.values()):
        raise CLIError(
            "No key was given; at least one is needed.", ExitCode.USAGE, hint=KEY_SOURCES
        )

    try:
        cfg = _writable_config()
        name = _profile_to_write(ctx, cfg, profile, create=True)
    except SaveDeclinedError as exc:
        raise CLIError(
            f"Cannot store keys: {exc.reason}.", ExitCode.USAGE, hint=exc.hint
        ) from exc
    except ConfigError as exc:
        raise CLIError(str(exc), ExitCode.USAGE) from exc

    resolved = _validation_config(ctx, cfg, name, keys)
    timeout = getattr(ctx, "transport_timeout", None)
    result: dict[str, Any] = {"profile": name, "path": None}
    identity: dict[str, Any] = {}
    if keys["platform"]:
        with translated(endpoint="whoami"):
            identity = platform_client(resolved, timeout=timeout).whoami()
        result["organization_id"] = identity.get("organization_id")
        result["organization_name"] = identity.get("organization_name")
    if keys["llmwhisperer"]:
        with translated(endpoint="get-usage-info"):
            llmwhisperer(resolved).get_usage_info()
    for credential, *_ in _CREDENTIALS:
        if not keys[credential]:
            result[credential] = "skipped"
        elif credential == "deployment":
            result[credential] = "stored"
        else:
            result[credential] = "verified"
    if keys["deployment"]:
        result["note"] = (
            "A deployment key has no side-effect-free endpoint to check it "
            "against, so it was stored as given."
        )

    org_id = str(identity["organization_id"]) if identity.get("organization_id") else None
    checked_as = name
    existing = cfg.profiles.get(name, {}).get(DOCSTUDIO, {}).get("org_id")
    if org_id and existing and existing != org_id:
        # Silently overwriting would repoint every deployment entry in the
        # profile at an organisation none of them belong to.
        found = f"{org_id} ({identity.get('organization_name')})"
        if not interactive and not force:
            raise CLIError(
                f"Profile {name!r} belongs to organisation {existing}, and this "
                f"platform key belongs to {found}.",
                ExitCode.USAGE,
                hint=f"Pass --profile <name> to write another profile, or --force "
                f"to overwrite {name!r}.",
            )
        if interactive and _confirm(
            f"Profile {name!r} belongs to organisation {existing}; this key belongs "
            f"to {found}. Create a new profile for it instead of overwriting?",
            default=True,
        ):
            name = _new_profile_name(cfg, org_id, identity.get("organization_name"))
            result["profile"] = name

    block = cfg.profiles.setdefault(name, {})
    for credential, _flag, _label, (product, key) in _CREDENTIALS:
        if not keys[credential]:
            continue
        product_block = block.setdefault(product, {})
        product_block[key] = keys[credential]
        # The key was checked against the host the run resolved -- a flag, or
        # the profile the login started from. A profile that records any other
        # host, or none, would send the key somewhere it was never checked.
        if (
            name != checked_as
            or "base_url" not in product_block
            or ctx.overrides.get(f"{product}.base_url")
        ):
            product_block["base_url"] = resolved.get(product, "base_url")
    if org_id:
        block.setdefault(DOCSTUDIO, {})["org_id"] = org_id
    if not cfg.default_profile:
        cfg.default_profile = name
    try:
        result["path"] = str(save_config(cfg))
    except (OSError, ConfigError) as exc:
        raise CLIError(
            f"The keys were accepted but could not be written: {exc}",
            ExitCode.SAVE_FAILED,
        ) from exc

    diagnostic(
        f"wrote profile {name!r} in {result['path']}",
        quiet=ctx.quiet,
        verbosity=ctx.verbosity,
    )
    finish(ctx, result)


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
            hint="Pass --platform-key instead, set $UNSTRACT_PLATFORM_KEY, or "
            "add `platform_key` to the [profiles.<name>.docstudio] block. A "
            "deployment key runs a deployment; a platform key describes the "
            "account.",
        )

    org_id = organisation(ctx.config)
    client = platform_client(
        ctx.config,
        org_id,
        timeout=getattr(ctx, "transport_timeout", None),
    )
    with translated(endpoint="api/deployment/"):
        page = client.list_deployments(org_id, api_name=api_name)

    rows = page.get("results") or []
    if not full:
        rows = [{field: row.get(field) for field in LISTING_FIELDS} for row in rows]
    # `count` is the server's total across pages, which is not `len(rows)` once
    # the account has more deployments than fit one page. Both are reported
    # rather than one standing in for the other, and `next` says whether asking
    # again would return more -- this command does not paginate on the caller's
    # behalf, so saying so is the honest surface.
    finish(
        ctx,
        {"results": rows},
        meta={
            "shown": len(rows),
            "count": page.get("count"),
            "more": bool(page.get("next")),
        },
    )


__all__ = ["SaveDeclinedError", "login", "ls", "whoami"]
