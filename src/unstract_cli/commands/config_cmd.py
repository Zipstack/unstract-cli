"""The `config` command group -- local only, no network calls.

These commands map to no API operation: they operate purely on the local config
layer, and they are how a user or an agent bootstraps every other command.

Nothing here prompts: `init` refuses to clobber an existing file unless
`--force` is passed, rather than asking, so the CLI behaves the same whether or
not a human is watching.
"""

from __future__ import annotations

from typing import Any

import click

from unstract_cli.config import (
    DOCSTUDIO,
    KEY_SOURCES,
    LLMWHISPERER,
    PRODUCTS,
    UNTRUSTED_PROJECT_KEYS,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    init_path,
    load_config,
    save_config,
    settings_for,
    starter_profiles,
)
from unstract_cli.core.clients import llmwhisperer, translated
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import (
    OutputFormat,
    diagnostic,
    emit_result,
    resolve_format,
)
from unstract_cli.core.platform import deployment_rows, organisation, platform_client

#: The probe entry for docstudio's platform key. Named for the credential,
#: not the product: the deployment key sits beside it under `docstudio`.
PLATFORM_KEY = "platform"

#: Keys whose value is never echoed back, even on explicit request: this output
#: is as likely to land in a log or a transcript as on a screen.
_SECRET_KEY_HINTS = ("key", "token", "secret")


def _is_secret(key: str) -> bool:
    return any(hint in key.lower() for hint in _SECRET_KEY_HINTS)


def _fmt(obj: Any) -> OutputFormat:
    """Output format from the root context, defaulting when invoked standalone."""
    return getattr(obj, "output", None) or resolve_format(None)


def _check_product(product: str) -> str:
    if product not in PRODUCTS:
        raise CLIError(
            f"Unknown config target {product!r}.",
            ExitCode.USAGE,
            hint="Valid targets: " + ", ".join(PRODUCTS) + ".",
        )
    return product


def _check_key(product: str, key: str) -> str:
    """A setting a product does not have would be written and never read again."""
    if key not in (known := settings_for(product)):
        raise CLIError(
            f"{product} has no setting {key!r}.",
            ExitCode.USAGE,
            hint=f"Valid keys for {product}: " + ", ".join(known) + ".",
        )
    return key


@click.group(name="config", help="Manage CLI configuration profiles (local only).")
def config_group() -> None:
    """Local configuration management. These commands make no network calls."""


@config_group.command("init")
@click.option(
    "--force", is_flag=True, default=False, help="Overwrite an existing config file."
)
@click.pass_obj
def config_init(obj: Any, force: bool) -> None:
    """Create a starter config file with profile stubs.

    The stubs reference environment variables and hold no keys. To store your
    keys and be done, run `unstract auth login` instead.
    """
    path = init_path()
    if path.exists() and not force:
        # Never prompt: state the situation and the exact flag that resolves it.
        raise CLIError(
            f"Config already exists at {path}.",
            ExitCode.USAGE,
            hint="Pass --force to overwrite it, or edit the file directly.",
        )

    replaced = path.exists()
    new = ConfigFile(
        default_profile="cloud-us", profiles=starter_profiles(), path=path, exists=True
    )
    written = save_config(new, path)
    emit_result(
        {
            "created": str(written),
            "default_profile": "cloud-us",
            "profiles": sorted(new.profiles),
            "replaced_existing": replaced,
            "note": (
                "Credentials use env: indirection, so this file holds no secrets. "
                "Set the referenced environment variables to authenticate, or run "
                "`unstract auth login` to store keys in a profile. " + KEY_SOURCES
            ),
        },
        _fmt(obj),
    )


@config_group.command("list", help="List profiles defined in the config file.")
@click.pass_obj
def config_list(obj: Any) -> None:
    cfg = _loaded(obj)
    emit_result(
        {
            "path": str(cfg.path),
            "exists": cfg.exists,
            "default_profile": cfg.default_profile,
            "profiles": {
                name: {
                    block: sorted(settings) if isinstance(settings, dict) else settings
                    for block, settings in blocks.items()
                }
                for name, blocks in cfg.profiles.items()
            },
        },
        _fmt(obj),
    )


