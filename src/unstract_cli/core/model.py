"""Declarative endpoint model — the single source of truth for the CLI.

Every command, flag, help string, validation rule and `--discover` entry is
derived from the `Endpoint` records in `unstract_cli.endpoints`. Nothing about a
command is written twice, so help text cannot drift from behaviour, and the
bundled Claude Skill has exactly one place to edit (SPEC.md D1).

The dataclasses here are frozen: records are a contract, not runtime state.

Parameter patterns P1-P12 map onto these fields as:

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
    """One of the three products built by Unstract.

    Unstract is the company, and the name of this CLI. It builds exactly three
    products: **Document Studio**, **LLMWhisperer** and **API Hub**. Document
    Studio's API groups use `platform`/`deployment` paths on the wire and
    `UNSTRACT_*` environment variables.
    """

    DOCUMENT_STUDIO = "docstudio"
    LLMWHISPERER = "llmwhisperer"
    APIHUB = "apihub"


#: Document Studio exposes three distinct API groups, each with its own base
#: path and credentials, so they stay separate for auth and config purposes even
#: though they belong to one product (SPEC.md §4.4).
class ApiGroup(str, Enum):
    """An API surface within a product. Determines base URL and credentials."""

    #: Document Studio -- Platform Management API v1.
    PLATFORM = "platform"
    #: Document Studio -- deployed API workflow execution.
    DEPLOYMENT = "deployment"
    #: Document Studio -- Human Quality Review (Enterprise).
    HITL = "hitl"
    #: LLMWhisperer -- text extraction.
    LLMWHISPERER = "llmwhisperer"
    #: API Hub -- vertical extraction.
    APIHUB = "apihub"


#: Which product each API group belongs to.
GROUP_PRODUCT: dict[ApiGroup, Product] = {
    ApiGroup.PLATFORM: Product.DOCUMENT_STUDIO,
    ApiGroup.DEPLOYMENT: Product.DOCUMENT_STUDIO,
    ApiGroup.HITL: Product.DOCUMENT_STUDIO,
    ApiGroup.LLMWHISPERER: Product.LLMWHISPERER,
    ApiGroup.APIHUB: Product.APIHUB,
}

#: Human-readable product names, for help text and `--discover`.
PRODUCT_LABELS: dict[Product, str] = {
    Product.DOCUMENT_STUDIO: "Document Studio",
    Product.LLMWHISPERER: "LLMWhisperer",
    Product.APIHUB: "API Hub",
}


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
class RequiredUnless(Constraint):
    """``params`` are required *unless* another flag holds a sentinel value.

    Encodes a rule the plain required/optional split cannot: a field that is
    mandatory in general but genuinely unused in one configuration. The motivating
    case is ``profile create --chunk-size 0``, meaning "no RAG" -- the vector store
    and embedding model are then never consulted, so demanding them makes the
    caller invent a value for something that will not be read.

    **Currently unused by any shipped record** (the profile-create records keep
    those fields plainly required). Retained, with tests, because the constraint
    is the correct encoding if that rule is adopted; delete it rather than let it
    drift if it is not.

    Marking such a field ``required=False`` alone would lose the check in the
    common case; this keeps it, conditioned on the flag that actually decides.
    """

    #: The flag whose value relaxes the requirement, e.g. ``"chunk_size"``.
    unless: str = ""
    #: Values of :attr:`unless` that switch the requirement off.
    unless_values: tuple[object, ...] = ()

    def _relaxed(self, supplied: Mapping[str, object]) -> bool:
        value = supplied.get(self.unless)
        # Compare as strings so 0 and "0" behave identically: Click hands the
        # value through typed, while a test or a config default may not.
        return any(str(value) == str(v) for v in self.unless_values)

    def check(self, supplied: Mapping[str, object]) -> str | None:
        if self._relaxed(supplied):
            return None
        missing = [p for p in self.params if supplied.get(p) in (None, (), [])]
        if not missing:
            return None
        flags = ", ".join(f"--{p.replace('_', '-')}" for p in missing)
        relaxers = " or ".join(f"--{self.unless.replace('_', '-')} {v}" for v in self.unless_values)
        return f"{flags} is required unless {relaxers} is set"

    def describe(self) -> str:
        flags = ", ".join(f"--{p.replace('_', '-')}" for p in self.params)
        relaxers = " or ".join(f"--{self.unless.replace('_', '-')} {v}" for v in self.unless_values)
        return f"{flags} required unless {relaxers}"


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
    #: P6 - dotted path into resolved config, e.g. ``"platform.org_id"``. A
    #: whitespace-separated list is tried in order, first resolved value winning.
    #: `deployment run` uses this to fall back to the platform block's org_id: the
    #: deployment block is a separate, initially-empty config section, and an
    #: org_id already set for the platform API is the same organization.
    default_from: str | None = None

    @property
    def default_sources(self) -> tuple[str, ...]:
        """The config paths tried, in order, for this parameter's default."""
        return tuple(self.default_from.split()) if self.default_from else ()
    #: P5 - collect arbitrary ``--flag KEY=VALUE`` pairs under this prefix.
    freeform_prefix: str | None = None
    #: P9 - documented applicability. Rendered in help; never enforced locally,
    #: because the server owns the rule and enforcing it here would guess wrong.
    applies_when: str | None = None
    #: P12 - this field replaces rather than appends server-side.
    replace_semantics: bool = False
    #: This path parameter's wire value intentionally spans path segments, so it
    #: must not be percent-encoded. Only `share --resource` qualifies: its
    #: friendly name `api-deployment` maps to the wire value `api/deployment`.
    #: Everything else is encoded, so a value like `../../admin` cannot traverse.
    spans_path_segments: bool = False
    #: Override the derived CLI flag name (rare; e.g. to avoid a collision).
    flag: str | None = None
    #: Exclude from the request payload (client-side only, e.g. ``--save``).
    client_side: bool = False
    #: Copy this PATH param into the JSON body as well. A defence against a server
    #: that reads an identifier only from the body and orphans the record when it
    #: is absent. The URL still
    #: carries the value; this just also sends it in the body under :attr:`name`.
    mirror_to_body: bool = False
    #: Body field name for the mirrored value, when the body spells the identifier
    #: differently from the path. `api-deployment key create` is the live case: the
    #: URL takes ``api_id`` while the body wants that same value as ``api``, so both
    #: had to be passed by hand. Implies :attr:`mirror_to_body`.
    mirror_as: str | None = None

    @property
    def mirrors(self) -> bool:
        """Whether this PATH param is also copied into the JSON body."""
        return self.mirror_to_body or self.mirror_as is not None

    @property
    def body_name(self) -> str:
        """The name this parameter takes in the body when mirrored."""
        return self.mirror_as or self.name

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
    #: Field name(s) holding the terminal state in the *status endpoint's* body.
    #: A tuple is tried in order, because the run POST and the status GET can spell
    #: the same state differently: the deployment run response nests
    #: ``execution_status`` under ``message``, while the status GET returns a
    #: top-level ``status`` (and its ``message`` is the *result*, not a nested
    #: object). The poll reads the status endpoint, so ``status`` must win there --
    #: a mismatch means the terminal state goes unrecognised, the one-shot result
    #: is consumed on that read, and the next poll returns HTTP 406.
    status_field: str | tuple[str, ...] = "status"
    terminal_success: tuple[str, ...] = ()
    terminal_failure: tuple[str, ...] = ()
    #: States that mean "keep polling". Declaring them explicitly lets an
    #: *unrecognised* status fail loudly instead of being mistaken for progress
    #: -- on a one-shot store the first poll already consumed the result, so
    #: polling on until timeout loses it.
    #:
    #: **Empty disables that check**, deliberately: an API whose intermediate
    #: states are not exhaustively documented cannot be enumerated safely from
    #: the outside, and guessing would turn a working poll into a hard failure
    #: the first time an unlisted state appeared. Leaving it empty is therefore
    #: a real choice, not an omission -- say which it is at the call site.
    in_progress: tuple[str, ...] = ()
    handle_field: str = ""
    handle_param: str = ""
    #: Fallback for a response that carries the handle only inside a URL, as
    #: ``(body_field, query_param)``. The deployment run POST is the live case:
    #: its body has no ``execution_id`` at all -- the id exists solely in the
    #: ``status_api`` query string -- so without this `--wait` cannot poll and
    #: silently returns the PENDING stub as though it were the result.
    handle_from_query: tuple[str, str] | None = None
    #: Values to forward from the *original* request into each poll of the status
    #: endpoint. Without this the status record's own defaults apply, which for
    #: ``include_metadata`` means the server strips the metadata the user asked
    #: for and, on a one-shot store, discards it permanently.
    poll_carry: tuple[str, ...] = ()
    retrieve_endpoint: str | None = None
    #: Values to forward from the *original* request into the retrieve call. Each
    #: entry is either a py_name carried as-is, or a ``(source, dest)`` pair that
    #: renames it -- the retrieve endpoint often spells the same identifier
    #: differently (fetch-response's ``id``/``document_id`` are the Output
    #: Manager's ``prompt_id``/``document_manager``). Without the rename the
    #: retrieve would return every row for the tool, not the one prompt+document
    #: the caller ran. The retrieve is otherwise keyed by the poll handle, but some
    #: result stores are keyed by an original-request identifier instead
    #: (prompt-studio reads its Output Manager by ``tool_id``, not by ``task_id``).
    retrieve_carry: tuple[str | tuple[str, str], ...] = ()
    #: Suppress passing the poll handle into the retrieve call. Set when the
    #: retrieve endpoint is keyed only by :attr:`retrieve_carry` values and would
    #: reject an unexpected handle parameter.
    retrieve_omits_handle: bool = False
    #: Constant param values injected into the retrieve call, keyed by py_name.
    #: Used where the retrieve endpoint needs a fixed flag the original request did
    #: not carry (single-pass results are read with ``is_single_pass_extract=true``).
    retrieve_extra: tuple[tuple[str, object], ...] = ()
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
    #: The API surface this endpoint belongs to, which fixes its base URL and
    #: credentials. The owning product is derived from it, never stored twice.
    api: ApiGroup
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
    #: Optional column hints for ``--output table``.
    table_columns: tuple[str, ...] = ()
    #: Response key holding the payload for ``--output raw``.
    raw_field: str | None = None
    #: Response fields that must be non-null on success, else the call is treated
    #: as a failure despite a 2xx status. Guards silent-orphan defects where the
    #: server returns 201 but leaves a linking field NULL.
    require_response_fields: tuple[str, ...] = ()
    #: True when reading this endpoint *destroys* the result it returns, so a
    #: retry after a lost response yields 406 rather than the data. Set on the
    #: destructive read itself; `PollSpec.one_shot` covers the poll-driven case.
    consumes_result: bool = False
    #: Body states that mean "this response IS the finished result", declared on
    #: the endpoint that *returns* it rather than on whoever polls it. A status
    #: endpoint carries no ``poll`` of its own, so without this a caller reading
    #: it directly -- `deployment status` with no --wait -- has no way to tell a
    #: completed result from an error, and the already-consumed heuristic
    #: discards a successful one-shot read whose text happens to say "already
    #: delivered".
    terminal_success: tuple[str, ...] = ()
    #: Field name(s) holding that state. Mirrors ``PollSpec.status_field``.
    status_field: str | tuple[str, ...] = "status"

    @property
    def product(self) -> Product:
        """The product this endpoint belongs to (derived, never stored)."""
        return GROUP_PRODUCT[self.api]

    @property
    def product_label(self) -> str:
        """Display name, e.g. ``"Document Studio"``."""
        return PRODUCT_LABELS[self.product]

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

    Defaults are cleared as well as required-ness. A PUT default describes the
    value to send when the caller supplies nothing *for a full replacement*; on a
    partial update it would be sent for every field the user never mentioned. That
    turned `pipeline patch --id X --cron-string ...` into a request that also set
    ``pipeline_type=DEFAULT``, silently converting a live ETL pipeline, and made
    `api-deployment patch --description ...` re-enable a deactivated deployment
    and revoke org sharing. A PATCH must carry only what the user actually passed.
    """
    kept = set(keep_required)
    params = tuple(
        p
        if (p.location is ParamLocation.PATH or p.name in kept)
        else replace(p, required=False, default=None)
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


__all__ = [
    "AtLeastOneOf",
    "BodyKind",
    "Constraint",
    "Endpoint",
    "MutuallyExclusive",
    "Param",
    "ParamLocation",
    "ParamType",
    "Permission",
    "PollSpec",
    "Product",
    "RequiredUnless",
    "derive_patch",
    "field",
    "with_params",
]
