"""Endpoint registry — the single source of truth for the CLI surface.

Every command in the tree originates here. The bundled Claude Skill edits these
modules and nothing else: because commands, help text and `--discover` are
all generated from these records, a change here propagates everywhere at once.
"""

from __future__ import annotations

from unstract_cli.core.model import Endpoint

from . import apihub, deployment, hitl, platform, whisper

#: Ordered so `--help` lists groups in a sensible progression: extraction first,
#: then execution, then management.
ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    *whisper.ENDPOINTS,
    *deployment.ENDPOINTS,
    *platform.ENDPOINTS,
    *hitl.ENDPOINTS,
    *apihub.ENDPOINTS,
)

_BY_NAME: dict[str, Endpoint] = {e.dotted_name: e for e in ALL_ENDPOINTS}


def get_endpoint(dotted_name: str) -> Endpoint:
    """Look up an endpoint by dotted name, e.g. ``whisper.status``.

    Used by the poller to find the status/retrieve endpoints named in a
    `PollSpec`, which keeps polling declarative rather than hard-coded.
    """
    try:
        return _BY_NAME[dotted_name]
    except KeyError:
        raise KeyError(
            f"No endpoint named {dotted_name!r}. Known: {', '.join(sorted(_BY_NAME))}"
        ) from None


def endpoints_for(group: str) -> tuple[Endpoint, ...]:
    """All endpoints in one product group."""
    return tuple(e for e in ALL_ENDPOINTS if e.group == group)


__all__ = ["ALL_ENDPOINTS", "endpoints_for", "get_endpoint"]
