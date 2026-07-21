"""Console entry point."""

from __future__ import annotations

import sys


def main() -> int:
    """Run the CLI.

    Click exits the process itself via SystemExit, carrying the exit code set by
    the command, so this normally does not return.
    """
    from unstract_cli.app import build_cli

    build_cli()(standalone_mode=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
