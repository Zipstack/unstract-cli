"""The check that matters: does downstream `except ConnectionError` still match?

`unstract/sdk1`'s x2text adapter catches `requests.exceptions.ConnectionError`
and `Timeout` by name around client calls. The generated transport is httpx,
whose exceptions are not subclasses of those. Nothing fails at import and
nothing fails in a test that does not simulate a network failure — so this is
the test that simulates one.

    build/poc-venv/bin/python poc/test_compat.py
"""

import sys
from pathlib import Path

import httpx
from requests.exceptions import ConnectionError, Timeout

sys.path.insert(0, str(Path(__file__).resolve().parent))
from facade import GeneratedAPIDeploymentsClient, _translate  # noqa: E402


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except BaseException as e:
        raise AssertionError(f"expected {exc.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc.__name__}, nothing raised")


def main():
    # httpx's own exceptions are NOT what downstream catches — prove it.
    assert not issubclass(httpx.ConnectError, ConnectionError)
    assert not issubclass(httpx.TimeoutException, Timeout)

    def boom(e):
        def f():
            raise e

        return f

    assert raises(lambda: _translate(boom(httpx.ConnectError("x"))), ConnectionError)
    assert raises(lambda: _translate(boom(httpx.ConnectTimeout("x"))), Timeout)
    assert raises(lambda: _translate(boom(httpx.ReadTimeout("x"))), Timeout)
    assert raises(lambda: _translate(boom(httpx.ReadError("x"))), ConnectionError)

    # End to end, through a real client pointed at a closed port.
    client = GeneratedAPIDeploymentsClient(
        api_url="http://127.0.0.1:1/deployment/api/org/api-name/",
        api_key="k",
        api_timeout=-1,
    )
    with open(__file__, "rb"):
        assert raises(lambda: client.structure_file([__file__]), ConnectionError)
    assert raises(
        lambda: client.check_execution_status("/x/?execution_id=1"), ConnectionError
    )

    # Return-shape contract: same keys as the published client, always.
    KEYS = {"status_code", "pending", "execution_status", "error", "extraction_result"}
    assert set(GeneratedAPIDeploymentsClient._shape(200, "", "", "")) == KEYS

    print("compat OK")


if __name__ == "__main__":
    main()
