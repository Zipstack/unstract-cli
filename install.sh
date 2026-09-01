#!/bin/sh
# Installs the `unstract` CLI. Override the source to install a branch or a
# local checkout:
#   UNSTRACT_CLI_SOURCE=/path/to/checkout sh install.sh
set -eu

# Flips to the bare PyPI name once the CLI is published there.
SOURCE="${UNSTRACT_CLI_SOURCE:-git+https://github.com/Zipstack/unstract-cli@main}"

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..." >&2
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer only edits shell rc files, which this shell has already read.
    PATH="${XDG_BIN_HOME:-${HOME}/.local/bin}:${HOME}/.cargo/bin:${PATH}"
    export PATH
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is installed but not on PATH; open a new shell and re-run." >&2
    exit 1
fi

# uv fetches its own interpreter, so the CLI's Python floor is not the user's problem.
uv tool install --force "$SOURCE"

if command -v unstract >/dev/null 2>&1; then
    echo
    unstract --version 2>/dev/null || true
    echo "Run 'unstract config init' to get started." >&2
    exit 0
fi

cat >&2 <<MSG

Installed, but 'unstract' is not on your PATH. Add uv's tool directory:

    export PATH="$(uv tool dir --bin 2>/dev/null || echo "${HOME}/.local/bin"):\$PATH"

Then re-open your shell, or run 'uv tool update-shell'.
MSG
exit 1
