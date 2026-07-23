"""Request construction and execution (SPEC.md §4.4, §5.7).

Owns auth injection, path substitution, retry/backoff, redaction and `--dry-run`.
Built once here so no individual command can forget any of it.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from unstract_cli.config.loader import ResolvedConfig
from unstract_cli.core.errors import (
    CLIError,
    ExitCode,
    exit_code_for_status,
    hint_for,
    is_retryable,
    redact_headers,
    redact_value,
)
from unstract_cli.core.model import (
    ApiGroup,
    BodyKind,
    Endpoint,
    Param,
    ParamLocation,
    ParamType,
)

#: Headers Kong injects downstream from the `apikey` lookup (SPEC.md §4.4).
#: The CLI must never send these: they are gateway-supplied, and a client-set
#: value would either be overwritten or -- worse -- be trusted as tenancy claims.
GATEWAY_INJECTED_HEADERS = frozenset(
    {
        "x-subscription-id",
        "x-subscription-name",
        "x-user-id",
        "x-product-id",
    }
)

USER_AGENT = "unstract-cli/0.1.0"


@dataclass
class RequestPlan:
    """A fully resolved request, ready to send or to print under ``--dry-run``."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    json_body: Any = None
    data: Any = None
    files: list[tuple[str, tuple[str, bytes, str]]] = field(default_factory=list)
    content: bytes | None = None
    #: Secret literals to scrub from any output.
    secrets: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        """Redacted, JSON-safe description for ``--dry-run`` (SPEC.md §5.7)."""
        body: Any = None
        if self.json_body is not None:
            body = redact_value(self.json_body)
        elif self.files:
            body = {
                "multipart": [
                    {"field": name, "filename": meta[0], "bytes": len(meta[1])}
                    for name, meta in self.files
                ]
            }
        elif self.content is not None:
            body = {"binary_bytes": len(self.content)}
        elif self.data:
            body = redact_value(self.data)
        return {
            "method": self.method,
            "url": self.url,
            "headers": redact_headers(self.headers),
            "query": redact_value(self.params),
            "body": body,
        }


def auth_headers(product: ApiGroup, config: ResolvedConfig) -> dict[str, str]:
    """Build auth headers for one API group (SPEC.md §4.4).

    Three products, three schemes -- unifying them is the CLI's core promise.
    Document Studio's three API groups share the Bearer scheme but each carries
    its own key, so credentials are still resolved per group.
    """
    match product:
        case ApiGroup.LLMWHISPERER:
            return {"unstract-key": config.require(product, "api_key")}
        case ApiGroup.PLATFORM | ApiGroup.DEPLOYMENT | ApiGroup.HITL:
            return {"Authorization": f"Bearer {config.require(product, 'api_key')}"}
        case ApiGroup.APIHUB:
            # `apikey` only. Kong resolves it to subscription/user identity.
            headers = {"apikey": config.require(product, "api_key")}
            if key := config.get(product, "llmwhisperer_key"):
                headers["X-LLMWhisperer-API-Key"] = key
            if key := config.get(product, "anthropic_key"):
                headers["X-Anthropic-API-Key"] = key
            return headers
    raise CLIError(f"Unknown API group: {product}", ExitCode.GENERIC)  # pragma: no cover


def collect_secrets(config: ResolvedConfig) -> list[str]:
    """Every credential in play, so output can be scrubbed of all of them."""
    secrets: list[str] = []
    for product in ApiGroup:
        for key in ("api_key", "llmwhisperer_key", "anthropic_key"):
            try:
                if (value := config.get(product, key)) and isinstance(value, str):
                    secrets.append(value)
            except Exception:  # pragma: no cover - resolution issues aren't fatal here
                continue
    return secrets


