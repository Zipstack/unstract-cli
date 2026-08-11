#!/usr/bin/env python
"""Generate the LLMWhisperer OpenAPI spec by AST-walking the Flask controller.

LLMWhisperer is Flask with no serializers, so nothing can be introspected at
runtime. The handlers are regular enough to read statically:

    @api_v2.route("/whisper", methods=["POST"])   -> path + methods
    request.args.get("name", default)             -> parameter + default
    int(x) / float(x) / x.lower() == "true"       -> type

Everything the walk cannot infer (request bodies, response shapes, tags,
descriptions) comes from ``specs/overlay/llmwhisperer.yaml``, merged by
operationId.

Note: LLMWhisperer reads its parameters from the **query string even on
POST /whisper** — the body is the raw file bytes.

Usage:
    python tools/gen_llmw_spec.py --out specs/llmwhisperer.json
"""

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / "zipstuff" / "unstract-llm-whisperer"
BLUEPRINT = "api_v2"


def const_map(constants_py: Path) -> dict[str, Any]:
    """`{"DefaultValues.DEFAULT_MEDIAN_FILTER_SIZE": 0, ...}` from constants.py."""
    tree = ast.parse(constants_py.read_text())
    flat: dict[str, Any] = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for stmt in cls.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            key = f"{cls.name}.{target.id}"
            if isinstance(stmt.value, ast.Constant):
                flat[key] = stmt.value.value
            elif isinstance(stmt.value, ast.Attribute):
                flat[key] = ("ref", ast.unparse(stmt.value))
    # One resolution pass is enough for the cross-class references present.
    for key, val in list(flat.items()):
        if isinstance(val, tuple) and val[0] == "ref":
            flat[key] = flat.get(val[1])
    return flat


