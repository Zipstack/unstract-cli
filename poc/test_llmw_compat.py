"""Offline compatibility check for the generated LLMWhisperer facade.

Asserts the facade is a drop-in for `LLMWhispererClientV2` — same public methods,
same signatures — and that every operation reaches the wire with the parameter
names the service actually reads. Runs with no network.
"""

import inspect
import sys
from pathlib import Path

import httpx
import requests
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmw_facade import GeneratedLLMWhispererClientV2  # noqa: E402

FACADE_ONLY = {"whisper"}  # gains **extra; compared field by field below


def public(cls):
    return {
        n: m
        for n, m in inspect.getmembers(cls, callable)
        if not n.startswith("_") and n in vars(cls)
    }


def check_surface():
    published, generated = public(LLMWhispererClientV2), public(
        GeneratedLLMWhispererClientV2
    )
    missing = set(published) - set(generated)
    assert not missing, f"facade is missing {sorted(missing)}"

    for name in published:
        if name in FACADE_ONLY:
            continue
        want = inspect.signature(published[name]).parameters
        got = inspect.signature(generated[name]).parameters
        assert list(want) == list(got), f"{name}: {list(want)} != {list(got)}"
        for p in want:
            assert want[p].default == got[p].default, f"{name}.{p} default differs"
    print(f"surface OK — {len(published)} public methods, signatures identical")


def check_construction():
    """`__init__` is contract too — positional order and every keyword."""
    want = inspect.signature(LLMWhispererClientV2.__init__).parameters
    got = inspect.signature(GeneratedLLMWhispererClientV2.__init__).parameters
    assert list(want) == list(got), f"__init__: {list(want)} != {list(got)}"
    for p in want:
        assert want[p].default == got[p].default, f"__init__.{p} default differs"

    for attr in ("base_url", "api_key", "api_timeout", "headers", "max_retries"):
        pub = getattr(LLMWhispererClientV2(api_key="k"), attr)
        gen = getattr(GeneratedLLMWhispererClientV2(api_key="k"), attr)
        assert pub == gen, f"{attr}: {pub!r} != {gen!r}"
    print(f"construction OK — {len(want) - 1} kwargs, defaulted attributes match")


def check_whisper_signature():
    want = inspect.signature(LLMWhispererClientV2.whisper).parameters
    got = inspect.signature(GeneratedLLMWhispererClientV2.whisper).parameters
    for p, spec in want.items():
        assert p in got, f"whisper lost {p}"
        assert got[p].default == spec.default, f"whisper.{p} default differs"
    assert "extra" in got, "whisper must accept **extra for ungrown parameters"
    print(f"whisper OK — all {len(want)} published parameters preserved")


def check_wire_names():
    """The two aliased parameters must land on the query string, correctly spelled."""
    from sdk_llmwhisperer.api.whisper import extract
    from sdk_llmwhisperer.types import File

    from llmw_facade import PARAM_ALIASES

    kwargs = extract._get_kwargs(
        body=File(payload=b"x"),
        line_splitter_strategy="right-priority",
        file_name="invoice.pdf",
        url_query="https://example.com/a.pdf",
    )
    params = kwargs["params"]
    assert params["line_splitter_strategy"] == "right-priority", params
    assert params["file_name"] == "invoice.pdf", params
    assert params["url"] == "https://example.com/a.pdf", params

    published = inspect.signature(LLMWhispererClientV2.whisper).parameters
    for old, new in PARAM_ALIASES.items():
        assert old in published, f"alias {old} is not a published parameter"
        assert new in kwargs["params"] or new == "url_query", f"{new} not on the wire"
    print(f"wire names OK — {len(PARAM_ALIASES)} aliases resolve to server names")


def check_no_injected_defaults():
    """The generated side must not send a parameter the published side omits.

    `_get_kwargs` writes every spec-declared default into the request, which
    pins a value the server would otherwise choose for itself. On staging this
    silently changed the extracted text.
    """
    import urllib.parse

    from llmw_facade import PARAM_ALIASES

    seen = {}

    def cap_pub(self, prepared, timeout=None, stream=False, deadline=None):
        query = urllib.parse.urlparse(prepared.url).query
        seen["pub"] = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        raise SystemExit

    def cap_gen(self, module, *, timeout=None, send_only=None, **kw):
        params = module._get_kwargs(**kw).get("params", {})
        if send_only is not None:
            params = {k: v for k, v in params.items() if k in send_only}
        seen["gen"] = params
        raise SystemExit

    pdf = Path(__file__).resolve().parent.parent / "README.md"
    for cls, patch in (
        (LLMWhispererClientV2, ("_send_request", cap_pub)),
        (GeneratedLLMWhispererClientV2, ("_call", cap_gen)),
    ):
        original = getattr(cls, patch[0])
        setattr(cls, patch[0], patch[1])
        try:
            cls(base_url="http://127.0.0.1:1", api_key="k").whisper(file_path=str(pdf))
        except SystemExit:
            pass
        finally:
            setattr(cls, patch[0], original)

    allowed = set(seen["pub"]) | set(PARAM_ALIASES.values()) | {"url", "url_in_post"}
    extra = set(seen["gen"]) - allowed
    assert not extra, f"generated sends parameters published never sends: {sorted(extra)}"
    print(f"no injected defaults OK — {len(seen['gen'])} params, none unrequested")


def check_exception_translation():
    """Transport failures must surface as `requests` exceptions, not httpx."""
    for exc in (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        assert not issubclass(exc, requests.RequestException), exc

    client = GeneratedLLMWhispererClientV2(
        base_url="http://127.0.0.1:1", api_key="x", max_retries=0
    )
    client.api_timeout = 1
    try:
        client.get_usage_info()
    except requests.ConnectionError:
        pass
    except httpx.HTTPError as e:
        raise AssertionError(f"httpx exception escaped: {e!r}") from e
    else:
        raise AssertionError("expected a connection failure")
    print("exception translation OK — httpx never escapes")


def check_webhook_body():
    from sdk_llmwhisperer.api.webhook import webhook_post
    from sdk_llmwhisperer.models import WebhookConfig

    kwargs = webhook_post._get_kwargs(
        body=WebhookConfig(url="https://x", auth_token="t", webhook_name="w")
    )
    body = kwargs["json"]
    assert body == {
        "url": "https://x",
        "auth_token": "t",
        "webhook_name": "w",
    }, body
    print("webhook body OK — JSON payload matches the handler's required keys")


if __name__ == "__main__":
    check_surface()
    check_construction()
    check_whisper_signature()
    check_wire_names()
    check_webhook_body()
    check_no_injected_defaults()
    check_exception_translation()
    print("\nllmw compat OK")
