"""`LLMWhispererClientV2` re-implemented over the GENERATED SDK.

Signatures and return shapes match the published client key for key; only the
transport underneath changes. What stays hand-written is what no generator can
emit: the retry policy, the `wait_for_completion` poll loop, the exception
contract, and the purely client-side `get_highlight_rect`.

Two published parameter names do not reach the service and are remapped here to
the names the generated spec derives from the server — see RUNBOOK.md
§"Parameter drift".
"""

import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path
from typing import IO, Any

import httpx
import requests  # dependency purely for its exception classes
import tenacity
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build"))

from sdk_llmwhisperer import AuthenticatedClient  # noqa: E402
from sdk_llmwhisperer.api.account import usage_info  # noqa: E402
from sdk_llmwhisperer.api.webhook import (  # noqa: E402
    webhook_delete,
    webhook_get,
    webhook_post,
    webhook_put,
)
from sdk_llmwhisperer.api.whisper import (  # noqa: E402
    detail,
    extract,
    highlights,
    retrieve,
    status,
)
from sdk_llmwhisperer.models import WebhookConfig  # noqa: E402
from sdk_llmwhisperer.types import File  # noqa: E402

BASE_URL_V2 = "https://llmwhisperer-api.us-central.unstract.com/api/v2"

# The published signature carries two names the service never reads. Routing
# them to the generated names makes them take effect for the first time.
# The generator names the query parameter `url` as `url_query` to avoid clashing
# with the request URL. The service's other renames are resolved in `whisper`.
PARAM_ALIASES = {"url": "url_query"}

class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response


