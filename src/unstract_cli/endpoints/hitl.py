"""Human Quality Review (HITL) endpoints (SPEC.md §6.4). Enterprise feature.

Authored from `unstract-docs/docs/unstract_platform/human_quality_review/`.

Pushing *into* HITL is not a separate command: it is
`unstract deployment run --hitl-queue-name <name>`.
"""

from __future__ import annotations

from unstract_cli.core.model import ApiGroup, Endpoint, Param, ParamLocation, ParamType

_DOCS = "unstract-docs/docs/unstract_platform/human_quality_review"

_ORG = Param(
    "org_id",
    location=ParamLocation.PATH,
    # Same fallback as `deployment run`: the hitl block is its own, initially
    # empty config section, but the organization is the same one.
    default_from="hitl.org_id platform.org_id",
    help="Organization identifier. Falls back to the platform block's org_id",
)

_CLASS_ID = Param(
    "class_id",
    location=ParamLocation.PATH,
    required=True,
    help="Class (workflow) identifier, from the Download and Sync Manager",
)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="get",
        group="docstudio",
        subgroup="hitl approved",
        method="GET",
        path="/mr/api/{org_id}/approved/result/{class_id}/",
        api=ApiGroup.HITL,
        summary="Dequeue one approved result from the review queue.",
        description=(
            "DEQUEUE, not a read: each call removes one item from the approved "
            "queue and returns it. The item is gone from the server afterwards, so "
            "pass --save to persist it. Use `hitl bulk-download` to page through "
            "results without consuming them."
        ),
        params=(
            _ORG,
            _CLASS_ID,
            Param("hitl_queue_name", help="Queue name suffix used at deployment run time"),
            Param("save", client_side=True,
                  help="Write the dequeued item to this path before exiting (recommended)"),
        ),
        doc_source=f"{_DOCS}/retrieve_approved_results.md",
        examples=(
            "unstract hitl approved get --class-id <workflow-id> --save approved.json",
        ),
    ),
    Endpoint(
        name="bulk-download",
        group="docstudio",
        subgroup="hitl",
        method="GET",
        path="/mr/api/{org_id}/approved/result/{class_id}/",
        api=ApiGroup.HITL,
        summary="Page through approved results, optionally including file content.",
        description=(
            "Unlike `hitl approved get`, this is a paginated read. With "
            "--download-files and a large page, the server may switch to an "
            "asynchronous job and return a job id -- poll it with "
            "`hitl download-status`."
        ),
        params=(
            _ORG,
            _CLASS_ID,
            Param("page", type=ParamType.INT, default=1, help="Page number"),
            Param("page_size", type=ParamType.INT, default=50,
                  help="Records per page (1-500)"),
            Param("download_files", type=ParamType.BOOL, default=False,
                  help="Include file content in the response"),
            Param("email", help="Email address to notify when an async download completes"),
            Param("save", client_side=True, help="Write the response to this path"),
        ),
        doc_source=f"{_DOCS}/bulk_download.md",
        examples=(
            "unstract hitl bulk-download --class-id <workflow-id> --page 1 --page-size 50",
        ),
    ),
    Endpoint(
        name="download-status",
        group="docstudio",
        subgroup="hitl",
        method="GET",
        path="/mr/api/{org_id}/approved/download-status/{job_id}/",
        api=ApiGroup.HITL,
        summary="Check the status of an asynchronous bulk download job.",
        params=(
            _ORG,
            Param("job_id", location=ParamLocation.PATH, required=True,
                  help="Job identifier returned by `hitl bulk-download`"),
        ),
        doc_source=f"{_DOCS}/bulk_download.md",
        examples=("unstract hitl download-status --job-id job-789",),
    ),
)
