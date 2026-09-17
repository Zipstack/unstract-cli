"""`unstract auth login`, `unstract auth whoami` and `unstract docstudio deployment ls`.

`whoami` and `ls` authenticate with a platform key rather than a deployment
key. The two credentials are not interchangeable and neither is going away: a
deployment key runs deployments and cannot describe the account, a platform key
describes the account and lists what is in it but cannot run anything. `login`
stores either, and the LLMWhisperer key, into one profile.

Both platform operations are declared in the vendored docstudio spec, but these
commands take their flags by hand rather than through `spec_options`.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import click

from unstract_cli.app import Context, auth_group, deployment_group, pass_context
from unstract_cli.commands.common import finish
from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    KEY_SOURCES,
    LLMWHISPERER,
    PRODUCTS,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    load_config,
    save_config,
)
from unstract_cli.core.clients import llmwhisperer, translated
from unstract_cli.core.errors import CLIError, ExitCode, remember_secret
from unstract_cli.core.output import diagnostic
from unstract_cli.core.platform import (
    deployment_rows,
    organisation,
    platform_client,
)

#: The fields a deployment listing shows. A row carries many more, run
#: histories included, and `--output table` wraps rather than truncates, so the
#: whole row is unreadable at a terminal. `--full` returns the rows as sent.
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
        # A `.unstract.toml` found by walking up is very likely committed, and
        # writing it would rewrite a teammate's settings, drop the file's
        # comments and narrow its mode. The config layer already declines to
        # trust such a file for credentials.
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
    file-default ladder every read uses, rather than a ladder re-derived here:
    one that misses a tier writes into a profile no later command reads.
    """
    selected = name or ctx.config.active_profile or cfg.default_profile
    if selected is None and cfg.exists and cfg.profiles:
        # Neither the caller nor the file named one, so the name below is this
        # function's own: refusing under it would quote a profile the caller
        # never typed.
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
        # `setdefault` would materialise the name, disarming the "profile not
        # found" check for every later command. Raised as a `ConfigError` so
        # the caller's SAVE_FAILED wrapper still carries the identity back.
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