def build_url(endpoint: Endpoint, config: ResolvedConfig, values: dict[str, Any]) -> str:
    """Resolve the base URL and substitute path parameters.

    ``Endpoint.path`` is used **verbatim** (P11): the upstream API is genuinely
    inconsistent about trailing slashes and about `profilemanager` vs
    `profile-manager`, and "tidying" a path yields a 404.
    """
    base = config.get(endpoint.api, "base_url")
    if not base:
        raise CLIError(
            f"No base URL configured for {endpoint.api.value}.",
            ExitCode.USAGE,
            hint=(
                "Set --base-url or the corresponding environment variable. "
                "API Hub has no default base URL (SPEC §11.1)."
            ),
        )

    path = endpoint.path
    for param in endpoint.path_params():
        value = values.get(param.py_name)
        if value is None:
            value = _config_default(param, config)
        if value is None:
            raise CLIError(
                f"Missing required path parameter {param.cli_flag}.",
                ExitCode.USAGE,
                hint=f"Pass {param.cli_flag}, or configure it in your profile.",
            )
        placeholder = "{" + param.name + "}"
        path = path.replace(placeholder, str(param.to_wire(value)))

    return f"{str(base).rstrip('/')}/{path.lstrip('/')}"


def _config_default(param: Param, config: ResolvedConfig) -> Any:
    """Resolve a parameter's profile default, trying each source in order.

    ``default_from`` may name several config paths; the first that resolves wins.
    That lets one setting stand in for another where they are genuinely the same
    value held in two blocks -- `deployment run`'s org_id falls back to the
    platform block, which is the same organization and is usually already set
    (GOTCHAS #7). An empty string counts as unset, since that is what a
    half-filled `config init` stub leaves behind.
    """
    for source in param.default_sources:
        product, _, key = source.partition(".")
        if (value := config.get(product, key)) not in (None, ""):
            return value
    return None


