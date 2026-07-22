"""Declarative endpoint model — the single source of truth for the CLI.

Every command, flag, help string, validation rule and `--discover` entry is
derived from the `Endpoint` records in `unstract_cli.endpoints`. Nothing about a
command is written twice, so help text cannot drift from behaviour, and the
bundled Claude Skill has exactly one place to edit (SPEC.md D1, IMPLEMENTATION_PLAN.md §1).

The dataclasses here are frozen: records are a contract, not runtime state.

Parameter patterns P1-P12 (IMPLEMENTATION_PLAN.md §2) map onto these fields as:

===  ==========================  ===================================================
P    Pattern                     Encoding
===  ==========================  ===================================================
P1   Mutually exclusive          ``Endpoint.constraints=[MutuallyExclusive(...)]``
P2   At-least-one-of             ``Endpoint.constraints=[AtLeastOneOf(...)]``
P3   Enum -> path segment        ``Param.choices={friendly: wire}`` (a mapping)
P4   Repeatable                  ``Param.multiple=True``
P5   Freeform key=value          ``Param.freeform_prefix="ext_"``
P6   Path param, profile default ``Param.location=PATH`` + ``Param.default_from=...``
P7   Location variants           ``Param.location`` + ``Endpoint.body``
P8   PATCH = PUT minus required  ``Endpoint.derive_patch_from=<endpoint>``
P9   Conditional applicability   ``Param.applies_when="mode=low_cost"`` (help only)
P10  Int vs UUID identifiers     ``Param.type`` (``ParamType.INT`` / ``UUID``)
P11  Trailing-slash sensitivity  ``Endpoint.path`` is literal; never normalised
P12  Replace-vs-append           ``Param.replace_semantics=True`` (+ helper flags)
===  ==========================  ===================================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Product(str, Enum):
    """A product surface. Determines base URL and auth strategy (SPEC.md §4.4)."""

    LLMWHISPERER = "llmwhisperer"
    PLATFORM = "platform"
    DEPLOYMENT = "deployment"
    HITL = "hitl"
    APIHUB = "apihub"


class ParamLocation(str, Enum):
    """Where a parameter travels in the HTTP request (P7)."""

    QUERY = "query"
    BODY = "body"
    PATH = "path"
    HEADER = "header"
    FORM = "form"


class BodyKind(str, Enum):
    """How the request body is encoded (P7)."""

    NONE = "none"
    JSON = "json"
    MULTIPART = "multipart"
    BINARY_FILE = "binary_file"
    TEXT = "text"


class ParamType(str, Enum):
    """Logical parameter type. Distinguishes INT from UUID identifiers (P10)."""

    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    UUID = "uuid"
    JSON = "json"
    FILE = "file"
    DATE = "date"


class Permission(str, Enum):
    """Platform API key permission level required (SPEC.md §4.4)."""

    READ = "read"
    READ_WRITE = "read_write"
    FULL_ACCESS = "full_access"


# --------------------------------------------------------------------------- #
# Constraints (P1, P2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Constraint:
    """Base class for pre-flight validation rules.

    Constraints are checked before any network call, so a malformed invocation
    costs an exit code 2 rather than a wasted round trip and a remote 400.
    """

    params: tuple[str, ...]

    def check(self, supplied: Mapping[str, object]) -> str | None:
        """Return an error message if violated, else ``None``."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable rule, rendered into ``--help`` and ``--discover``."""
        raise NotImplementedError


@dataclass(frozen=True)
class MutuallyExclusive(Constraint):
    """Exactly one of ``params`` must be supplied (P1).

    Example: ``whisper extract`` takes ``--file`` or ``--url``, never both.
    """

    required: bool = True

    def check(self, supplied: Mapping[str, object]) -> str | None:
        present = [p for p in self.params if supplied.get(p) not in (None, (), [])]
        flags = ", ".join(f"--{p.replace('_', '-')}" for p in self.params)
        if len(present) > 1:
            given = ", ".join(f"--{p.replace('_', '-')}" for p in present)
            return f"{given} are mutually exclusive; supply exactly one of: {flags}"
        if self.required and not present:
            return f"one of {flags} is required"
        return None

    def describe(self) -> str:
        flags = " | ".join(f"--{p.replace('_', '-')}" for p in self.params)
        return f"exactly one of: {flags}" if self.required else f"at most one of: {flags}"


