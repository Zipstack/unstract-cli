"""Console entry point."""

from __future__ import annotations

import sys

import click


def main() -> int:
    """Run the CLI.

    Runs Click with ``standalone_mode=False`` so that Click's own parse errors
    (`No such option`, missing argument) reach us instead of being printed and
    swallowed internally. We then render them through the same structured error
    envelope every other error uses, additionally on stdout when piped, so a
    wrapper feeding stdout to a JSON parser sees valid JSON on the error path too.

    That mode also changes how ``ctx.exit(code)`` behaves: instead of raising
    ``SystemExit``, Click *returns* the code from the invocation. Returning 0
    unconditionally here therefore reported every failure as success -- a
    validation error printed ``"exit_code": 2`` in its envelope while the process
    exited 0, so `set -e` and any agent branching on the status code saw a pass.
    The returned value is the command's real exit code, so it is what we return
    (SPEC §5.4).
    """
    from unstract_cli.app import build_cli
    from unstract_cli.config.loader import ConfigError
    from unstract_cli.core.errors import CLIError, ExitCode

    try:
        result = build_cli()(standalone_mode=False)
        # Click returns whatever `ctx.exit` was given; a command that simply
        # falls off the end returns None, which is success.
        return result if isinstance(result, int) else 0
    except click.UsageError as exc:
        # Click's human-facing message (with its usage/help hint) to stderr...
        exc.show()
        # ...and the machine-readable envelope to stdout when piped.
        CLIError(exc.format_message(), ExitCode.USAGE).emit_stdout_only()
        return exc.exit_code if exc.exit_code is not None else int(ExitCode.USAGE)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except CLIError as exc:
        exc.emit()
        return int(exc.exit_code)
    except ConfigError as exc:
        # A malformed or unreadable config file. `config get` already mapped this
        # to exit 2; its six sibling commands did not, so `config list` on a bad
        # TOML escaped as a bare traceback with nothing on stdout -- an agent
        # piping to a JSON parser saw an empty stream, not an error it could
        # branch on. Handled here so every command inherits the same contract.
        CLIError(str(exc), ExitCode.USAGE).emit()
        return int(ExitCode.USAGE)
    except Exception as exc:  # noqa: BLE001 - the envelope is the contract
        # Last line of defence for the guarantee advertised in app.py: *every*
        # failure is a structured error, never a traceback.
        CLIError(str(exc) or exc.__class__.__name__, ExitCode.GENERIC).emit()
        return int(ExitCode.GENERIC)


if __name__ == "__main__":
    sys.exit(main())
