"""Output rendering (SPEC.md §5.1).

The contract an agent depends on:

* ``--output json`` is the default whenever stdout is **not** a TTY, so piping or
  capturing the CLI yields machine-readable output with no extra flags.
* **stdout carries the payload and nothing else.** Banners, warnings, progress
  and diagnostics all go to stderr, so `unstract ... | jq` always parses.
* ``raw`` emits the payload alone (extracted text, file bytes) for piping.
"""

from __future__ import annotations

import json
import os
import sys
from enum import Enum
from typing import Any, TextIO

import yaml


class OutputFormat(str, Enum):
    JSON = "json"
    YAML = "yaml"
    TABLE = "table"
    RAW = "raw"


def default_format(stream: TextIO | None = None) -> OutputFormat:
    """JSON unless a human is watching (SPEC.md §5.1).

    ``UNSTRACT_OUTPUT`` overrides. TTY detection means an agent gets JSON without
    passing a flag, while a human at a terminal gets a table.
    """
    if env := os.environ.get("UNSTRACT_OUTPUT"):
        try:
            return OutputFormat(env.lower())
        except ValueError:
            pass  # An invalid value must not break the command; fall through.
    target = stream or sys.stdout
    try:
        return OutputFormat.TABLE if target.isatty() else OutputFormat.JSON
    except (AttributeError, ValueError):  # pragma: no cover - detached stream
        return OutputFormat.JSON


def _flatten(value: Any) -> str:
    """Render a cell. Nested structures become compact JSON rather than Python reprs."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _rows_and_columns(
    data: Any, columns: tuple[str, ...] = ()
) -> tuple[list[str], list[list[str]]]:
    """Derive table columns and rows from arbitrary JSON.

    The generic rule (IMPLEMENTATION_PLAN.md M1.6):

    * list of objects -> columns from the union of keys, in first-seen order
    * single object   -> two-column key/value listing
    * anything else   -> a single ``value`` column

    ``columns`` overrides column selection for responses where the generic rule
    reads poorly.
    """
    if isinstance(data, dict):
        # Unwrap a single list-valued envelope, e.g. {"results": [...]}.
        for key in ("results", "message", "members", "data", "highlights"):
            inner = data.get(key)
            if isinstance(inner, list) and inner:
                data = inner
                break

    if isinstance(data, list):
        if not data:
            return [], []
        if all(isinstance(item, dict) for item in data):
            if columns:
                headers = list(columns)
            else:
                headers = []
                for item in data:
                    headers.extend(k for k in item if k not in headers)
            return headers, [[_flatten(item.get(h)) for h in headers] for item in data]
        return ["value"], [[_flatten(item)] for item in data]

    if isinstance(data, dict):
        keys = list(columns) if columns else list(data)
        return ["key", "value"], [[k, _flatten(data.get(k))] for k in keys]

    return ["value"], [[_flatten(data)]]


def render_table(data: Any, columns: tuple[str, ...] = ()) -> str:
    """Render as an aligned plain-text table.

    Deliberately plain text rather than Rich box-drawing: tables are the
    human-facing format, but they still end up in logs and terminals of varying
    width, and ASCII survives both.
    """
    headers, rows = _rows_and_columns(data, columns)
    if not headers:
        return "(no results)"

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    widths = [min(w, 60) for w in widths]

    def fmt(cells: list[str]) -> str:
        return "  ".join(
            (c[:57] + "..." if len(c) > w else c).ljust(w)
            for c, w in zip(cells, widths, strict=False)
        ).rstrip()

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def render(
    data: Any,
    fmt: OutputFormat,
    *,
    columns: tuple[str, ...] = (),
    raw_field: str | None = None,
) -> str:
    """Render a payload in the requested format."""
    match fmt:
        case OutputFormat.JSON:
            return json.dumps(data, indent=2, default=str)
        case OutputFormat.YAML:
            return yaml.safe_dump(data, sort_keys=False, default_flow_style=False).rstrip()
        case OutputFormat.TABLE:
            return render_table(data, columns)
        case OutputFormat.RAW:
            if isinstance(data, dict) and raw_field and raw_field in data:
                value = data[raw_field]
            elif isinstance(data, (str, bytes)):
                value = data
            else:
                value = data
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            if isinstance(value, str):
                return value
            return json.dumps(value, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)  # pragma: no cover


def emit(
    data: Any,
    fmt: OutputFormat,
    *,
    columns: tuple[str, ...] = (),
    raw_field: str | None = None,
) -> None:
    """Write a payload to stdout -- and nothing else to stdout."""
    print(render(data, fmt, columns=columns, raw_field=raw_field))


def diagnostic(message: str, *, quiet: bool = False, verbosity: int = 0, level: int = 0) -> None:
    """Write a human-facing note to **stderr**, keeping stdout pure.

    ``level`` is the minimum ``-v`` count required: 0 always shows (unless
    ``--quiet``), 1 needs ``-v``, 2 needs ``-vv``.
    """
    if quiet or verbosity < level:
        return
    print(message, file=sys.stderr)


__all__ = [
    "OutputFormat",
    "default_format",
    "diagnostic",
    "emit",
    "render",
    "render_table",
]
