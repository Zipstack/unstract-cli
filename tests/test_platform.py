"""The Platform API client itself, exercised rather than stubbed.

Every command test replaces the client factory, which is the right seam for
asking what a command hands the client -- and it means nothing in the suite ever
constructs `CLIPlatformClient` or runs the URL it builds. Two mutations proved
that: dropping the `/unstract/` segment from `whoami`, and forcing every listing
to run against organisation `""`, both left the suite green.

These tests close that. No network: `_send` is replaced with a recorder, which
is the boundary between "what we build" and "what requests does with it".
"""

from __future__ import annotations

import pytest
from unstract.clone.context import OrgEndpoint

from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    PLATFORM,
    ConfigFile,
    ResolvedConfig,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.platform import (
    CLIPlatformClient,
    platform_base_url,
    platform_client,
)


def _client(**kwargs):
    return CLIPlatformClient(
        OrgEndpoint(
            base_url=kwargs.pop("base_url", "https://host.example"),
            organization_id=kwargs.pop("organization_id", ""),
            platform_key="pk-000000000000",
            **kwargs,
        )
    )


def _resolved(profiles, **overrides):
    return ResolvedConfig(
        file=ConfigFile(profiles=profiles, default_profile="p", exists=True),
        overrides=overrides,
    )


# --- the URL whoami builds ------------------------------------------------


def test_whoami_asks_for_no_organisation() -> None:
    """The parent's `_url` injects the organisation this call exists to find,
    so `whoami` builds its own URL -- and nothing else checks that it does.
    """
    client = _client()
    seen = {}
    client._send = lambda method, url, label, **kw: (
        seen.update(method=method, url=url) or {"organization_id": "acme"}
    )

    client.whoami()

    assert seen["url"] == "https://host.example/api/v1/unstract/whoami/"
    assert seen["method"] == "GET"


def test_whoami_url_survives_a_trailing_slash_on_the_base() -> None:
    client = _client(base_url="https://host.example/")
    seen = {}
    client._send = lambda m, url, label, **kw: seen.update(url=url) or {"a": 1}

    client.whoami()

    assert seen["url"] == "https://host.example/api/v1/unstract/whoami/"


@pytest.mark.parametrize("body", [None, [], [{"organization_id": "acme"}], "text"])
def test_whoami_rejects_a_body_that_is_not_an_identity(body) -> None:
    """`_send` returns None on a 204 or an empty 2xx, and whatever `resp.json()`
    decoded otherwise. Unguarded, `body.get(...)` raises an AttributeError that
    matches no arm in `__main__`, so the caller gets a traceback and no envelope
    -- the one thing this CLI promises never to do.
    """
    client = _client()
    client._send = lambda *a, **kw: body

    with pytest.raises(CLIError) as excinfo:
        client.whoami()

    assert excinfo.value.exit_code == ExitCode.SERVER_ERROR


# --- what the factory puts in the endpoint --------------------------------


def test_the_factory_passes_the_organisation_through() -> None:
    """`deployment ls` runs inside an organisation; `whoami` runs before one is
    known. Forcing the org-less value for both left every test green.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config, "org_ABC").endpoint.organization_id == "org_ABC"
    assert platform_client(config).endpoint.organization_id == ""


def test_the_factory_threads_a_timeout() -> None:
    """`--transport-timeout` was accepted on `deployment ls` and ignored: the
    parent's own default is 60s, spent per page.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config, timeout=3).timeout == 3


# --- which host the key is sent to ----------------------------------------


def test_the_platform_host_follows_docstudio_when_unset() -> None:
    """A profile written before the `platform` block existed names only
    docstudio's host. Resolving `platform.base_url` alone fell through to the
    built-in cloud default and sent the key there.
    """
    config = _resolved(
        {"p": {DOCSTUDIO: {"base_url": "https://onprem.example"}}},
    )

    assert platform_base_url(config) == "https://onprem.example"


