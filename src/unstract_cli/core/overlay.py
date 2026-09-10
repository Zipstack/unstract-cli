"""What the specs cannot say about a flag.

The committed specs are generated from server code, so they describe the API but
not the command line: no short flags, no wording aimed at someone typing, no
way to narrow a value list or hide a parameter a caller should not reach for.
Those four live here rather than in the derivation, so adding one is an edit to
a data file instead of a special case in code.

TOML, read with the stdlib, for the same reason the config file is TOML: no
parser dependency, and the file stays editable without a code change.

Anything not overridden falls through to the spec, so an empty overlay is a
valid overlay.
"""

from __future__ import annotations

import sys
import tomllib
from functools import cache
from importlib import resources
from typing import Any

OVERLAY_FILE = "overlay.toml"


@cache
def load_overlay() -> dict[str, Any]:
    """Read the packaged overlay."""
    text = (resources.files("unstract_cli") / OVERLAY_FILE).read_text(encoding="utf-8")
    return tomllib.loads(text)


def overlay_for(product: str, operation_id: str) -> dict[str, dict[str, Any]]:
    """Per-parameter overrides for one operation, keyed by parameter name."""
    entries = load_overlay().get(product, {}).get(operation_id, {})
    out = {}
    for name, entry in entries.items():
        if isinstance(entry, dict):
            out[name] = entry
        else:
            print(
                f"warning: ignoring {OVERLAY_FILE} entry [{product}.{operation_id}."
                f"{name}]: expected a table, found {type(entry).__name__}.",
                file=sys.stderr,
            )
    return out


__all__ = ["OVERLAY_FILE", "load_overlay", "overlay_for"]
