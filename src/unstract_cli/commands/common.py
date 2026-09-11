"""Pieces every product command shares: the wait flags and result emission."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from unstract_cli.app import Context
from unstract_cli.core.output import emit_result

#: Poll interval and the ceiling on the whole wait. Both are flags.
DEFAULT_INTERVAL = 3.0
DEFAULT_TIMEOUT = 300.0

#: The shortest interval worth allowing: below this the polling is closer to a
#: busy loop against a metered service than to a wait.
MIN_INTERVAL = 0.1

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
                    # Bounded below: an interval of zero polls a metered service
                    # as fast as the loop can issue calls.
                    type=click.FloatRange(min=MIN_INTERVAL),
                    default=DEFAULT_INTERVAL,
                    show_default=True,
                    help="Seconds between polls.",
                ),
                click.option(
                    "--timeout",
                    "wait_timeout",
                    # Zero is meaningful -- one poll, then give up -- but a
                    # negative deadline has already passed.
                    type=click.FloatRange(min=0),
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


def raw_fields(*fields: str) -> Callable[[click.Command], click.Command]:
    """Declare what `--output raw` prints for this command, best answer first.

    Several, because one command has several answers: a queued run replies with
    a handle and no result, and a status read replies with a state until there
    is a result. Raw prints the first of these the answer actually carries.

    Recorded on the command so `--discover full` can report the whole list: a
    caller asking for raw output has to know what it is going to get, and one
    field named there would be wrong for every other shape the command returns.
    """

    def decorate(command: click.Command) -> click.Command:
        command.raw_fields = fields
        return command

    return decorate


def finish(
    ctx: Context,
    data: Any,
    *,
    raw_fields: tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
) -> None:
    """Emit one result envelope, scrubbing any resolved credential from it."""
    emit_result(
        data,
        ctx.output,
        meta=meta,
        raw_fields=raw_fields,
        secrets=ctx.secrets(),
    )


__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_TIMEOUT",
    "MIN_INTERVAL",
    "finish",
    "raw_fields",
    "wait_options",
]
