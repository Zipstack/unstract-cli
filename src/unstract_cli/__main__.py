"""Entry point: turns every failure into an envelope plus a stable exit code.

Click's own error handling is bypassed on purpose. By default it prints prose to
stderr and exits 1 or 2 with nothing on stdout, which leaves a caller parsing
stdout with an empty stream and no way to tell a usage error from a server
failure.
"""

from __future__ import annotations

import sys

import click

from unstract_cli.app import cli
from unstract_cli.config import ConfigError
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.output import OutputFormat, emit_error


def _format_from_argv(argv: list[str]) -> OutputFormat:
    """Best-effort read of --output before Click has parsed anything.

    A failure during parsing still has to be rendered, and the parsed context
    does not exist yet at that point.
    """
    for i, arg in enumerate(argv):
        value = None
        if arg.startswith("--output="):
            value = arg.split("=", 1)[1]
        elif arg in ("--output", "-o") and i + 1 < len(argv):
            value = argv[i + 1]
        if value:
            try:
                return OutputFormat(value)
            except ValueError:
                break
    return OutputFormat.JSON


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fmt = _format_from_argv(args)
    try:
        cli.main(args=args, standalone_mode=False)
    except CLIError as exc:
        return int(emit_error(exc, fmt))
    except ConfigError as exc:
        return int(emit_error(CLIError(str(exc), ExitCode.USAGE), fmt))
    except click.UsageError as exc:
        return int(
            emit_error(
                CLIError(exc.format_message(), ExitCode.USAGE, hint="Run with --help."),
                fmt,
            )
        )
    except OSError as exc:
        # Not a crash worth a traceback: a full disk or an unwritable path is
        # the caller's to fix, and they still need a parseable envelope.
        return int(
            emit_error(
                CLIError(str(exc), ExitCode.GENERIC, hint="Check the path and disk."),
                fmt,
            )
        )
    except click.Abort:
        return int(ExitCode.GENERIC)
    except click.exceptions.Exit as exc:  # --help and --version exit through here
        return int(exc.exit_code)
    return int(ExitCode.SUCCESS)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
