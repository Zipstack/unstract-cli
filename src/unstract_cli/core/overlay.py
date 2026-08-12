"""What the specs cannot say about a flag.

The committed specs are generated from server code, so they carry names, types
and defaults but no allowed-value lists, no short flags and, today, no parameter
descriptions. Those live here rather than in the derivation, so adding one is an
edit to a data file instead of a special case in code.

TOML, read with the stdlib, for the same reason the config file is TOML: no
parser dependency, and the file stays editable without a code change.

Anything not overridden falls through to the spec, so an empty overlay is a
valid overlay.
"""

from __future__ import annotations

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
    return {name: entry for name, entry in entries.items() if isinstance(entry, dict)}


__all__ = ["OVERLAY_FILE", "load_overlay", "overlay_for"]
