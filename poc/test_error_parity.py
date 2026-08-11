"""Error-path parity: do both clients report the same failure the same way?

The existing compat checks compare signatures and one transport failure. They say
nothing about what a caller sees on a 401, a 429, a 500, or a body that is not
the JSON the client assumed — which is where every silent defect this POC found
actually lived.

Both clients are pointed at a local server that returns a scripted status and
body, and the outcome is compared: exception class, its `status_code`, its
message, or the returned dict.

Divergences that already exist between the published clients' own methods are
listed in KNOWN, with the reason. A divergence not in that list fails the run.

    build/poc-venv/bin/python poc/test_error_parity.py
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from unstract.api_deployments.client import APIDeploymentsClient  # noqa: E402
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2  # noqa: E402

from facade import GeneratedAPIDeploymentsClient  # noqa: E402
from llmw_facade import GeneratedLLMWhispererClientV2  # noqa: E402

SCRIPT = {"status": 200, "body": "{}", "ctype": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def _reply(self):
        body = SCRIPT["body"].encode()
        self.send_response(SCRIPT["status"])
        self.send_header("Content-Type", SCRIPT["ctype"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _reply

    def log_message(self, *a):
        pass


def outcome(fn):
    """Normalise a call into a comparable tuple."""
    try:
        return ("return", fn())
    except Exception as e:  # noqa: BLE001 — comparing failure modes is the point
        value = getattr(e, "value", None)
        if isinstance(value, dict):
            value = json.dumps(value, sort_keys=True)
        return (
            "raise",
            type(e).__name__,
            getattr(e, "status_code", None),
            str(value or e)[:120],
        )


#: Divergences that are accepted rather than fixed, each with the reason.
#: Anything not listed here fails the run.
KNOWN = {
    # The published client handles errors two different ways depending on the
    # method. `whisper_status` and `whisper_detail` guard the parse and pass the
    # status positionally; `get_usage_info`, `get_highlight_data`,
    # `whisper_retrieve` and the webhook methods call `json.loads` unguarded and
    # raise with one argument, leaving `.status_code` None. The facade applies
    # the guarded form everywhere, so it diverges from the unguarded half.
    "usage/json-401",
    "usage/json-500",
    "usage/wrapped-500",
    "usage/non-json-400",
    "usage/empty-500",
    "retrieve/json-401",
    "retrieve/json-500",
    "retrieve/wrapped-500",
    "retrieve/non-json-400",
    "retrieve/empty-500",
    # The facade writes `status_code` into the error body *and* passes it
    # positionally. The guarded published methods do only the latter, so the body
    # gains a key. A superset of what callers read either way, so it is kept.
    "status/json-401",
    "status/json-500",
    "status/wrapped-500",
}

CASES = [
    # (id, status, body, content-type)
    ("json-401", 401, '{"message": "Unauthorized"}', "application/json"),
    ("json-500", 500, '{"message": "boom"}', "application/json"),
    # A shape the deployment clients actually expect, so the comparison is not
    # dominated by both of them crashing on `message` being a string.
    (
        "wrapped-500",
        500,
        '{"message": {"execution_status": "ERROR", "error": "boom"}}',
        "application/json",
    ),
    ("non-json-400", 400, "<html>gateway</html>", "text/html"),
    ("empty-500", 500, "", "text/plain"),
]


def compare(label, published_fn, generated_fn, failures):
    for case_id, status, body, ctype in CASES:
        SCRIPT.update(status=status, body=body, ctype=ctype)
        pub, gen = outcome(published_fn), outcome(generated_fn)
        key = f"{label}/{case_id}"
        if pub == gen:
            print(f"  {key:<28} same  {pub[1] if pub[0] == 'raise' else 'returned'}")
        elif key in KNOWN:
            print(f"  {key:<28} KNOWN {pub[1:3]} vs {gen[1:3]}")
        else:
            print(f"  {key:<28} DIFF  {pub} != {gen}")
            failures.append(key)


def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    failures = []

    # max_retries=0 throughout: a 429/500 retry loop would test tenacity, not the
    # error contract, and would make the run take minutes.
    pub_lw = LLMWhispererClientV2(base_url=base, api_key="k", max_retries=0)
    gen_lw = GeneratedLLMWhispererClientV2(base_url=base, api_key="k", max_retries=0)

    print("LLMWhisperer")
    compare("usage", pub_lw.get_usage_info, gen_lw.get_usage_info, failures)
    compare(
        "status",
        lambda: pub_lw.whisper_status("h"),
        lambda: gen_lw.whisper_status("h"),
        failures,
    )
    compare(
        "retrieve",
        lambda: pub_lw.whisper_retrieve("h"),
        lambda: gen_lw.whisper_retrieve("h"),
        failures,
    )

    api = f"{base}/deployment/api/org/api-name/"
    pub_dep = APIDeploymentsClient(api_url=api, api_key="k", max_retries=0)
    gen_dep = GeneratedAPIDeploymentsClient(api_url=api, api_key="k", max_retries=0)

    # Each side is given the status endpoint it documents: the published client
    # appends its argument to `base_url`, the facade parses a full URL. Feeding
    # both the same string would test URL joining, not error handling.
    pub_status = "/deployment/api/org/api-name/?execution_id=1"
    gen_status = f"{api}?execution_id=1"

    print("Document Studio")
    compare(
        "execute",
        lambda: pub_dep.structure_file([__file__]),
        lambda: gen_dep.structure_file([__file__]),
        failures,
    )
    compare(
        "exec-status",
        lambda: pub_dep.check_execution_status(pub_status),
        lambda: gen_dep.check_execution_status(gen_status),
        failures,
    )

    server.shutdown()
    if failures:
        raise AssertionError(f"error-path parity broke: {failures}")
    print(f"\nerror parity OK — {len(CASES) * 5} comparisons, {len(KNOWN)} known")


if __name__ == "__main__":
    main()