class GeneratedLLMWhispererClientV2:
    # Published exposes this as a class attribute, not a constructor argument.
    api_timeout: int = 120

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        logging_level: str = "",
        custom_headers: dict[str, str] | None = None,
        max_retries: int = 3,
        retry_min_wait: float = 1.0,
        retry_max_wait: float = 60.0,
    ) -> None:
        level = logging_level or os.getenv("LLMWHISPERER_LOGGING_LEVEL", "DEBUG")
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(getattr(logging, level.upper(), logging.DEBUG))
        self.api_key = api_key or os.getenv("LLMWHISPERER_API_KEY", "")
        self.headers = {"unstract-key": self.api_key}
        if custom_headers:
            self.headers.update(custom_headers)
        self.max_retries = max_retries
        self.retry_min_wait = retry_min_wait
        self.retry_max_wait = retry_max_wait
        root = base_url or os.getenv("LLMWHISPERER_BASE_URL_V2", BASE_URL_V2)
        # The published client's base_url carries `/api/v2`; the generated spec
        # already puts it in every path.
        self.base_url = root.rstrip("/")
        if self.base_url.endswith("/api/v2"):
            root = self.base_url[: -len("/api/v2")]
        self._client = AuthenticatedClient(
            base_url=root.rstrip("/"),
            token=self.api_key,
            auth_header_name="unstract-key",
            prefix="",
            timeout=httpx.Timeout(self.api_timeout),
            headers=self.headers,
            raise_on_unexpected_status=False,
        )

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, requests.ConnectionError | requests.Timeout | _RetryableStatus)

    def _retry_wait(self, retry_state: tenacity.RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _RetryableStatus) and exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After")
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        return tenacity.wait_exponential_jitter(
            initial=self.retry_min_wait, max=self.retry_max_wait
        )(retry_state=retry_state)

    def _call(self, module, *, timeout=None, send_only=None, **kwargs) -> httpx.Response:
        """Execute a generated operation and hand back the raw response.

        `_get_kwargs` is the generated part: it owns the URL, the query-parameter
        names and their encoding. Response handling stays here because the
        published contract is hand-built dicts, not typed models. The retry
        policy mirrors the published client's, which no spec describes.

        `send_only` names the query parameters the caller actually set. Anything
        else is dropped: the generator writes the spec's declared default into
        every request, which pins a value the server would otherwise pick for
        itself, and the two disagree the moment the service changes a default.
        """
        request_kwargs = module._get_kwargs(**kwargs)
        if send_only is not None and "params" in request_kwargs:
            request_kwargs["params"] = {
                k: v for k, v in request_kwargs["params"].items() if k in send_only
            }
        request_kwargs["timeout"] = httpx.Timeout(
            self.api_timeout if timeout is None else timeout
        )
        client = self._client.get_httpx_client()

        def attempt() -> httpx.Response:
            try:
                response = client.request(**request_kwargs)
            except httpx.TimeoutException as e:  # must precede ConnectError
                raise requests.Timeout(str(e)) from e
            except httpx.ConnectError as e:
                raise requests.ConnectionError(str(e)) from e
            except httpx.TransportError as e:
                raise requests.ConnectionError(str(e)) from e
            if response.status_code == 429 or response.status_code >= 500:
                raise _RetryableStatus(response)
            return response

        try:
            if self.max_retries == 0:
                return attempt()
            return tenacity.Retrying(
                retry=tenacity.retry_if_exception(self._is_retryable),
                stop=tenacity.stop_after_attempt(self.max_retries + 1),
                wait=self._retry_wait,
                reraise=True,
            )(attempt)
        except _RetryableStatus as e:
            return e.response

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        """The published error contract: parsed body plus `status_code`."""
        text = response.text or ""
        if not text.strip():
            raise LLMWhispererClientException(
                "API error: empty response body", response.status_code
            )
        try:
            err = json.loads(text)
        except json.JSONDecodeError as e:
            preview = text[:500] + "..." if len(text) > 500 else text
            raise LLMWhispererClientException(
                f"API error: non-JSON response - {preview}", response.status_code
            ) from e
        if isinstance(err, dict):
            err["status_code"] = response.status_code
        raise LLMWhispererClientException(err, response.status_code)

    def _json_or_raise(self, response: httpx.Response, ok: int = 200) -> Any:
        if response.status_code != ok:
            self._raise(response)
        return json.loads(response.text)

    def get_usage_info(self) -> Any:
        return self._json_or_raise(self._call(usage_info))

    def get_highlight_data(
        self, whisper_hash: str, lines: str, extract_all_lines: bool = False
    ) -> Any:
        response = self._call(
            highlights,
            whisper_hash=whisper_hash,
            lines=lines,
            extract_all_lines=str(extract_all_lines).lower(),
        )
        return self._json_or_raise(response)

    def whisper_detail(self, whisper_hash: str) -> Any:
        return self._json_or_raise(self._call(detail, whisper_hash=whisper_hash))

    def whisper_status(self, whisper_hash: str) -> Any:
        response = self._call(status, whisper_hash=whisper_hash)
        message = self._json_or_raise(response)
        message["status_code"] = response.status_code
        return message

    def whisper_retrieve(self, whisper_hash: str, encoding: str = "utf-8") -> Any:
        response = self._call(
            retrieve, whisper_hash=whisper_hash, send_only={"whisper_hash"}
        )
        response.encoding = encoding
        if response.status_code != 200:
            self._raise(response)
        return {
            "status_code": response.status_code,
            "extraction": json.loads(response.text),
        }

    def register_webhook(self, url: str, auth_token: str, webhook_name: str) -> Any:
        body = WebhookConfig(url=url, auth_token=auth_token, webhook_name=webhook_name)
        return self._json_or_raise(self._call(webhook_post, body=body), ok=201)

    def update_webhook_details(
        self, webhook_name: str, url: str, auth_token: str
    ) -> Any:
        body = WebhookConfig(url=url, auth_token=auth_token, webhook_name=webhook_name)
        return self._json_or_raise(self._call(webhook_put, body=body))

    def get_webhook_details(self, webhook_name: str) -> Any:
        return self._json_or_raise(self._call(webhook_get, webhook_name=webhook_name))

    def delete_webhook(self, webhook_name: str) -> Any:
        return self._json_or_raise(
            self._call(webhook_delete, webhook_name=webhook_name)
        )

    def get_highlight_rect(
        self, line_metadata: list[int], target_width: int, target_height: int
    ) -> tuple[int, int, int, int, int]:
        """Pure client-side geometry — nothing to generate, nothing to call."""
        page, baseline, height, original_height = line_metadata[:4]
        y1 = int(((baseline - height) / original_height) * target_height)
        y2 = int((baseline / original_height) * target_height)
        return (page, 0, y1, target_width, y2)

    def _resolve_deprecated_param(
        self, name, value, deprecated_name, deprecated_value, default, *, forward
    ):
        """Mirrors the published resolver, including which renames forward.

        `forward=False` means the deprecated value is dropped rather than
        applied: that parameter never reached the service, so honouring it now
        would silently change extraction output on upgrade.
        """
        if deprecated_value is None:
            return default if value is None else value
        if value is not None:
            raise LLMWhispererClientException(
                f"Cannot pass both '{deprecated_name}' and '{name}', use '{name}' only", 1
            )
        message = (
            f"'{deprecated_name}' is deprecated and will be removed in a future "
            f"release, use '{name}' instead"
        )
        if not forward:
            message += (
                f". The value passed is ignored: '{deprecated_name}' never reached the service"
            )
        self.logger.warning(message)
        warnings.warn(message, DeprecationWarning, stacklevel=3)
        return deprecated_value if forward else default

    def whisper(
        self,
        file_path: str = "",
        stream: IO[bytes] | None = None,
        url: str = "",
        mode: str = "form",
        output_mode: str = "layout_preserving",
        page_seperator: str | None = None,
        pages_to_extract: str = "",
        median_filter_size: int = 0,
        gaussian_blur_radius: int = 0,
        line_splitter_tolerance: float = 0.4,
        horizontal_stretch_factor: float = 1.0,
        mark_vertical_lines: bool = False,
        mark_horizontal_lines: bool = False,
        line_spitter_strategy: str | None = None,
        add_line_nos: bool = False,
        include_line_confidence: bool = False,
        word_confidence_threshold: float = 0.3,
        lang: str = "eng",
        tag: str = "default",
        filename: str | None = None,
        webhook_metadata: str = "",
        use_webhook: str = "",
        wait_for_completion: bool = False,
        wait_timeout: int = 180,
        encoding: str = "utf-8",
        page_separator: str | None = None,
        line_splitter_strategy: str | None = None,
        file_name: str | None = None,
        **extra: Any,
    ) -> Any:
        """`**extra` reaches the parameters the published signature never grew."""
        page_separator = self._resolve_deprecated_param(
            "page_separator", page_separator, "page_seperator", page_seperator, "<<<", forward=True
        )
        line_splitter_strategy = self._resolve_deprecated_param(
            "line_splitter_strategy",
            line_splitter_strategy,
            "line_spitter_strategy",
            line_spitter_strategy,
            "left-priority",
            forward=False,
        )
        file_name = self._resolve_deprecated_param(
            "file_name", file_name, "filename", filename, "", forward=True
        )
        params = {
            "mode": mode,
            "output_mode": output_mode,
            "page_separator": page_separator,
            "pages_to_extract": pages_to_extract,
            "median_filter_size": median_filter_size,
            "gaussian_blur_radius": gaussian_blur_radius,
            "line_splitter_tolerance": line_splitter_tolerance,
            "horizontal_stretch_factor": horizontal_stretch_factor,
            "mark_vertical_lines": mark_vertical_lines,
            "mark_horizontal_lines": mark_horizontal_lines,
            "line_splitter_strategy": line_splitter_strategy,
            "add_line_nos": add_line_nos,
            "include_line_confidence": include_line_confidence,
            "word_confidence_threshold": word_confidence_threshold,
            "lang": lang,
            "tag": tag,
            "file_name": file_name,
            "webhook_metadata": webhook_metadata,
            "use_webhook": use_webhook,
            **extra,
        }
        params = {PARAM_ALIASES.get(k, k): v for k, v in params.items()}

        if use_webhook != "" and wait_for_completion:
            raise LLMWhispererClientException(
                "Cannot wait for completion when using webhook", 1
            )
        if url == "" and file_path == "" and stream is None:
            raise LLMWhispererClientException(
                "Either url, stream or file_path must be provided", 1
            )

        if url:
            params["url_query"] = url
            params["url_in_post"] = True
            payload = url.encode()
        elif stream is not None:
            payload = b"".join(stream)
        else:
            payload = Path(file_path).read_bytes()

        start = time.time()
        response = self._call(
            extract,
            body=File(payload=payload),
            timeout=min(self.api_timeout, wait_timeout),
            send_only={"url" if k == "url_query" else k for k in params},
            **params,
        )
        response.encoding = encoding

        if response.status_code not in (200, 202):
            message = self._body(response)
            message["status_code"] = response.status_code
            message["extraction"] = {}
            raise LLMWhispererClientException(message)

        message = self._body(response)
        message["status_code"] = response.status_code
        if response.status_code == 200:
            return message

        message["extraction"] = {}
        if not wait_for_completion:
            return message
        return self._poll(message, message["whisper_hash"], start, wait_timeout)

    @staticmethod
    def _body(response: httpx.Response) -> dict:
        try:
            parsed = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return {"message": response.text}
        return parsed if isinstance(parsed, dict) else {"message": str(parsed)}

    def _poll(self, message: dict, whisper_hash: str, start: float, timeout: int) -> Any:
        def failed(reason: str) -> dict:
            message.update(status_code=-1, message=reason, extraction={})
            return message

        while time.time() - start < timeout:
            state = self.whisper_status(whisper_hash=whisper_hash)
            if state["status_code"] != 200:
                return failed("Whisper client operation failed")
            if state["status"] == "error" or "error" in state["status"]:
                message["status"] = "error"
                return failed(state.get("message") or state["status"])
            if state["status"] == "processed":
                result = self.whisper_retrieve(whisper_hash=whisper_hash)
                if result["status_code"] != 200:
                    return failed("Whisper client operation failed")
                message.update(
                    status_code=200,
                    message="Whisper operation completed",
                    status="processed",
                    extraction=result["extraction"],
                )
                return message
            time.sleep(5)
        return failed("Whisper client operation timed out")
