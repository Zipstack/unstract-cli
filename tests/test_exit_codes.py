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


class TestDebugEscapeHatch:
    """The envelope contract hides the stack; UNSTRACT_DEBUG brings it back.

    Turning every unexpected exception into a structured error is right for
    agents, but it also hides the traceback in the one case a maintainer needs
    it -- a genuinely unexpected crash. The flag adds the stack to stderr
    without changing the envelope or the exit code.
    """

    def _run(self, env_extra):
        import os
        import subprocess
        import sys
        import textwrap

        patch = textwrap.dedent(
            """
            import unstract_cli.app as A
            def boom(*a, **k): raise RuntimeError("synthetic unexpected crash")
            A.build_cli = boom
            from unstract_cli.__main__ import main
            import sys; sys.exit(main())
            """
        )
        return subprocess.run(
            [sys.executable, "-c", patch, "whisper", "usage"],
            capture_output=True,
            text=True,
            env={**os.environ, **env_extra},
            timeout=60,
        )

    def test_traceback_hidden_by_default(self):
        result = self._run({})
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert '"error"' in result.stderr, "the envelope is still the contract"

    def test_traceback_shown_with_debug_flag(self):
        result = self._run({"UNSTRACT_DEBUG": "1"})
        assert result.returncode == 1, "the flag must not change the exit code"
        assert "Traceback" in result.stderr
        assert '"error"' in result.stderr, "the envelope is still emitted too"
