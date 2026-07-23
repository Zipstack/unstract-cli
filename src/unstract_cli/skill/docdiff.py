"""Cross-reference endpoint records against the public API documentation.

Supports the `update-unstract-cli` Claude Skill (SPEC.md §8): parse the docs,
parse the records, and report drift with citations.

Two documentation formats:

* **Markdown** (LLMWhisperer, API deployment, HITL) -- parameters in tables with
  ``Parameter | Type | Default | Required | Description`` columns.
* **MDX** (Platform v1) -- parameters as ``<ApiEndpoint>`` component props. This
  is JSX rather than Markdown, so props are parsed, not table cells.

Findings are *reported*, never auto-applied. Documentation lags implementation,
so an endpoint missing from the docs is usually undocumented rather than removed
(SPEC.md §8.5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unstract_cli.core.model import Endpoint
from unstract_cli.endpoints import ALL_ENDPOINTS

#: Front matter marking an unstable contract; such pages are excluded entirely.
DRAFT_MARKER = re.compile(r"^draft:\s*true\s*$", re.MULTILINE)


@dataclass
class DocParam:
    """A parameter as the documentation describes it."""

    name: str
    type: str = ""
    default: str = ""
    required: bool = False
    description: str = ""


@dataclass
class DocEndpoint:
    """An endpoint as the documentation describes it."""

    method: str
    path: str
    params: list[DocParam] = field(default_factory=list)
    source: str = ""
    heading: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.method.upper(), self.path)


@dataclass
class Finding:
    """One drift observation, always carrying a citation."""

    kind: str  # missing_in_cli | missing_in_docs | param_drift
    severity: str  # info | warning | action
    message: str
    citation: str = ""
    command: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "citation": self.citation,
            "command": self.command,
            "suggestion": self.suggestion,
        }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_TRUTHY = {"yes", "true", "required"}


def _clean(cell: str) -> str:
    """Strip Markdown emphasis, links and code ticks from a table cell."""
    text = cell.strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace("<br/>", " ")
    return text.strip()


#: Headings that begin a *request parameter* table. Everything after a heading
#: not in this set is response/status documentation, which must not be mistaken
#: for input parameters -- otherwise every response field looks like a missing
#: flag.
_REQUEST_HEADINGS = re.compile(
    r"^#{1,4}\s+(request\s+)?(parameters|request\s+body|query\s+parameters|"
    r"path\s+parameters|body|arguments|request)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{1,4}\s+\S")


def parse_markdown_params(text: str) -> list[DocParam]:
    """Extract request parameters from a Markdown parameter table.

    Only tables under a request-parameter heading are considered. API reference
    pages document responses, metadata and status codes in the same table format,
    and treating those as inputs produces a flood of false drift.
    """
    params: list[DocParam] = []
    seen: set[str] = set()
    in_request_section = False

    for line in text.splitlines():
        if _HEADING.match(line):
            in_request_section = bool(_REQUEST_HEADINGS.match(line))
            continue
        if not in_request_section:
            continue
        if not line.strip().startswith("|"):
            continue
        cells = [_clean(c) for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue

        name = cells[0]
        # Skip header and separator rows, and prose rows that aren't parameters.
        if not name or name.lower() in {"parameter", "name", "field"}:
            continue
        if set(name) <= {"-", ":", " "}:
            continue
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            continue
        if name in seen:
            continue

        seen.add(name)
        required = len(cells) > 3 and any(t in cells[3].lower() for t in _TRUTHY)
        params.append(
            DocParam(
                name=name,
                type=cells[1] if len(cells) > 1 else "",
                default=cells[2] if len(cells) > 2 else "",
                required=required,
                description=cells[4] if len(cells) > 4 else "",
            )
        )
    return params


def parse_markdown_doc(path: Path) -> list[DocEndpoint]:
    """Parse a Markdown API reference page."""
    text = path.read_text(encoding="utf-8")
    if DRAFT_MARKER.search(text):
        return []  # Unstable contract; excluded by policy (SPEC.md §11).

    endpoint = ""
    for pattern in (
        r"\|\s*Endpoint\s*\|\s*`([^`]+)`",
        r"\|\s*URL\s*\|\s*`https?://[^/]+(/[^`]+)`",
    ):
        if match := re.search(pattern, text):
            endpoint = match.group(1).strip()
            break
    if not endpoint:
        return []

    methods = re.findall(r"\|\s*Method\s*\|\s*(.+?)\s*\|", text)
    found = re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\b", methods[0]) if methods else ["GET"]

    params = parse_markdown_params(text)
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    return [
        DocEndpoint(method=m, path=endpoint, params=params, source=str(path))
        for m in (found or ["GET"])
    ]


def _prop_entries(block: str, prop: str) -> list[tuple[str, str]]:
    """Return ``(name, raw_object)`` pairs from one `<ApiEndpoint>` prop array.

    Extracts ``prop={[ {...}, {...} ]}`` by scanning for the matching bracket, so
    a nested brace inside a description cannot terminate the array early.
    """
    match = re.search(rf"\b{prop}=\{{\[", block)
    if not match:
        return []

    depth = 0
    start = match.end() - 1
    end = start
    for index in range(start, len(block)):
        char = block[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index
                break
    else:
        return []

    return [
        (m.group(1), m.group(0))
        for m in re.finditer(r'\{\s*name:\s*"([^"]+)"[^}]*\}', block[start : end + 1])
    ]


def parse_mdx_doc(path: Path) -> list[DocEndpoint]:
    """Parse `<ApiEndpoint>` components from a Platform v1 MDX page."""
    text = path.read_text(encoding="utf-8")
    if DRAFT_MARKER.search(text):
        return []

    endpoints: list[DocEndpoint] = []
    for block in re.findall(r"<ApiEndpoint\b(.*?)/>", text, re.DOTALL):
        method = re.search(r'method="([^"]+)"', block)
        path_match = re.search(r'path="([^"]+)"', block)
        if not method or not path_match:
            continue

        params: list[DocParam] = []
        seen: set[str] = set()
        # Only request-side props. `responseBody` describes output: treating it
        # as input makes every response field look like a missing flag, which is
        # the MDX equivalent of the response-table problem in Markdown.
        for prop in ("pathParams", "queryParams", "requestBody"):
            for entry in _prop_entries(block, prop):
                name = entry[0]
                # `members[].id`-style rows document nested response shapes, and
                # prose placeholders like "(any writable field)" are descriptions
                # of a whole class of fields rather than a named parameter.
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", name):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                params.append(
                    DocParam(
                        name=name,
                        required="required: true" in entry[1]
                        or "required={true}" in entry[1],
                        type=(
                            t.group(1)
                            if (t := re.search(r'type:\s*"([^"]+)"', entry[1]))
                            else ""
                        ),
                    )
                )

        endpoints.append(
            DocEndpoint(
                method=method.group(1).upper(),
                path=path_match.group(1),
                params=params,
                source=str(path),
            )
        )
    return endpoints


def parse_docs(root: Path) -> list[DocEndpoint]:
    """Parse every documentation page under a docs repository root."""
    results: list[DocEndpoint] = []
    for path in sorted(root.rglob("*.md")):
        results.extend(parse_markdown_doc(path))
    for path in sorted(root.rglob("*.mdx")):
        results.extend(parse_mdx_doc(path))
    return results


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #


def _normalise(path: str) -> str:
    """Compare paths by shape, ignoring placeholder spelling and prefixes.

    The same endpoint is written several ways across sources: records use
    `{org_id}` while the Markdown docs use `<organization_id>`, and Platform v1
    paths carry an org-scoped prefix that its MDX omits. Only the shape
    distinguishes one endpoint from another, so both placeholder syntaxes
    collapse to `{}`.

    Note that trailing slashes are normalised **here only**, for matching.
    `Endpoint.path` itself remains literal, because the server distinguishes
    them (P11).
    """
    path = re.sub(r"^/api/v1/unstract/\{org_id\}", "", path)
    path = re.sub(r"\{[^}]+\}", "{}", path)
    path = re.sub(r"<[^>]+>", "{}", path)
    return path.rstrip("/") or "/"


def diff(
    doc_endpoints: list[DocEndpoint],
    cli_endpoints: tuple[Endpoint, ...] = ALL_ENDPOINTS,
) -> list[Finding]:
    """Compare documentation against records, three ways (SPEC.md §8.3)."""
    findings: list[Finding] = []

    by_cli: dict[tuple[str, str], Endpoint] = {
        (e.method.upper(), _normalise(e.path)): e for e in cli_endpoints
    }
    by_doc: dict[tuple[str, str], DocEndpoint] = {
        (d.method.upper(), _normalise(d.path)): d for d in doc_endpoints
    }

    for key, doc in by_doc.items():
        cli = by_cli.get(key)
        if cli is None:
            findings.append(
                Finding(
                    kind="missing_in_cli",
                    severity="action",
                    message=f"{doc.method} {doc.path} is documented but has no CLI command.",
                    citation=doc.source,
                    suggestion="Add an Endpoint record to the matching endpoints/*.py module.",
                )
            )
            continue

        # A record carrying `doc_conflict` has already been reconciled against the
        # docs by hand, and the divergence is the point -- `api-deployment key
        # create` deliberately drops the documented `pipeline` body param because
        # the path fixes the resource. Reporting it would invite the Skill to
        # "restore" the very thing that was removed, so the exemption covers
        # parameters as well as the endpoint's existence.
        if cli.doc_conflict:
            continue

        cli_names = {p.name for p in cli.params}
        for param in doc.params:
            if param.name in cli_names:
                continue
            # A parameter mirrored into the body from a path param is present on
            # the wire under its documented name, just not as a separate flag.
            if any(p.mirrors and p.body_name == param.name for p in cli.params):
                continue
            # Path parameters often appear under different placeholder names.
            if any(param.name in cli.path for _ in (0,)):
                continue
            findings.append(
                Finding(
                    kind="param_drift",
                    severity="action",
                    message=(
                        f"{cli.dotted_name}: documented parameter "
                        f"{param.name!r} is not exposed as a flag."
                    ),
                    citation=doc.source,
                    command="unstract " + " ".join(cli.command_path),
                    suggestion=(
                        f"Add Param({param.name!r}"
                        + (", required=True" if param.required else "")
                        + ") to this record."
                    ),
                )
            )

    for key, cli in by_cli.items():
        if key in by_doc:
            continue
        if cli.doc_conflict:
            continue  # A verified deliberate divergence; never revert it.
        findings.append(
            Finding(
                kind="missing_in_docs",
                severity="info",
                message=(
                    f"{cli.dotted_name} ({cli.method} {cli.path}) was not found in "
                    "the parsed docs. Docs lag implementation -- report only."
                ),
                citation=cli.doc_source,
                command="unstract " + " ".join(cli.command_path),
                suggestion="Verify manually. NEVER delete a command for this reason alone.",
            )
        )

    return findings


def report(findings: list[Finding]) -> str:
    """Render findings as JSON for the Skill to act on."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind, []).append(finding.to_dict())
    return json.dumps(
        {
            "summary": {kind: len(items) for kind, items in by_kind.items()},
            "total": len(findings),
            "findings": by_kind,
        },
        indent=2,
    )


__all__ = [
    "DocEndpoint",
    "DocParam",
    "Finding",
    "diff",
    "parse_docs",
    "parse_markdown_doc",
    "parse_markdown_params",
    "parse_mdx_doc",
    "report",
]