def _guess_content_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _parse_json_param(param: Param, value: object) -> object:
    """Parse a ``ParamType.JSON`` value from a string into a real object (BUG 1).

    Click hands JSON params through as plain strings, so without this the body
    field would hold a quoted string (``"data": "{...}"``) and the server would
    reject it as ``Expected a dictionary of items but got type "str"``. Accepts
    either inline JSON or an ``@path/to/file.json`` reference -- large payloads
    such as an exported prompts file are painful to pass inline and hit shell
    argument limits. A value that is already parsed (a dict/list, e.g. a test
    passing a native object) is returned unchanged.
    """
    if param.type is not ParamType.JSON or not isinstance(value, str):
        return value
    text = value
    if text.startswith("@"):
        ref = Path(text[1:]).expanduser()
        try:
            text = ref.read_text()
        except OSError as exc:
            raise CLIError(
                f"{param.cli_flag} could not read {ref}: {exc}", ExitCode.USAGE
            ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CLIError(
            f"{param.cli_flag} expects valid JSON (or @file.json): {exc}",
            ExitCode.USAGE,
        ) from exc


def build_request(
    endpoint: Endpoint,
    config: ResolvedConfig,
    values: dict[str, Any],
    *,
    extra_query: dict[str, Any] | None = None,
) -> RequestPlan:
    """Turn resolved flag values into a concrete request.

    Constraints are checked first: a malformed invocation should cost exit code 2,
    not a network round trip and a remote rejection.
    """
    if violations := endpoint.validate(values):
        raise CLIError(
            "; ".join(violations),
            ExitCode.USAGE,
            endpoint=f"{endpoint.method} {endpoint.path}",
        )

    headers = {"User-Agent": USER_AGENT, **auth_headers(endpoint.api, config)}
    params: dict[str, Any] = {}
    body: dict[str, Any] = {}
    form: dict[str, Any] = {}
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    content: bytes | None = None

    for param in endpoint.params:
        if param.client_side:
            continue
        if param.location is ParamLocation.PATH:
            # A PATH param normally travels only in the URL. Mirroring
            # additionally copies it into the JSON body, defending against a
            # server that links a record by a body field and orphans it when the
            # field is absent (BUG 2), or that simply wants the same identifier
            # twice under two names (`api_id` in the URL, `api` in the body --
            # GOTCHAS #6). `body_name` is the path name unless `mirror_as` renames it.
            if param.mirrors and (raw := values.get(param.py_name)) is not None:
                body[param.body_name] = param.to_wire(raw)
            continue

        raw = values.get(param.py_name)

        if param.freeform_prefix and raw:
            # P5: `--ext-param foo=bar` -> `ext_foo=bar`, so parameters newer than
            # the CLI remain reachable without a release.
            for item in raw if isinstance(raw, (list, tuple)) else [raw]:
                key, sep, val = str(item).partition("=")
                if not sep:
                    raise CLIError(
                        f"{param.cli_flag} expects KEY=VALUE, got {item!r}",
                        ExitCode.USAGE,
                    )
                params[f"{param.freeform_prefix}{key.strip()}"] = val.strip()
            continue

        if raw is None:
            raw = _config_default(param, config)
        if raw is None:
            raw = param.default
        if raw is None or (isinstance(raw, (list, tuple)) and not raw):
            if param.required:
                raise CLIError(
                    f"Missing required parameter {param.cli_flag}.", ExitCode.USAGE
                )
            continue

        value = _parse_json_param(param, param.to_wire(raw))

        match param.location.value:
            case "query":
                params[param.name] = value
            case "body":
                body[param.name] = value
            case "header":
                headers[param.name] = str(value)
            case "form":
                if param.type.value == "file":
                    for item in value if isinstance(value, (list, tuple)) else [value]:
                        p = Path(str(item))
                        if not p.exists():
                            raise CLIError(f"File not found: {p}", ExitCode.USAGE)
                        files.append(
                            (param.name, (p.name, p.read_bytes(), _guess_content_type(p)))
                        )
                elif param.type is ParamType.JSON and isinstance(value, (dict, list)):
                    # A multipart form field carries a JSON object as a *string*;
                    # the server (a DRF form field) `json.loads` it. Left as a
                    # dict, httpx cannot form-encode it. Re-serialize here so BODY
                    # gets an object and FORM gets the string it expects -- the
                    # double-encode guard the JSON parse would otherwise trip.
                    form[param.name] = json.dumps(value)
                else:
                    form[param.name] = value

    if extra_query:
        params.update(extra_query)

    if endpoint.body is BodyKind.BINARY_FILE:
        # LLMWhisperer takes the document as a raw octet-stream body.
        if file_value := values.get("file"):
            p = Path(str(file_value))
            if not p.exists():
                raise CLIError(f"File not found: {p}", ExitCode.USAGE)
            content = p.read_bytes()
            headers["Content-Type"] = "application/octet-stream"
        elif url_value := values.get("url"):
            # `url_in_post` sends the source URL as a plain-text body instead.
            content = str(url_value).encode()
            headers["Content-Type"] = "text/plain"
            params["url_in_post"] = "true"

    # Defence in depth: even if a record mistakenly declared one, gateway-injected
    # headers must never leave this process (SPEC.md §4.4).
    headers = {k: v for k, v in headers.items() if k.lower() not in GATEWAY_INJECTED_HEADERS}

    return RequestPlan(
        method=endpoint.method,
        url=build_url(endpoint, config, values),
        headers=headers,
        params={k: _stringify(v) for k, v in params.items() if v is not None},
        json_body=body if body and endpoint.body is BodyKind.JSON else None,
        data=form or None,
        files=files,
        content=content,
        secrets=collect_secrets(config),
    )


def _stringify(value: Any) -> Any:
    """Booleans must travel as `true`/`false`, not Python's `True`/`False`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return [_stringify(v) for v in value]
    if isinstance(value, dict):
        return json.dumps(value)
    return value


@dataclass
class Response:
    """A completed HTTP exchange."""

    status: int
    payload: Any
    headers: dict[str, str]
    raw: bytes

    @property
    def is_json(self) -> bool:
        return isinstance(self.payload, (dict, list))


def _parse(response: httpx.Response) -> Any:
    ctype = response.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return response.json()
        except ValueError:
            # Some endpoints emit several JSON objects concatenated (a streamed
            # NDJSON-ish body), which a single parse rejects with "Extra data" and
            # which breaks any `| json` consumer (CAPTURE2 DOC 6). Recover them into
            # one array so the CLI still emits exactly one valid JSON document; fall
            # back to raw text only if they are not clean concatenated JSON.
            if (parts := _split_concatenated_json(response.text)) is not None:
                return parts
            return response.text
    if ctype.startswith("text/") or not ctype:
        return response.text
    return response.content


def _split_concatenated_json(text: str) -> list[Any] | None:
    """Parse a run of back-to-back JSON values into a list, or None if it isn't one.

    Returns None unless there are at least two values (a single value would have
    parsed already), so a genuinely malformed body still falls through to raw text.
    """
    decoder = json.JSONDecoder()
    items: list[Any] = []
    idx, length = 0, len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except ValueError:
            return None
        items.append(value)
        idx = end
    return items if len(items) > 1 else None


def execute(
    plan: RequestPlan,
    *,
    endpoint: Endpoint | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    client: httpx.Client | None = None,
    sleep=time.sleep,
) -> Response:
    """Send a request, retrying only where retrying is safe (SPEC.md §5.7).

    Retries apply to 429 and 5xx. They never apply to 4xx: the server has already
    rejected the request on its merits, and for one-shot reads a blind retry can
    silently consume a result the first attempt already delivered.
    """
    owns_client = client is None
    # Note: following redirects means a wrong trailing slash would be silently
    # "corrected" by the server rather than failing loudly, which softens the P11
    # literal-path guarantee. Redirects are kept because these APIs legitimately
    # use them; the `no_trailing_slash` test is what actually protects paths.
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    last_error: httpx.HTTPError | None = None

    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.request(
                    plan.method,
                    plan.url,
                    headers=plan.headers,
                    params=plan.params or None,
                    json=plan.json_body,
                    data=plan.data,
                    files=plan.files or None,
                    content=plan.content,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                sleep(_backoff(attempt))
                continue

            if is_retryable(response.status_code) and attempt < max_retries:
                sleep(_retry_after(response) or _backoff(attempt))
                continue

            return Response(
                status=response.status_code,
                payload=_parse(response),
                headers=dict(response.headers),
                raw=response.content,
            )
    finally:
        if owns_client:
            client.close()

    raise CLIError(
        f"Request failed after {max_retries + 1} attempt(s): {last_error}",
        ExitCode.SERVER_ERROR,
        endpoint=f"{plan.method} {plan.url}",
        retryable=True,
        hint="Check network connectivity and the configured base URL.",
    )


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so retries don't synchronise across agents."""
    return min(2.0**attempt, 30.0) * (0.5 + random.random() / 2)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None


#: Body phrases meaning "this result was already delivered". LLMWhisperer signals
#: this with HTTP 200 and a message, not an error status, so a status-only check
#: would report success while the agent silently loses the data (SPEC.md §5.6).
_CONSUMED_PHRASES = ("already delivered", "already acknowledged", "already retrieved")


def _looks_consumed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    message = str(payload.get("message", "")).lower()
    return any(phrase in message for phrase in _CONSUMED_PHRASES)


def raise_for_status(response: Response, endpoint: Endpoint | None = None) -> None:
    """Convert an unsuccessful -- or deceptively successful -- response into an error."""
    # Checked before the status check: this arrives as a 200.
    if _looks_consumed(response.payload):
        raise CLIError(
            _extract_message(response.payload) or "Result already retrieved.",
            ExitCode.ALREADY_CONSUMED,
            http_status=response.status,
            details=response.payload if isinstance(response.payload, dict) else None,
            endpoint=f"{endpoint.method} {endpoint.path}" if endpoint else None,
            hint=hint_for(406),
            retryable=False,
        )

    if response.status < 400:
        return

    message = _extract_message(response.payload) or f"HTTP {response.status}"
    code = exit_code_for_status(response.status)

    # The deployment API returns 406 for "result already acknowledged"; treat any
    # already-delivered signal as the dedicated one-shot exit code (SPEC.md §5.6).
    if response.status == 406 or "already" in message.lower() and "deliver" in message.lower():
        code = ExitCode.ALREADY_CONSUMED

    raise CLIError(
        message,
        code,
        http_status=response.status,
        details=response.payload if isinstance(response.payload, (dict, list)) else None,
        endpoint=f"{endpoint.method} {endpoint.path}" if endpoint else None,
        hint=hint_for(response.status, endpoint.path if endpoint else None, message),
        retryable=is_retryable(response.status),
    )


def _extract_message(payload: Any) -> str | None:
    """Pull a human message out of the several error shapes these APIs use."""
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key in ("message", "detail", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        parts = [
            e.get("detail") for e in errors if isinstance(e, dict) and e.get("detail")
        ]
        if parts:
            return "; ".join(str(p) for p in parts)
    return None


__all__ = [
    "GATEWAY_INJECTED_HEADERS",
    "RequestPlan",
    "Response",
    "auth_headers",
    "build_request",
    "build_url",
    "collect_secrets",
    "execute",
    "raise_for_status",
]
