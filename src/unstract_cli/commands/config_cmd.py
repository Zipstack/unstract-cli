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
    GROUP_PATH,
    TARGET_NAMES,
    ConfigError,
    ResolvedConfig,
    config_path,
    load_config,
    resolve_target,
    save_config,
    starter_profiles,
)
from unstract_cli.core.errors import CLIError, ExitCode, scrub
from unstract_cli.core.output import OutputFormat, default_format, emit


def _resolve_or_fail(target: str) -> str:
    """Turn a `TARGET` argument into an API group, or exit 2 with the valid set.

    A group owned by a product must be named through it (`docstudio.platform`),
    so a setting always says which product it configures. Both separators work,
    because `docstudio platform` is the natural thing to type after
    `unstract docstudio platform ...`.
    """
    if (group := resolve_target(target)) is not None:
        return group

    ctx = click.get_current_context()
    CLIError(
        f"Unknown config target {target!r}.",
        ExitCode.USAGE,
        hint=(
            "Valid targets: " + ", ".join(TARGET_NAMES) + ". "
            "Groups owned by a product are addressed through it, e.g. "
            "`docstudio.platform` (or `docstudio platform`)."
        ),
    ).emit()
    ctx.exit(int(ExitCode.USAGE))
    raise AssertionError  # pragma: no cover - ctx.exit raises


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
@click.argument("target", nargs=-1, required=True, metavar="TARGET... KEY")
@click.option("--profile", "-p", default=None, help="Profile to read from.")
@_output_option
def config_get(target: tuple[str, ...], profile: str | None, output: str | None) -> None:
    """Show a resolved setting, following flag > env > profile > default.

    TARGET and KEY are positional -- not flags. Credentials are reported as
    configured or not, never echoed.

    \b
    Examples:
      unstract config get docstudio.platform org_id
      unstract config get docstudio platform org_id
      unstract config get --profile cloud-eu llmwhisperer base_url
    """
    ctx = click.get_current_context()
    *target_parts, key = target
    if not target_parts:
        CLIError(
            "Expected a target and a key, e.g. `config get docstudio.platform org_id`.",
            ExitCode.USAGE,
            hint="Valid targets: " + ", ".join(TARGET_NAMES) + ".",
        ).emit()
        ctx.exit(int(ExitCode.USAGE))
    product = _resolve_or_fail(" ".join(target_parts))
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
@click.argument("args", nargs=-1, required=True, metavar="TARGET... KEY VALUE")
@click.option("--profile", "-p", default=None, help="Profile to write to.")
@_output_option
def config_set(args: tuple[str, ...], profile: str | None, output: str | None) -> None:
    """Set a value in the config file.

    TARGET, KEY and VALUE are positional -- not flags. Writes to the active
    profile unless --profile names another.

    \b
    Examples:
      unstract config set docstudio.platform org_id org_ABC123
      unstract config set docstudio platform org_id org_ABC123
      unstract config set llmwhisperer api_key 'env:LLMWHISPERER_API_KEY'
      unstract config set --profile cloud-eu llmwhisperer base_url https://…/api/v2

    \b
    Prefer `env:VAR_NAME` for credentials: the file then records where the
    secret lives rather than the secret itself, and a literal value also lands
    in your shell history.
    """
    ctx = click.get_current_context()
    if len(args) < 3:
        CLIError(
            "Expected a target, a key and a value, e.g. "
            "`config set docstudio.platform org_id org_ABC123`.",
            ExitCode.USAGE,
            hint="Valid targets: " + ", ".join(TARGET_NAMES) + ".",
        ).emit()
        ctx.exit(int(ExitCode.USAGE))

    *target_parts, key, value = args
    product = _resolve_or_fail(" ".join(target_parts))
    cfg = load_config()
    name = profile or cfg.default_profile or "cloud-us"

    # Write to the same nested location the reader looks in
    # ([profiles.X.docstudio.platform]), or a flat block would be created that
    # `config get` then silently ignores.
    node: dict[str, Any] = cfg.profiles.setdefault(name, {})
    for segment in GROUP_PATH.get(product, (product,)):
        node = node.setdefault(segment, {})
    node[key] = value
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

    # Keyed by the same target names `config get`/`set` accept, so what is
    # reported here can be copied straight into a command.
    for target, group in ((".".join(p), g) for g, p in GROUP_PATH.items()):
        block: dict[str, Any] = {}
        for key in ("base_url", "org_id"):
            try:
                if (value := resolved.get(group, key)) is not None:
                    block[key] = value
            except ConfigError:
                continue
        try:
            block["api_key_configured"] = bool(resolved.get(group, "api_key"))
        except ConfigError:
            block["api_key_configured"] = False
        settings[target] = block

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