@dataclass(frozen=True)
class AtLeastOneOf(Constraint):
    """At least one of ``params`` must be supplied (P2).

    Example: ``file-history clear`` refuses to run without a filter, which would
    otherwise delete every record.
    """

    def check(self, supplied: Mapping[str, object]) -> str | None:
        if any(supplied.get(p) not in (None, (), []) for p in self.params):
            return None
        flags = ", ".join(f"--{p.replace('_', '-')}" for p in self.params)
        return f"at least one of {flags} is required"

    def describe(self) -> str:
        flags = " | ".join(f"--{p.replace('_', '-')}" for p in self.params)
        return f"at least one of: {flags}"


# --------------------------------------------------------------------------- #
# Param
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Param:
    """One CLI flag, and how it reaches the wire.

    ``name`` is the API's spelling (used on the wire verbatim, typos included --
    e.g. ``page_seperator``); the CLI flag is the kebab-cased form.
    """

    name: str
    type: ParamType = ParamType.STR
    location: ParamLocation = ParamLocation.QUERY
    required: bool = False
    default: object | None = None
    help: str = ""

    #: P3 - friendly value -> wire value. A plain sequence means identity mapping.
    choices: Mapping[str, str] | Sequence[str] | None = None
    #: P4 - repeatable flag, collected into a list.
    multiple: bool = False
    #: P6 - dotted path into resolved config, e.g. ``"platform.org_id"``.
    default_from: str | None = None
    #: P5 - collect arbitrary ``--flag KEY=VALUE`` pairs under this prefix.
    freeform_prefix: str | None = None
    #: P9 - documented applicability. Rendered in help; never enforced locally,
    #: because the server owns the rule and enforcing it here would guess wrong.
    applies_when: str | None = None
    #: P12 - this field replaces rather than appends server-side.
    replace_semantics: bool = False
    #: Override the derived CLI flag name (rare; e.g. to avoid a collision).
    flag: str | None = None
    #: Exclude from the request payload (client-side only, e.g. ``--save``).
    client_side: bool = False

    @property
    def cli_flag(self) -> str:
        """The long-form CLI flag, e.g. ``--word-confidence-threshold``."""
        return self.flag or f"--{self.name.replace('_', '-')}"

    @property
    def py_name(self) -> str:
        """The Python identifier used for this parameter.

        Derived from :attr:`flag` when one is set, so a renamed flag does not
        collide with a global option of the same API name. `deployment run` is
        the live case: its API parameter is `timeout`, but the CLI exposes it as
        ``--execution-timeout`` to leave ``--timeout`` meaning the HTTP timeout.
        Without this, the global flag would silently overwrite the API value.
        """
        if self.flag:
            return self.flag.lstrip("-").replace("-", "_")
        return self.name.replace("-", "_")

    @property
    def wire_name(self) -> str:
        """The parameter name as the API expects it, typos preserved."""
        return self.name

    def choice_map(self) -> dict[str, str] | None:
        """Normalise :attr:`choices` to a ``{friendly: wire}`` mapping (P3)."""
        if self.choices is None:
            return None
        if isinstance(self.choices, Mapping):
            return dict(self.choices)
        return {c: c for c in self.choices}

    def to_wire(self, value: object) -> object:
        """Translate a user-supplied value to its wire representation (P3)."""
        mapping = self.choice_map()
        if mapping and isinstance(value, str):
            return mapping.get(value, value)
        return value


