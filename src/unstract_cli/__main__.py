"""Entry point: turns every failure into an envelope plus a stable exit code.

Click's own error handling is bypassed on purpose. By default it prints prose to
stderr and exits 1 or 2 with nothing on stdout, which leaves a caller parsing
stdout with an empty stream and no way to tell a usage error from a server
failure.
"""

from __future__ import annotations

import contextlib
import os
import re
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


#: The root group's options, including `-o` at the end of a `-qv` cluster.
#: `-p` is deliberately absent: leaf commands declare their own `-p` with a
#: different meaning.
_GLOBAL_VALUED = re.compile(r"^(-[qv]*o|--output|--agent|--config)$")
_GLOBAL_FLAG = re.compile(
    r"^(-[qv]*o.+|--output=.*|--agent=.*|--config=.*|-[qv]+|--quiet|--verbose)$"
)


def _hoist_globals(argv: list[str]) -> list[str]:
    """Move the root group's options ahead of the subcommand.

    Click stops accepting group options once it has read the subcommand name,
    but `-o json` is as natural at the end of a line as at the start.
    """
    hoisted: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        if _GLOBAL_VALUED.match(arg):
            if i + 1 == len(argv):
                raise click.BadOptionUsage(arg, f"Option '{arg}' requires an argument.")
            hoisted.extend(argv[i : i + 2])
            i += 1
        elif _GLOBAL_FLAG.match(arg):
            hoisted.append(arg)
        else:
            rest.append(arg)
        i += 1
    return hoisted + rest


def _format_from_argv(argv: list[str]) -> OutputFormat:
    """Resolve the format the same way the parsed run would."""
    if "--" in argv:
        argv = argv[: argv.index("--")]
    # `-qojson` is `-o json` to Click; unfold it so the scan sees it too.
    flat: list[str] = []
    for arg in argv:
        joined = re.fullmatch(r"-[qv]*o(.+)", arg)
        flat.extend(["-o", joined[1]] if joined else [arg])
    try:
        return resolve_format(
            _option_from_argv(flat, "--output", "-o"),
            _option_from_argv(argv, "--agent") or AgentMode.AUTO,
        )
    except CLIError:
        # Nothing renders an envelope this early, so a bad value is left for
        # Click's own Choice to reject once parsing reaches it.
        return resolve_format(None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Guessed from argv, then corrected by the root callback: a failure after
    # parsing has to render in the format the run actually resolved.
    ctx = Context(output=_format_from_argv(args))
    try:
        cli.main(args=_hoist_globals(args), standalone_mode=False, obj=ctx)
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
        # Nowhere left to render the envelope. Python flushes stdout at exit,
        # so point it at devnull or this is raised again on the way out.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return int(ExitCode.GENERIC)
    except OSError as exc:
        # A full disk or an unwritable path is the caller's to fix, and they
        # still need a parseable envelope rather than a traceback.
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
        # Notes held before a sink was bound still have to reach the user.
        set_warning_sink(None)
    return int(ExitCode.SUCCESS)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