def _hosts_from_prompts(resolved: ResolvedConfig) -> dict[str, str]:
    """One visible prompt per product, Enter keeping the host the run resolved."""
    return {
        f"{product}.base_url": _prompt(
            f"{product} base URL", default=resolved.get(product, "base_url")
        ).strip()
        for product in (DOCSTUDIO, LLMWHISPERER)
    }


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
    ctx: Context,
    cfg: ConfigFile,
    name: str,
    keys: dict[str, str | None],
    hosts: dict[str, str] | None = None,
) -> ResolvedConfig:
    """The keys being stored, resolved as the profile they will land in.

    Validating through the ordinary config layer means the same base URL the
    profile will run against is the one the keys are checked against. A
    profile that does not exist yet resolves against nothing but the flags and
    the environment, so a stranger's host cannot be the one that answers.
    """
    overrides = {**ctx.overrides, **(hosts or {})}
    for credential, _flag, _label, (product, key) in _CREDENTIALS:
        if keys.get(credential):
            overrides[f"{product}.{key}"] = keys[credential]
    if name in cfg.profiles:
        return ResolvedConfig(file=cfg, profile_name=name, overrides=overrides)
    return ResolvedConfig(file=ConfigFile(), profile_name=None, overrides=overrides)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _host(url: str) -> tuple[str, str, str]:
    """A trailing slash or a capitalised host does not move a key anywhere."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/")


def _stranded_credentials(
    cfg: ConfigFile, name: str, keys: dict[str, str | None], resolved: ResolvedConfig
) -> tuple[list[str], list[tuple[str, ...]]]:
    """Keys the profile holds that this login neither supplied nor checked.

    Storing the host a key was checked against moves every other credential in
    the profile to a host none of them were checked against. Returns what is
    affected, named for a message, and where each one sits.
    """
    profile = cfg.profiles.get(name, {})
    supplied = {
        (product, key)
        for credential, _flag, _label, (product, key) in _CREDENTIALS
        if keys.get(credential)
    }
    labels: list[str] = []
    paths: list[tuple[str, ...]] = []
    for product in PRODUCTS:
        stored = profile.get(product, {}).get("base_url")
        if isinstance(stored, str) and stored.startswith("env:"):
            stored = os.environ.get(stored[4:].strip())
        stored = stored or DEFAULT_BASE_URLS[product]
        if _host(stored) == _host(resolved.get(product, "base_url")):
            continue
        for key in ("api_key", "platform_key"):
            if (product, key) not in supplied and profile.get(product, {}).get(key):
                labels.append(f"{product} {key}")
                paths.append((product, key))
        if product != DOCSTUDIO:
            continue
        for api_name, entry in (profile.get("deployments") or {}).items():
            if isinstance(entry, dict) and entry.get("api_key"):
                labels.append(f"deployment {api_name}")
                paths.append(("deployments", api_name, "api_key"))
    return labels, paths


def _new_profile_name(cfg: ConfigFile, org_id: str, org_name: Any) -> str:
    """Ask for the profile to hold this organisation, until the answer is safe.

    The profile named here is replaced, not merged into: its credentials were
    never checked against this key's host. A typed name that already exists
    therefore needs a confirmation of its own -- all the more when it belongs
    to another organisation, or the guard's remedy would be the overwrite it
    exists to prevent.
    """
    suggested = _slug(str(org_name or "")) or org_id
    while True:
        name = _prompt("Profile name", default=suggested)
        if name not in cfg.profiles:
            return name
        other = cfg.profiles[name].get(DOCSTUDIO, {}).get("org_id")
        owner = f" and belongs to organisation {other}" if other else ""
        if _confirm(
            f"Profile {name!r} already exists{owner}. Replace it?", default=False
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
    help="Overwrite a profile that belongs to a different organisation, or drop "
    "keys a host change leaves unchecked.",
)
@pass_context
def login(ctx: Context, profile: str | None, force: bool, **given: str | None) -> None:
    """Store your keys in a profile, checking each one that can be checked.

    At a terminal this first asks for each product's host, Enter keeping the
    one shown, then for each key in turn -- platform, deployment, LLMWhisperer
    -- and Enter skips one; at least one is needed. Without a terminal, pass
    the keys as flags, one of them as `-` to read it from stdin, and the host
    as `--base-url` if it is not the default.

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
    has passed. Running it again replaces the keys given and keeps the rest,
    unless it stores a different host: keys this run did not check are dropped
    rather than left pointing at a server that never accepted them.

    Exits 3 when a key is rejected and 2 when no key was given.
    """
    if given["platform"] is None:
        given["platform"] = ctx.overrides.get(f"{DOCSTUDIO}.platform_key")
    interactive = not any(value is not None for value in given.values())
    if interactive and not _interactive():
        raise CLIError(
            "No keys were given and stdin is not a terminal, so there is nothing "
            "to ask for them with.",
            ExitCode.USAGE,
            hint="Pass --platform-key, --deployment-key or --llmwhisperer-key, as a "
            "value or as `-` to read one of them from stdin.",
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

    hosts: dict[str, str] = {}
    if interactive:
        click.echo(
            "Welcome to Unstract -- let's connect this machine to your organisation.",
            err=True,
        )
        hosts = _hosts_from_prompts(_validation_config(ctx, cfg, name, {}))
        keys = _keys_from_prompts()
    else:
        keys = _keys_from_flags(given)
    if not any(keys.values()):
        raise CLIError(
            "No key was given; at least one is needed.", ExitCode.USAGE, hint=KEY_SOURCES
        )
    # A key given here is never read back through the config layer that
    # registers one, so nothing else would scrub it out of an error payload.
    for value in keys.values():
        remember_secret(value)

    resolved = _validation_config(ctx, cfg, name, keys, hosts)
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
    if keys["platform"] and not org_id:
        diagnostic(
            "warning: the platform API returned no organization_id; the key is "
            "stored without one.",
            quiet=ctx.quiet,
            verbosity=ctx.verbosity,
        )
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
            cfg.profiles[name] = {}

    stranded, paths = (
        _stranded_credentials(cfg, name, keys, resolved)
        if name == checked_as and name in cfg.profiles
        else ([], [])
    )
    if stranded:
        # Keeping them would leave credentials this run never checked beside the
        # host it stores, and the next command would send them there.
        listed = ", ".join(stranded)
        if not interactive and not force:
            raise CLIError(
                f"Profile {name!r} holds credentials that were not re-supplied, and "
                f"this login stores a different host for them: {listed}.",
                ExitCode.USAGE,
                hint="Supply them in this login, pass --profile <name> to write "
                "another profile, or --force to drop them.",
            )
        if (
            interactive
            and not force
            and not _confirm(
                f"This login stores a different host for {listed}, which "
                f"{name!r} holds but this run did not check. Drop them?",
                default=False,
            )
        ):
            raise CLIError(
                "Nothing was written.",
                ExitCode.USAGE,
                hint="Supply the keys in this login, or pass --profile <name> to "
                "write another profile.",
            )

    block = cfg.profiles.setdefault(name, {})
    for path in paths:
        table = block
        for segment in path[:-1]:
            table = table.get(segment, {})
        table.pop(path[-1], None)
        # An entry left with no key is still listed as a deployment the profile holds.
        if path[0] == "deployments" and not table:
            block["deployments"].pop(path[1], None)
    noticed: set[str] = set()
    for credential, _flag, _label, (product, key) in _CREDENTIALS:
        if not keys[credential]:
            continue
        product_block = block.setdefault(product, {})
        product_block[key] = keys[credential]
        # The key was checked against the host the run resolved -- a flag, the
        # environment, or the profile the login started from. A profile that
        # records any other host, or none, would send the key somewhere it was
        # never checked.
        host_source = resolved.resolution_source(product, "base_url")
        decided_by = host_source["source"]
        if (
            name != checked_as
            or "base_url" not in product_block
            or decided_by == "flag/override"
            or decided_by.startswith("env:")
        ):
            product_block["base_url"] = resolved.get(product, "base_url")
        elif (
            host_source["resolved"]
            and decided_by.startswith("profile -> env:")
            and product not in noticed
        ):
            # The profile holds a reference rather than a host, so the host the
            # keys were checked against can change without the file changing.
            noticed.add(product)
            diagnostic(
                f"{product} base_url resolves through "
                f"${decided_by.removeprefix('profile -> env:')}; the keys were "
                f"checked against {resolved.get(product, 'base_url')}. Run "
                "`unstract auth login` again if that variable changes.",
                quiet=ctx.quiet,
                verbosity=ctx.verbosity,
            )
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
        # The write is a convenience; failing the command would discard the
        # identity it was asked for.
        diagnostic(
            f"note: org_id was not stored -- {exc.reason}. {exc.hint}",
            quiet=ctx.quiet,
            verbosity=ctx.verbosity,
        )
        finish(ctx, identity, meta={"saved": False, "reason": exc.reason})
        return
    except (OSError, ConfigError) as exc:
        raise CLIError(
            f"Resolved the organisation but could not write it: {exc}",
            ExitCode.SAVE_FAILED,
            details=identity,
            hint="`details` carries the identity; set $UNSTRACT_ORG_ID or run "
            f"`unstract config set docstudio org_id {org_id}`.",
        ) from exc

    # `meta` is not rendered by `-o table` or `-o raw`, so the write is said here.
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
        # `--api-key` on this group means a deployment key, and this command
        # authenticates with a platform key: honouring it would send the wrong
        # credential, and ignoring it silently reads as a broken flag.
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

    rows = deployment_rows(page)
    if not full:
        rows = [{field: row.get(field) for field in LISTING_FIELDS} for row in rows]
    # `count` is the server's total across pages, which is not `len(rows)` once
    # there are more deployments than fit one page. This command does not
    # paginate, so both are reported and `next` says whether more would come.
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
