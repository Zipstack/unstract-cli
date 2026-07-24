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
import shutil
import sys
import textwrap
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

    The generic rule:

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


def _terminal_width(default: int = 100) -> int:
    """Usable width for table output."""
    try:
        return max(shutil.get_terminal_size((default, 24)).columns, 40)
    except Exception:  # pragma: no cover - detached terminal
        return default


def render_table(
    data: Any, columns: tuple[str, ...] = (), *, max_width: int | None = None
) -> str:
    """Render as an aligned plain-text table.

    Deliberately plain text rather than Rich box-drawing: tables are the
    human-facing format, but they still end up in logs and terminals of varying
    width, and ASCII survives both.

    Long cells are **wrapped, never truncated**: a table is a view of the data,
    not a lossy summary, and silently dropping the tail of a value is the kind of
    thing you only notice after acting on it. Wrapped continuation lines are
    indented under their column so the table still reads as a grid.
    """
    headers, rows = _rows_and_columns(data, columns)
    if not headers:
        return "(no results)"

    gutter = 2
    total_width = max_width or _terminal_width()

    natural = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(natural):
                natural[i] = max(natural[i], max((len(p) for p in cell.split("\n")), default=0))

    # Shrink only the widest columns, and only as far as the terminal requires,
    # so a narrow column is never squeezed on behalf of a wide neighbour.
    widths = list(natural)
    budget = total_width - gutter * (len(headers) - 1)
    while sum(widths) > budget and max(widths) > 8:
        widest = widths.index(max(widths))
        widths[widest] -= 1

    def fmt(cells: list[str]) -> list[str]:
        """Lay one logical row out over as many physical lines as it needs."""
        wrapped = [
            textwrap.wrap(cell, width=w, break_long_words=True, break_on_hyphens=False)
            or [""]
            for cell, w in zip(cells, widths, strict=False)
        ]
        height = max(len(parts) for parts in wrapped)
        lines = []
        for line_no in range(height):
            pieces = [
                (parts[line_no] if line_no < len(parts) else "").ljust(w)
                for parts, w in zip(wrapped, widths, strict=False)
            ]
            lines.append((" " * gutter).join(pieces).rstrip())
        return lines

    out = fmt(headers)
    out.append((" " * gutter).join("-" * w for w in widths).rstrip())
    for row in rows:
        out.extend(fmt(row))
    return "\n".join(out)


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
