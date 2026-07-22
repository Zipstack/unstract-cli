"""API Hub (Verticals) endpoints (SPEC.md §6.5).

**Source of truth is code, not docs.** API Hub has no public documentation site,
so these records were authored from `unstract-verticals/src/api_v1/api.py` and the
Postman collections in `verticals-portal/portal/postman-collection/`. The Skill
must flag changes here for human review rather than applying them silently
(SPEC.md §8.4).

Auth is the `apikey` header at the Kong gateway. Kong resolves that key in Redis
and injects `X-Subscription-Id`, `X-Subscription-Name`, `X-User-Id` and
`X-Product-Id` downstream -- the CLI must never send those itself.

Extraction parameters (`ext_*`) are forwarded verbatim to the vertical worker, so
`--ext-param KEY=VALUE` exists as an escape hatch for parameters newer than this
CLI. That matters more here than elsewhere precisely because there are no docs to
track.
"""

from __future__ import annotations

from unstract_cli.core.model import (
    ApiGroup,
    BodyKind,
    Endpoint,
    MutuallyExclusive,
    Param,
    ParamLocation,
    ParamType,
    PollSpec,
)

_SRC = "unstract-verticals/src/api_v1/api.py"
_POSTMAN = "verticals-portal/portal/postman-collection"

#: LLMWhisperer conversion passthrough. These mirror the `whisper extract` flags
#: but reach LLMWhisperer via the vertical worker, hence the `conv_` prefix.
_CONV_PARAMS: tuple[Param, ...] = (
    Param("conv_mode", default="high_quality",
          choices=["native_text", "low_cost", "high_quality", "form", "table"],
          help="LLMWhisperer processing mode"),
    Param("conv_output_mode", default="layout_preserving",
          choices=["layout_preserving", "text"], help="LLMWhisperer output mode"),
    Param("conv_lang", default="eng", help="Language hint for OCR"),
    Param("conv_tag", default="default", help="Audit tag for usage reporting"),
    Param("conv_filename", help="Audit file name for usage reporting"),
    Param("conv_page_seperator", default="<<<", flag="--conv-page-separator",
          help="Page separator string"),
    Param("conv_pages_to_extract", help="Pages to extract, e.g. '1-5,7,21-'"),
    Param("conv_median_filter_size", type=ParamType.INT,
          applies_when="conv_mode=low_cost", help="Median filter size for denoising"),
    Param("conv_gaussian_blur_radius", type=ParamType.FLOAT,
          applies_when="conv_mode=low_cost", help="Gaussian blur radius for denoising"),
    Param("conv_mark_vertical_lines", type=ParamType.BOOL, default=False,
          help="Reproduce vertical lines"),
    Param("conv_mark_horizontal_lines", type=ParamType.BOOL, default=False,
          help="Reproduce horizontal lines"),
    Param("conv_line_splitter_strategy", default="left-priority",
          help="Line splitter strategy"),
    Param("conv_line_splitter_tolerance", type=ParamType.FLOAT, default=0.4,
          help="Line splitter tolerance"),
    Param("conv_horizontal_stretch_factor", type=ParamType.FLOAT, default=1.0,
          help="Horizontal stretch factor"),
)

