"""The `config` command group -- hand-authored, not generated.

These commands map to no API endpoint: they operate purely on the local config
layer. They are how a user or agent bootstraps every other command.

`--discover` describes them by introspecting these Click commands directly, so
the index always matches the parser. An earlier parallel description of them
drifted and advertised `--product/--key/--value` for `config set`, which really
takes three positional arguments -- hence: no second description, ever.

Per SPEC.md §5.2 nothing here prompts: `init` refuses to clobber an existing file
unless `--force` is passed, rather than asking.
"""

from __future__ import annotations

from typing import Any

import click

from unstract_cli.config.loader import (
    BLOCK_ALIASES,
    PRODUCT_KEYS,
    ConfigError,
    ResolvedConfig,
    config_path,
    load_config,
    save_config,
    starter_profiles,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import OutputFormat, default_format, emit

#: Product names accepted on the command line, including config-file aliases.
_PRODUCT_CHOICES = sorted(
    {*PRODUCT_KEYS, *(a for aliases in BLOCK_ALIASES.values() for a in aliases)}
)

#: Alias -> canonical product, so `config get whisper api_key` resolves.
_CANONICAL = {a: p for p, aliases in BLOCK_ALIASES.items() for a in aliases}


def _fmt(output: str | None) -> OutputFormat:
    return OutputFormat(output) if output else default_format()


def _output_option(f):
    return click.option(
        "--output", "-o", "output",
        type=click.Choice([f.value for f in OutputFormat]), default=None,
        help="Output format. Defaults to json when stdout is not a TTY.",
    )(f)


@click.group(name="config", help="Manage CLI configuration profiles (local only).")
def config_group() -> None:
    """Local configuration management. These commands make no network calls."""


@config_group.command("init", help="Create a starter config file with profile stubs.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite an existing config file.")
@_output_option
def config_init(force: bool, output: str | None) -> None:
    ctx = click.get_current_context()
    path = config_path()

    if path.exists() and not force:
        # Never prompt: state the situation and the exact flag that resolves it.
        CLIError(
            f"Config already exists at {path}.",
            ExitCode.USAGE,
            hint="Pass --force to overwrite it, or edit the file directly.",
        ).emit()
        ctx.exit(int(ExitCode.USAGE))

    cfg = load_config(path) if path.exists() else None
    from unstract_cli.config.loader import ConfigFile

    new = ConfigFile(
        default_profile="cloud-us",
        profiles=starter_profiles(),
        path=path,
        exists=True,
    )
    written = save_config(new, path)
    emit(
        {
            "created": str(written),
            "default_profile": "cloud-us",
            "profiles": sorted(new.profiles),
            "note": (
                "Credentials use env: indirection, so this file holds no secrets. "
                "Set the referenced environment variables to authenticate."
            ),
            "replaced_existing": bool(cfg),
        },
        _fmt(output),
    )


@config_group.command("list", help="List profiles defined in the config file.")
@_output_option
def config_list(output: str | None) -> None:
    cfg = load_config()
    profiles = {
        name: {product: sorted(block) for product, block in blocks.items()}
        for name, blocks in cfg.profiles.items()
    }
    emit(
        {
            "path": str(cfg.path),
            "exists": cfg.exists,
            "default_profile": cfg.default_profile,
            "profiles": profiles,
        },
        _fmt(output),
    )


@config_group.command("get")
@click.argument("product", type=click.Choice(_PRODUCT_CHOICES))
@click.argument("key")
@click.option("--profile", "-p", default=None, help="Profile to read from.")
@_output_option
def config_get(product: str, key: str, profile: str | None, output: str | None) -> None:
    """Show a resolved setting, following flag > env > profile > default.

    PRODUCT and KEY are positional -- not flags. Credentials are reported as
    configured or not, never echoed.

    \b
    Examples:
      unstract config get platform org_id
      unstract config get --profile cloud-eu whisper base_url
    """
    ctx = click.get_current_context()
    product = _CANONICAL.get(product, product)
    try:
        resolved = ResolvedConfig(file=load_config(), profile_name=profile)
        value = resolved.get(product, key)
    except ConfigError as exc:
        CLIError(str(exc), ExitCode.USAGE).emit()
        ctx.exit(int(ExitCode.USAGE))
        return

    # Credentials are never echoed, even on explicit request: this output is as
    # likely to land in a log or a transcript as on a screen.
    is_secret = any(h in key for h in ("key", "token", "secret"))
    emit(
        {
            "product": product,
            "key": key,
            "value": ("***SET***" if value else None) if is_secret else value,
            "configured": value is not None,
        },
        _fmt(output),
    )


@config_group.command("set")
@click.argument("product", type=click.Choice(_PRODUCT_CHOICES))
@click.argument("key")
@click.argument("value")
@click.option("--profile", "-p", default=None, help="Profile to write to.")
@_output_option
def config_set(product: str, key: str, value: str, profile: str | None, output: str | None) -> None:
    """Set a value in the config file.

    PRODUCT, KEY and VALUE are positional -- not flags. Writes to the active
    profile unless --profile names another.

    \b
    Examples:
      unstract config set platform org_id org_ABC123
      unstract config set whisper api_key 'env:LLMWHISPERER_API_KEY'
      unstract config set --profile cloud-eu whisper base_url https://…/api/v2

    \b
    Prefer `env:VAR_NAME` for credentials: the file then records where the
    secret lives rather than the secret itself, and a literal value also lands
    in your shell history.
    """
    ctx = click.get_current_context()
    cfg = load_config()
    name = profile or cfg.default_profile or "cloud-us"

    cfg.profiles.setdefault(name, {}).setdefault(product, {})[key] = value
    if not cfg.default_profile:
        cfg.default_profile = name
    written = save_config(cfg)

    warning = None
    if any(h in key for h in ("key", "token", "secret")) and not value.startswith("env:"):
        warning = (
            "Value stored literally. Prefer `env:VAR_NAME` so the config file "
            "holds a reference rather than the secret itself."
        )

    emit(
        {"profile": name, "product": product, "key": key, "path": str(written),
         "warning": warning},
        _fmt(output),
    )
    ctx.exit(int(ExitCode.SUCCESS))


@config_group.command("use", help="Set the default profile.")
@click.argument("name")
@_output_option
def config_use(name: str, output: str | None) -> None:
    ctx = click.get_current_context()
    cfg = load_config()

    if cfg.profiles and name not in cfg.profiles:
        known = ", ".join(sorted(cfg.profiles)) or "none"
        CLIError(
            f"Profile {name!r} is not defined.",
            ExitCode.USAGE,
            hint=f"Known profiles: {known}. Create one with `unstract config set`.",
        ).emit()
        ctx.exit(int(ExitCode.USAGE))

    cfg.default_profile = name
    written = save_config(cfg)
    emit({"default_profile": name, "path": str(written)}, _fmt(output))


@config_group.command("current", help="Show the active profile and its resolved settings.")
@click.option("--profile", "-p", default=None, help="Profile to inspect.")
@_output_option
def config_current(profile: str | None, output: str | None) -> None:
    resolved = ResolvedConfig(file=load_config(), profile_name=profile)
    settings: dict[str, Any] = {}

    for product in PRODUCT_KEYS:
        block: dict[str, Any] = {}
        for key in ("base_url", "org_id"):
            try:
                if (value := resolved.get(product, key)) is not None:
                    block[key] = value
            except ConfigError:
                continue
        try:
            block["api_key_configured"] = bool(resolved.get(product, "api_key"))
        except ConfigError:
            block["api_key_configured"] = False
        settings[product] = block

    emit(
        {
            "active_profile": resolved.active_profile,
            "config_path": str(resolved.file.path),
            "config_exists": resolved.file.exists,
            "settings": settings,
        },
        _fmt(output),
    )


@config_group.command("path", help="Show the config file path.")
@_output_option
def config_path_cmd(output: str | None) -> None:
    path = config_path()
    emit({"path": str(path), "exists": path.exists()}, _fmt(output))


__all__ = ["config_group"]
