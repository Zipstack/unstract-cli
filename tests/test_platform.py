"""Where the platform key is sent, and the one flag the CLI validates itself.

The transport and the two operations are generated into `unstract-client` and
covered there; what is left here is CLI-owned: which host and key the client
is built with, and refusing a `--transport-timeout` the connection layer would
reject deep inside itself.
"""

from __future__ import annotations

import pytest

from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    ConfigFile,
    ResolvedConfig,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.platform import platform_client

KEY = {"platform_key": "pk-000000000000"}


def _resolved(profiles, **overrides):
    return ResolvedConfig(
        file=ConfigFile(profiles=profiles, default_profile="p", exists=True),
        overrides=overrides,
    )


def test_the_factory_threads_a_timeout() -> None:
    """`--transport-timeout` was accepted on `deployment ls` and ignored: the
    parent's own default is 60s, spent per page.
    """
    config = _resolved({"p": {DOCSTUDIO: KEY}})

    assert platform_client(config, timeout=3).transport_timeout == 3


# --- which host the key is sent to ----------------------------------------


def test_the_platform_host_is_docstudios() -> None:
    """One deployment serves both the platform API and the deployments it
    manages, so the host a caller named for docstudio is the host for this.
    """
    config = _resolved({"p": {DOCSTUDIO: {**KEY, "base_url": "https://onprem.example"}}})

    assert platform_client(config).base_url == "https://onprem.example"


def test_a_docstudio_base_url_flag_reaches_the_platform_call() -> None:
    config = _resolved(
        {"p": {DOCSTUDIO: {**KEY, "base_url": "https://profile.example"}}},
        **{"docstudio.base_url": "https://flag.example"},
    )

    assert platform_client(config).base_url == "https://flag.example"


def test_the_built_in_default_is_the_last_resort() -> None:
    assert (
        platform_client(_resolved({"p": {DOCSTUDIO: KEY}})).base_url
        == (DEFAULT_BASE_URLS[DOCSTUDIO])
    )


def test_the_platform_key_is_not_the_deployment_key() -> None:
    """Both live on the docstudio block; the client must take the right one."""
    config = _resolved(
        {"p": {DOCSTUDIO: {**KEY, "api_key": "dk-deployment-key"}}},
    )

    assert platform_client(config).api_key == KEY["platform_key"]


def test_a_missing_platform_key_names_where_one_goes() -> None:
    with pytest.raises(Exception, match="docstudio.platform_key") as caught:
        platform_client(_resolved({"p": {}}))

    assert "UNSTRACT_PLATFORM_KEY" in str(caught.value)
    # A secret flag exists but is never suggested: it lands in shell history.
    assert "--platform-key" not in str(caught.value)


@pytest.mark.parametrize("value", [0, 0.0, -1])
def test_a_non_positive_timeout_is_refused_before_the_transport_sees_it(value) -> None:
    """httpx rejects a non-positive timeout deep in the connection layer, with
    an error that matches no arm in `__main__`. Refused at the flag instead, so
    the caller gets a usage error and an envelope.
    """
    config = _resolved({"p": {DOCSTUDIO: KEY}})

    with pytest.raises(CLIError) as caught:
        platform_client(config, timeout=value)

    assert caught.value.exit_code == ExitCode.USAGE


def test_a_sub_second_timeout_survives_as_a_float() -> None:
    """An earlier revision truncated this with `int()`, which sent
    `--transport-timeout 0.5` as 0 -- the rejection above -- and silently
    rounded 1.9 down to 1.
    """
    config = _resolved({"p": {DOCSTUDIO: KEY}})

    assert platform_client(config, timeout=0.5).transport_timeout == 0.5
    assert platform_client(config, timeout=1.9).transport_timeout == 1.9
