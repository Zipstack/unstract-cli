"""Flag derivation: what the spec says, what the client accepts, what is sent.

Each test here corresponds to a way derived flags can be wrong while still
looking right: a value silently dropped, a default silently pinned, a flag
offered that the client cannot accept.
"""

from __future__ import annotations

import click
import pytest
from unstract.api_deployments.client import APIDeploymentsClient
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2

from unstract_cli.core import params as params_module
from unstract_cli.core.params import (
    Param,
    click_option,
    derive_params,
    docstring_params,
    find_operation,
    operation_params,
    requested,
)


def _by_name(params: list[Param]) -> dict[str, Param]:
    return {p.name: p for p in params}


# --------------------------------------------------------------------------- #
# Reading the spec
# --------------------------------------------------------------------------- #


def test_query_parameters_carry_type_and_default():
    params = _by_name(operation_params("llmwhisperer", "extract"))
    assert params["mode"].type == "string"
    assert params["add_line_nos"].type == "boolean"
    assert params["median_filter_size"].type == "integer"
    assert params["horizontal_stretch_factor"].default == 1.0


def test_body_parameters_are_derived_too():
    """The deployment declares its parameters in a multipart body, not a query."""
    params = _by_name(operation_params("docstudio", "execute"))
    assert params["tags"].type == "string"
    assert params["timeout"].type == "integer"
    assert params["presigned_urls"].array is True
    # `null | string` in the spec: the null branch carries nothing for a flag.
    assert params["llm_profile_id"].type == "string"
    assert params["llm_profile_id"].nullable is True


def test_a_required_body_parameter_stays_required():
    params = _by_name(operation_params("llmwhisperer", "webhook_post"))
    assert {p.name for p in params.values() if p.required} == {
        "url",
        "auth_token",
        "webhook_name",
    }


def test_the_uploaded_document_is_not_a_flag():
    """The binary body is the document itself, which the command takes as an
    argument."""
    assert "body" not in _by_name(operation_params("llmwhisperer", "extract"))
    assert find_operation("llmwhisperer", "extract")["method"] == "post"


def test_an_unknown_operation_names_itself():
    with pytest.raises(KeyError, match="whisper_sideways"):
        find_operation("llmwhisperer", "whisper_sideways")


# --------------------------------------------------------------------------- #
# Intersecting the spec with the published client
# --------------------------------------------------------------------------- #


def test_only_parameters_the_client_accepts_become_flags():
    """A flag the client cannot accept raises TypeError at the call instead of
    reaching the API, so it is not offered at all."""
    spec = set(_by_name(operation_params("llmwhisperer", "extract")))
    derived = set(
        _by_name(
            derive_params(
                "llmwhisperer", "extract", client_method=LLMWhispererClientV2.whisper
            )
        )
    )
    assert derived < spec
    # In URL mode the URL travels in the body, and saying so is the client's
    # decision, not a caller's.
    assert spec - derived == {"url_in_post"}


def test_the_clients_default_wins_over_the_specs():
    """What a caller gets by omitting a flag is the client's default, since the
    client sends its own value regardless of the spec's."""
    spec = _by_name(operation_params("llmwhisperer", "extract"))
    derived = _by_name(
        derive_params(
            "llmwhisperer", "extract", client_method=LLMWhispererClientV2.whisper
        )
    )
    assert spec["line_splitter_tolerance"].default == 0.75
    assert derived["line_splitter_tolerance"].default == 0.4


def test_every_deployment_parameter_survives_the_intersection():
    derived = _by_name(
        derive_params(
            "docstudio", "execute", client_method=APIDeploymentsClient.structure_file
        )
    )
    assert "tags" in derived and "hitl_queue_name" in derived


def test_excluded_parameters_do_not_become_flags():
    derived = _by_name(
        derive_params(
            "llmwhisperer",
            "extract",
            client_method=LLMWhispererClientV2.whisper,
            exclude=("use_webhook",),
        )
    )
    assert "use_webhook" not in derived


# --------------------------------------------------------------------------- #
# Help text
# --------------------------------------------------------------------------- #


def test_help_comes_from_the_clients_docstring():
    """The specs carry no parameter descriptions; the clients document every
    parameter, so that is where the text comes from."""
    derived = _by_name(
        derive_params(
            "llmwhisperer", "extract", client_method=LLMWhispererClientV2.whisper
        )
    )
    assert "language" in derived["lang"].description.lower()


def test_the_docstrings_own_restated_sentences_are_dropped():
    """The default and the allowed values are rendered from the signature and the
    spec; printing the docstring's copies too shows each twice and disagrees the
    moment either drifts."""
    described = docstring_params(LLMWhispererClientV2.whisper)
    assert not described["lang"].endswith('Defaults to "eng".')
    # A default that itself contains a period, which is where a sentence-shaped
    # match stops early and leaves half of it behind.
    assert not described["checkbox_confidence_threshold"].endswith("Defaults to 0.3.")
    assert described["mode"] == "The processing mode."
    assert described["tag"] == "The tag for the document."


