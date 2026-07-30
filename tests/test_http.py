"""HTTP layer: auth injection, exit-code mapping, retries and redaction."""

from __future__ import annotations

import httpx
import pytest
import respx

from unstract_cli.config.loader import (
    ConfigError,
    ConfigFile,
    ResolvedConfig,
    load_config,
)
from unstract_cli.core.errors import (
    REDACTED,
    CLIError,
    ExitCode,
    exit_code_for_status,
    is_retryable,
    redact_headers,
    scrub,
)
from unstract_cli.core.http import (
    GATEWAY_INJECTED_HEADERS,
    auth_headers,
    build_request,
    execute,
    raise_for_status,
)
from unstract_cli.core.model import ApiGroup
from unstract_cli.endpoints import get_endpoint

from .conftest import FAKE_KEY, WHISPER_BASE


def _config(monkeypatch, **env) -> ResolvedConfig:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ResolvedConfig(file=ConfigFile(exists=False))


class TestExitCodeMapping:
    """SPEC §5.4 - the table an agent branches on."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (200, ExitCode.SUCCESS),
            (400, ExitCode.VALIDATION),
            (401, ExitCode.AUTH),
            (403, ExitCode.AUTH),
            (404, ExitCode.NOT_FOUND),
            (406, ExitCode.ALREADY_CONSUMED),
            (409, ExitCode.VALIDATION),
            (422, ExitCode.VALIDATION),
            (429, ExitCode.RATE_LIMITED),
            (500, ExitCode.SERVER_ERROR),
            (503, ExitCode.SERVER_ERROR),
        ],
    )
    def test_status_maps_to_exit_code(self, status, expected):
        assert exit_code_for_status(status) is expected

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_retryable(self, status):
        assert is_retryable(status)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_not_retryable(self, status):
        """Retrying a 4xx re-sends a request already rejected on its merits --
        and for one-shot reads could consume a result the first call delivered."""
        assert not is_retryable(status)


class TestAuth:
    """SPEC §4.4 - three products, three schemes."""

    def test_llmwhisperer_uses_unstract_key(self, monkeypatch):
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        assert auth_headers(ApiGroup.LLMWHISPERER, cfg) == {"unstract-key": FAKE_KEY}

    def test_platform_uses_bearer(self, monkeypatch):
        cfg = _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY)
        assert auth_headers(ApiGroup.PLATFORM, cfg)["Authorization"] == f"Bearer {FAKE_KEY}"

    def test_apihub_uses_apikey(self, monkeypatch):
        cfg = _config(monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY)
        assert auth_headers(ApiGroup.APIHUB, cfg)["apikey"] == FAKE_KEY

    def test_apihub_never_sends_gateway_headers(self, monkeypatch):
        """Kong injects tenancy headers from its own Redis lookup.

        A client-set value would be overwritten at best, and treated as a tenancy
        claim at worst, so the CLI must never emit them.
        """
        cfg = _config(
            monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY, UNSTRACT_APIHUB_BASE_URL="https://hub.test"
        )
        plan = build_request(
            get_endpoint("apihub.status"), cfg, {"file_hash": "abc"}
        )
        for header in GATEWAY_INJECTED_HEADERS:
            assert header not in {k.lower() for k in plan.headers}


class TestEnumKeyedLookup:
    """Callers pass `ApiGroup`; the resolver must accept it, not just strings.

    When only `Product` was normalised, an `ApiGroup` key fell through
    unconverted: profile values were silently missed and error messages read
    "ApiGroup.LLMWHISPERER.api_key".
    """

    def test_enum_and_string_agree(self, tmp_path, monkeypatch):
        config = tmp_path / "c.toml"
        config.write_text(
            'default_profile = "p"\n\n'
            "[profiles.p.llmwhisperer]\n"
            'base_url = "https://from-file.example/api/v2"\n'
        )
        monkeypatch.setenv("UNSTRACT_CONFIG", str(config))
        cfg = ResolvedConfig(file=load_config(config))

        assert cfg.get(ApiGroup.LLMWHISPERER, "base_url") == cfg.get(
            "llmwhisperer", "base_url"
        ) == "https://from-file.example/api/v2"

    def test_error_message_uses_the_plain_name(self, monkeypatch):
        monkeypatch.delenv("LLMWHISPERER_API_KEY", raising=False)
        cfg = ResolvedConfig(file=ConfigFile(exists=False))
        with pytest.raises(ConfigError) as exc:
            cfg.require(ApiGroup.LLMWHISPERER, "api_key")
        assert "llmwhisperer.api_key" in str(exc.value)
        assert "ApiGroup." not in str(exc.value)


class TestRedaction:
    """SPEC §5.7 - secrets appear in no output stream."""

    def test_headers_redacted(self):
        out = redact_headers({"unstract-key": FAKE_KEY, "Authorization": f"Bearer {FAKE_KEY}"})
        assert FAKE_KEY not in str(out)
        assert out["unstract-key"] == REDACTED

    def test_scrub_removes_literals(self):
        assert FAKE_KEY not in scrub(f"failed with key {FAKE_KEY}", [FAKE_KEY])

    def test_dry_run_describe_redacts(self, monkeypatch):
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.status"), cfg, {"whisper_hash": "h"})
        assert FAKE_KEY not in str(plan.describe())


class TestRequestBuilding:
    def test_query_params_and_url(self, monkeypatch):
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.status"), cfg, {"whisper_hash": "abc123"})
        assert plan.url == f"{WHISPER_BASE}/whisper-status"
        assert plan.params["whisper_hash"] == "abc123"

    def test_booleans_serialise_lowercase(self, monkeypatch):
        """Python's True would be rejected; the wire format is `true`."""
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(
            get_endpoint("whisper.retrieve"), cfg, {"whisper_hash": "h", "text_only": True}
        )
        assert plan.params["text_only"] == "true"

    def test_path_params_substituted_from_env(self, monkeypatch):
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="org_abc"
        )
        plan = build_request(
            get_endpoint("docstudio.deployment.status"),
            cfg,
            {"api_name": "invoice-api", "execution_id": "e1"},
        )
        assert plan.url.endswith("/deployment/api/org_abc/invoice-api/")

    def test_missing_required_path_param_is_usage_error(self, monkeypatch):
        cfg = _config(monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="o")
        with pytest.raises(CLIError) as exc:
            build_request(get_endpoint("docstudio.deployment.status"), cfg, {"execution_id": "e"})
        assert exc.value.exit_code is ExitCode.USAGE

    def test_freeform_ext_param(self, monkeypatch):
        """P5 - `--ext-param foo=bar` must reach the wire as `ext_foo=bar`."""
        cfg = _config(
            monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY, UNSTRACT_APIHUB_BASE_URL="https://hub.test"
        )
        plan = build_request(
            get_endpoint("apihub.retrieve"),
            cfg,
            {"file_hash": "h", "ext_param": ["foo=bar", "baz=qux"]},
        )
        # `retrieve` has no ext_param; verify on `extract`, which does.
        plan = build_request(
            get_endpoint("apihub.extract"),
            cfg,
            {"vertical": "table", "sub_vertical": "extract_table",
             "use_cached_file_hash": "h", "ext_param": ["foo=bar", "baz=qux"]},
        )
        assert plan.params["ext_foo"] == "bar"
        assert plan.params["ext_baz"] == "qux"

    def test_freeform_rejects_malformed(self, monkeypatch):
        cfg = _config(
            monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY, UNSTRACT_APIHUB_BASE_URL="https://hub.test"
        )
        with pytest.raises(CLIError) as exc:
            build_request(
                get_endpoint("apihub.extract"), cfg,
                {"vertical": "table", "sub_vertical": "extract_table",
                 "use_cached_file_hash": "h", "ext_param": ["novalue"]},
            )
        assert exc.value.exit_code is ExitCode.USAGE

    def test_share_resource_maps_to_path_segment(self, monkeypatch):
        """P3 - the friendly name is not the URL segment."""
        cfg = _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="o")
        plan = build_request(
            get_endpoint("docstudio.platform.share"), cfg,
            {"resource": "api-deployment", "id": "abc"},
        )
        assert "/api/deployment/abc/share/" in plan.url

    def test_apihub_without_base_url_is_usage_error(self, monkeypatch):
        """API Hub has no default base URL; inventing one would send documents
        to a host we cannot vouch for (SPEC §11.1)."""
        cfg = _config(monkeypatch, UNSTRACT_APIHUB_KEY=FAKE_KEY)
        with pytest.raises(CLIError) as exc:
            build_request(get_endpoint("apihub.status"), cfg, {"file_hash": "h"})
        assert exc.value.exit_code is ExitCode.USAGE


