"""LLMWhisperer v2 endpoint records (SPEC.md §6.1).

Authored from `llmwhisperer-docs/docs/llm_whisperer/apis/`. Base URL is
region-specific (US / EU) and configurable for on-prem; auth is the
`unstract-key` header.
"""

from __future__ import annotations

from unstract_cli.core.model import (
    BodyKind,
    Endpoint,
    MutuallyExclusive,
    Param,
    ParamLocation,
    ParamType,
    PollSpec,
    Product,
)

_DOCS = "llmwhisperer-docs/docs/llm_whisperer/apis"

#: Parameters shared by the extraction endpoint. Kept as a module constant so the
#: `--wait` flow and the raw command describe exactly the same surface.
_EXTRACT_PARAMS: tuple[Param, ...] = (
    Param(
        "file",
        type=ParamType.FILE,
        location=ParamLocation.BODY,
        client_side=True,
        help="Path to the document to convert",
    ),
    Param(
        "url",
        location=ParamLocation.BODY,
        client_side=True,
        help="Publicly accessible URL to fetch the document from; sets url_in_post",
    ),
    Param(
        "mode",
        default="form",
        choices=["native_text", "low_cost", "high_quality", "form", "table"],
        help=(
            "Processing mode. native_text for digital PDFs, low_cost for clean "
            "scans, high_quality for handwriting/noisy scans, form for forms and "
            "checkboxes, table for dense tabular documents"
        ),
    ),
    Param(
        "output_mode",
        default="layout_preserving",
        choices=["layout_preserving", "text"],
        help="layout_preserving keeps document structure (best for LLMs); text is plain",
    ),
    # The API's own spelling is `page_seperator`; preserved verbatim on the wire.
    Param("page_seperator", default="<<<", flag="--page-separator",
          help="String used to separate pages"),
    Param("pages_to_extract", help="Pages to extract, e.g. '1-5,7,21-'"),
    Param("median_filter_size", type=ParamType.INT, applies_when="mode=low_cost",
          help="Median filter size for denoising"),
    Param("gaussian_blur_radius", type=ParamType.FLOAT, applies_when="mode=low_cost",
          help="Gaussian blur radius for denoising"),
    Param("line_splitter_tolerance", type=ParamType.FLOAT, default=0.4,
          help="Fraction of average character height before text moves to the next line"),
    Param("line_splitter_strategy", default="left-priority",
          help="Advanced line-splitting strategy"),
    Param("horizontal_stretch_factor", type=ParamType.FLOAT, default=1.0,
          help="Horizontal stretch; raise slightly when multi-column layouts merge"),
    Param("mark_vertical_lines", type=ParamType.BOOL, default=False,
          applies_when="mode is not native_text",
          help="Reproduce vertical lines in the output"),
    Param("mark_horizontal_lines", type=ParamType.BOOL, default=False,
          applies_when="mode is not native_text and mark_vertical_lines=true",
          help="Reproduce horizontal lines in the output"),
    Param("lang", default="eng", help="Language hint for OCR (currently auto-detected)"),
    Param("tag", default="default", help="Audit tag, cross-referenced in usage reports"),
    Param("file_name", help="Audit file name, cross-referenced in usage reports"),
    Param("use_webhook", help="Name of a registered webhook to call on completion"),
    Param("webhook_metadata", help="Metadata forwarded verbatim to the webhook"),
    Param("add_line_nos", type=ParamType.BOOL, default=False,
          help="Add line numbers and store line metadata for the highlights API"),
    Param("allow_rotated_text", type=ParamType.BOOL, default=True,
          applies_when="mode in form/high_quality/table",
          help="Keep rotated text such as watermarks; false filters it out"),
    Param("word_confidence_threshold", type=ParamType.FLOAT, default=0.3,
          applies_when="mode in form/high_quality/table",
          help="Drop words whose OCR confidence is below this value (0-1)"),
)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="usage",
        group="whisper",
        method="GET",
        path="/get-usage-info",
        product=Product.LLMWHISPERER,
        summary="Show usage metrics for your LLMWhisperer account.",
        doc_source=f"{_DOCS}/usage_api.md",
        examples=("unstract whisper usage",),
    ),
    Endpoint(
        name="extract",
        group="whisper",
        method="POST",
        path="/whisper",
        product=Product.LLMWHISPERER,
        summary="Convert a document to LLM-ready text.",
        description=(
            "Accepts PDFs, scanned documents, images, Office documents and "
            "spreadsheets. Returns a whisper_hash immediately; use --wait to poll "
            "and retrieve the text in one step."
        ),
        params=_EXTRACT_PARAMS,
        body=BodyKind.BINARY_FILE,
        constraints=(MutuallyExclusive(("file", "url")),),
        poll=PollSpec(
            status_endpoint="whisper.status",
            status_field="status",
            terminal_success=("processed",),
            terminal_failure=("error",),
            handle_field="whisper_hash",
            handle_param="whisper_hash",
            retrieve_endpoint="whisper.retrieve",
            one_shot=True,
        ),
        doc_source=f"{_DOCS}/whisper.md",
        raw_field="result_text",
        examples=(
            "unstract whisper extract --file invoice.pdf --mode form --wait",
            "unstract whisper extract --url https://example.com/doc.pdf --mode high_quality",
        ),
    ),
    Endpoint(
        name="status",
        group="whisper",
        method="GET",
        path="/whisper-status",
        product=Product.LLMWHISPERER,
        summary="Check the status of a conversion.",
        description="Statuses: accepted, processing, processed, error, retrieved.",
        params=(
            Param("whisper_hash", required=True, help="Hash returned by `whisper extract`"),
        ),
        doc_source=f"{_DOCS}/whisper_status.md",
        examples=("unstract whisper status --whisper-hash abc123",),
    ),
    Endpoint(
        name="retrieve",
        group="whisper",
        method="GET",
        path="/whisper-retrieve",
        product=Product.LLMWHISPERER,
        summary="Retrieve the extracted text of a completed conversion.",
        description=(
            "ONE-SHOT: text can be retrieved only once, for privacy reasons. A "
            "second call fails and the text cannot be recovered -- pass --save to "
            "persist it on the first read."
        ),
        params=(
            Param("whisper_hash", required=True, help="Hash returned by `whisper extract`"),
            Param("text_only", type=ParamType.BOOL, default=False,
                  help="Return only the text, omitting metadata"),
            Param("save", client_side=True,
                  help="Write the result to this path before exiting (recommended)"),
        ),
        doc_source=f"{_DOCS}/whisper_retrieve.md",
        raw_field="result_text",
        examples=(
            "unstract whisper retrieve --whisper-hash abc123 --save result.json",
            "unstract whisper retrieve --whisper-hash abc123 --text-only --output raw",
        ),
    ),
    Endpoint(
        name="detail",
        group="whisper",
        method="GET",
        path="/whisper-detail",
        product=Product.LLMWHISPERER,
        summary="Show processing details and metadata for a conversion.",
        params=(
            Param("whisper_hash", required=True, help="Hash returned by `whisper extract`"),
        ),
        doc_source=f"{_DOCS}/whisper_detail.md",
        # The docs *index* lists `/whisper-details` (plural), but the endpoint page
        # and the official client both use the singular. Verified against
        # llm-whisperer-python-client client_v2.py:349.
        doc_conflict=(
            "Docs index says /whisper-details (plural); endpoint page and official "
            "Python client use /whisper-detail (singular). Singular is correct -- "
            "do not 'fix' this from the index page."
        ),
        examples=("unstract whisper detail --whisper-hash abc123",),
    ),
    Endpoint(
        name="highlights",
        group="whisper",
        method="GET",
        path="/highlights",
        product=Product.LLMWHISPERER,
        summary="Retrieve line metadata (bounding boxes) for highlighting.",
        description="Requires the conversion to have been run with --add-line-nos.",
        params=(
            Param("whisper_hash", required=True, help="Hash returned by `whisper extract`"),
            Param("lines", required=True, help="Lines to fetch, e.g. '1-5,7,21-'"),
        ),
        doc_source=f"{_DOCS}/highlighting_api.md",
        examples=("unstract whisper highlights --whisper-hash abc123 --lines 1-20",),
    ),
    Endpoint(
        name="usage-by-tag",
        group="whisper",
        method="GET",
        path="/usage",
        product=Product.LLMWHISPERER,
        summary="Show usage metrics filtered by audit tag.",
        description="Without dates, usage covers the last 30 days.",
        params=(
            Param("tag", required=True, help="Audit tag to report on"),
            Param("from_date", type=ParamType.DATE, help="Start date, YYYY-MM-DD"),
            Param("to_date", type=ParamType.DATE, help="End date, YYYY-MM-DD"),
        ),
        doc_source=f"{_DOCS}/usage_stat.md",
        examples=("unstract whisper usage-by-tag --tag invoices --from-date 2026-01-01",),
    ),
    Endpoint(
        name="create",
        group="whisper",
        subgroup="webhook",
        method="POST",
        path="/whisper-manage-callback",
        product=Product.LLMWHISPERER,
        summary="Register a webhook to be called when a conversion completes.",
        description=(
            "The URL is verified at registration: LLMWhisperer posts a test payload "
            "and refuses to register if it does not return 200."
        ),
        params=(
            Param("webhook_name", required=True, location=ParamLocation.BODY,
                  help="Name used to reference this webhook in `whisper extract --use-webhook`"),
            Param("url", required=True, location=ParamLocation.BODY,
                  help="URL to call when conversion completes"),
            Param("auth_token", location=ParamLocation.BODY, default="",
                  help="Bearer token for the webhook; empty if unauthenticated"),
        ),
        body=BodyKind.JSON,
        doc_source=f"{_DOCS}/webhook_manage.md",
        examples=(
            "unstract whisper webhook create --webhook-name prod --url https://example.com/hook",
        ),
    ),
    Endpoint(
        name="get",
        group="whisper",
        subgroup="webhook",
        method="GET",
        path="/whisper-manage-callback",
        product=Product.LLMWHISPERER,
        summary="Show a registered webhook's configuration.",
        params=(Param("webhook_name", required=True, help="Name of the webhook"),),
        doc_source=f"{_DOCS}/webhook_manage.md",
        examples=("unstract whisper webhook get --webhook-name prod",),
    ),
    Endpoint(
        name="update",
        group="whisper",
        subgroup="webhook",
        method="PUT",
        path="/whisper-manage-callback",
        product=Product.LLMWHISPERER,
        summary="Update a registered webhook.",
        params=(
            Param("webhook_name", required=True, location=ParamLocation.BODY,
                  help="Name of the existing webhook"),
            Param("url", required=True, location=ParamLocation.BODY, help="New URL"),
            Param("auth_token", location=ParamLocation.BODY, default="",
                  help="New bearer token; empty if unauthenticated"),
        ),
        body=BodyKind.JSON,
        doc_source=f"{_DOCS}/webhook_manage.md",
        examples=(
            "unstract whisper webhook update --webhook-name prod --url https://example.com/v2",
        ),
    ),
    Endpoint(
        name="delete",
        group="whisper",
        subgroup="webhook",
        method="DELETE",
        path="/whisper-manage-callback",
        product=Product.LLMWHISPERER,
        summary="Delete a registered webhook.",
        params=(Param("webhook_name", required=True, help="Name of the webhook"),),
        doc_source=f"{_DOCS}/webhook_manage.md",
        examples=("unstract whisper webhook delete --webhook-name prod",),
    ),
)
