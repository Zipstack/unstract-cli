"""Task 0 acceptance: the schema expresses parameter patterns P1-P12.

IMPLEMENTATION_PLAN.md §2 calls this the load-bearing design work: if any pattern
in the endpoint reference cannot be expressed declaratively, generation degrades
into per-command special-casing and the Skill's diff stops being meaningful.
Each test below pins one pattern to a real record in the shipped surface.
"""

from __future__ import annotations

import pytest

from unstract_cli.core.model import (
    AtLeastOneOf,
    BodyKind,
    Endpoint,
    MutuallyExclusive,
    Param,
    ParamLocation,
    ParamType,
    Product,
    derive_patch,
    with_params,
)
from unstract_cli.endpoints import ALL_ENDPOINTS, get_endpoint


class TestP1MutuallyExclusive:
    """P1 - exactly one of a set of flags."""

    def test_rejects_both(self):
        c = MutuallyExclusive(("file", "url"))
        assert "mutually exclusive" in (c.check({"file": "a", "url": "b"}) or "")

    def test_rejects_neither_when_required(self):
        assert "required" in (MutuallyExclusive(("file", "url")).check({}) or "")

    def test_accepts_exactly_one(self):
        assert MutuallyExclusive(("file", "url")).check({"file": "a"}) is None

    def test_optional_variant_allows_neither(self):
        assert MutuallyExclusive(("api", "pipeline"), required=False).check({}) is None

    def test_real_endpoint_uses_it(self):
        endpoint = get_endpoint("whisper.extract")
        assert any(isinstance(c, MutuallyExclusive) for c in endpoint.constraints)
        assert endpoint.validate({"file": "a", "url": "b"})


class TestP2AtLeastOneOf:
    """P2 - guard against an unfiltered destructive operation."""

    def test_requires_one(self):
        assert AtLeastOneOf(("ids", "status")).check({}) is not None
        assert AtLeastOneOf(("ids", "status")).check({"status": "ERROR"}) is None

    def test_file_history_clear_is_guarded(self):
        # Without a filter this endpoint would delete every file history.
        endpoint = get_endpoint("platform.workflow.file-history.clear")
        assert any(isinstance(c, AtLeastOneOf) for c in endpoint.constraints)
        assert endpoint.validate({"workflow_id": "w"})


class TestP3ChoiceMapping:
    """P3 - friendly value maps to a different wire value."""

    def test_mapping_translates(self):
        param = Param("resource", choices={"api-deployment": "api/deployment"})
        assert param.to_wire("api-deployment") == "api/deployment"

    def test_plain_sequence_is_identity(self):
        param = Param("mode", choices=["form", "table"])
        assert param.to_wire("form") == "form"

    def test_share_resource_mapping(self):
        param = get_endpoint("platform.share").param("resource")
        assert param.choice_map()["api-deployment"] == "api/deployment"


class TestP4Repeatable:
    def test_multiple_flag(self):
        assert get_endpoint("deployment.run").param("files").multiple


class TestP5Freeform:
    """P5 - escape hatch for parameters newer than the CLI."""

    def test_ext_param_declares_prefix(self):
        param = get_endpoint("apihub.extract").param("ext_param")
        assert param.freeform_prefix == "ext_"
        assert param.multiple


class TestP6PathParamDefaults:
    def test_org_id_defaults_from_profile(self):
        param = get_endpoint("deployment.run").param("org_id")
        assert param.location is ParamLocation.PATH
        assert param.default_from == "deployment.org_id"

    def test_api_name_has_no_default(self):
        # One profile serves many deployments, so this cannot be defaulted.
        param = get_endpoint("deployment.run").param("api_name")
        assert param.required and param.default_from is None


class TestP7Locations:
    def test_all_locations_representable(self):
        used = {p.location for e in ALL_ENDPOINTS for p in e.params}
        assert {ParamLocation.QUERY, ParamLocation.BODY, ParamLocation.PATH,
                ParamLocation.FORM} <= used

    def test_body_kinds_used(self):
        used = {e.body for e in ALL_ENDPOINTS}
        assert {BodyKind.NONE, BodyKind.JSON, BodyKind.MULTIPART,
                BodyKind.BINARY_FILE} <= used


