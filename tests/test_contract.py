"""What the CLI can reach of what the APIs offer.

The vendored specs and the pinned clients move independently: a refreshed spec
can declare a parameter the published client has no argument for, and such a
parameter is dropped from the CLI rather than offered and then rejected at the
call. Dropping it silently is the failure mode this file exists to prevent --
the gap is written down, so widening it is a decision someone makes on purpose.
"""

from __future__ import annotations

import inspect

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
