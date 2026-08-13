"""What the CLI can reach of what the APIs offer.

The vendored specs and the pinned clients move independently: a refreshed spec
can declare a parameter the published client has no argument for, and such a
parameter is dropped from the CLI rather than offered and then rejected at the
call. Dropping it silently is the failure mode this file exists to prevent --
the gap is written down, so widening it is a decision someone makes on purpose.
"""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from unstract.api_deployments.client import APIDeploymentsClient
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2

from unstract_cli.core.params import derive_params, operation_params

#: (product, operationId, client method) per command that derives its flags,
#: with the spec parameters that method cannot accept. Most are a parameter the
#: client owns rather than one it lacks: `url_in_post` says the URL is in the
#: body, which the client decides; `files` is built from the paths given;
#: `execution_id` is read out of the endpoint URL the server handed back.
#: `highlights.mode` is the exception -- the endpoint reads it for quota
#: accounting and the published client has no argument for it, so the CLI cannot
#: offer it without the call failing.
COMMANDS = [
    (
        "llmwhisperer",
        "extract",
        LLMWhispererClientV2.whisper,
        {"url_in_post"},
    ),
    ("llmwhisperer", "highlights", LLMWhispererClientV2.get_highlight_data, {"mode"}),
    ("docstudio", "execute", APIDeploymentsClient.structure_file, {"files"}),
    (
        "docstudio",
        "status",
        APIDeploymentsClient.check_execution_status,
        {"execution_id"},
    ),
]


@pytest.mark.parametrize(
    ("product", "operation", "method", "unreachable"),
    COMMANDS,
    ids=[f"{p}:{o}" for p, o, _, _ in COMMANDS],
)
def test_the_parameters_no_command_can_reach_are_the_known_ones(
    product, operation, method, unreachable
):
    declared = {p.name for p in operation_params(product, operation)}
    derived = {p.name for p in derive_params(product, operation, client_method=method)}
    assert declared - derived == unreachable
    assert derived <= declared


@pytest.mark.parametrize(
    ("product", "operation", "method"),
    [(p, o, m) for p, o, m, _ in COMMANDS],
    ids=[f"{p}:{o}" for p, o, _, _ in COMMANDS],
)
def test_every_derived_flag_is_an_argument_the_client_accepts(product, operation, method):
    """The check the CLI cannot make at runtime: a flag the client has no
    parameter for raises TypeError at the call, after the document is read."""
    accepted = set(inspect.signature(method).parameters)
    for param in derive_params(product, operation, client_method=method):
        assert param.name in accepted


#: The flags the specs derive today. Every other check in this file reads the
#: spec on both sides of its comparison, so a spec that loses a parameter loses
#: the flag and the expectation together; this file is the side that does not
#: move on its own.
SNAPSHOT = Path(__file__).parent / "derived_flags.json"

#: Refreshing the snapshot is a decision, not a side effect of running the suite.
REFRESH = "UNSTRACT_CLI_REFRESH_FLAG_SNAPSHOT"


def _derived_flags() -> dict[str, dict[str, Any]]:
    """Every flag the specs derive, with the whole of what each one accepts.

    Names alone would let a spec narrow an enum, or change a type or a default,
    without moving the snapshot -- and the CLI would start rejecting a value it
    used to take, with nothing here to say so.
    """
    return {
        f"{product}:{operation}": {
            # Choices as a list: JSON has no tuple, and the snapshot is compared
            # against what a JSON reader gives back.
            param.flag: {**asdict(param), "choices": list(param.choices)}
            for param in sorted(
                derive_params(product, operation, client_method=method),
                key=lambda param: param.flag,
            )
        }
        for product, operation, method, _ in COMMANDS
    }


def _changed(current: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """The flags that moved, named. Comparing whole payloads reports neither."""
    return sorted(
        f"{operation} {flag}"
        for operation in current.keys() | expected.keys()
        for flag in current.get(operation, {}).keys() | expected.get(operation, {}).keys()
        if current.get(operation, {}).get(flag) != expected.get(operation, {}).get(flag)
    )


def test_the_derived_flags_are_the_ones_last_reviewed():
    current = _derived_flags()
    if os.environ.get(REFRESH):
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected, (
        "What the vendored specs derive has changed: "
        f"{', '.join(_changed(current, expected))}. A flag that disappears here "
        "disappears from the CLI, and a choice or a type that narrows here "
        "rejects a value the CLI used to take. Review the difference, then "
        f"refresh the snapshot with {REFRESH}=1."
    )