@config_group.command("get")
@click.argument("product")
@click.argument("key")
@click.pass_obj
def config_get(obj: Any, product: str, key: str) -> None:
    """Show a resolved setting, following flag > env > profile > default.

    PRODUCT and KEY are positional -- not flags. Credentials are reported as
    configured or not, never echoed.

    \b
    Examples:
      unstract config get docstudio org_id
      unstract --profile cloud-eu config get llmwhisperer base_url
    """
    _check_product(product)
    try:
        value = _resolved(obj).get(product, key)
    except ConfigError as exc:
        raise CLIError(str(exc), ExitCode.USAGE) from exc

    emit_result(
        {
            "product": product,
            "key": key,
            "value": ("***SET***" if value else None) if _is_secret(key) else value,
            "configured": value is not None,
        },
        _fmt(obj),
    )


@config_group.command("set")
@click.argument("product")
@click.argument("key")
@click.argument("value")
@click.option("--profile", "-p", "profile", default=None, help="Profile to write to.")
@click.option(
    "--deployment",
    default=None,
    metavar="API_NAME",
    help="Store a docstudio api_key for this one deployment only.",
)
@click.pass_obj
def config_set(
    obj: Any,
    product: str,
    key: str,
    value: str,
    profile: str | None,
    deployment: str | None,
) -> None:
    """Set a value in the config file.

    PRODUCT, KEY and VALUE are positional -- not flags. Writes to the active
    profile unless --profile names another.

    \b
    Examples:
      unstract config set docstudio org_id org_ABC123
      unstract config set llmwhisperer api_key 'env:LLMWHISPERER_API_KEY'
      unstract config set docstudio api_key dk_... --deployment invoice-parser

    \b
    A credential can be stored either way. `env:VAR_NAME` records where the
    secret lives rather than the secret itself, which is what a shared machine
    or a CI checkout wants; a literal value is what `auth login` writes, into a
    file created `0600`. Passed here a literal also lands in your shell history.
    """
    _check_product(product)
    _check_key(product, key)
    if deployment is not None and (product, key) != (DOCSTUDIO, "api_key"):
        raise CLIError(
            "--deployment only applies to `docstudio api_key`.",
            ExitCode.USAGE,
            hint="A deployment entry holds nothing but the key that runs it.",
        )
    cfg = _loaded(obj)
    name = profile or getattr(obj, "profile", None) or cfg.default_profile or "cloud-us"

    block = cfg.profiles.setdefault(name, {})
    if deployment is not None:
        block = block.setdefault("deployments", {}).setdefault(deployment, {})
    else:
        block = block.setdefault(product, {})
    block[key] = value
    if not cfg.default_profile:
        cfg.default_profile = name
    written = save_config(cfg)

    warnings = []
    if _is_secret(key) and not value.startswith("env:"):
        warnings.append(
            "Value stored literally, in a file created 0600. Write "
            "`env:VAR_NAME` instead to hold a reference rather than the secret."
        )
    if cfg.is_project_local and key in UNTRUSTED_PROJECT_KEYS:
        warnings.append(
            f"{written} was found by searching upwards rather than named, so "
            f"`{key}` written there is withheld when the config is loaded. Pass "
            f"--config {written} to use it, or write it to the home config."
        )
    if cfg.is_project_local and value.startswith("env:"):
        # Refused for every key, not only the withheld ones, so writing it
        # without a word would report success for a setting that never resolves.
        warnings.append(
            f"{written} was found by searching upwards rather than named, so it "
            f"may not choose which environment variable is read and `{value}` is "
            f"ignored when the config is loaded. Pass --config {written} to use "
            f"it, or write it to the home config."
        )
    warning = " ".join(warnings) or None

    emit_result(
        {
            "profile": name,
            "product": product,
            "key": key,
            "deployment": deployment,
            "path": str(written),
            "warning": warning,
        },
        _fmt(obj),
    )


