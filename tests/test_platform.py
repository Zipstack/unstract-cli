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

from unstract_cli.config import DOCSTUDIO, PLATFORM, ConfigFile, ResolvedConfig
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