class TestJsonParams:
    """BUG 1 - ParamType.JSON params must reach the wire as objects, not strings."""

    def _cfg(self, monkeypatch):
        return _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="o")

    def test_body_json_param_is_parsed_to_object(self, monkeypatch):
        """A BODY+JSON param becomes a dict in json_body, not an escaped string."""
        plan = build_request(
            get_endpoint("docstudio.platform.prompt-studio.sync-prompts"),
            self._cfg(monkeypatch),
            {"tool_id": "t", "data": '{"prompts": [{"prompt_key": "k"}]}'},
        )
        assert plan.json_body["data"] == {"prompts": [{"prompt_key": "k"}]}
        assert isinstance(plan.json_body["data"], dict)

    def test_dry_run_renders_json_param_as_object(self, monkeypatch):
        plan = build_request(
            get_endpoint("docstudio.platform.prompt-studio.sync-prompts"),
            self._cfg(monkeypatch),
            {"tool_id": "t", "data": '{"a": 1}'},
        )
        assert plan.describe()["body"]["data"] == {"a": 1}

    def test_json_param_accepts_file_reference(self, monkeypatch, tmp_path):
        payload = tmp_path / "prompts.json"
        payload.write_text('{"prompts": ["x"]}')
        plan = build_request(
            get_endpoint("docstudio.platform.prompt-studio.sync-prompts"),
            self._cfg(monkeypatch),
            {"tool_id": "t", "data": f"@{payload}"},
        )
        assert plan.json_body["data"] == {"prompts": ["x"]}

    def test_invalid_json_is_usage_error(self, monkeypatch):
        with pytest.raises(CLIError) as exc:
            build_request(
                get_endpoint("docstudio.platform.prompt-studio.sync-prompts"),
                self._cfg(monkeypatch),
                {"tool_id": "t", "data": "{not json"},
            )
        assert exc.value.exit_code is ExitCode.USAGE

    def test_missing_json_file_is_usage_error(self, monkeypatch):
        with pytest.raises(CLIError) as exc:
            build_request(
                get_endpoint("docstudio.platform.prompt-studio.sync-prompts"),
                self._cfg(monkeypatch),
                {"tool_id": "t", "data": "@/no/such/file.json"},
            )
        assert exc.value.exit_code is ExitCode.USAGE

    def test_form_json_param_is_serialized_back_to_string(self, monkeypatch):
        """FORM+JSON (deployment run --custom-data) must be a *string* in the
        multipart field -- httpx cannot form-encode a dict, and the server
        json.loads it. This guards against a double-encode regression."""
        cfg = _config(
            monkeypatch, UNSTRACT_DEPLOYMENT_KEY=FAKE_KEY, UNSTRACT_ORG_ID="o"
        )
        plan = build_request(
            get_endpoint("docstudio.deployment.run"),
            cfg,
            {"api_name": "inv", "custom_data": '{"k": "v"}'},
        )
        assert plan.data["custom_data"] == '{"k": "v"}'
        assert isinstance(plan.data["custom_data"], str)