# --------------------------------------------------------------------------- #
# Polling (SPEC.md §3.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PollSpec:
    """Describes how ``--wait`` drives an async execute -> poll -> retrieve flow.

    Terminal states are matched against a field in the *response body*, never the
    HTTP status code: the Unstract deployment API currently returns HTTP 422 for
    the in-progress states ``PENDING``/``EXECUTING`` (SPEC.md §6.2), a documented
    server defect. Branching on the body keeps behaviour identical before and
    after that defect is fixed.
    """

    status_endpoint: str
    status_field: str = "status"
    terminal_success: tuple[str, ...] = ()
    terminal_failure: tuple[str, ...] = ()
    handle_field: str = ""
    handle_param: str = ""
    retrieve_endpoint: str | None = None
    #: Results can be read exactly once; a second read loses data (SPEC.md §5.6).
    one_shot: bool = False


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Endpoint:
    """One CLI command, and the API call behind it."""

    name: str
    group: str
    method: str
    path: str
    product: Product
    summary: str
    params: tuple[Param, ...] = ()
    body: BodyKind = BodyKind.NONE
    #: Sub-group for three-level commands, e.g. ``platform prompt-studio file upload``.
    subgroup: str | None = None
    constraints: tuple[Constraint, ...] = ()
    poll: PollSpec | None = None
    #: Documentation file this record was authored from; the Skill's diff anchor.
    doc_source: str = ""
    permission: Permission | None = None
    #: Longer help text appended below the summary.
    description: str = ""
    #: Worked example(s) rendered into ``--help`` (SPEC.md §5.3).
    examples: tuple[str, ...] = ()
    #: A deliberate divergence from the docs; the Skill must not silently revert
    #: it (SPEC.md §8.5). Example: `/whisper-detail` is singular despite the docs
    #: index saying otherwise.
    doc_conflict: str | None = None
    #: P11 - some paths legitimately lack a trailing slash. Recorded so a test can
    #: assert intent rather than treating every missing slash as a typo.
    no_trailing_slash: bool = False
    #: Optional column hints for ``--output table`` (IMPLEMENTATION_PLAN.md M1.6).
    table_columns: tuple[str, ...] = ()
    #: Response key holding the payload for ``--output raw``.
    raw_field: str | None = None

    @property
    def command_path(self) -> tuple[str, ...]:
        """Full command path, e.g. ``("platform", "prompt-studio", "file", "upload")``."""
        parts = [self.group]
        if self.subgroup:
            parts.extend(self.subgroup.split())
        parts.append(self.name)
        return tuple(parts)

    @property
    def dotted_name(self) -> str:
        """Stable identifier, e.g. ``whisper.usage``."""
        return ".".join(self.command_path)

    def param(self, name: str) -> Param | None:
        """Look up a parameter by API name."""
        return next((p for p in self.params if p.name == name), None)

    def path_params(self) -> tuple[Param, ...]:
        return tuple(p for p in self.params if p.location is ParamLocation.PATH)

    def validate(self, supplied: Mapping[str, object]) -> list[str]:
        """Run every constraint, returning all violations (P1, P2)."""
        return [m for c in self.constraints if (m := c.check(supplied)) is not None]


def derive_patch(
    source: Endpoint,
    *,
    name: str = "patch",
    summary: str | None = None,
    keep_required: Sequence[str] = (),
) -> Endpoint:
    """Derive a PATCH endpoint from its PUT counterpart (P8).

    PATCH accepts the same fields as PUT but makes them all optional, except for
    identifiers (path params, plus anything named in ``keep_required``). Deriving
    rather than copying means a parameter added to the PUT record cannot be
    forgotten on the PATCH one -- duplication is precisely how definitions drift.
    """
    kept = set(keep_required)
    params = tuple(
        p
        if (p.location is ParamLocation.PATH or p.name in kept)
        else replace(p, required=False)
        for p in source.params
    )
    return replace(
        source,
        name=name,
        method="PATCH",
        params=params,
        summary=summary or f"Partially update. {source.summary}",
        constraints=(),
    )


def with_params(source: Endpoint, *extra: Param) -> Endpoint:
    """Return a copy of ``source`` with additional parameters appended.

    Used where a derived PATCH accepts a field its PUT counterpart does not --
    for instance `pipeline patch --active`, which has no PUT equivalent.
    """
    return replace(source, params=(*source.params, *extra))


@dataclass(frozen=True)
class HandAuthoredCommand:
    """A command with no API endpoint behind it (IMPLEMENTATION_PLAN.md §1).

    The `config` and `completion` groups operate purely locally. They are recorded
    so `--discover` can describe the whole surface while still letting an
    agent tell local operations from remote calls.
    """

    name: str
    group: str
    summary: str
    params: tuple[Param, ...] = ()
    description: str = ""
    examples: tuple[str, ...] = ()

    @property
    def command_path(self) -> tuple[str, ...]:
        return (self.group, self.name)

    @property
    def dotted_name(self) -> str:
        return ".".join(self.command_path)


__all__ = [
    "AtLeastOneOf",
    "BodyKind",
    "Constraint",
    "Endpoint",
    "HandAuthoredCommand",
    "MutuallyExclusive",
    "Param",
    "ParamLocation",
    "ParamType",
    "Permission",
    "PollSpec",
    "Product",
    "derive_patch",
    "field",
    "with_params",
]
