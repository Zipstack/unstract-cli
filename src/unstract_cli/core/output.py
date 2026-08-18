"""Output rendering, and choosing which rendering to use.

The contract a caller depends on:

* ``-o json`` writes **one envelope to stdout and nothing else** -- ``{ok, data,
  error, meta}`` -- on success and on failure alike, so a failed run still
  yields a valid object rather than an empty stream.
* What ``-o json`` produces depends on nothing but the command and its
  arguments: not on a terminal, not on configuration, not on who is calling.
* Human-facing notes, warnings and progress all go to stderr.
* Without ``-o`` the output is ``table``, which is for people to read and is
  free to change. Anything parsing this CLI passes ``-o json`` explicitly.

The one thing an unflagged run reads from its environment is which *default* to
use: a coding agent gets ``json``, because an agent that has to be told twice is
an agent that parses a table. Detection is never allowed to reach past the
default -- see ``resolve_format``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from collections.abc import Mapping
from enum import StrEnum
from fnmatch import fnmatch
from typing import Any

from unstract_cli.core.errors import CLIError, ExitCode, known_secrets, scrub

#: Major version of the stdout envelope, published in every ``meta``.
CONTRACT_VERSION = 1

#: Environment markers the coding agents set for the tools they drive. Patterns,
#: so a family of variables can be named once.
AGENT_ENV = ("CLAUDECODE", "CURSOR_AGENT", "CODEX_*", "AI_AGENT")


class OutputFormat(StrEnum):
    JSON = "json"
    TABLE = "table"
    RAW = "raw"


class AgentMode(StrEnum):
    AUTO = "auto"
    YES = "yes"
    NO = "no"


def agent_detected(env: Mapping[str, str] | None = None) -> bool:
    """Whether the environment looks like a coding agent's."""
    names = os.environ if env is None else env
    return any(
        names[name] and fnmatch(name, pattern) for name in names for pattern in AGENT_ENV
    )


def resolve_format(
    explicit: str | None,
    agent: str = AgentMode.AUTO,
    env: Mapping[str, str] | None = None,
) -> OutputFormat:
    """The format to render in.

    An explicit ``-o`` wins outright, so detection can only ever pick the
    default: two runs of ``-o json`` in different environments render the same
    bytes, which is the property a script is relying on.
    """
    if explicit:
        return OutputFormat(explicit)
    if agent == AgentMode.YES or (agent == AgentMode.AUTO and agent_detected(env)):
        return OutputFormat.JSON
    return OutputFormat.TABLE


def envelope(
    *,
    data: Any = None,
    error: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stdout envelope. ``ok`` is derived, never passed in."""
    return {
        "ok": error is None,
        "data": data,
        "error": error,
        "meta": {**(meta or {}), "contract_version": CONTRACT_VERSION},
    }


def _flatten(value: Any) -> str:
    """Render a cell. Nested structures become compact JSON, not Python reprs."""
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

    List of objects -> columns from the union of keys in first-seen order;
    single object -> a two-column key/value listing; anything else -> one
    ``value`` column. ``columns`` overrides the selection where the generic rule
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
    try:
        return max(shutil.get_terminal_size((default, 24)).columns, 40)
    except Exception:  # pragma: no cover - detached terminal
        return default


def render_table(
    data: Any, columns: tuple[str, ...] = (), *, max_width: int | None = None
) -> str:
    """Render as an aligned plain-text table.

    Plain text rather than box drawing: tables end up in logs and terminals of
    varying width, and ASCII survives both.

    Long cells are **wrapped, never truncated**: a table is a view of the data,
    not a lossy summary, and a silently dropped tail is the kind of thing you
    only notice after acting on it.
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
                natural[i] = max(
                    natural[i], max((len(p) for p in cell.split("\n")), default=0)
                )

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
    env: dict[str, Any],
    fmt: OutputFormat = OutputFormat.JSON,
    *,
    columns: tuple[str, ...] = (),
    raw_field: str | None = None,
) -> str:
    """Render an envelope. ``table`` and ``raw`` show ``data``, or the error."""
    if fmt is OutputFormat.JSON:
        return json.dumps(env, indent=2, default=str)

    payload = env["data"] if env["ok"] else env["error"]
    if fmt is OutputFormat.TABLE:
        return render_table(payload, columns)

    if isinstance(payload, dict) and raw_field and raw_field in payload:
        payload = payload[raw_field]
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, indent=2, default=str)


def emit(
    env: dict[str, Any],
    fmt: OutputFormat = OutputFormat.JSON,
    *,
    columns: tuple[str, ...] = (),
    raw_field: str | None = None,
    secrets: list[str] | None = None,
) -> None:
    """Write one envelope to stdout -- and nothing else to stdout.

    Every credential resolved during the run is scrubbed whether or not the
    caller passed one: an emitter that has to remember is an emitter that
    eventually forgets.
    """
    emit_text(render(env, fmt, columns=columns, raw_field=raw_field), secrets=secrets)


def emit_text(text: str, *, secrets: list[str] | None = None) -> None:
    """Write already-rendered text to stdout, scrubbed the way an envelope is.

    A command that renders its own table is still writing to the stream no
    credential may reach, and scrubbing it by hand is the arrangement that
    eventually forgets.
    """
    if to_hide := [*(secrets or []), *known_secrets()]:
        text = scrub(text, to_hide)
    print(text)


def emit_result(
    data: Any,
    fmt: OutputFormat = OutputFormat.JSON,
    *,
    meta: dict[str, Any] | None = None,
    columns: tuple[str, ...] = (),
    raw_field: str | None = None,
    secrets: list[str] | None = None,
) -> None:
    """Write a successful result."""
    emit(
        envelope(data=data, meta=meta),
        fmt,
        columns=columns,
        raw_field=raw_field,
        secrets=secrets,
    )


def emit_error(
    error: CLIError,
    fmt: OutputFormat = OutputFormat.JSON,
    *,
    meta: dict[str, Any] | None = None,
    secrets: list[str] | None = None,
) -> ExitCode:
    """Write a failure envelope to stdout and a one-line summary to stderr.

    Returns the exit code so the caller can hand it straight to the shell.
    """
    emit(envelope(error=error.to_dict(), meta=meta), fmt, secrets=secrets)
    summary = error.message
    if to_hide := [*(secrets or []), *known_secrets()]:
        summary = scrub(summary, to_hide)
    print(f"error: {summary}", file=sys.stderr)
    return error.exit_code


def diagnostic(
    message: str, *, quiet: bool = False, verbosity: int = 0, level: int = 0
) -> None:
    """Write a human-facing note to **stderr**, keeping stdout parseable.

    ``level`` is the minimum ``-v`` count required: 0 always shows (unless
    ``--quiet``), 1 needs ``-v``, 2 needs ``-vv``.
    """
    if quiet or verbosity < level:
        return
    print(message, file=sys.stderr)


__all__ = [
    "AGENT_ENV",
    "CONTRACT_VERSION",
    "AgentMode",
    "OutputFormat",
    "agent_detected",
    "diagnostic",
    "emit",
    "emit_error",
    "emit_result",
    "emit_text",
    "envelope",
    "render",
    "render_table",
    "resolve_format",
]