class TestResponseFieldGuards:
    """BUG 2 - mirror tool_id into the prompt-create body so it links."""

    def test_prompt_create_mirrors_tool_id_into_body(self, monkeypatch):
        plan = build_request(
            get_endpoint("docstudio.platform.prompt-studio.prompt.create"),
            _config(monkeypatch, UNSTRACT_PLATFORM_KEY=FAKE_KEY, UNSTRACT_ORG_ID="o"),
            {"tool_id": "the-tool", "prompt_key": "k"},
        )
        # The path still carries it...
        assert "/the-tool/" in plan.url
        # ...and it is also sent in the body, defeating the orphaning defect.
        assert plan.json_body["tool_id"] == "the-tool"

    def test_prompt_create_declares_required_response_field(self):
        ep = get_endpoint("docstudio.platform.prompt-studio.prompt.create")
        assert "tool_id" in ep.require_response_fields


class TestExecute:
    @respx.mock
    def test_success(self, monkeypatch):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(200, json={"subscription_plan": "free"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.usage"), cfg, {})
        assert execute(plan, max_retries=0).payload["subscription_plan"] == "free"

    @respx.mock
    def test_concatenated_json_body_becomes_one_array(self, monkeypatch):
        """CAPTURE2 DOC 6 - an endpoint that streams several JSON objects back to
        back would break `| json`. The CLI recovers them into one array so the
        contract 'exactly one JSON document' holds."""
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/json"},
                content=b'{"a": 1}\n{"b": 2}',
            )
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.usage"), cfg, {})
        payload = execute(plan, max_retries=0).payload
        assert payload == [{"a": 1}, {"b": 2}]

    @respx.mock
    def test_truly_malformed_json_falls_back_to_text(self, monkeypatch):
        respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/json"},
                content=b'{"a": 1} not json at all',
            )
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.usage"), cfg, {})
        assert execute(plan, max_retries=0).payload == '{"a": 1} not json at all'

    @respx.mock
    def test_retries_on_500_then_succeeds(self, monkeypatch):
        route = respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.usage"), cfg, {})
        assert execute(plan, max_retries=2, sleep=lambda _: None).status == 200
        assert route.call_count == 2

    @respx.mock
    def test_does_not_retry_4xx(self, monkeypatch):
        route = respx.get(f"{WHISPER_BASE}/get-usage-info").mock(
            return_value=httpx.Response(403, json={"message": "Unauthorized"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        plan = build_request(get_endpoint("whisper.usage"), cfg, {})
        assert execute(plan, max_retries=3, sleep=lambda _: None).status == 403
        assert route.call_count == 1, "a 4xx must not be retried"

    @respx.mock
    def test_already_consumed_maps_to_exit_9(self, monkeypatch):
        """SPEC §5.6 - the one-shot footgun gets its own exit code."""
        respx.get(f"{WHISPER_BASE}/whisper-retrieve").mock(
            return_value=httpx.Response(406, json={"message": "Whisper already delivered"})
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        endpoint = get_endpoint("whisper.retrieve")
        plan = build_request(endpoint, cfg, {"whisper_hash": "h"})
        response = execute(plan, max_retries=0)
        with pytest.raises(CLIError) as exc:
            raise_for_status(response, endpoint)
        assert exc.value.exit_code is ExitCode.ALREADY_CONSUMED
        assert "already" in (exc.value.hint or "").lower()

    @respx.mock
    def test_error_carries_hint_and_details(self, monkeypatch):
        respx.get(f"{WHISPER_BASE}/whisper-status").mock(
            return_value=httpx.Response(
                422,
                json={"type": "validation_error",
                      "errors": [{"code": "invalid", "detail": "bad hash", "attr": "whisper_hash"}]},
            )
        )
        cfg = _config(monkeypatch, LLMWHISPERER_API_KEY=FAKE_KEY)
        endpoint = get_endpoint("whisper.status")
        plan = build_request(endpoint, cfg, {"whisper_hash": "x"})
        with pytest.raises(CLIError) as exc:
            raise_for_status(execute(plan, max_retries=0), endpoint)
        payload = exc.value.to_dict()["error"]
        assert payload["message"] == "bad hash"
        assert payload["retryable"] is False
        assert payload["exit_code"] == 5


class TestConsumedHeuristicPrecision:
    """`already delivered` in a *result* is document text, not a status signal."""

    def test_successful_result_containing_the_phrase_survives(self):
        """The deployment status endpoint returns the result in `message`.

        Substring-matching a stringified list let ordinary invoice or delivery-note
        wording destroy a successful one-shot read.
        """
        from unstract_cli.core.http import Response, raise_for_status
        from unstract_cli.endpoints import get_endpoint

        payload = {
            "status": "COMPLETED",
            "message": [{"file": "note.pdf", "result": "goods were already delivered"}],
        }
        # Must not raise: this is a completed extraction, not a consumed result.
        raise_for_status(
            Response(status=200, payload=payload, headers={}, raw=b""),
            get_endpoint("docstudio.deployment.status"),
        )

    def test_genuine_consumed_signal_still_raises(self):
        from unstract_cli.core.errors import ExitCode
        from unstract_cli.core.http import Response, raise_for_status

        with pytest.raises(CLIError) as excinfo:
            raise_for_status(
                Response(
                    status=200,
                    payload={"message": "Whisper already delivered"},
                    headers={},
                    raw=b"",
                )
            )
        assert excinfo.value.exit_code == ExitCode.ALREADY_CONSUMED


class TestRetrySafety:
    """What may be replayed, and what a replay would destroy."""

    def _plan(self, method):
        from unstract_cli.core.http import RequestPlan

        return RequestPlan(
            method=method, url="https://x.invalid/y", headers={}, params={},
            json_body=None, data=None, files=None, content=None,
        )

    @pytest.mark.parametrize(
        "method,expected",
        [("POST", 1), ("PATCH", 1), ("GET", 4), ("PUT", 4), ("DELETE", 4)],
    )
    def test_5xx_is_retried_only_for_idempotent_methods(self, method, expected):
        """A 5xx on a POST may mean the write landed and the response was lost.

        Replaying it duplicates workflow executions and their billing.
        """
        from unstract_cli.core.http import execute

        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, json={"error": "upstream"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        # execute() returns the final response; only transport errors raise here.
        execute(self._plan(method), client=client, max_retries=3, sleep=lambda _: None)
        assert calls["n"] == expected

    def test_429_is_retried_even_for_post(self):
        """429 is the server explicitly stating it did not process the request."""
        from unstract_cli.core.http import execute

        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(429, json={"error": "slow down"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        execute(self._plan("POST"), client=client, max_retries=2, sleep=lambda _: None)
        assert calls["n"] == 3

    def test_one_shot_read_is_never_retried(self):
        """The first attempt may have consumed the result it failed to deliver."""
        from unstract_cli.core.http import execute
        from unstract_cli.endpoints import get_endpoint

        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ReadTimeout("lost the response", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(CLIError):
            execute(
                self._plan("GET"),
                endpoint=get_endpoint("whisper.retrieve"),
                client=client,
                max_retries=3,
                sleep=lambda _: None,
            )
        assert calls["n"] == 1


class TestRetryAfter:
    def _resp(self, value):
        return httpx.Response(429, headers={"retry-after": value})

    def test_negative_value_does_not_reach_sleep(self):
        """time.sleep raises ValueError on a negative, escaping every handler."""
        from unstract_cli.core.http import _retry_after

        assert _retry_after(self._resp("-5")) == 0.0

    def test_large_value_is_capped(self):
        """Retry-After: 86400 would park the CLI for a day, silently."""
        from unstract_cli.core.http import MAX_RETRY_AFTER, _retry_after

        assert _retry_after(self._resp("86400")) == MAX_RETRY_AFTER

    def test_http_date_form_is_understood(self):
        """RFC 7231 permits a date; float() alone silently ignored it."""
        from unstract_cli.core.http import _retry_after

        assert _retry_after(self._resp("Wed, 21 Oct 2099 07:28:00 GMT")) is not None

    def test_unparseable_falls_back_to_backoff(self):
        from unstract_cli.core.http import _retry_after

        assert _retry_after(self._resp("soon")) is None