def _probe(resolved: ResolvedConfig) -> dict[str, Any]:
    """Check each credential against the service, where that is possible.

    LLMWhisperer has a read-only usage endpoint, so its key can be verified for
    real, and so does the platform API -- `whoami` reads nothing but the key
    itself. A deployment has no side-effect-free endpoint -- the only thing to
    call is an execution -- so its entry reports that the settings resolve and
    says plainly that nothing was verified. Claiming otherwise would be worse
    than not checking.

    Keyed by credential rather than by product: docstudio holds two keys that
    are checked differently.
    """
    out: dict[str, Any] = {}
    try:
        with translated(endpoint="get-usage-info"):
            llmwhisperer(resolved).get_usage_info()
    except CLIError as exc:
        out[LLMWHISPERER] = {
            "checked": True,
            "ok": False,
            "detail": exc.message,
            "exit_code": int(exc.exit_code),
        }
    except ConfigError as exc:
        out[LLMWHISPERER] = {"checked": False, "ok": False, "detail": str(exc)}
    else:
        out[LLMWHISPERER] = {
            "checked": True,
            "ok": True,
            "detail": "The key was accepted by the usage endpoint.",
        }

    try:
        with translated(endpoint="whoami"):
            identity = platform_client(resolved).whoami()
    except CLIError as exc:
        out[PLATFORM_KEY] = {
            "checked": True,
            "ok": False,
            "detail": exc.message,
            "exit_code": int(exc.exit_code),
        }
    except ConfigError as exc:
        # Null, not False: a platform key is optional -- a caller holding only a
        # deployment key is the common case -- so an absent one is a report
        # rather than a failure, and must not decide this command's exit code.
        out[PLATFORM_KEY] = {"checked": False, "ok": None, "detail": str(exc)}
    else:
        out[PLATFORM_KEY] = {
            "checked": True,
            "ok": True,
            # The organisation is the reason to hold this key, so the probe
            # reports which one answered rather than only that one did.
            "organization_id": identity.get("organization_id"),
            "detail": "The key was accepted, and resolved to an organisation.",
        }

    resolves = all(
        resolved.get(DOCSTUDIO, key) for key in ("org_id", "api_key", "base_url")
    )
    out[DOCSTUDIO] = {
        "checked": False,
        # Null, not True: nothing was called, so there is no verdict to report.
        # A `true` beside `checked: false` reads as a live check that passed.
        "ok": None,
        "resolved": resolves,
        "detail": (
            "Credentials resolve (org and key present) but were NOT verified -- "
            "the deployment API has no side-effect-free endpoint to call, so a "
            "wrong key is only discovered by running a deployment."
            if resolves
            else "Organisation or key is missing; nothing was called."
        ),
    }
    return out


def _stale_deployments(resolved: ResolvedConfig, names: list[str]) -> list[str]:
    """Which deployment entries name a deployment the server no longer has.

    An API name is editable server-side, so an entry written under one can be
    orphaned without anything local changing. Raises when the listing cannot
    be asked: a check that was requested and did not run must not read as a
    check that passed.
    """
    org_id = organisation(resolved)
    client = platform_client(resolved, org_id)
    with translated(endpoint="api/deployment/"):
        return [
            name
            for name in names
            if not deployment_rows(client.list_deployments(org_id, api_name=name))
        ]


