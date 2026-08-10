"""Hand-written facade over the generated Document Studio transport.

Everything here is what a spec cannot express and a generator will never emit:
retry policy, the sync/async POST distinction, the poll loop, the dict return
shapes callers depend on, and the requests-exception translation that keeps
downstream `except ConnectionError` handlers matching.

Public surface is deliberately identical to
``unstract.api_deployments.client.APIDeploymentsClient``.
"""

import ntpath
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# `requests` is a dependency purely for its exception classes. Downstream code
# (unstract/sdk1's x2text adapter) catches these by name; httpx's are not
# subclasses, so a network blip would otherwise stop being caught at all.
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build"))

from sdk_docstudio import AuthenticatedClient  # noqa: E402
from sdk_docstudio.api.deployment import execute, status  # noqa: E402
from sdk_docstudio.models import ExecuteRequest  # noqa: E402
from sdk_docstudio.types import UNSET, File  # noqa: E402

IN_PROGRESS = ["PENDING", "EXECUTING", "READY", "QUEUED", "INITIATED"]


class APIDeploymentsClientException(Exception):
    pass


def _translate(fn, *args, **kwargs):
    """Re-raise httpx transport errors as their `requests` equivalents."""
    try:
        return fn(*args, **kwargs)
    except httpx.TimeoutException as e:  # must precede ConnectError check
        raise requests.Timeout(str(e)) from e
    except httpx.ConnectError as e:
        raise requests.ConnectionError(str(e)) from e
    except httpx.TransportError as e:
        raise requests.ConnectionError(str(e)) from e


class GeneratedAPIDeploymentsClient:
    in_progress_statuses = IN_PROGRESS

    def __init__(
        self,
        api_url: str,
        api_key: str,
        api_timeout: int = 300,
        include_metadata: bool = False,
        verify: bool = True,
        max_retries: int = 4,
        initial_delay: float = 2.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.api_timeout = api_timeout
        self.include_metadata = include_metadata
        parsed = urlparse(api_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        # The generated client owns the path; org/api come off the deployment URL.
        self.org_name, self.api_name = parsed.path.strip("/").split("/")[-2:]
        self._client = AuthenticatedClient(
            base_url=self.base_url,
            token=api_key,
            verify_ssl=verify,
            # `api_timeout` is a backend execution mode (-1 = async), never a
            # transport timeout. httpx has no default; requests had none either.
            timeout=httpx.Timeout(None),
            raise_on_unexpected_status=False,
        )
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    def _retry(self, call, *, rewind=()):
        """Retry on 5xx/429 honouring Retry-After, else exponential backoff."""
        delay = self.initial_delay
        for attempt in range(self.max_retries + 1):
            resp = _translate(call)
            if attempt == self.max_retries or not (
                resp.status_code >= 500 or resp.status_code == 429
            ):
                return resp
            wait = delay
            if resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", delay))
                except (TypeError, ValueError):
                    pass
            time.sleep(min(wait, self.max_delay))
            delay *= self.backoff_factor
            for f in rewind:
                f.seek(0)
        raise AssertionError("unreachable")

    @staticmethod
    def _plain(value):
        """Generated models are attrs objects; callers are promised plain JSON.

        `to_dict()` round-trips undeclared keys through `additional_properties`,
        so nothing the backend sent is dropped by an incomplete annotation.
        """
        if isinstance(value, list):
            return [GeneratedAPIDeploymentsClient._plain(v) for v in value]
        return value.to_dict() if hasattr(value, "to_dict") else value

    @staticmethod
    def _shape(status_code, execution_status, error, result):
        return {
            "status_code": status_code,
            "pending": False,
            "execution_status": execution_status,
            "error": error,
            "extraction_result": result,
        }

    def structure_file(self, file_paths: list[str], **kwargs) -> dict:
        handles = []
        try:
            for p in file_paths:
                fh = open(p, "rb")
                handles.append(fh)
        except FileNotFoundError as e:
            for fh in handles:
                fh.close()
            raise APIDeploymentsClientException("File not found: " + str(e))

        body = ExecuteRequest(
            timeout=self.api_timeout,
            include_metadata=self.include_metadata,
            files=[
                File(
                    payload=fh,
                    file_name=ntpath.basename(p),
                    mime_type="application/octet-stream",
                )
                for p, fh in zip(file_paths, handles)
            ],
            # Every other backend param arrives here for free — see cli_generated.
            **kwargs,
        )

        def call():
            return execute.sync_detailed(
                self.org_name, self.api_name, client=self._client, body=body
            )

        try:
            if self.api_timeout == 0:
                # Async: server returns after queueing, so a 5xx is safe to retry.
                resp = self._retry(call, rewind=handles)
            else:
                # Sync: a lost 5xx may mean the work was already done.
                resp = _translate(call)
        finally:
            for fh in handles:
                fh.close()

        return self._unwrap_execute(resp)

    def _unwrap_execute(self, resp) -> dict:
        if resp.parsed is None:
            if resp.status_code == 401:
                return self._shape(resp.status_code, "", self._detail(resp), "")
            return self._shape(
                resp.status_code, "", "Invalid JSON response from API", ""
            )
        msg = resp.parsed.message
        execution_status = msg.execution_status or ""
        # UNSET means the key was absent (published client's `.get(k, "")`);
        # None means the backend sent null and callers see null today.
        result = "" if msg.result is UNSET else self._plain(msg.result)
        error = "" if msg.error is UNSET else msg.error
        out = self._shape(resp.status_code, execution_status, error, result)
        if 200 <= resp.status_code < 300 and (
            execution_status in IN_PROGRESS
            or (execution_status == "SUCCESS" and not result)
        ):
            out.update({"status_check_api_endpoint": msg.status_api, "pending": True})
        return out

    @staticmethod
    def _detail(resp) -> str:
        try:
            import json

            return json.loads(resp.content)["errors"][0]["detail"]
        except Exception:
            return "Unauthorized"

    def check_execution_status(self, status_check_api_endpoint: str) -> dict:
        execution_id = _query_value(status_check_api_endpoint, "execution_id")
        resp = self._retry(
            lambda: status.sync_detailed(
                self.org_name,
                self.api_name,
                client=self._client,
                execution_id=execution_id,
                include_metadata=self.include_metadata,
            )
        )
        if resp.parsed is None:
            return self._shape(
                resp.status_code, "", "Invalid JSON response from API", ""
            )
        out = self._shape(
            resp.status_code,
            resp.parsed.status or "",
            "",
            "" if resp.parsed.message is UNSET else self._plain(resp.parsed.message),
        )
        if out["execution_status"] in IN_PROGRESS or resp.status_code >= 500:
            out["pending"] = True
        return out


def _query_value(url: str, key: str) -> str:
    from urllib.parse import parse_qs

    return parse_qs(urlparse(url).query).get(key, [""])[0]
