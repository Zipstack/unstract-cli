"""Command flags, derived from the committed API specs.

The specs are the same artifacts the published clients are generated from, so a
parameter the API gains reaches the CLI by refreshing a JSON file rather than by
hand-editing a flag list that drifts the moment nobody looks at it.

Two rules make the derivation safe to hand to a caller:

* **A flag not passed is not sent.** Every option defaults to ``None``, which
  means "absent", and the server's own default applies. Writing the spec's
  default into the request instead would pin a value the server would otherwise
  choose, and the two diverge the moment the server's default moves.
* **A falsy value is a choice, not an absence.** ``0``, ``false`` and ``""`` all
  travel; only ``None`` is filtered.

What the spec cannot express -- allowed values, short flags, wording -- comes
from the overlay, never from a guess made here.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources
from typing import Any

import click

from unstract_cli.core.overlay import overlay_for

#: Spec file per product, vendored so flags derive with no network and no
#: dependency on where the client happens to be installed from.
SPEC_FILES = {"llmwhisperer": "llmwhisperer.json", "docstudio": "docstudio.json"}

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

#: OpenAPI type -> Click type. `array` is handled separately, as repetition.
_TYPES: dict[str, click.ParamType] = {
    "string": click.STRING,
    "integer": click.INT,
    "number": click.FLOAT,
}


@cache
def load_spec(product: str) -> dict[str, Any]:
    """Read one vendored spec."""
    try:
        filename = SPEC_FILES[product]
    except KeyError:
        raise KeyError(f"No spec vendored for product {product!r}") from None
    text = (resources.files("unstract_cli.specs") / filename).read_text(encoding="utf-8")
    return json.loads(text)


def find_operation(product: str, operation_id: str) -> dict[str, Any]:
    """Look one operation up by its operationId."""
    for path, methods in load_spec(product)["paths"].items():
        for method, operation in methods.items():
            if method in _HTTP_METHODS and operation.get("operationId") == operation_id:
                return {"path": path, "method": method, **operation}
    raise KeyError(f"{product} spec declares no operation {operation_id!r}")


@dataclass(frozen=True)
class Param:
    """One request parameter, as the spec describes it."""

    name: str
    type: str = "string"
    default: Any = None
    description: str = ""
    array: bool = False
    nullable: bool = False
    required: bool = False
    choices: tuple[str, ...] = ()

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


def _from_schema(
    name: str, schema: dict[str, Any], description: str, *, required: bool = False
) -> Param:
    """Read one parameter out of its JSON schema.

    Nullability has two spellings -- a `null` branch in a type union, and 3.0's
    `nullable` keyword, which is what the deployment spec uses. The null branch
    carries no information for a flag, so the other branch decides the type.
    """
    types = schema.get("type")
    if isinstance(types, list):
        nullable = "null" in types
        remaining = [t for t in types if t != "null"]
        type_name = remaining[0] if remaining else "string"
    else:
        nullable = bool(schema.get("nullable"))
        type_name = types or "string"

    array = type_name == "array"
    if array:
        item = schema.get("items") or {}
        type_name = item.get("type", "string")

    return Param(
        name=name,
        type=type_name,
        default=schema.get("default"),
        description=(description or schema.get("description") or "").strip(),
        array=array,
        nullable=nullable,
        required=required,
        choices=tuple(schema.get("enum") or ()),
    )


def operation_params(product: str, operation_id: str) -> list[Param]:
    """Every parameter one operation accepts: query, then request body.

    Path parameters are excluded: they are the route, supplied by the command
    from configuration, not by the caller as a flag.
    """
    operation = find_operation(product, operation_id)
    params = [
        _from_schema(
            p["name"],
            p.get("schema") or {},
            p.get("description", ""),
            required=bool(p.get("required")),
        )
        for p in operation.get("parameters", [])
        if p.get("in") == "query"
    ]

    body = operation.get("requestBody", {}).get("content", {})
    for media_type, content in body.items():
        # A binary body is the document itself, passed as an argument.
        if media_type == "application/octet-stream":
            continue
        schema = content.get("schema") or {}
        if ref := schema.get("$ref"):
            schema = _resolve_ref(product, ref)
        mandatory = set(schema.get("required") or ())
        for name, prop in (schema.get("properties") or {}).items():
            params.append(
                _from_schema(
                    name,
                    prop,
                    prop.get("description", ""),
                    required=name in mandatory,
                )
            )

    return params


def client_params(method: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """The parameters a client method accepts, by name.

    The published clients are frozen, so a spec parameter the client's signature
    does not name cannot be reached at all: passing it raises ``TypeError``
    rather than sending it. Flags are intersected with this to keep the CLI's
    surface equal to what actually works.
    """
    return {
        name: p
        for name, p in inspect.signature(method).parameters.items()
        if name not in ("self", "cls")
    }


#: Python annotation -> OpenAPI type. A source-derived spec reports what the
#: endpoint reads off the wire, which can differ from what the call takes:
#: `extract_all_lines` is a string there and a `bool` in the signature.
_ANNOTATIONS: dict[Any, str] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
}


def _is_unset(value: Any) -> bool:
    """Whether a default is a generated client's "absent" sentinel.

    Matched by name rather than by import: each client ships its own ``Unset``
    inside its generated tree, and that path is regenerated wholesale.
    """
    return type(value).__name__ == "Unset"


def _from_signature(param: Param, signature: inspect.Parameter) -> Param:
    """Reconcile a spec parameter with the client signature that will carry it."""
    updates: dict[str, Any] = {}
    if (mapped := _ANNOTATIONS.get(signature.annotation)) is not None:
        updates["type"] = mapped
    if signature.default is inspect.Parameter.empty:
        # No default in the signature means the call cannot omit it.
        updates["required"] = True
    elif not _is_unset(signature.default):
        # What omitting the flag gets you: the client sends its own value. An
        # `Unset` default sends nothing, so there the spec's default is the
        # honest answer, because the server applies it.
        updates["default"] = signature.default
    return replace(param, **updates)


#: `name (type, optional): description` -- the Args entry of a Google-style
#: docstring, which is how both clients document their parameters.
_ARG_LINE = re.compile(r"^\s*(\w+)\s*(\([^)]*\))?\s*:\s*(.*)$")

#: Sentences a description restates from elsewhere. Each is anchored at the end,
#: so they are stripped in the order a description carries them. The value list
#: is matched on its opening quote, leaving prose that says "can be" alone.
_RESTATED = (
    re.compile(r"\s*Defaults to .*\.\s*$"),
    re.compile(r'\s*Can be ".*\.\s*$'),
)


def docstring_params(method: Callable[..., Any]) -> dict[str, str]:
    """Parameter descriptions from a client method's own docstring.

    The specs are generated from server code and carry no parameter
    descriptions, while the published clients document every parameter. Reading
    the docstring keeps one description per parameter, maintained where the
    parameter is implemented, instead of a second copy here that goes stale
    quietly.
    """
    doc = inspect.getdoc(method) or ""
    _, _, args = doc.partition("Args:")
    if not args:
        return {}

    out: dict[str, str] = {}
    current: str | None = None
    for line in args.splitlines():
        if not line.strip():
            continue
        if line[:1] not in " \t" or re.match(r"^\s{0,4}(Returns|Raises|Yields):", line):
            break
        if (match := _ARG_LINE.match(line)) and (match.group(2) or current is None):
            current = match.group(1)
            out[current] = match.group(3).strip()
        elif current:
            out[current] = f"{out[current]} {line.strip()}".strip()
    # The default and the allowed values are rendered from the signature and the
    # spec, so the docstring's own sentences for them are a second copy that
    # disagrees the moment either drifts.
    return {name: _strip_restated(text) for name, text in out.items() if text}


def _strip_restated(text: str) -> str:
    text = " ".join(text.split())
    for pattern in _RESTATED:
        text = pattern.sub("", text)
    return text.strip()


def _resolve_ref(product: str, ref: str) -> dict[str, Any]:
    node: Any = load_spec(product)
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _help_text(param: Param, choices: tuple[str, ...]) -> str:
    """Help for one flag: what it does, what it accepts, what omitting it means.

    The default is reported but never applied. It answers "what happens if I
    leave this out", which is the only question a default can honestly answer
    here: the CLI does not resend it, the client or the server does.
    """
    parts = [param.description] if param.description else []
    if choices:
        parts.append(f"One of: {', '.join(choices)}.")
    if param.default not in (None, "") and not param.required:
        rendered = (
            str(param.default).lower()
            if isinstance(param.default, bool)
            else str(param.default)
        )
        parts.append(f"[default: {rendered}]")
    return " ".join(parts)


def click_option(param: Param, spec_overlay: dict[str, Any]) -> click.Option:
    """Build one Click option from a spec parameter and its overlay entry."""
    entry = spec_overlay.get(param.name, {})
    # Falling back to the spec's own enum, so a hand-written list is needed only
    # to narrow one on purpose -- a copy of it goes stale as the service grows.
    choices = tuple(entry.get("choices", ())) or param.choices
    help_text = entry.get("help") or _help_text(param, choices)
    short = entry.get("short")

    if param.type == "boolean":
        # A paired flag, not `is_flag`: a parameter whose default is true cannot
        # be turned off by a flag that only knows how to turn things on, and
        # `default=None` keeps "not passed" distinct from "passed false".
        decls = [f"{param.flag}/--no-{param.name.replace('_', '-')}"]
        if short:
            decls.insert(0, short)
        return click.Option(decls, default=None, required=param.required, help=help_text)

    decls = [param.flag]
    if short:
        decls.insert(0, short)
    return click.Option(
        decls,
        type=click.Choice(choices) if choices else _TYPES.get(param.type, click.STRING),
        default=None,
        required=param.required,
        multiple=param.array,
        help=help_text,
    )


def derive_params(
    product: str,
    operation_id: str,
    *,
    client_method: Callable[..., Any] | None = None,
    exclude: tuple[str, ...] = (),
) -> list[Param]:
    """The parameters one command exposes, in spec order.

    With ``client_method``, the spec is intersected with what that method
    accepts and the method's own defaults win, because that is the value the
    caller gets by omitting the flag. A spec parameter the method does not name
    is dropped rather than offered and then rejected at the call.
    """
    spec_overlay = overlay_for(product, operation_id)
    hidden = {name for name, entry in spec_overlay.items() if entry.get("hidden")}
    accepted = client_params(client_method) if client_method is not None else None
    described = docstring_params(client_method) if client_method is not None else {}

    out: list[Param] = []
    for param in operation_params(product, operation_id):
        if param.name in exclude or param.name in hidden:
            continue
        if accepted is not None:
            if param.name not in accepted:
                continue
            param = _from_signature(param, accepted[param.name])
        if not param.description and (text := described.get(param.name)):
            param = replace(param, description=text)
        out.append(param)
    return out


def spec_options(
    product: str,
    operation_id: str,
    *,
    client_method: Callable[..., Any] | None = None,
    exclude: tuple[str, ...] = (),
) -> Callable[[Any], Any]:
    """Decorator: hang one operation's parameters off a command as options.

    ``exclude`` drops parameters the command supplies itself -- the document to
    extract is an argument, not a flag, and the CLI owns the polling that
    ``use_webhook`` would bypass.

    Applies either above or below ``@group.command()``: above it decorates a
    built command, below it a bare function that Click has yet to build.
    """
    spec_overlay = overlay_for(product, operation_id)

    def decorate(target: Any) -> Any:
        options = [
            click_option(param, spec_overlay)
            for param in derive_params(
                product, operation_id, client_method=client_method, exclude=exclude
            )
        ]
        if isinstance(target, click.Command):
            target.params.extend(options)
        else:
            # Click reads this list back in reverse, so the help lists the
            # parameters in the order the spec declares them.
            pending = getattr(target, "__click_params__", [])
            target.__click_params__ = list(reversed(options)) + pending
        return target

    return decorate


def requested(values: dict[str, Any], *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """Keep the parameters the caller actually passed.

    ``None`` is the only absence. An empty tuple from a repeatable option is one
    too -- Click spells "not passed" that way for ``multiple=True`` -- but ``0``,
    ``False`` and ``""`` are values the caller chose and must survive.
    """
    return {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in values.items()
        if name not in drop and value is not None and value != ()
    }


__all__ = [
    "SPEC_FILES",
    "Param",
    "click_option",
    "client_params",
    "derive_params",
    "docstring_params",
    "find_operation",
    "load_spec",
    "operation_params",
    "requested",
    "spec_options",
]
