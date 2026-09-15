#!/bin/sh
# Exercise a built `unstract` binary with no interpreter in reach.
#
#   scripts/smoke-binary.sh dist/unstract [expected-version]
#
# Run by ci.yml on every pull request and by release.yml before a binary is
# attached to a release, so the same checks decide both. A dev box has a Python
# that would answer an import the bundle is missing, which is why every command
# below runs under `env -i` with an empty PATH.
set -eu

BIN=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
EXPECTED_VERSION="${2:-}"

mkdir -p /tmp/emptybin
run() { env -i PATH=/tmp/emptybin HOME="$HOME" "$BIN" "$@"; }

# Not a trivial path: importing the command modules applies the `@spec_options`
# decorators, which read `overlay.toml` and both vendored specs before Click
# parses anything. A bundle missing its data files fails here.
run --version
run --discover full >/dev/null

# Click wraps help text to the terminal width, so a phrase can arrive split
# across lines; squeeze the whitespace rather than pin the wrapping.
help_text() { run "$@" --help | tr -s '[:space:]' ' '; }

# One derived flag per vendored spec, proving each was reachable...
help_text whisper extract | grep -q -- '--add-line-nos'
help_text docstudio deployment run | grep -q -- '--hitl-packet-id'
# ...and one help string, which comes from the published client's docstring
# rather than from the spec. A build made with `-OO` passes everything above and
# fails only this.
help_text whisper extract | grep -q 'Adds line numbers'

# The release job stamps the version it published; a binary that disagrees with
# the release it is attached to is worse than no binary.
if [ -n "$EXPECTED_VERSION" ]; then
    run --version | grep -q "$EXPECTED_VERSION"
fi

echo "OK  $(basename "$BIN")"
