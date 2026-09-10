"""Where the platform key is sent, and the one flag the CLI validates itself.

The transport and the two operations are generated into `unstract-client` and
covered there; what is left here is CLI-owned: which host the key goes to when
several tiers and two products could each name one, and refusing a
`--transport-timeout` the connection layer would reject deep inside itself.

The whoami URL and the identity body used to be tested here, because an earlier
revision hand-built both on a subclass of the clone tool's admin client. They
moved upstream with the operation.
"""

from __future__ import annotations

import pytest

from unstract_cli.config import (
    DEFAULT_BASE_URLS,
    DOCSTUDIO,
    PLATFORM,
    ConfigFile,
    ResolvedConfig,
)
from unstract_cli.core.errors import CLIError, ExitCode
from unstract_cli.core.platform import platform_base_url, platform_client


def _resolved(profiles, **overrides):
    return ResolvedConfig(
        file=ConfigFile(profiles=profiles, default_profile="p", exists=True),
        overrides=overrides,
    )


# --- the URL whoami builds ------------------------------------------------


def test_the_factory_threads_a_timeout() -> None:
    """`--transport-timeout` was accepted on `deployment ls` and ignored: the
    parent's own default is 60s, spent per page.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config, timeout=3).transport_timeout == 3


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


def test_a_docstudio_flag_beats_a_platform_host_in_the_profile() -> None:
    """Greptile, on PR #3. Walking `platform`'s three tiers before docstudio's
    let a *profile* value beat a *flag*, inverting the precedence the config
    layer promises everywhere else -- and `config init` writes
    `platform.base_url` into every profile it generates, so the flag was ignored
    for every generated config, not a corner case.
    """
    config = _resolved(
        {"p": {PLATFORM: {"base_url": DEFAULT_BASE_URLS[PLATFORM]}}},
        **{"docstudio.base_url": "https://flag.example"},
    )

    assert platform_base_url(config) == "https://flag.example"


def test_a_platform_flag_still_beats_a_docstudio_flag() -> None:
    """Within one tier the specific product wins; across tiers it does not."""
    config = _resolved(
        {"p": {}},
        **{
            "platform.base_url": "https://platform-flag.example",
            "docstudio.base_url": "https://docstudio-flag.example",
        },
    )

    assert platform_base_url(config) == "https://platform-flag.example"


def test_an_environment_host_beats_a_profile_on_either_product() -> None:
    """`$UNSTRACT_BASE_URL` maps to both products, and env outranks profile."""
    import os

    config = _resolved({"p": {PLATFORM: {"base_url": "https://profile.example"}}})
    os.environ["UNSTRACT_BASE_URL"] = "https://env.example"
    try:
        assert platform_base_url(config) == "https://env.example"
    finally:
        del os.environ["UNSTRACT_BASE_URL"]


def test_the_built_in_default_is_the_last_resort_not_a_veto() -> None:
    """Nobody named a host anywhere: the built-in default is still the answer.
    `get_explicit` stopping before the defaults must not lose that.
    """
    assert platform_base_url(_resolved({"p": {}})) == DEFAULT_BASE_URLS[PLATFORM]


@pytest.mark.parametrize("value", [0, 0.0, -1])
def test_a_non_positive_timeout_is_refused_before_the_transport_sees_it(value) -> None:
    """httpx rejects a non-positive timeout deep in the connection layer, with
    an error that matches no arm in `__main__`. Refused at the flag instead, so
    the caller gets a usage error and an envelope.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    with pytest.raises(CLIError) as caught:
        platform_client(config, timeout=value)

    assert caught.value.exit_code == ExitCode.USAGE


def test_a_sub_second_timeout_survives_as_a_float() -> None:
    """An earlier revision truncated this with `int()`, which sent
    `--transport-timeout 0.5` as 0 -- the rejection above -- and silently
    rounded 1.9 down to 1.
    """
    config = _resolved({"p": {PLATFORM: {"api_key": "pk-000000000000"}}})

    assert platform_client(config, timeout=0.5).transport_timeout == 0.5
    assert platform_client(config, timeout=1.9).transport_timeout == 1.9
