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
    PRODUCTS,
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    config_path,
    load_config,
    save_config,
    starter_profiles,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import OutputFormat, emit_result

#: Keys whose value is never echoed back, even on explicit request: this output
#: is as likely to land in a log or a transcript as on a screen.
_SECRET_KEY_HINTS = ("key", "token", "secret")


def _is_secret(key: str) -> bool:
    return any(hint in key.lower() for hint in _SECRET_KEY_HINTS)


def _fmt(obj: Any) -> OutputFormat:
    """Output format from the root context, defaulting when invoked standalone."""
    return getattr(obj, "output", None) or OutputFormat.JSON


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
                "Set the referenced environment variables to authenticate."
            ),
        },
        _fmt(obj),
    )


@config_group.command("list", help="List profiles defined in the config file.")
@click.pass_obj
def config_list(obj: Any) -> None:
    cfg = load_config()
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
    cfg = load_config()
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


@config_group.command("doctor", help="Diagnose how each setting resolves.")
@click.pass_obj
def config_doctor(obj: Any) -> None:
    """Report where each setting resolves from, without echoing any secret.

    Answers the question that costs the most time: the CLI reports a key as "not
    configured", but you set it -- where is it looking? For `env:` references it
    says whether the variable is present in THIS process, a shell `export` in a
    login profile the CLI never inherited being the classic trap.
    """
    resolved = _resolved(obj)
    products: dict[str, Any] = {}
    for product in PRODUCTS:
        entry: dict[str, Any] = {}
        for key in ("base_url", "api_key", "org_id"):
            try:
                entry[key] = resolved.resolution_source(product, key)
            except ConfigError as exc:
                entry[key] = {"resolved": False, "source": "unset", "detail": str(exc)}
        products[product] = entry

    try:
        aliases = list(resolved.deployment_aliases())
    except ConfigError:
        aliases = []

    emit_result(
        {
            "active_profile": resolved.active_profile,
            "config_path": str(resolved.file.path),
            "config_exists": resolved.file.exists,
            "products": products,
            "deployment_aliases": aliases,
        },
        _fmt(obj),
    )


def _resolved(obj: Any) -> ResolvedConfig:
    """The root context's config, or a freshly loaded one when invoked standalone."""
    if (existing := getattr(obj, "_config", None)) is not None:
        return existing
    return ResolvedConfig(file=load_config(), profile_name=getattr(obj, "profile", None))


__all__ = ["config_group"]
