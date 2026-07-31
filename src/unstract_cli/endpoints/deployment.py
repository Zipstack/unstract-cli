"""Unstract API Deployment runtime endpoints (SPEC.md §6.2).

Authored from `unstract-docs/docs/unstract_platform/api_deployment/`.

Two behaviours worth knowing before reading the records:

* ``org_id`` and ``api_name`` are URL *path segments*. ``--org-id`` falls back to
  the active profile; ``--api-name`` cannot, because one profile serves many
  deployments.
* The in-progress states ``PENDING``/``EXECUTING`` are currently returned with
  HTTP 422 rather than 200 -- a documented server-side defect. The poller reads
  the body's ``status`` field, so behaviour is unchanged when that is fixed.
"""

from __future__ import annotations

from unstract_cli.core.model import (
    ApiGroup,
    BodyKind,
    Endpoint,
    Param,
    ParamLocation,
    ParamType,
    PollSpec,
)

_DOCS = "unstract-docs/docs/unstract_platform/api_deployment"

#: Path parameters shared by every deployment command.
_PATH_PARAMS: tuple[Param, ...] = (
    Param(
        "org_id",
        location=ParamLocation.PATH,
        # Falls back to the platform block: `docstudio.deployment` is a separate,
        # initially-empty config section, so a user who has configured the
        # Platform API still hit "missing org_id" here even though it is the same
        # organization. The deployment block still wins when set.
        default_from="deployment.org_id platform.org_id",
        help="Organization identifier. Falls back to the platform block's org_id",
    ),
    Param(
        "api_name",
        location=ParamLocation.PATH,
        required=True,
        help="Deployed API name, as shown in the API Deployments page",
    ),
)

