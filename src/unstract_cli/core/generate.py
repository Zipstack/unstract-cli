"""Turn `Endpoint` records into Click commands (SPEC.md D1).

This module is the load-bearing abstraction: the command tree, every flag, all
help text, validation and `--discover` derive from the same records. Adding
an endpoint means adding one record -- there is no second place to update, so
help text cannot drift from behaviour.

Click is used directly rather than Typer's decorator API because the parameters
are known only at runtime, from data. Typer's ergonomics are built for
statically-declared function signatures.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import click

from unstract_cli.config.loader import ConfigError, ResolvedConfig, load_config
from unstract_cli.core import http
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.model import (
    Endpoint,
    Param,
    ParamType,
)
from unstract_cli.core.output import (
    OutputFormat,
    default_format,
    diagnostic,
    emit,
)
from unstract_cli.core.poll import wait_for_completion

#: Flags every generated command accepts (SPEC.md §5.1, §5.7).
GLOBAL_FLAGS = ("output", "profile", "quiet", "verbose", "dry_run", "max_retries", "no_retry", "timeout", "base_url")

_CLICK_TYPES: dict[ParamType, Any] = {
    ParamType.STR: click.STRING,
    ParamType.INT: click.INT,
    ParamType.FLOAT: click.FLOAT,
    ParamType.BOOL: click.BOOL,
    ParamType.UUID: click.STRING,
    ParamType.JSON: click.STRING,
    ParamType.FILE: click.STRING,
    ParamType.DATE: click.STRING,
}


def _help_text(param: Param) -> str:
    """Compose help for one flag, folding in everything an agent needs.

    Defaults, enums and conditional applicability all appear here, because
    `--help` is the primary discovery surface (SPEC.md §5.3).
    """
    parts = [param.help.rstrip(".") if param.help else ""]
    if mapping := param.choice_map():
        parts.append(f"One of: {', '.join(mapping)}")
    if param.default is not None and not isinstance(param.default, bool):
        parts.append(f"Default: {param.default}")
    if param.applies_when:
        # P9: documented, never enforced -- the server owns the rule.
        parts.append(f"Applies only when {param.applies_when}")
    if param.multiple:
        parts.append("Repeatable")
    if param.replace_semantics:
        parts.append("REPLACES the existing list rather than appending")
    if param.default_from:
        parts.append("Defaults from the active profile")
    return ". ".join(p for p in parts if p) + "." if any(parts) else ""


def _click_option(param: Param) -> click.Option:
    """Build a Click option from a `Param` record."""
    mapping = param.choice_map()
    if mapping:
        ptype: Any = click.Choice(list(mapping))
    else:
        ptype = _CLICK_TYPES.get(param.type, click.STRING)

    kwargs: dict[str, Any] = {
        "help": _help_text(param),
        "required": False,  # Enforced later, so we can give a better message.
        "multiple": param.multiple,
        "type": ptype,
    }
    # A default is applied server-side too; sending it explicitly is harmless and
    # keeps `--discover` honest about what the value will be.
    if param.default is not None and not param.multiple:
        kwargs["default"] = param.default
        kwargs["show_default"] = True

    if param.type is ParamType.BOOL and not mapping:
        flag = param.cli_flag.lstrip("-")
        return click.Option([f"--{flag}/--no-{flag}"], default=None, help=kwargs["help"])

    return click.Option([param.cli_flag, param.py_name], **kwargs)


def _resolve_config(ctx: click.Context, kwargs: dict[str, Any], endpoint: Endpoint) -> ResolvedConfig:
    overrides: dict[str, Any] = {}
    if base_url := kwargs.get("base_url"):
        overrides[f"{endpoint.product.value}.base_url"] = base_url
    if org_id := kwargs.get("org_id"):
        overrides[f"{endpoint.product.value}.org_id"] = org_id

    root = ctx.find_root().params if ctx.find_root() else {}
    cfg = load_config()
    resolved = ResolvedConfig(
        file=cfg,
        profile_name=kwargs.get("profile") or root.get("profile"),
        overrides=overrides,
    )
    for warning in cfg.warnings:
        diagnostic(f"warning: {warning}", quiet=bool(kwargs.get("quiet")))
    return resolved


def _save_payload(payload: Any, destination: str, raw_field: str | None) -> None:
    """Persist a result to disk **atomically, before exit** (SPEC.md §5.6).

    One-shot results cannot be re-fetched, so a partial file from an interrupted
    write would be indistinguishable from data loss. Write to a temp file in the
    same directory, then rename.
    """
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(payload, dict) and raw_field and raw_field in payload:
        body = payload[raw_field]
    else:
        body = payload

    data = (
        body.encode()
        if isinstance(body, str)
        else body
        if isinstance(body, bytes)
        else json.dumps(body, indent=2, default=str).encode()
    )

    tmp = path.with_name(f".{path.name}.partial")
    tmp.write_bytes(data)
    tmp.replace(path)


def build_command(endpoint: Endpoint) -> click.Command:
    """Generate the Click command for one endpoint."""
    options: list[click.Parameter] = [_click_option(p) for p in endpoint.params]

    if endpoint.poll:
        options.append(
            click.Option(
                ["--wait/--no-wait"],
                default=False,
                help=(
                    "Poll until the operation reaches a terminal state, then "
                    "retrieve the result. Avoids scripting the poll loop."
                ),
            )
        )
        options.append(
            click.Option(
                ["--poll-interval"], type=click.FLOAT, default=3.0, show_default=True,
                help="Seconds between status checks when --wait is used.",
            )
        )
        options.append(
            click.Option(
                ["--wait-timeout"], type=click.FLOAT, default=300.0, show_default=True,
                help="Give up waiting after this many seconds; exits 7 with the handle.",
            )
        )

    options.extend(
        [
            click.Option(
                ["--output", "-o", "output"],
                type=click.Choice([f.value for f in OutputFormat]),
                default=None,
                help="Output format. Defaults to json when stdout is not a TTY.",
            ),
            click.Option(["--profile", "-p", "profile"], default=None, help="Config profile to use."),
            click.Option(["--base-url"], default=None, help="Override the product base URL."),
            click.Option(["--dry-run"], is_flag=True, default=False,
                         help="Print the resolved request as JSON and exit without sending."),
            click.Option(["--quiet", "-q"], is_flag=True, default=False, help="Suppress stderr diagnostics."),
            click.Option(["--verbose", "-v"], count=True, help="Increase stderr diagnostics."),
            click.Option(["--max-retries"], type=click.INT, default=3, show_default=True,
                         help="Retries for 429/5xx responses."),
            click.Option(["--no-retry"], is_flag=True, default=False, help="Disable retries entirely."),
            click.Option(["--timeout"], type=click.FLOAT, default=60.0, show_default=True,
                         help="Per-request timeout in seconds."),
        ]
    )

    def callback(**kwargs: Any) -> None:
        ctx = click.get_current_context()
        _run_endpoint(ctx, endpoint, kwargs)

    help_text = _command_help(endpoint)
    return click.Command(
        name=endpoint.name,
        params=options,
        callback=callback,
        help=help_text,
        short_help=endpoint.summary,
    )


def _command_help(endpoint: Endpoint) -> str:
    """Full help body: summary, semantics, endpoint, constraints, examples."""
    lines = [endpoint.summary, ""]
    if endpoint.description:
        lines += [endpoint.description, ""]
    lines.append(f"API: {endpoint.method} {endpoint.path}")
    if endpoint.permission:
        lines.append(f"Requires platform key permission: {endpoint.permission.value}")
    if endpoint.poll and endpoint.poll.one_shot:
        lines.append(
            "ONE-SHOT: the result can be retrieved only once. Use --save to persist it; "
            "a second attempt exits 9."
        )
    for constraint in endpoint.constraints:
        lines.append(f"Constraint: {constraint.describe()}")
    if endpoint.examples:
        lines += ["", "Examples:"] + [f"  {ex}" for ex in endpoint.examples]
    return "\n".join(lines)


def _run_endpoint(ctx: click.Context, endpoint: Endpoint, kwargs: dict[str, Any]) -> None:
    """Execute one generated command end-to-end."""
    quiet = bool(kwargs.get("quiet"))
    verbosity = int(kwargs.get("verbose") or 0)
    fmt = OutputFormat(kwargs["output"]) if kwargs.get("output") else default_format()

    # Click always supplies `multiple` options as (), which would otherwise look
    # like "explicitly set to empty" further down.
    values = {k: v for k, v in kwargs.items() if not (isinstance(v, tuple) and not v)}

    try:
        config = _resolve_config(ctx, values, endpoint)
        plan = http.build_request(endpoint, config, values)

        if kwargs.get("dry_run"):
            emit(plan.describe(), fmt if fmt is not OutputFormat.RAW else OutputFormat.JSON)
            ctx.exit(int(ExitCode.SUCCESS))

        diagnostic(f"{plan.method} {plan.url}", quiet=quiet, verbosity=verbosity, level=1)

        max_retries = 0 if kwargs.get("no_retry") else int(kwargs.get("max_retries") or 0)
        response = http.execute(
            plan,
            endpoint=endpoint,
            timeout=float(kwargs.get("timeout") or 60.0),
            max_retries=max_retries,
        )
        http.raise_for_status(response, endpoint)
        payload = response.payload

        if kwargs.get("wait") and endpoint.poll:
            payload = wait_for_completion(
                endpoint=endpoint,
                initial=payload,
                config=config,
                values=values,
                poll_interval=float(kwargs.get("poll_interval") or 3.0),
                timeout=float(kwargs.get("wait_timeout") or 300.0),
                max_retries=max_retries,
                request_timeout=float(kwargs.get("timeout") or 60.0),
                quiet=quiet,
                verbosity=verbosity,
            )

        if destination := values.get("save"):
            _save_payload(payload, str(destination), endpoint.raw_field)
            diagnostic(f"saved to {destination}", quiet=quiet, verbosity=verbosity)

        emit(payload, fmt, columns=endpoint.table_columns, raw_field=endpoint.raw_field)
        ctx.exit(int(ExitCode.SUCCESS))

    except ConfigError as exc:
        CLIError(str(exc), ExitCode.USAGE).emit()
        ctx.exit(int(ExitCode.USAGE))
    except CLIError as exc:
        secrets: list[str] = []
        # Redaction is best-effort here: if credentials cannot be resolved, that
        # must not stop the underlying error from being reported.
        with contextlib.suppress(Exception):
            secrets = http.collect_secrets(_resolve_config(ctx, values, endpoint))
        exc.emit(secrets)
        ctx.exit(int(exc.exit_code))


def build_group_tree(endpoints: list[Endpoint]) -> dict[str, click.Group]:
    """Assemble endpoints into nested Click groups by command path."""
    groups: dict[str, click.Group] = {}

    for endpoint in endpoints:
        group_name = endpoint.group
        if group_name not in groups:
            groups[group_name] = click.Group(
                name=group_name, help=_GROUP_HELP.get(group_name, f"{group_name} commands.")
            )
        parent = groups[group_name]

        if endpoint.subgroup:
            for part in endpoint.subgroup.split():
                existing = parent.commands.get(part)
                if not isinstance(existing, click.Group):
                    existing = click.Group(name=part, help=f"{part} commands.")
                    parent.add_command(existing)
                parent = existing

        parent.add_command(build_command(endpoint))

    return groups


_GROUP_HELP = {
    "whisper": "LLMWhisperer v2 — convert documents to LLM-ready text.",
    "deployment": "Run deployed Unstract API workflows and check their status.",
    "platform": "Unstract Platform Management API (v1) — manage platform resources.",
    "hitl": "Human Quality Review — retrieve approved results (Enterprise).",
    "apihub": "API Hub — vertical extraction (tables, bank statements, doc splitting).",
}


__all__ = ["build_command", "build_group_tree", "GLOBAL_FLAGS"]
