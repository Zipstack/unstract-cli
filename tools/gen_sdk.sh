#!/usr/bin/env bash
# Generate both SDKs from the committed specs into build/ (gitignored).
# Generated code is NEVER hand-edited — regeneration overwrites wholesale.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/build/gen-venv"
CFG="$REPO/tools/openapi-client.yaml"

if [ ! -x "$VENV/bin/openapi-python-client" ]; then
  uv venv "$VENV"
  uv pip install --python "$VENV/bin/python" openapi-python-client
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