def test_a_docstudio_base_url_flag_reaches_the_platform_call() -> None:
    """`docstudio --base-url` records `docstudio.base_url`; `deployment ls`
    reads the platform block. The flag was accepted and dropped.
    """
    config = _resolved({"p": {}}, **{"docstudio.base_url": "https://flag.example"})

    assert platform_base_url(config) == "https://flag.example"


def test_an_explicit_platform_host_still_wins() -> None:
    config = _resolved(
        {
            "p": {
                DOCSTUDIO: {"base_url": "https://docstudio.example"},
                PLATFORM: {"base_url": "https://platform.example"},
            }
        }
    )

    assert platform_base_url(config) == "https://platform.example"


def test_the_saas_default_is_honoured_when_the_caller_names_it() -> None:
    """The first fix compared the resolved value against
    `DEFAULT_BASE_URLS[PLATFORM]` to tell "unset" from "chosen". Those are the
    same string, so a caller who named the SaaS host was read as having named
    nothing and silently redirected to docstudio's -- the inverse of the defect
    it fixed. `config init` writes that exact host into every profile, so this
    is the common shape, not a corner of it.
    """
    profile = _resolved(
        {
            "p": {
                DOCSTUDIO: {"base_url": "https://onprem.example"},
                PLATFORM: {"base_url": DEFAULT_BASE_URLS[PLATFORM]},
            }
        }
    )
    flag = _resolved(
        {"p": {DOCSTUDIO: {"base_url": "https://onprem.example"}}},
        **{"platform.base_url": DEFAULT_BASE_URLS[PLATFORM]},
    )

    assert platform_base_url(profile) == DEFAULT_BASE_URLS[PLATFORM]
    assert platform_base_url(flag) == DEFAULT_BASE_URLS[PLATFORM]


def test_the_built_in_default_is_the_last_resort_not_a_veto() -> None:
    """Nobody named a host anywhere: the built-in default is still the answer.
    `get_explicit` stopping before the defaults must not lose that.
    """
    assert platform_base_url(_resolved({"p": {}})) == DEFAULT_BASE_URLS[PLATFORM]


@pytest.mark.parametrize("value", [0, 0.0, -1])
def test_a_non_positive_timeout_is_refused_before_urllib3_sees_it(value) -> None:
    """urllib3 raises a bare `ValueError` for a non-positive timeout, which
    matches no arm in `__main__`. Truncating the float with `int()` turned every
    `--transport-timeout` under 1s into exactly that.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    with pytest.raises(CLIError) as caught:
        platform_client(config, timeout=value)

    assert caught.value.exit_code == ExitCode.USAGE


def test_a_sub_second_timeout_survives_as_a_float() -> None:
    """`int(0.5)` is 0, which is the ValueError above; `int(1.9)` is 1, which is
    a bound the caller did not ask for.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config, timeout=0.5).timeout == 0.5
    assert platform_client(config, timeout=1.9).timeout == 1.9


def test_the_api_prefix_env_var_is_wired(monkeypatch) -> None:
    """The profile tier reads the block directly without consulting `ENV_VARS`,
    so removing the entry left the suite green while `$UNSTRACT_API_PREFIX` did
    nothing.
    """
    monkeypatch.setenv("UNSTRACT_API_PREFIX", "unstract-api/v1")
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config).endpoint.api_path_prefix == "unstract-api/v1"


def test_a_self_hosted_api_prefix_reaches_the_endpoint() -> None:
    """`clone` takes --api-prefix because a self-hosted deployment can mount the
    Platform API elsewhere. Without threading it, whoami and `deployment ls`
    were unreachable on exactly those installs, with no flag, env var or profile
    key that could fix it.
    """
    config = _resolved(
        {"p": {PLATFORM: {"api_key": "pk-000000000000", "api_prefix": "unstract-api/v1"}}}
    )

    client = platform_client(config)

    assert client.endpoint.api_path_prefix == "unstract-api/v1"
    seen = {}
    client._send = lambda m, url, label, **kw: seen.update(url=url) or {"a": 1}
    client.whoami()
    assert seen["url"].endswith("/unstract-api/v1/unstract/whoami/")