class TestP8DerivedPatch:
    """P8 - PATCH derives from PUT so records cannot drift apart."""

    def test_strips_required_but_keeps_path_params(self):
        put = Endpoint(
            name="update", group="g", method="PUT", path="/x/{id}/",
            product=Product.PLATFORM, summary="s",
            params=(
                Param("id", location=ParamLocation.PATH, required=True),
                Param("name", location=ParamLocation.BODY, required=True),
            ),
        )
        patch = derive_patch(put)
        assert patch.method == "PATCH"
        assert patch.param("id").required, "path params stay required"
        assert not patch.param("name").required, "body fields become optional"

    def test_shipped_patches_mirror_their_put(self):
        for group in ("platform.prompt-studio", "platform.workflow"):
            put = get_endpoint(f"{group}.update")
            patch = get_endpoint(f"{group}.patch")
            assert {p.name for p in patch.params} >= {p.name for p in put.params}, (
                "a PATCH missing a PUT field means the records have drifted"
            )

    def test_with_params_appends(self):
        base = Endpoint(name="a", group="g", method="PATCH", path="/x",
                        product=Product.PLATFORM, summary="s")
        extended = with_params(base, Param("active", type=ParamType.BOOL))
        assert extended.param("active") is not None
        assert base.param("active") is None, "the source record must be untouched"


class TestP9ConditionalApplicability:
    def test_documented_not_enforced(self):
        param = get_endpoint("whisper.extract").param("median_filter_size")
        assert param.applies_when == "mode=low_cost"
        # Deliberately no constraint: the server owns this rule, and enforcing it
        # locally would guess wrong when the server's behaviour changes.
        assert not get_endpoint("whisper.extract").validate({"file": "x"})


class TestP10IdentifierTypes:
    def test_group_ids_are_ints_not_uuids(self):
        assert get_endpoint("platform.group.patch").param("id").type is ParamType.INT
        assert get_endpoint("platform.workflow.get").param("id").type is ParamType.UUID


class TestP11LiteralPaths:
    """P11 - upstream paths are inconsistent, and that inconsistency is load-bearing."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("platform.prompt-studio.profile.create",
             "/api/v1/unstract/{org_id}/prompt-studio/profilemanager/{tool_id}"),
            ("platform.prompt-studio.profile.get",
             "/api/v1/unstract/{org_id}/prompt-studio/profile-manager/{profile_id}/"),
            ("platform.group.member.remove",
             "/api/v1/unstract/{org_id}/groups/{id}/members/{user_id}"),
            ("platform.pipeline.postman-collection",
             "/api/v1/unstract/{org_id}/pipeline/api/postman_collection/{id}/"),
            ("platform.api-deployment.postman-collection",
             "/api/v1/unstract/{org_id}/api/postman_collection/{id}/"),
        ],
    )
    def test_exact_paths(self, name, expected):
        assert get_endpoint(name).path == expected

    def test_missing_trailing_slash_is_declared(self):
        """A path without a trailing slash must say so, so it reads as intent."""
        for endpoint in ALL_ENDPOINTS:
            if endpoint.product is not Product.PLATFORM:
                continue
            if "{" in endpoint.path.split("/")[-1] and not endpoint.path.endswith("/"):
                assert endpoint.no_trailing_slash, (
                    f"{endpoint.dotted_name} lacks a trailing slash but does not "
                    "declare no_trailing_slash -- typo or intent?"
                )


class TestP12ReplaceSemantics:
    def test_shared_users_marked_replace(self):
        assert get_endpoint("platform.share").param("shared_users").replace_semantics


class TestRegistryIntegrity:
    def test_names_unique(self):
        names = [e.dotted_name for e in ALL_ENDPOINTS]
        assert len(names) == len(set(names)), "duplicate command paths"

    def test_every_endpoint_documented(self):
        for endpoint in ALL_ENDPOINTS:
            assert endpoint.summary, f"{endpoint.dotted_name} has no summary"
            assert endpoint.doc_source, f"{endpoint.dotted_name} has no doc_source"

    def test_every_param_has_help(self):
        for endpoint in ALL_ENDPOINTS:
            for param in endpoint.params:
                assert param.help, f"{endpoint.dotted_name}:{param.name} has no help"

    def test_poll_specs_reference_real_endpoints(self):
        for endpoint in ALL_ENDPOINTS:
            if not endpoint.poll:
                continue
            get_endpoint(endpoint.poll.status_endpoint)
            if endpoint.poll.retrieve_endpoint:
                get_endpoint(endpoint.poll.retrieve_endpoint)

    def test_methods_are_valid(self):
        valid = {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert {e.method for e in ALL_ENDPOINTS} <= valid