#: Vertical-worker extraction parameters.
_EXT_PARAMS: tuple[Param, ...] = (
    Param("ext_section_name", help="Section of the document containing the target table"),
    Param("ext_compress_double_space", type=ParamType.BOOL,
          help="Collapse double-spaced lines, common in bank/credit-card statements"),
    Param("ext_headers", help="Comma-separated headers to force the extraction to use"),
    Param("ext_start_page", type=ParamType.INT, help="Start searching from this page"),
    Param("ext_end_page", type=ParamType.INT, help="Stop searching at this page"),
    Param("ext_page_filter_strategy", help="Page filtering strategy"),
    Param("ext_use_bank_schema", type=ParamType.BOOL,
          applies_when="sub_vertical=bank_statement",
          help="Apply the bank statement schema"),
    Param("ext_pattern", choices=["generic_table", "indent_as_groups"],
          applies_when="sub_vertical=extract_table", help="Table extraction pattern"),
    Param("ext_table_no", type=ParamType.INT, applies_when="sub_vertical=extract_table",
          help="Which discovered table to extract, 1-based"),
    Param("ext_cache_result", type=ParamType.BOOL,
          help="Cache the result for 24 hours, for reuse via --use-cached-file-hash"),
    Param("ext_cache_text", type=ParamType.BOOL,
          help="Cache extracted text for 24 hours"),
    Param("ext_param", multiple=True, freeform_prefix="ext_",
          help=(
              "Escape hatch for extraction parameters newer than this CLI, as "
              "KEY=VALUE (sent as ext_KEY=VALUE); repeatable"
          )),
)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="extract",
        group="apihub",
        method="POST",
        path="/api/v1/extract",
        api=ApiGroup.APIHUB,
        summary="Submit a document for vertical extraction.",
        description=(
            "Processing runs in stages: QUEUED_FOR_WHISPER -> "
            "QUEUED_FOR_EXTRACTION -> COMPLETED. Use --wait to follow it through.\n\n"
            "Pass --use-cached-file-hash instead of --file to re-run extraction "
            "over a document already processed by table discovery, which skips "
            "re-conversion."
        ),
        params=(
            Param("vertical", required=True, default="table",
                  help="Vertical, e.g. 'table'"),
            Param("sub_vertical", required=True,
                  choices=["bank_statement", "discover_tables", "extract_table"],
                  help="Sub-vertical determining which worker handles the document"),
            Param("file", type=ParamType.FILE, location=ParamLocation.BODY,
                  client_side=True, help="Path to the document to process"),
            Param("use_cached_file_hash",
                  help="Reuse a previously processed document by its file hash"),
            *_CONV_PARAMS,
            *_EXT_PARAMS,
        ),
        body=BodyKind.BINARY_FILE,
        constraints=(MutuallyExclusive(("file", "use_cached_file_hash")),),
        poll=PollSpec(
            status_endpoint="apihub.status",
            status_field="status",
            terminal_success=("COMPLETED",),
            terminal_failure=("ERROR", "FAILED"),
            handle_field="file_hash",
            handle_param="file_hash",
            retrieve_endpoint="apihub.retrieve",
        ),
        doc_source=f"{_SRC} + {_POSTMAN}",
        examples=(
            "unstract apihub extract --vertical table --sub-vertical bank_statement "
            "--file statement.pdf --wait",
            "unstract apihub extract --vertical table --sub-vertical extract_table "
            "--use-cached-file-hash <hash> --ext-table-no 1",
        ),
    ),
    Endpoint(
        name="status",
        group="apihub",
        method="GET",
        path="/api/v1/status",
        api=ApiGroup.APIHUB,
        summary="Check the processing status of a submitted document.",
        description="Statuses: QUEUED_FOR_WHISPER, QUEUED_FOR_EXTRACTION, COMPLETED.",
        params=(
            Param("file_hash", required=True, help="Hash returned by `apihub extract`"),
        ),
        doc_source=f"{_SRC} + {_POSTMAN}",
        examples=("unstract apihub status --file-hash <hash>",),
    ),
    Endpoint(
        name="retrieve",
        group="apihub",
        method="GET",
        path="/api/v1/retrieve",
        api=ApiGroup.APIHUB,
        summary="Retrieve extraction results for a processed document.",
        params=(
            Param("file_hash", required=True, help="Hash returned by `apihub extract`"),
            Param("output_mode", default="full", choices=["raw", "full"],
                  help="raw returns the extraction payload alone"),
            Param("sub_vertical", help="Sub-vertical used for the extraction"),
            Param("save", client_side=True, help="Write the result to this path"),
        ),
        doc_source=f"{_SRC} + {_POSTMAN}",
        examples=("unstract apihub retrieve --file-hash <hash> --save tables.json",),
    ),
    Endpoint(
        name="upload",
        group="apihub",
        subgroup="doc-splitter",
        method="POST",
        path="/doc-splitter/documents/upload",
        api=ApiGroup.APIHUB,
        summary="Upload a document for splitting.",
        params=(
            Param("file", type=ParamType.FILE, location=ParamLocation.FORM,
                  required=True, help="Path to the document to split"),
        ),
        body=BodyKind.MULTIPART,
        doc_source=f"{_POSTMAN}/Verticals-DocSplitter.postman_collection.json",
        examples=("unstract apihub doc-splitter upload --file bundle.pdf",),
    ),
    Endpoint(
        name="status",
        group="apihub",
        subgroup="doc-splitter",
        method="GET",
        path="/doc-splitter/jobs/status",
        api=ApiGroup.APIHUB,
        summary="Check the status of a document splitting job.",
        params=(Param("job_id", required=True, help="Job identifier from `upload`"),),
        doc_source=f"{_POSTMAN}/Verticals-DocSplitter.postman_collection.json",
        examples=("unstract apihub doc-splitter status --job-id <id>",),
    ),
    Endpoint(
        name="download",
        group="apihub",
        subgroup="doc-splitter",
        method="GET",
        path="/doc-splitter/jobs/download",
        api=ApiGroup.APIHUB,
        summary="Download the output of a completed splitting job.",
        params=(
            Param("job_id", required=True, help="Job identifier from `upload`"),
            Param("save", client_side=True, help="Write the downloaded output to this path"),
        ),
        doc_source=f"{_POSTMAN}/Verticals-DocSplitter.postman_collection.json",
        examples=("unstract apihub doc-splitter download --job-id <id> --save split.zip",),
    ),
)
