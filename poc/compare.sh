#!/usr/bin/env bash
# Phase 5: run the same two commands through both CLIs against a live backend
# and diff the JSON. Each side gets its own execution — the status GET is
# one-shot, so a shared execution would make the second read a 406.
#
#   UNSTRACT_API_URL=... UNSTRACT_API_KEY=... poc/compare.sh <file>
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/build/poc-venv/bin/python"
OUT="${OUT_DIR:-$(mktemp -d)}"
mkdir -p "$OUT"
FILE="${1:?usage: compare.sh <file-to-upload>}"

run() {   # run <name> <cli-args...>
  local name=$1; shift
  PYTHONPATH="$REPO/build:$REPO/poc" "$PY" "$@" >"$OUT/$name.json" 2>"$OUT/$name.err"
}

for side in published generated; do
  run "$side.execute" "$REPO/poc/cli_$side.py" deployment execute "$FILE" --timeout -1
  ep=$("$PY" -c "import json,sys;print(json.load(open('$OUT/$side.execute.json')).get('status_check_api_endpoint') or '')" 2>/dev/null)
  [ -n "$ep" ] || { echo "$side: no status endpoint; see $OUT/$side.execute.*"; continue; }
  for i in $(seq 30); do
    run "$side.status" "$REPO/poc/cli_$side.py" deployment status "$ep"
    cp "$OUT/$side.status.json" "$OUT/$side.status.$i.json"
    if ! "$PY" -c "import json;json.load(open('$OUT/$side.status.json'))" 2>/dev/null; then
      echo "$side poll $i produced no JSON:"; cat "$OUT/$side.status.err"; break
    fi
    "$PY" -c "import json;d=json.load(open('$OUT/$side.status.json'));raise SystemExit(0 if not d['pending'] else 1)" && break
    sleep 4
  done
done

echo "artifacts: $OUT"
for stage in execute status; do
  echo "--- $stage: published vs generated ---"
  diff <("$PY" -m json.tool "$OUT/published.$stage.json" 2>/dev/null) \
       <("$PY" -m json.tool "$OUT/generated.$stage.json" 2>/dev/null) \
    && echo "identical"
done