def test_the_spec_wins_over_the_docstring_and_the_overlay_wins_over_both(monkeypatch):
    """Three sources can describe one flag, and only the most specific should
    show. Today no spec parameter carries a description, so the precedence is
    unexercised until one does -- which is when it would silently invert."""
    described = Param("lang", "string", description="From the spec.")
    monkeypatch.setattr(params_module, "operation_params", lambda *_: [described])

    derived = derive_params(
        "llmwhisperer", "extract", client_method=LLMWhispererClientV2.whisper
    )
    assert derived[0].description == "From the spec."
    assert click_option(derived[0], {}).help.startswith("From the spec.")
    assert click_option(derived[0], {"lang": {"help": "From the overlay."}}).help == (
        "From the overlay."
    )


def test_a_multi_line_description_is_joined():
    text = docstring_params(LLMWhispererClientV2.whisper)["word_confidence_threshold"]
    assert "\n" not in text and "confidence" in text


def test_the_default_is_reported_in_help():
    param = Param("mode", "string", default="form", description="The mode.")
    assert click_option(param, {}).help == "The mode. [default: form]"


# --------------------------------------------------------------------------- #
# Building Click options
# --------------------------------------------------------------------------- #


def test_a_boolean_gets_a_paired_flag_defaulting_to_neither():
    """`is_flag` cannot turn off a parameter that defaults to on, and cannot
    distinguish "not passed" from "passed false"."""
    option = click_option(Param("allow_rotated_text", "boolean", default=True), {})
    assert option.secondary_opts == ["--no-allow-rotated-text"]
    assert option.default is None


def test_no_option_carries_a_value_by_default():
    """A default written into the option would be sent on every call, pinning a
    value the client or server would otherwise choose."""
    for param in derive_params(
        "llmwhisperer", "extract", client_method=LLMWhispererClientV2.whisper
    ):
        assert click_option(param, {}).default is None


def test_choices_come_from_the_spec_unless_the_overlay_narrows_them():
    """A wrong value must fail before the request, not after -- and the list it
    is checked against is the service's own, not a copy that can fall behind."""
    spec_declared = _by_name(operation_params("llmwhisperer", "extract"))["mode"]
    assert "excel" in spec_declared.choices
    assert click_option(spec_declared, {}).type.choices == spec_declared.choices

    option = click_option(spec_declared, {"mode": {"choices": ["form", "table"]}})
    assert isinstance(option.type, click.Choice)
    assert option.type.choices == ("form", "table")


def test_an_array_becomes_a_repeatable_option():
    option = click_option(Param("presigned_urls", "string", array=True), {})
    assert option.multiple is True


def test_types_map_onto_click_types():
    assert click_option(Param("n", "integer"), {}).type is click.INT
    assert click_option(Param("x", "number"), {}).type is click.FLOAT
    assert click_option(Param("s", "string"), {}).type is click.STRING


def test_a_required_parameter_stays_required():
    assert click_option(Param("url", "string", required=True), {}).required is True


@pytest.mark.parametrize("type_name", ["string", "boolean"])
def test_a_required_flag_carries_no_default_at_all(type_name):
    """No command mounts a required derived flag today, so the parser check
    below has nothing live to protect. This pins the property itself: Click
    treats any default as a value the caller supplied."""
    option = click_option(Param("lines", type_name, required=True), {})
    bare = click.Option(["--bare"])
    assert option.default is bare.default
    assert click_option(Param("lines", type_name), {}).default is None


@pytest.mark.parametrize("type_name", ["string", "boolean"])
def test_a_required_flag_is_enforced_by_the_parser(type_name):
    """From Click 8.2 a default counts as a value the caller supplied, so a
    required option given one is never actually required."""
    option = click_option(Param("lines", type_name, required=True), {})
    command = click.Command("c", params=[option], callback=lambda **_: None)
    with pytest.raises(click.MissingParameter):
        command.make_context("c", [])


# --------------------------------------------------------------------------- #
# Choosing what to send
# --------------------------------------------------------------------------- #


def test_falsy_values_are_sent():
    """0, false and "" are choices. A truthiness filter eats them and hands the
    decision back to the server without telling anyone."""
    assert requested({"a": 0, "b": False, "c": "", "d": 0.0}) == {
        "a": 0,
        "b": False,
        "c": "",
        "d": 0.0,
    }


def test_unpassed_values_are_not_sent():
    assert requested({"a": None, "b": (), "c": 1}) == {"c": 1}


def test_dropped_names_are_not_sent():
    assert requested({"a": 1, "b": 2}, drop=("b",)) == {"a": 1}
