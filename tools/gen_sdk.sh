#!/usr/bin/env bash
# Generate both SDKs from the committed specs into build/ (gitignored).
# Generated code is NEVER hand-edited — regeneration overwrites wholesale.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/build/gen-venv"
CFG="$REPO/tools/openapi-client.yaml"
# Unpinned, a generator upgrade and a spec change produce the same diff.
GENERATOR="openapi-python-client==0.29.0"

if [ ! -x "$VENV/bin/openapi-python-client" ]; then
  uv venv "$VENV"
  uv pip install --python "$VENV/bin/python" "$GENERATOR"
fi

want="${GENERATOR#*==}"
have="$("$VENV/bin/openapi-python-client" --version | awk '{print $NF}')"
if [ "$have" != "$want" ]; then
  echo "generator is $have, expected $want — reinstalling" >&2
  uv pip install --python "$VENV/bin/python" "$GENERATOR"
fi

gen() {
  local spec="$1" out="$2"
  rm -rf "$REPO/build/$out"
  (cd "$REPO/build" && "$VENV/bin/openapi-python-client" generate \
      --path "$REPO/specs/$spec" --output-path "$REPO/build/$out" \
      --config "$CFG" --overwrite --meta none)
  echo "generated build/$out ($(find "$REPO/build/$out" -name '*.py' | wc -l) files)"
}

gen docstudio.json sdk_docstudio
gen llmwhisperer.json sdk_llmwhisperer
