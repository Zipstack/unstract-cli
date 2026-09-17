"""CLI helpers shared across subcommand groups.

Keeping option decorators, state checks and error mappings here keeps individual
command files short and focused on their own flow.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, TypeVar

import click

from unstract_cli.core.context import Context
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import (
    AgentMode,
    OutputFormat,
    emit_result,
    resolve_format,
)

F = TypeVar("F", bound=Callable[..., Any])


def common_options(func: F) -> F:
    """Attach the flags every command shares: output format, profile, verbose."""

    @click.option(
        "-o",
        "--output",
        "explicit_format",
        type=click.Choice([f.value for f in OutputFormat]),
        default=None,
        help="Format for stdout (json, table, raw). JSON emits the stdout contract.",
    )
    @click.option(
        "--agent",
        type=click.Choice([m.value for m in AgentMode]),
        default=AgentMode.AUTO.value,
        show_default=True,
        help="Whether to detect coding-agent callers and default to -o json.",
    )
    @click.option(
        "-p",
        "--profile",
        default=None,
        help="Configuration profile to use.",
    )
    @click.option(
        "-v",
        "--verbose",
        count=True,
        help="Increase diagnostic noise on stderr.",
    )
    @click.option(
        "-q",
        "--quiet",
        is_flag=True,
        help="Suppress all diagnostic noise on stderr.",
    )
    def wrapper(
        explicit_format: str | None,
        agent: str,
        profile: str | None,
        verbose: int,
        quiet: bool,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Build the context from global options
        ctx = Context(
            output=resolve_format(explicit_format, agent=agent),
            profile_name=profile,
            verbosity=verbose,
            quiet=quiet,
        )

        # Pass context as first positional parameter
        return func(ctx, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def text_only_option(func: F) -> F:
    """Flag for commands whose output can be stripped to raw text."""

    @click.option(
        "--text-only",
        is_flag=True,
        help="Print only raw output strings without JSON envelopes or formatting.",
    )
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def wait_options(func: F) -> F:
    """Attach flags for long-running task polling."""

    @click.option(
        "--wait",
        is_flag=True,
        help="Block until the execution finishes.",
    )
    @click.option(
        "--timeout",
        type=int,
        default=300,
        show_default=True,
        help="Maximum time to wait in seconds.",
    )
    @click.option(
        "--poll-interval",
        type=int,
        default=5,
        show_default=True,
        help="Interval between polling status checks in seconds.",
    )
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def finish(
    ctx: Context,
    data: Any,
    *,
    raw_fields: tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
    text_only: bool = False,
) -> None:
    """Emit one result envelope, scrubbing any resolved credential from it."""
    fmt = OutputFormat.RAW if text_only else ctx.output
    emit_result(
        data,
        fmt,
        meta=meta,
        raw_fields=raw_fields,
        secrets=ctx.secrets(),
    )


def require_file(path: str, description: str = "File") -> str:
    """Ensure a required file exists and is readable."""
    if not os.path.exists(path):
        raise CLIError(
            f"{description} not found at {path!r}.",
            ExitCode.USAGE,
        )
    if not os.path.isfile(path):
        raise CLIError(
            f"{description} path {path!r} is a directory, not a file.",
            ExitCode.USAGE,
        )
    return path