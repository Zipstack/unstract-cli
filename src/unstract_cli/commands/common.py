"""Pieces every product command shares: the wait flags and result emission."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from unstract_cli.app import Context
from unstract_cli.core.output import emit_result

#: Seconds between polls, and the ceiling on the whole wait. Both are flags; the
#: defaults are a compromise between a fast small document and not hammering the
#: service while a large one runs.
DEFAULT_INTERVAL = 3.0
DEFAULT_TIMEOUT = 300.0

F = Callable[..., Any]


def wait_options(*, default: bool = True) -> Callable[[F], F]:
    """`--wait` and its two knobs.

    ``--wait`` is a gate, not a duration: how long to wait is ``--timeout`` and
    how often to check is ``--interval``, so neither has two spellings.
    """

    def decorate(func: F) -> F:
        for option in reversed(
            [
                click.option(
                    "--wait/--no-wait",
                    default=default,
                    help="Poll until the job reaches a terminal state.",
                ),
                click.option(
                    "--interval",
                    type=float,
                    default=DEFAULT_INTERVAL,
                    show_default=True,
                    help="Seconds between polls.",
                ),
                click.option(
                    "--timeout",
                    "wait_timeout",
                    type=float,
                    default=DEFAULT_TIMEOUT,
                    show_default=True,
                    help="Seconds to wait before giving up. The job keeps running.",
                ),
                click.option(
                    "--save",
                    type=click.Path(dir_okay=False),
                    default=None,
                    help="Write the result here before printing it.",
                ),
            ]
        ):
            func = option(func)
        return func

    return decorate


def raw_field(field: str) -> Callable[[click.Command], click.Command]:
    """Declare which field `--output raw` prints for this command.

    Recorded on the command so `--discover full` can report it: a caller asking
    for raw output has to know what it is going to get.
    """

    def decorate(command: click.Command) -> click.Command:
        command.raw_field = field
        return command

    return decorate


def finish(
    ctx: Context,
    data: Any,
    *,
    raw_field: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Emit one result envelope, scrubbing any resolved credential from it."""
    emit_result(
        data,
        ctx.output,
        meta=meta,
        raw_field=raw_field,
        secrets=ctx.secrets(),
    )


__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_TIMEOUT",
    "finish",
    "raw_field",
    "wait_options",
]