@config_group.command("doctor", help="Diagnose how each setting resolves.")
@click.option(
    "--probe/--no-probe",
    default=False,
    help="Also check the resolved credentials against the service.",
)
@click.pass_obj
def config_doctor(obj: Any, probe: bool) -> None:
    """Report where each setting resolves from, without echoing any secret.

    Answers the question that costs the most time: the CLI reports a key as "not
    configured", but you set it -- where is it looking? For `env:` references it
    says whether the variable is present in THIS process, a shell `export` in a
    login profile the CLI never inherited being the classic trap.

    Resolution is answered offline. --probe adds the second question -- does the
    resolved key work -- which needs the network, so it is opt-in. With a
    platform key it also checks that every deployment entry still names a
    deployment the organisation has.

    Exits 0 only when nothing it checked failed. A setting that is simply not
    configured is a report, not a failure; a setting that points somewhere and
    does not arrive -- an unset `env:` variable, an unknown profile, a probe the
    service rejected -- exits non-zero, because a setup script branches on that.
    """
    resolved = _resolved(obj)
    problems: list[str] = []
    products: dict[str, Any] = {}
    for product in PRODUCTS:
        entry: dict[str, Any] = {}
        for key in settings_for(product):
            try:
                entry[key] = resolved.resolution_source(product, key)
            except ConfigError as exc:
                entry[key] = {"resolved": False, "source": "unset", "detail": str(exc)}
            if detail := entry[key].get("detail"):
                problems.append(f"{product}.{key}: {detail}")
        for stray in resolved.unknown_settings(product):
            # Nothing reads it, so it is a setting the user believes is in force.
            problems.append(f"{product}.{stray}: not a setting {product} has.")
        products[product] = entry

    try:
        deployments = list(resolved.deployment_names())
    except ConfigError as exc:
        deployments = []
        problems.append(str(exc))
    for api_name in deployments:
        # A deployment entry is a second place a project file can name a key --
        # and a run falls back to the profile's key silently.
        if detail := resolved.withheld_detail("deployments", api_name, "api_key"):
            problems.append(f"deployment {api_name}: {detail}")
        try:
            # Resolved the way a run resolves it: that an entry is *listed* says
            # nothing about whether the key behind it arrives.
            resolved.deployment_key(api_name)
        except ConfigError as exc:
            problems.append(f"deployment {api_name}: {exc}")

    report: dict[str, Any] = {
        "active_profile": resolved.active_profile,
        "config_path": str(resolved.file.path),
        "config_exists": resolved.file.exists,
        "products": products,
        "deployments": deployments,
    }
    if any(
        not entry["api_key"]["resolved"]
        for entry in products.values()
        if "api_key" in entry
    ):
        # The next question after "no key" is always where one comes from. The
        # field name avoids the word the payload scrubber redacts on.
        report["getting_started"] = KEY_SOURCES
    if probe:
        report["probe"] = _probe(resolved)
        problems += [
            f"probe {name}: {result.get('detail')}"
            for name, result in report["probe"].items()
            if result["ok"] is False
        ]
        notes = []
        platform_probe = report["probe"][PLATFORM_KEY]
        if deployments and not platform_probe["checked"]:
            # The flag was explicit, so the skip is said rather than silent.
            notes.append(
                "probe: deployment entries were not checked against the "
                f"organisation -- no platform key resolves for profile "
                f"{resolved.active_profile!r}."
            )
        elif deployments and platform_probe["ok"] is not True:
            # The listing would send the key that was just rejected, so it can
            # only fail the same way and report the same failure twice.
            notes.append(
                "probe: deployment entries were not checked against the "
                "organisation -- the platform key itself did not pass."
            )
        elif deployments:
            try:
                stale = _stale_deployments(resolved, deployments)
            except (CLIError, ConfigError) as exc:
                problems.append(f"probe deployments: {exc}")
            else:
                # A warning, not a problem: the entry is harmless until it is
                # run, and the server is the only authority on what it is
                # called now.
                report["stale_deployments"] = stale
                notes += [
                    f"warning: no deployment is called {api_name!r} any more; "
                    "run `unstract docstudio deployment ls` for the current names."
                    for api_name in stale
                ]
        for note in notes:
            diagnostic(
                note,
                quiet=getattr(obj, "quiet", False),
                verbosity=getattr(obj, "verbosity", 0),
            )

    if problems:
        report["problems"] = problems
        more = "" if len(problems) == 1 else f" (+{len(problems) - 1} more)"
        raise CLIError(
            f"{len(problems)} configuration check(s) failed: {problems[0]}{more}",
            ExitCode.GENERIC,
            details=report,
            hint=(
                "`details` carries the whole report, including where each setting "
                "resolved from."
            ),
        )
    emit_result(report, _fmt(obj))


def _loaded(obj: Any) -> ConfigFile:
    """The config file, with its warnings reported.

    A `config` subcommand may run with no root context to have loaded the file,
    so it reports here what the file warned about -- reading it silently would
    make these the quietest commands in the CLI about their own subject.
    """
    cfg = load_config()
    for warning in cfg.warnings:
        diagnostic(
            warning,
            quiet=getattr(obj, "quiet", False),
            verbosity=getattr(obj, "verbosity", 0),
        )
    return cfg


def _resolved(obj: Any) -> ResolvedConfig:
    """The root context's config, or a freshly loaded one when invoked standalone."""
    # Already loaded means the context already reported its warnings.
    if (existing := getattr(obj, "_config", None)) is not None:
        return existing
    return ResolvedConfig(file=_loaded(obj), profile_name=getattr(obj, "profile", None))


__all__ = ["config_group"]