_EXECUTION_STATUSES = ("PENDING", "EXECUTING", "COMPLETED", "STOPPED", "ERROR")


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="run",
        group="docstudio",
        subgroup="deployment",
        method="POST",
        path="/deployment/api/{org_id}/{api_name}/",
        api=ApiGroup.DEPLOYMENT,
        summary="Execute a deployed API workflow on one or more files.",
        description=(
            "Accepts up to 32 files per call, counting --file and --presigned-url "
            "together. Results are keyed by file name, so names must be unique "
            "within a call. Use --wait to poll to completion.\n\n"
            "ONE-SHOT with --wait: the result store is read exactly once. --wait "
            "returns the result from the poll that first observes COMPLETED and "
            "does not re-read it. Pass --save to persist it on that single read; "
            "without --save the result is only printed and cannot be re-fetched.\n\n"
            "Synchronous mode (--timeout > 0) is deprecated upstream; --wait uses "
            "asynchronous execution plus polling, which is the supported path.\n\n"
            "Auth comes from the `docstudio.deployment` config block, which is "
            "separate from the platform block and starts empty. After creating a "
            "deployment and a key, wire the key in with:\n"
            "  unstract config set docstudio.deployment api_key env:UNSTRACT_DEPLOYMENT_KEY\n"
            "--org-id now falls back to the platform block's org_id, so that one "
            "usually needs no second entry."
        ),
        params=(
            *_PATH_PARAMS,
            Param("files", type=ParamType.FILE, location=ParamLocation.FORM,
                  multiple=True, flag="--file", help="File to process; repeatable"),
            Param("presigned_urls", location=ParamLocation.FORM, multiple=True,
                  flag="--presigned-url",
                  help="HTTPS AWS S3 presigned URL to fetch a file from; repeatable"),
            Param("timeout", type=ParamType.INT, location=ParamLocation.FORM, default=0,
                  flag="--execution-timeout",
                  help="Seconds to wait server-side (0-300). 0 runs asynchronously"),
            Param("include_metadata", type=ParamType.BOOL, location=ParamLocation.FORM,
                  default=False,
                  help="Include LLM/embedding usage and cost metadata in the result"),
            Param("include_metrics", type=ParamType.BOOL, location=ParamLocation.FORM,
                  default=False,
                  help="Include execution metrics in the result"),
            Param("tags", location=ParamLocation.FORM,
                  help="Tag for this execution; must start with a letter (limit 1)"),
            Param("llm_profile_id", type=ParamType.UUID, location=ParamLocation.FORM,
                  help="Override the tool's default LLM profile"),
            Param("custom_data", type=ParamType.JSON, location=ParamLocation.FORM,
                  help="JSON object addressable in prompts as {{custom_data.key}}"),
            Param("hitl_queue_name", location=ParamLocation.FORM,
                  help="Route results to this Human Quality Review queue instead of returning them"),
            Param("save", client_side=True,
                  help="With --wait, write the one-shot result to this path before "
                       "exiting (strongly recommended: the result can be read only once)"),
        ),
        body=BodyKind.MULTIPART,
        # ONE-SHOT poll: the status endpoint *is* the result store. The poll that
        # first observes COMPLETED consumes the result, so that terminal poll's
        # body is returned as the result and no second read is issued (a second
        # read would 406). `status_field` lists `status` first because the status
        # GET returns a top-level `status` (its `message` holds the result); the
        # nested `execution_status` is the run POST's shape. Reading the wrong one
        # made the terminal state go unrecognised.
        poll=PollSpec(
            status_endpoint="docstudio.deployment.status",
            status_field=("status", "execution_status"),
            terminal_success=("COMPLETED",),
            terminal_failure=("ERROR", "STOPPED"),
            in_progress=("PENDING", "EXECUTING"),
            handle_field="execution_id",
            handle_param="execution_id",
            # The run POST returns no `execution_id` anywhere in its body --
            # {"execution_status", "status_api", "error", "result"} -- so the id
            # has to come out of the status_api query string. Without this,
            # --wait cannot poll and silently returns the PENDING stub.
            handle_from_query=("status_api", "execution_id"),
            # The status GET defaults include_metadata to False; on this one-shot
            # store the server strips the metadata AND drops it, so a user who
            # asked for it would lose it permanently on the poll.
            poll_carry=("include_metadata", "include_metrics"),
            one_shot=True,
        ),
        doc_source=f"{_DOCS}/api_execution.md",
        examples=(
            "unstract deployment run --api-name invoice-api --file invoice.pdf --wait --save out.json",
            "unstract deployment run --api-name invoice-api --file a.pdf --file b.pdf",
            "unstract deployment run --api-name invoice-api --file x.pdf --hitl-queue-name review",
        ),
    ),
    Endpoint(
        name="status",
        group="docstudio",
        subgroup="deployment",
        method="GET",
        path="/deployment/api/{org_id}/{api_name}/",
        api=ApiGroup.DEPLOYMENT,
        summary="Check the status of an execution and retrieve its result.",
        description=(
            f"Execution statuses: {', '.join(_EXECUTION_STATUSES)}.\n\n"
            "ONE-SHOT: results are removed from the server once retrieved. A "
            "second call returns HTTP 406 and exits 9 -- pass --save to persist "
            "them on the first read.\n\n"
            "Note: PENDING and EXECUTING currently return HTTP 422 rather than "
            "200. This CLI reads the status from the response body, so that "
            "server-side defect does not affect it."
        ),
        params=(
            *_PATH_PARAMS,
            Param("execution_id", type=ParamType.UUID, required=True,
                  help="Execution identifier returned by `deployment run`"),
            Param("include_metadata", type=ParamType.BOOL, default=False,
                  help="Include LLM/embedding usage and cost metadata"),
            Param("include_metrics", type=ParamType.BOOL, default=False,
                  help="Include execution metrics"),
            Param("save", client_side=True,
                  help="Write the result to this path before exiting (recommended)"),
        ),
        doc_source=f"{_DOCS}/api_execution_status.md",
        examples=(
            "unstract deployment status --api-name invoice-api --execution-id <uuid> --save out.json",
        ),
    ),
    Endpoint(
        name="highlight",
        group="docstudio",
        subgroup="deployment",
        method="GET",
        path="/deployment/api/{org_id}/{api_name}/highlight/",
        api=ApiGroup.DEPLOYMENT,
        summary="Fetch line coordinates for highlighting extracted values.",
        description=(
            "Requires 'Enable Highlight' on the exported tool. The whisper_hash "
            "and line_numbers come from the execution result, at "
            "result.metadata.whisper_hash and result.metadata.line_numbers."
        ),
        params=(
            *_PATH_PARAMS,
            Param("whisper_hash", required=True,
                  help="From result.metadata.whisper_hash in the execution result"),
            Param("line_numbers", required=True,
                  help="Comma-separated line numbers, e.g. '2,3,6'"),
            Param("text_extractor_name", required=True,
                  help="Text extractor adapter name, e.g. llm-whisperer-v2"),
        ),
        doc_source=f"{_DOCS}/api_get_highlight_data.md",
        examples=(
            "unstract deployment highlight --api-name inv --whisper-hash h "
            "--line-numbers 2,3,6 --text-extractor-name llm-whisperer-v2",
        ),
    ),
)
