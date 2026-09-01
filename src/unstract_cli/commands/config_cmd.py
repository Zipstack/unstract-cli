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
    PLATFORM,
    PRODUCTS,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    config_path,
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
from unstract_cli.core.platform import platform_client

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


@click.group(name="config", help="Manage CLI configuration profiles (local only).")
def config_group() -> None:
    """Local configuration management. These commands make no network calls."""


@config_group.command("init", help="Create a starter config file with profile stubs.")
@click.option(
    "--force", is_flag=True, default=False, help="Overwrite an existing config file."
)
@click.pass_obj
def config_init(obj: Any, force: bool) -> None:
    path = config_path()
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
                "Set the referenced environment variables to authenticate. " + KEY_SOURCES
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
@click.pass_obj
def config_set(obj: Any, product: str, key: str, value: str, profile: str | None) -> None:
    """Set a value in the config file.

    PRODUCT, KEY and VALUE are positional -- not flags. Writes to the active
    profile unless --profile names another.

    \b
    Examples:
      unstract config set docstudio org_id org_ABC123
      unstract config set llmwhisperer api_key 'env:LLMWHISPERER_API_KEY'

    \b
    Prefer `env:VAR_NAME` for credentials: the file then records where the secret
    lives rather than the secret itself, and a literal value also lands in your
    shell history.
    """
    _check_product(product)
    cfg = _loaded(obj)
    name = profile or getattr(obj, "profile", None) or cfg.default_profile or "cloud-us"

    cfg.profiles.setdefault(name, {}).setdefault(product, {})[key] = value
    if not cfg.default_profile:
        cfg.default_profile = name
    written = save_config(cfg)

    warning = None
    if _is_secret(key) and not value.startswith("env:"):
        warning = (
            "Value stored literally. Prefer `env:VAR_NAME` so the config file holds "
            "a reference rather than the secret itself."
        )

    emit_result(
        {
            "profile": name,
            "product": product,
            "key": key,
            "path": str(written),
            "warning": warning,
        },
        _fmt(obj),
    )


def _probe(resolved: ResolvedConfig) -> dict[str, Any]:
    """Check each product's credentials against the service, where that is possible.

    LLMWhisperer has a read-only usage endpoint, so its key can be verified for
    real, and so does the platform API -- `whoami` reads nothing but the key
    itself. A deployment has no side-effect-free endpoint -- the only thing to
    call is an execution -- so its entry reports that the settings resolve and
    says plainly that nothing was verified. Claiming otherwise would be worse
    than not checking.
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
        out[PLATFORM] = {
            "checked": True,
            "ok": False,
            "detail": exc.message,
            "exit_code": int(exc.exit_code),
        }
    except ConfigError as exc:
        # Null, not False: a platform key is optional -- a caller holding only a
        # deployment key is the common case -- so an absent one is a report
        # rather than a failure, and must not decide this command's exit code.
        out[PLATFORM] = {"checked": False, "ok": None, "detail": str(exc)}
    else:
        out[PLATFORM] = {
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
    resolved key work -- which needs the network, so it is opt-in.

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
        products[product] = entry

    try:
        aliases = list(resolved.deployment_aliases())
    except ConfigError as exc:
        aliases = []
        problems.append(str(exc))
    for alias in aliases:
        # An alias carries a key of its own, so it is a second place a project
        # file can name one -- and it falls back to the profile's key silently.
        if detail := resolved.withheld_detail("deployments", alias, "api_key"):
            problems.append(f"deployment alias {alias}: {detail}")
        try:
            # Resolved the way a run resolves it: that an alias is *listed* says
            # nothing about whether the settings behind it arrive.
            resolved.deployment(alias)
        except ConfigError as exc:
            problems.append(f"deployment alias {alias}: {exc}")

    report: dict[str, Any] = {
        "active_profile": resolved.active_profile,
        "config_path": str(resolved.file.path),
        "config_exists": resolved.file.exists,
        "products": products,
        "deployment_aliases": aliases,
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

    These commands load the file themselves rather than through the root
    context, and they are the two a user runs *to understand* their config --
    reading it here without repeating what it warned about would make them the
    quietest commands in the CLI about their own subject.
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