#: A cheap authenticated GET per API group, used by `config doctor --check` to
#: prove the resolved credential actually works. Kept read-only and side-effect
#: free. Groups without a safe no-arg probe are reported as source-only.
_DOCTOR_PROBES: dict[str, str] = {
    "docstudio.platform": "docstudio.platform.prompt-studio.select-choices",
    "llmwhisperer": "whisper.usage",
}


@config_group.command(
    "doctor", help="Diagnose credential resolution and (optionally) test it live."
)
@click.option("--profile", "-p", default=None, help="Profile to diagnose.")
@click.option("--check/--no-check", default=True,
              help="Make one authenticated call per configured group to confirm it works.")
@_output_option
def config_doctor(profile: str | None, check: bool, output: str | None) -> None:
    """Report where each credential resolves from, and whether it authenticates.

    Answers the question that costs the most time: the CLI reports a key as "not
    configured", but you set it -- where is it looking? For `env:` refs it says
    whether the variable is present in THIS process (a shell `export` in a login
    profile the CLI did not inherit is the classic trap), and with --check it
    makes one real call so a working credential reads as working.
    """
    from unstract_cli.core import http
    from unstract_cli.endpoints import get_endpoint

    ctx = click.get_current_context()
    resolved = ResolvedConfig(file=load_config(), profile_name=profile)
    groups: list[dict[str, Any]] = []

    for target, group in ((".".join(p), g) for g, p in GROUP_PATH.items()):
        entry: dict[str, Any] = {"target": target}
        for key in ("base_url", "api_key", "org_id"):
            try:
                entry[key] = resolved.resolution_source(group, key)
            except ConfigError:
                entry[key] = {"resolved": False, "source": "unset"}

        if check and entry["api_key"]["resolved"]:
            if probe := _DOCTOR_PROBES.get(target):
                entry["live_check"] = _probe(http, probe, resolved)
            else:
                # Say so explicitly. An absent `live_check` is indistinguishable
                # from one that ran and passed, which would make `doctor --check`
                # look like it covered more than it did.
                entry["live_check"] = {
                    "skipped": "no safe no-argument probe for this group"
                }
        groups.append(entry)

    emit(
        {
            "active_profile": resolved.active_profile,
            "config_path": str(resolved.file.path),
            "config_exists": resolved.file.exists,
            "groups": groups,
        },
        _fmt(output),
    )

    # Exit non-zero when a live check actually failed, so `config doctor && deploy`
    # is a usable gate. Reporting `"ok": false` in the body while exiting 0 forced
    # an agent to parse prose to discover its credentials were rejected -- the
    # opposite of the branch-on-exit-code contract this CLI advertises.
    if any(g.get("live_check", {}).get("ok") is False for g in groups):
        ctx.exit(int(ExitCode.AUTH))


def _probe(http: Any, probe_name: str, resolved: ResolvedConfig) -> dict[str, Any]:
    """Run one authenticated GET and report only pass/fail, never the payload."""
    try:
        # Resolving the endpoint is inside the try: an unknown probe name would
        # otherwise raise straight out of doctor, whose whole contract is that it
        # never itself crashes.
        from unstract_cli.endpoints import get_endpoint

        endpoint = get_endpoint(probe_name)
        plan = http.build_request(endpoint, resolved, {})
        response = http.execute(plan, endpoint=endpoint, max_retries=0)
    except Exception as exc:  # noqa: BLE001 - doctor must never itself crash
        # Upstream messages can quote the rejected credential back at us, and
        # this output is as likely to land in a CI log as on a screen. Every
        # other error path scrubs; so does this one.
        return {"ok": False, "detail": scrub(str(exc), http.collect_secrets(resolved))}
    ok = response.status < 400
    detail = "authenticated" if ok else f"HTTP {response.status}"
    if response.status in (401, 403):
        detail = f"HTTP {response.status}: credential rejected"
    return {"ok": ok, "detail": detail}


__all__ = ["config_group"]
