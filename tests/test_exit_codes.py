"""Exit codes must reach the shell (SPEC §5.4).

These run the CLI as a **subprocess**, deliberately. Click's `CliRunner`
invokes commands with `standalone_mode=True` and never executes
`unstract_cli.__main__.main`, so the entire class of "the contract is right but
the entry point drops it" is invisible to it — which is exactly the bug these
were written for: `main()` discarded the code Click returns under
`standalone_mode=False`, so every failure exited 0 while its error envelope
reported `"exit_code": 2`. `set -e` never tripped, and an agent branching on the
status code saw success.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def run_cli(*args: str, **env: str) -> int:
    """Run the CLI in a subprocess and return its real process exit code."""
    return subprocess.run(
        [sys.executable, "-m", "unstract_cli", *args],
        capture_output=True,
        env={
            **os.environ,
            "UNSTRACT_PLATFORM_KEY": "k",
            "UNSTRACT_ORG_ID": "o",
            **env,
        },
    ).returncode


#: (argv, expected code). Each row pins one documented code at the process
#: boundary; `--dry-run` keeps the success cases off the network.
CASES: list[tuple[list[str], int]] = [
    # Usage error (2): a constraint rejected before any request is sent.
    (
        [
            "docstudio", "platform", "prompt-studio", "profile", "create",
            "--tool-id", "t", "--profile-name", "p", "--llm", "l",
            "--x2text", "x", "--chunk-size", "1024",
        ],
        2,
    ),
    # Usage error (2): Click's own parse failure, routed through the same envelope.
    (["whisper", "--definitely-not-a-flag"], 2),
    (["docstudio", "platform", "prompt-studio", "nonexistent-command"], 2),
    # Success (0): the paths that must not regress into a non-zero code.
    (["docstudio", "platform", "prompt-studio", "list", "--dry-run"], 0),
    (["--help"], 0),
    (["--discover"], 0),
]


@pytest.mark.parametrize("argv,expected", CASES, ids=lambda v: None)
def test_exit_code_reaches_the_shell(argv: list[str], expected: int) -> None:
    assert run_cli(*argv) == expected, (
        f"`unstract {' '.join(argv)}` should exit {expected}. A failure exiting 0 "
        "silently breaks `set -e` and any agent branching on the status code."
    )
