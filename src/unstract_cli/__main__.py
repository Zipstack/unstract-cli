"""Entry point: turns every failure into an envelope plus a stable exit code.

Click's own error handling is bypassed on purpose. By default it prints prose to
stderr and exits 1 or 2 with nothing on stdout, which leaves a caller parsing
stdout with an empty stream and no way to tell a usage error from a server
failure.
"""

from __future__ import annotations

import contextlib
import os
import sys

import click

from unstract_cli.app import Context, cli
from unstract_cli.config import ConfigError
from unstract_cli.core.errors import CLIError, ExitCode, set_warning_sink
from unstract_cli.core.output import AgentMode, OutputFormat, emit_error, resolve_format


def _option_from_argv(argv: list[str], *spellings: str) -> str | None:
    """Best-effort read of one option before Click has parsed anything.

    A failure during parsing still has to be rendered, and the parsed context
    does not exist yet at that point.
    """
    for i, arg in enumerate(argv):
        for spelling in spellings:
            if arg.startswith(f"{spelling}="):
                return arg.split("=", 1)[1]
            if arg == spelling and i + 1 < len(argv):
                return argv[i + 1]
    return None


def _format_from_argv(argv: list[str]) -> OutputFormat:
    """Resolve the format the same way the parsed run would."""
    try:
        return resolve_format(
            _option_from_argv(argv, "--output", "-o"),
            _option_from_argv(argv, "--agent") or AgentMode.AUTO,
        )
    except CLIError:
        # An unusable value here is Click's error to report, not ours to guess
        # around: this runs outside the handler that renders an envelope, so
        # raising would lose the stream contract entirely. Fall back to the
        # default and let Click's own Choice reject the value downstream.
        return resolve_format(None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Seeded with the guess and then filled in by the root callback, so a
    # failure after parsing renders in the format the run actually resolved --
    # which argv alone cannot tell, since `-o json` and `-ojson` mean the same
    # thing to Click and only one of them looks like an option to read by hand.
    ctx = Context(output=_format_from_argv(args))
    try:
        cli.main(args=args, standalone_mode=False, obj=ctx)
    except CLIError as exc:
        return int(emit_error(exc, ctx.output))
    except ConfigError as exc:
        return int(emit_error(CLIError(str(exc), ExitCode.USAGE), ctx.output))
    except click.UsageError as exc:
        return int(
            emit_error(
                CLIError(exc.format_message(), ExitCode.USAGE, hint="Run with --help."),
                ctx.output,
            )
        )
    except BrokenPipeError:
        # The reader is gone, so there is nowhere to render the envelope. Point
        # stdout at devnull first: Python flushes it at exit and would otherwise
        # raise this again on the way out.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return int(ExitCode.GENERIC)
    except OSError as exc:
        # Not a crash worth a traceback: a full disk or an unwritable path is
        # the caller's to fix, and they still need a parseable envelope.
        return int(
            emit_error(
                CLIError(str(exc), ExitCode.GENERIC, hint="Check the path and disk."),
                ctx.output,
            )
        )
    except (click.Abort, KeyboardInterrupt):
        # Nothing here prompts, so Click's Abort can only mean an interrupt.
        return int(
            emit_error(
                CLIError("Interrupted.", ExitCode.INTERRUPTED, retryable=True), ctx.output
            )
        )
    except click.exceptions.Exit as exc:  # --help and --version exit through here
        return int(exc.exit_code)
    finally:
        # A run that failed before the root callback bound a sink still has to
        # show what was held, rather than swallowing it for being early.
        set_warning_sink(None)
    return int(ExitCode.SUCCESS)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