def literal(node: ast.expr, consts: dict[str, Any]) -> Any:
    """Best-effort static value of a default expression."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        dotted = ast.unparse(node)
        if dotted in consts:
            return consts[dotted]
        # `Mode.FORM.value` -> look up `Mode.FORM`
        if dotted.endswith(".value") and dotted[: -len(".value")] in consts:
            return consts[dotted[: -len(".value")]]
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"str", "int", "float"} and node.args:
            return literal(node.args[0], consts)
    return None


def routes(node: ast.FunctionDef) -> list[tuple[str, list[str]]]:
    found = []
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        f = dec.func
        if not (
            isinstance(f, ast.Attribute)
            and f.attr == "route"
            and isinstance(f.value, ast.Name)
            and f.value.id == BLUEPRINT
        ):
            continue
        path = dec.args[0].value if dec.args else None
        methods = ["GET"]
        for kw in dec.keywords:
            if kw.arg == "methods":
                methods = [e.value for e in kw.value.elts]
        found.append((path, methods))
    return found


def coercions(fn: ast.AST) -> tuple[dict[str, str], set[str]]:
    """`({varname: type}, {param_name_coerced_inline})` from later coercions.

    Two shapes occur: the value is assigned to a local and coerced later, or
    `request.args.get(...)` is chained straight into `.lower() == "true"`.
    """
    by_var: dict[str, str] = {}
    inline_bool: set[str] = set()

    def arg_name(node: ast.expr) -> str | None:
        """The param name if `node` is a `request.args.get("x", ...)` call."""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and ast.unparse(node.func.value) == "request.args"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            return node.args[0].value
        return None

    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.args:
            if n.func.id in {"int", "float"} and isinstance(n.args[0], ast.Name):
                by_var[n.args[0].id] = "integer" if n.func.id == "int" else "number"
        if isinstance(n, ast.Compare) and n.comparators:
            left, right = n.left, n.comparators[0]
            if not (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Attribute)
                and left.func.attr == "lower"
                and isinstance(right, ast.Constant)
                and right.value in {"true", "false"}
            ):
                continue
            recv = left.func.value
            if isinstance(recv, ast.Name):
                by_var[recv.id] = "boolean"
            elif (name := arg_name(recv)) is not None:
                inline_bool.add(name)
    return by_var, inline_bool


TYPE_OF_DEFAULT = {bool: "boolean", int: "integer", float: "number", str: "string"}


def params(fn: ast.AST, consts: dict[str, Any]) -> list[dict[str, Any]]:
    coerced, inline_bool = coercions(fn)
    # target var name for each `x = request.args.get(...)`
    assigned: dict[int, str] = {}
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                assigned[id(stmt.value)] = stmt.targets[0].id

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (
            isinstance(f, ast.Attribute)
            and f.attr == "get"
            and ast.unparse(f.value) == "request.args"
        ):
            continue
        if not n.args or not isinstance(n.args[0], ast.Constant):
            continue
        name = n.args[0].value
        if name in seen:
            continue
        seen.add(name)
        default = literal(n.args[1], consts) if len(n.args) > 1 else None
        var = assigned.get(id(n))
        typ = (
            "boolean"
            if name in inline_bool
            else coerced.get(var or "") or TYPE_OF_DEFAULT.get(type(default), "string")
        )
        # `"false"` as a default of a boolean flag is a string in source only.
        if typ == "boolean" and isinstance(default, str):
            default = default.lower() == "true"
        schema: dict[str, Any] = {"type": typ}
        if default is not None:
            schema["default"] = default
        out.append(
            {
                "name": name,
                "in": "query",
                "required": False,
                "schema": schema,
            }
        )
    return sorted(out, key=lambda p: p["name"])


def helper_params(source: Path, consts: dict[str, Any]) -> dict[str, list[dict]]:
    """`{helper_name: params}` for every function outside the controller that
    reads `request.args` — handlers delegate parsing and the params vanish.

    Matched by bare function name, one level deep — add import resolution if two
    helpers ever share a name.
    """
    out: dict[str, list[dict]] = {}
    for py in sorted(source.rglob("*.py")):
        if py.name == "controller_v2.py":
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and (found := params(node, consts)):
                out[node.name] = found
    return out


def called_names(fn: ast.AST) -> set[str]:
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def build(controller: Path, consts: dict[str, Any], overlay: dict[str, Any]) -> dict:
    tree = ast.parse(controller.read_text())
    base = overlay.get("_base", {})
    helpers = helper_params(controller.parent.parent, consts)
    paths: dict[str, Any] = {}
    walked = 0

    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for path, methods in routes(fn):
            walked += 1
            full = base.get("basePath", "/api/v2") + path
            over = overlay.get("operations", {}).get(fn.name, {})
            query = params(fn, consts)
            seen = {p["name"] for p in query}
            for helper in called_names(fn) & helpers.keys():
                query += [p for p in helpers[helper] if p["name"] not in seen]
                seen |= {p["name"] for p in helpers[helper]}
            # A helper that takes the argument name as a parameter hides every
            # literal passed at the call site, so the walk cannot recover it.
            for name, schema in over.get("addParams", {}).items():
                if name not in seen:
                    query.append(
                        {
                            "name": name,
                            "in": "query",
                            "required": False,
                            "schema": dict(schema),
                        }
                    )
            query.sort(key=lambda p: p["name"])
            for p in query:
                patch = over.get("params", {}).get(p["name"])
                if patch:
                    p["schema"].update(patch)
            for method in methods:
                op_id = over.get("operationId", fn.name)
                if len(methods) > 1:
                    op_id = f"{op_id}_{method.lower()}"
                op: dict[str, Any] = {
                    "operationId": op_id,
                    "tags": over.get("tags", ["whisper"]),
                    "summary": over.get("summary", fn.name.replace("_", " ")),
                    "parameters": query,
                    "responses": over.get("responses") or base["defaultResponses"],
                }
                if method in {"POST", "PUT", "PATCH"} and over.get("requestBody"):
                    op["requestBody"] = over["requestBody"]
                paths.setdefault(full, {})[method.lower()] = op

    return {
        "openapi": "3.0.3",
        "info": {"title": "Unstract LLMWhisperer", "version": "v2"},
        "servers": [
            {"url": base.get("server", "https://llmwhisperer-api.unstract.com")}
        ],
        "paths": paths,
        "components": {
            "schemas": overlay.get("schemas", {}),
            "securitySchemes": base.get("securitySchemes", {}),
        },
        "security": base.get("security", []),
    }, walked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument(
        "--overlay", type=Path, default=REPO / "specs/overlay/llmwhisperer.yaml"
    )
    ap.add_argument("--out", type=Path, default=REPO / "specs/llmwhisperer.json")
    args = ap.parse_args()

    backend = args.source / "backend"
    controller = backend / "app/controller/controller_v2.py"
    if not controller.exists():
        print(f"no controller at {controller}", file=sys.stderr)
        return 2

    overlay = yaml.safe_load(args.overlay.read_text())
    spec, walked = build(controller, const_map(backend / "app/constants.py"), overlay)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")

    n_params = sum(
        len(op["parameters"]) for p in spec["paths"].values() for op in p.values()
    )
    print(
        f"{args.out}: {walked} routes walked, {len(spec['paths'])} paths, "
        f"{sum(len(p) for p in spec['paths'].values())} operations, {n_params} params"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
