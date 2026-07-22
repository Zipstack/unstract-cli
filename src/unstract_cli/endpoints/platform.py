"""Unstract Platform Management API v1 endpoints (SPEC.md §6.3).

Authored from `unstract-docs/docs/unstract_platform/api_documentation/versions/v1-*.mdx`.

Base path is `{host}/api/v1/unstract/{org_id}`; auth is a platform API key as a
Bearer token. Key permission levels gate methods: `read` allows GET, `read_write`
everything but DELETE, `full_access` everything.

Two upstream inconsistencies are reproduced here deliberately, because
"correcting" either produces a 404:

* Trailing slashes are not uniform. Several Prompt Studio paths and the group
  member-removal path genuinely have none; those records set
  ``no_trailing_slash=True`` so a test can assert intent rather than treating the
  omission as a typo.
* Profile creation uses ``profilemanager`` (one word) while profile CRUD uses
  ``profile-manager`` (hyphenated).

PATCH records are *derived* from their PUT counterparts via `derive_patch`
(P8): a parameter added to a PUT record cannot then be forgotten on the PATCH.
"""

from __future__ import annotations

from unstract_cli.core.model import (
    ApiGroup,
    AtLeastOneOf,
    BodyKind,
    Endpoint,
    MutuallyExclusive,
    Param,
    ParamLocation,
    ParamType,
    Permission,
    derive_patch,
    with_params,
)

_DOCS = "unstract-docs/docs/unstract_platform/api_documentation/versions"
_BASE = "/api/v1/unstract/{org_id}"

_ORG = Param(
    "org_id",
    location=ParamLocation.PATH,
    default_from="platform.org_id",
    help="Organization identifier",
)

#: Shared pagination flags (SPEC.md §6.3).
_PAGE = (
    Param("page", type=ParamType.INT, default=1, help="Page number"),
    Param("page_size", type=ParamType.INT, default=50,
          help="Results per page (max 1000)"),
)

_DELETE_NOTE = (
    "Requires a platform API key with full_access permission: DELETE is blocked "
    "for read and read_write keys at the middleware."
)

#: Friendly resource name -> URL path segment (P3). The segment is not guessable
#: from the friendly name -- `api-deployment` lives at `api/deployment` -- so this
#: is an enum rather than free text, and a wrong value fails locally with exit 2
#: instead of a confusing remote 404.
SHARE_RESOURCES = {
    "adapter": "adapter",
    "connector": "connector",
    "workflow": "workflow",
    "pipeline": "pipeline",
    "api-deployment": "api/deployment",
    "prompt-studio": "prompt-studio",
}


def _ep(
    name: str,
    method: str,
    path: str,
    summary: str,
    params: tuple[Param, ...] = (),
    *,
    subgroup: str | None = None,
    body: BodyKind = BodyKind.NONE,
    doc: str = "v1-prompt-studio.mdx",
    permission: Permission | None = None,
    description: str = "",
    examples: tuple[str, ...] = (),
    constraints: tuple = (),
    no_trailing_slash: bool = False,
    table_columns: tuple[str, ...] = (),
) -> Endpoint:
    """Construct a Platform endpoint, filling in the org-scoped base path."""
    return Endpoint(
        name=name,
        group="docstudio",
        # Every Platform API command lives under `docstudio platform ...`.
        subgroup=f"platform {subgroup}" if subgroup else "platform",
        method=method,
        path=f"{_BASE}{path}",
        api=ApiGroup.PLATFORM,
        summary=summary,
        params=(_ORG, *params),
        body=body,
        doc_source=f"{_DOCS}/{doc}",
        permission=permission,
        description=description,
        examples=examples,
        constraints=constraints,
        no_trailing_slash=no_trailing_slash,
        table_columns=table_columns,
    )


# --------------------------------------------------------------------------- #
# Prompt Studio
# --------------------------------------------------------------------------- #

_PS = "prompt-studio"
_TOOL_ID = Param("tool_id", type=ParamType.UUID, location=ParamLocation.PATH,
                 required=True, help="Prompt Studio project (tool) identifier")

_PS_FIELDS: tuple[Param, ...] = (
    Param("tool_name", location=ParamLocation.BODY, required=True,
          help="Project name, unique within the organization"),
    Param("description", location=ParamLocation.BODY, required=True,
          help="Project description"),
    Param("author", location=ParamLocation.BODY, required=True, help="Project author"),
    Param("icon", location=ParamLocation.BODY, help="Project icon"),
    Param("preamble", location=ParamLocation.BODY, help="Text prepended to every prompt"),
    Param("postamble", location=ParamLocation.BODY, help="Text appended to every prompt"),
    Param("summarize_context", type=ParamType.BOOL, location=ParamLocation.BODY,
          default=False, help="Summarize context before extraction"),
    Param("single_pass_extraction_mode", type=ParamType.BOOL, location=ParamLocation.BODY,
          default=False, help="Run all prompts in a single LLM call"),
    Param("enable_challenge", type=ParamType.BOOL, location=ParamLocation.BODY,
          default=False, help="Enable LLM challenge for extraction validation"),
    Param("enable_highlight", type=ParamType.BOOL, location=ParamLocation.BODY,
          default=False, help="Record line metadata for source highlighting"),
    Param("custom_data", type=ParamType.JSON, location=ParamLocation.BODY,
          help="JSON object addressable in prompts as {{custom_data.key}}"),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
          replace_semantics=True, help="User IDs to share with"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
)

_PROMPT_FIELDS: tuple[Param, ...] = (
    Param("prompt_key", location=ParamLocation.BODY, required=True,
          help="Output key for this prompt, unique within the project"),
    Param("enforce_type", location=ParamLocation.BODY, default="text",
          choices=["text", "number", "email", "date", "boolean", "json", "line-item", "table"],
          help="Expected output type"),
    Param("prompt", location=ParamLocation.BODY, help="Prompt text sent to the LLM"),
    Param("sequence_number", type=ParamType.INT, location=ParamLocation.BODY,
          help="Position within the project"),
    Param("prompt_type", location=ParamLocation.BODY, choices=["PROMPT", "NOTES"],
          help="PROMPT extracts a value; NOTES is an annotation"),
    Param("active", type=ParamType.BOOL, location=ParamLocation.BODY, default=True,
          help="Whether the prompt runs"),
)

_PROFILE_FIELDS: tuple[Param, ...] = (
    Param("profile_name", location=ParamLocation.BODY, required=True,
          help="Profile name, unique within the project"),
    Param("vector_store", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="Vector DB adapter id"),
    Param("embedding_model", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="Embedding adapter id"),
    Param("llm", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="LLM adapter id"),
    Param("x2text", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="Text extractor adapter id"),
    Param("chunk_size", type=ParamType.INT, location=ParamLocation.BODY, help="Chunk size"),
    Param("chunk_overlap", type=ParamType.INT, location=ParamLocation.BODY,
          help="Overlap between chunks"),
    Param("retrieval_strategy", location=ParamLocation.BODY, default="simple",
          choices=["simple", "subquestion", "fusion", "recursive", "router",
                   "keyword_table", "automerging"],
          help="Retrieval strategy for RAG"),
    Param("similarity_top_k", type=ParamType.INT, location=ParamLocation.BODY,
          help="Number of chunks to retrieve"),
)

_ps_update = _ep(
    "update", "PUT", "/prompt-studio/{tool_id}/", "Replace a Prompt Studio project.",
    (_TOOL_ID, *_PS_FIELDS), subgroup=_PS, body=BodyKind.JSON,
    permission=Permission.READ_WRITE,
)

_prompt_update = _ep(
    "update", "PUT", "/prompt-studio/prompt/{prompt_id}/", "Replace a prompt.",
    (Param("prompt_id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
           help="Prompt identifier"), *_PROMPT_FIELDS),
    subgroup=f"{_PS} prompt", body=BodyKind.JSON, permission=Permission.READ_WRITE,
)

_profile_update = _ep(
    "update", "PUT", "/prompt-studio/profile-manager/{profile_id}/",
    "Replace an LLM profile.",
    (Param("profile_id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
           help="Profile identifier"), *_PROFILE_FIELDS),
    subgroup=f"{_PS} profile", body=BodyKind.JSON, permission=Permission.READ_WRITE,
)

_PROMPT_STUDIO: tuple[Endpoint, ...] = (
    _ep("list", "GET", "/prompt-studio/", "List Prompt Studio projects.",
        subgroup=_PS, permission=Permission.READ,
        table_columns=("tool_id", "tool_name", "author", "created_by_email"),
        examples=("unstract platform prompt-studio list",)),
    _ep("create", "POST", "/prompt-studio/", "Create a Prompt Studio project.",
        _PS_FIELDS, subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE,
        examples=("unstract platform prompt-studio create --tool-name Invoices "
                  "--description 'Invoice extraction' --author me",)),
    _ep("get", "GET", "/prompt-studio/{tool_id}/", "Show one Prompt Studio project.",
        (_TOOL_ID,), subgroup=_PS, permission=Permission.READ),
    _ps_update,
    derive_patch(_ps_update, summary="Partially update a Prompt Studio project."),
    _ep("delete", "DELETE", "/prompt-studio/{tool_id}/", "Delete a Prompt Studio project.",
        (_TOOL_ID,), subgroup=_PS, permission=Permission.FULL_ACCESS,
        description=f"{_DELETE_NOTE} Returns 409 if the tool is exported and in use."),
    _ep("export-project", "GET", "/prompt-studio/project-transfer/{tool_id}",
        "Export a project as a JSON file.",
        (_TOOL_ID, Param("save", client_side=True, help="Write the export to this path")),
        subgroup=_PS, permission=Permission.READ, no_trailing_slash=True,
        examples=("unstract platform prompt-studio export-project --tool-id <id> --save proj.json",)),
    _ep("import-project", "POST", "/prompt-studio/project-transfer/",
        "Import a project from an exported JSON file.",
        (Param("file", type=ParamType.FILE, location=ParamLocation.FORM, required=True,
               help="Previously exported project JSON"),),
        subgroup=_PS, body=BodyKind.MULTIPART, permission=Permission.READ_WRITE),
    _ep("sync-prompts", "POST", "/prompt-studio/{tool_id}/sync-prompts/",
        "Sync prompts from an export into an existing project.",
        (_TOOL_ID,
         Param("data", type=ParamType.JSON, location=ParamLocation.BODY, required=True,
               help="Export JSON containing a `prompts` key"),
         Param("create_copy", type=ParamType.BOOL, location=ParamLocation.BODY,
               default=False, help="Back the project up before syncing")),
        subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE),
    _ep("export-tool", "POST", "/prompt-studio/export/{tool_id}",
        "Export a project to the tool registry for deployment.",
        (_TOOL_ID,
         Param("is_shared_with_org", type=ParamType.BOOL, location=ParamLocation.BODY,
               help="Share the exported tool with the organization"),
         Param("user_id", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
               help="User IDs to share the exported tool with"),
         Param("force_export", type=ParamType.BOOL, location=ParamLocation.BODY,
               default=False, help="Export even if validation warns")),
        subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE,
        no_trailing_slash=True),
    _ep("export-info", "GET", "/prompt-studio/export/{tool_id}",
        "Show export status for a project.", (_TOOL_ID,), subgroup=_PS,
        permission=Permission.READ, no_trailing_slash=True,
        description="Returns 204 No Content if the project has never been exported."),
    _ep("upload", "POST", "/prompt-studio/file/{tool_id}",
        "Upload documents to a project.",
        (_TOOL_ID, Param("file", type=ParamType.FILE, location=ParamLocation.FORM,
                         required=True, multiple=True, help="File to upload; repeatable")),
        subgroup=f"{_PS} file", body=BodyKind.MULTIPART, permission=Permission.READ_WRITE,
        no_trailing_slash=True),
    _ep("get", "GET", "/prompt-studio/file/{tool_id}", "Fetch a document's contents.",
        (_TOOL_ID,
         Param("document_id", type=ParamType.UUID, required=True, help="Document identifier"),
         Param("view_type", choices=["ORIGINAL", "EXTRACT", "SUMMARIZE"],
               help="Which rendition to fetch")),
        subgroup=f"{_PS} file", permission=Permission.READ, no_trailing_slash=True),
    _ep("delete", "DELETE", "/prompt-studio/file/{tool_id}",
        "Delete a document from a project.",
        (_TOOL_ID, Param("document_id", type=ParamType.UUID, location=ParamLocation.BODY,
                         required=True, help="Document identifier")),
        subgroup=f"{_PS} file", body=BodyKind.JSON, permission=Permission.FULL_ACCESS,
        description=_DELETE_NOTE, no_trailing_slash=True),
    _ep("create", "POST", "/prompt-studio/prompt-studio-prompt/{tool_id}/",
        "Create a prompt in a project.", (_TOOL_ID, *_PROMPT_FIELDS),
        subgroup=f"{_PS} prompt", body=BodyKind.JSON, permission=Permission.READ_WRITE),
    _ep("get", "GET", "/prompt-studio/prompt/{prompt_id}/", "Show one prompt.",
        (Param("prompt_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Prompt identifier"),),
        subgroup=f"{_PS} prompt", permission=Permission.READ),
    _prompt_update,
    derive_patch(_prompt_update, summary="Partially update a prompt."),
    _ep("delete", "DELETE", "/prompt-studio/prompt/{prompt_id}/", "Delete a prompt.",
        (Param("prompt_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Prompt identifier"),),
        subgroup=f"{_PS} prompt", permission=Permission.FULL_ACCESS, description=_DELETE_NOTE),
    _ep("reorder", "POST", "/prompt-studio/prompt/reorder/", "Reorder a prompt.",
        (Param("start_sequence_number", type=ParamType.INT, location=ParamLocation.BODY,
               required=True, help="Current position"),
         Param("end_sequence_number", type=ParamType.INT, location=ParamLocation.BODY,
               required=True, help="Target position"),
         Param("prompt_id", type=ParamType.UUID, location=ParamLocation.BODY,
               required=True, help="Prompt to move")),
        subgroup=f"{_PS} prompt", body=BodyKind.JSON, permission=Permission.READ_WRITE),
    _ep("list", "GET", "/prompt-studio/prompt-studio-profile/{tool_id}/",
        "List LLM profiles for a project.", (_TOOL_ID,), subgroup=f"{_PS} profile",
        permission=Permission.READ),
    _ep("set-default", "PATCH", "/prompt-studio/prompt-studio-profile/{tool_id}/",
        "Set the project's default LLM profile.",
        (_TOOL_ID, Param("default_profile", type=ParamType.UUID, location=ParamLocation.BODY,
                         required=True, help="Profile to make default")),
        subgroup=f"{_PS} profile", body=BodyKind.JSON, permission=Permission.READ_WRITE),
    # Note the path: `profilemanager` (one word) on create, `profile-manager`
    # (hyphenated) on CRUD. This asymmetry is upstream, and is load-bearing.
    _ep("create", "POST", "/prompt-studio/profilemanager/{tool_id}",
        "Create an LLM profile (maximum 4 per project).",
        (_TOOL_ID, *_PROFILE_FIELDS), subgroup=f"{_PS} profile", body=BodyKind.JSON,
        permission=Permission.READ_WRITE, no_trailing_slash=True),
    _ep("get", "GET", "/prompt-studio/profile-manager/{profile_id}/", "Show one LLM profile.",
        (Param("profile_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Profile identifier"),),
        subgroup=f"{_PS} profile", permission=Permission.READ),
    _profile_update,
    derive_patch(_profile_update, summary="Partially update an LLM profile."),
    _ep("delete", "DELETE", "/prompt-studio/profile-manager/{profile_id}/",
        "Delete an LLM profile.",
        (Param("profile_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Profile identifier"),),
        subgroup=f"{_PS} profile", permission=Permission.FULL_ACCESS, description=_DELETE_NOTE),
    _ep("index-document", "POST", "/prompt-studio/index-document/{tool_id}",
        "Index a document for retrieval.",
        (_TOOL_ID, Param("document_id", type=ParamType.UUID, location=ParamLocation.BODY,
                         required=True, help="Document identifier")),
        subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE,
        no_trailing_slash=True),
    _ep("fetch-response", "POST", "/prompt-studio/fetch_response/{tool_id}",
        "Run one prompt against a document.",
        (_TOOL_ID,
         Param("document_id", type=ParamType.UUID, location=ParamLocation.BODY,
               required=True, help="Document to run against"),
         Param("id", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
               help="Prompt identifier"),
         Param("run_id", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Execution tracking identifier"),
         Param("profile_manager", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Override the project's default LLM profile")),
        subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE,
        no_trailing_slash=True),
    _ep("single-pass", "POST", "/prompt-studio/single-pass-extraction/{tool_id}",
        "Run all active prompts in a single pass.",
        (_TOOL_ID,
         Param("document_id", type=ParamType.UUID, location=ParamLocation.BODY,
               required=True, help="Document to run against"),
         Param("run_id", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Execution tracking identifier")),
        subgroup=_PS, body=BodyKind.JSON, permission=Permission.READ_WRITE,
        no_trailing_slash=True,
        description="Requires single_pass_extraction_mode enabled on the project."),
    _ep("users", "GET", "/prompt-studio/users/{tool_id}", "List users a project is shared with.",
        (_TOOL_ID,), subgroup=_PS, permission=Permission.READ, no_trailing_slash=True),
    _ep("check-deployment-usage", "GET", "/prompt-studio/{tool_id}/check_deployment_usage/",
        "Check whether an exported tool is used by deployments.", (_TOOL_ID,),
        subgroup=_PS, permission=Permission.READ),
    _ep("select-choices", "GET", "/prompt-studio/select_choices/",
        "List dropdown values for Prompt Studio fields.", subgroup=_PS,
        permission=Permission.READ),
    _ep("adapter-choices", "GET", "/prompt-studio/adapter-choices/",
        "List adapters available for LLM profiles.", subgroup=_PS,
        permission=Permission.READ),
    _ep("retrieval-strategies", "GET", "/prompt-studio/{tool_id}/get_retrieval_strategies/",
        "List retrieval strategies available to a project.", (_TOOL_ID,),
        subgroup=_PS, permission=Permission.READ),
)


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #

_WF_ID = Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Workflow identifier")

_WF_FIELDS: tuple[Param, ...] = (
    Param("workflow_name", location=ParamLocation.BODY, required=True,
          help="Workflow name, unique per organization (max 128 chars)"),
    Param("description", location=ParamLocation.BODY, help="Description (max 490 chars)"),
    Param("deployment_type", location=ParamLocation.BODY, default="DEFAULT",
          choices=["DEFAULT", "ETL", "TASK", "API", "APP"], help="Deployment type"),
    Param("source_settings", type=ParamType.JSON, location=ParamLocation.BODY,
          help="Source connector configuration"),
    Param("destination_settings", type=ParamType.JSON, location=ParamLocation.BODY,
          help="Destination connector configuration"),
    Param("max_file_execution_count", type=ParamType.INT, location=ParamLocation.BODY,
          help="Maximum executions per file (minimum 1)"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
          replace_semantics=True, help="User IDs to share with"),
)

_wf_update = _ep("update", "PUT", "/workflow/{id}/", "Replace a workflow.",
                 (_WF_ID, *_WF_FIELDS), subgroup="workflow", body=BodyKind.JSON,
                 doc="v1-workflows.mdx", permission=Permission.READ_WRITE)

_WORKFLOWS: tuple[Endpoint, ...] = (
    _ep("list", "GET", "/workflow/", "List workflows.",
        (Param("project", help="Filter by project id"),
         Param("workflow_owner", help="Filter by owner user id"),
         Param("is_active", type=ParamType.BOOL, help="Filter by active state"),
         Param("order_by", choices=["asc", "desc"], help="Sort by modified_at"),
         *_PAGE),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ,
        table_columns=("id", "workflow_name", "deployment_type", "is_active"),
        examples=("unstract platform workflow list",)),
    _ep("create", "POST", "/workflow/", "Create a workflow.", _WF_FIELDS,
        subgroup="workflow", body=BodyKind.JSON, doc="v1-workflows.mdx",
        permission=Permission.READ_WRITE),
    _ep("get", "GET", "/workflow/{id}/", "Show one workflow.", (_WF_ID,),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ),
    _wf_update,
    derive_patch(_wf_update, summary="Partially update a workflow."),
    _ep("delete", "DELETE", "/workflow/{id}/", "Delete a workflow.", (_WF_ID,),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.FULL_ACCESS,
        description=f"{_DELETE_NOTE} Only the workflow owner may delete."),
    _ep("execute", "POST", "/workflow/execute/", "Execute a workflow.",
        (Param("workflow_id", type=ParamType.UUID, location=ParamLocation.BODY,
               required=True, help="Workflow to execute"),
         Param("execution_action", location=ParamLocation.BODY,
               choices=["START", "NEXT", "STOP", "CONTINUE"], help="Execution action"),
         Param("execution_id", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Required for NEXT, STOP and CONTINUE"),
         Param("log_guid", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Correlates log entries"),
         # The API field is `files`; the flag reads better singular since it is
         # repeated once per file.
         Param("files", type=ParamType.FILE, location=ParamLocation.FORM,
               multiple=True, flag="--file",
               help="File to process, for API-mode workflows; repeatable")),
        subgroup="workflow", body=BodyKind.JSON, doc="v1-workflows.mdx",
        permission=Permission.READ_WRITE),
    _ep("toggle-active", "PUT", "/workflow/active/{id}/", "Toggle a workflow's active state.",
        (_WF_ID,), subgroup="workflow", doc="v1-workflows.mdx",
        permission=Permission.READ_WRITE),
    _ep("can-update", "GET", "/workflow/{id}/can-update/",
        "Check whether the current user may update a workflow.", (_WF_ID,),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("clear-file-marker", "GET", "/workflow/{id}/clear-file-marker/",
        "Clear file processing markers so files can be reprocessed.", (_WF_ID,),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ_WRITE,
        description=(
            "NOTE: this is a mutating GET request -- that is how the API defines it. "
            "It changes server state despite the method."
        )),
    _ep("schema", "GET", "/workflow/schema/", "Show the connector configuration schema.",
        (Param("type", default="src", choices=["src", "dest"], help="Endpoint side"),
         Param("entity", default="file", choices=["file", "api", "db"], help="Entity type")),
        subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("users", "GET", "/workflow/{id}/users/", "List users a workflow is shared with.",
        (_WF_ID,), subgroup="workflow", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("list", "GET", "/workflow/{id}/execution/", "List executions of a workflow.",
        (_WF_ID, *_PAGE), subgroup="workflow execution", doc="v1-workflows.mdx",
        permission=Permission.READ,
        table_columns=("id", "status", "total_files", "execution_time")),
    _ep("get", "GET", "/workflow/execution/{id}/", "Show one execution.",
        (Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Execution identifier"),),
        subgroup="workflow execution", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("logs", "GET", "/workflow/execution/{id}/logs/", "Show logs for an execution.",
        (Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Execution identifier"),
         Param("file_execution_id", help="Filter by file execution; pass 'null' for non-file logs"),
         Param("log_level", default="INFO", choices=["DEBUG", "INFO", "WARN", "ERROR"],
               help="Minimum log level"),
         Param("ordering", help="Ordering field, e.g. event_time"),
         *_PAGE),
        subgroup="workflow execution", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("list", "GET", "/workflow/{workflow_id}/file-histories/", "List file histories.",
        (Param("workflow_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Workflow identifier"),
         Param("status", help="Comma-separated statuses to filter by"),
         Param("execution_count_min", type=ParamType.INT, help="Minimum execution count"),
         Param("execution_count_max", type=ParamType.INT, help="Maximum execution count"),
         Param("file_path", help="File path prefix"),
         *_PAGE),
        subgroup="workflow file-history", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("get", "GET", "/workflow/{workflow_id}/file-histories/{id}/",
        "Show one file history entry.",
        (Param("workflow_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Workflow identifier"),
         Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="File history identifier")),
        subgroup="workflow file-history", doc="v1-workflows.mdx", permission=Permission.READ),
    _ep("delete", "DELETE", "/workflow/{workflow_id}/file-histories/{id}/",
        "Delete one file history entry.",
        (Param("workflow_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Workflow identifier"),
         Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="File history identifier")),
        subgroup="workflow file-history", doc="v1-workflows.mdx",
        permission=Permission.FULL_ACCESS, description=_DELETE_NOTE),
    _ep("clear", "POST", "/workflow/{workflow_id}/file-histories/clear/",
        "Bulk delete file histories matching filters.",
        (Param("workflow_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Workflow identifier"),
         Param("ids", type=ParamType.UUID, location=ParamLocation.BODY, multiple=True,
               help="Specific entries to delete (max 100)"),
         Param("status", location=ParamLocation.BODY, multiple=True,
               help="Statuses to delete"),
         Param("execution_count_min", type=ParamType.INT, location=ParamLocation.BODY,
               help="Minimum execution count"),
         Param("execution_count_max", type=ParamType.INT, location=ParamLocation.BODY,
               help="Maximum execution count"),
         Param("file_path", location=ParamLocation.BODY, help="File path prefix")),
        subgroup="workflow file-history", body=BodyKind.JSON, doc="v1-workflows.mdx",
        permission=Permission.FULL_ACCESS,
        constraints=(AtLeastOneOf(("ids", "status", "execution_count_min",
                                   "execution_count_max", "file_path")),),
        description=(
            "At least one filter is required: an unfiltered clear would delete every "
            "file history for the workflow."
        )),
)


# --------------------------------------------------------------------------- #
# API Deployments (management)
# --------------------------------------------------------------------------- #

_DEP_ID = Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
                help="Deployment identifier")

_DEP_FIELDS: tuple[Param, ...] = (
    Param("display_name", location=ParamLocation.BODY, help="Display name (max 30 chars)"),
    Param("description", location=ParamLocation.BODY, help="Description (max 255 chars)"),
    Param("workflow", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="Workflow to deploy; must have source and destination configured"),
    Param("api_name", location=ParamLocation.BODY,
          help="URL name, matching ^[a-zA-Z0-9_-]+$ (max 30 chars, unique per org)"),
    Param("is_active", type=ParamType.BOOL, location=ParamLocation.BODY, default=True,
          help="Whether the deployment accepts requests"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
          replace_semantics=True, help="User IDs to share with"),
)

_dep_update = _ep("update", "PUT", "/api/deployment/{id}/", "Replace an API deployment.",
                  (_DEP_ID, *_DEP_FIELDS), subgroup="api-deployment", body=BodyKind.JSON,
                  doc="v1-api-deployments.mdx", permission=Permission.READ_WRITE)

_API_DEPLOYMENTS: tuple[Endpoint, ...] = (
    _ep("list", "GET", "/api/deployment/", "List API deployments.",
        (Param("workflow", type=ParamType.UUID, help="Filter by workflow"),
         Param("search", help="Case-insensitive search on display name"), *_PAGE),
        subgroup="api-deployment", doc="v1-api-deployments.mdx", permission=Permission.READ,
        table_columns=("id", "api_name", "display_name", "is_active", "run_count")),
    _ep("create", "POST", "/api/deployment/", "Create an API deployment.", _DEP_FIELDS,
        subgroup="api-deployment", body=BodyKind.JSON, doc="v1-api-deployments.mdx",
        permission=Permission.READ_WRITE,
        description=(
            "Returns an api_key in the response -- store it, it is how the deployment "
            "is called. Only one active deployment is allowed per workflow."
        )),
    _ep("get", "GET", "/api/deployment/{id}/", "Show one API deployment.", (_DEP_ID,),
        subgroup="api-deployment", doc="v1-api-deployments.mdx", permission=Permission.READ),
    _dep_update,
    derive_patch(_dep_update, summary="Partially update an API deployment."),
    _ep("delete", "DELETE", "/api/deployment/{id}/", "Delete an API deployment.", (_DEP_ID,),
        subgroup="api-deployment", doc="v1-api-deployments.mdx",
        permission=Permission.FULL_ACCESS, description=f"{_DELETE_NOTE} Owner only."),
    _ep("users", "GET", "/api/deployment/{id}/users/",
        "List users a deployment is shared with.", (_DEP_ID,), subgroup="api-deployment",
        doc="v1-api-deployments.mdx", permission=Permission.READ),
    _ep("by-prompt-studio-tool", "GET", "/api/deployment/by-prompt-studio-tool/",
        "Find deployments backed by a Prompt Studio tool.",
        (Param("tool_id", type=ParamType.UUID, required=True, help="Prompt Studio tool id"),),
        subgroup="api-deployment", doc="v1-api-deployments.mdx", permission=Permission.READ),
    _ep("postman-collection", "GET", "/api/postman_collection/{id}/",
        "Download a Postman collection for a deployment.",
        (_DEP_ID, Param("save", client_side=True, help="Write the collection to this path")),
        subgroup="api-deployment", doc="v1-api-deployments.mdx", permission=Permission.READ,
        description="Returns 409 if the deployment has no active API key."),
    _ep("list", "GET", "/api/keys/api/{api_id}/", "List API keys for a deployment.",
        (Param("api_id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Deployment identifier"),),
        subgroup="api-deployment key", doc="v1-api-deployments.mdx", permission=Permission.READ),
    _ep("create", "POST", "/api/keys/api/{api_id}/", "Create an API key for a deployment.",
        (Param("api_id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Deployment identifier"),
         Param("api", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Deployment to attach the key to"),
         Param("pipeline", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Pipeline to attach the key to"),
         Param("description", location=ParamLocation.BODY, help="Description (max 255 chars)"),
         Param("is_active", type=ParamType.BOOL, location=ParamLocation.BODY, default=True,
               help="Whether the key is usable")),
        subgroup="api-deployment key", body=BodyKind.JSON, doc="v1-api-deployments.mdx",
        permission=Permission.READ_WRITE,
        constraints=(MutuallyExclusive(("api", "pipeline")),)),
    _ep("get", "GET", "/api/keys/{id}/", "Show one API key.",
        (Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Key identifier"),),
        subgroup="api-deployment key", doc="v1-api-deployments.mdx", permission=Permission.READ),
    _ep("update", "PUT", "/api/keys/{id}/", "Update an API key.",
        (Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Key identifier"),
         Param("is_active", type=ParamType.BOOL, location=ParamLocation.BODY,
               help="Whether the key is usable"),
         Param("description", location=ParamLocation.BODY, help="Description (max 255 chars)")),
        subgroup="api-deployment key", body=BodyKind.JSON, doc="v1-api-deployments.mdx",
        permission=Permission.READ_WRITE),
    _ep("delete", "DELETE", "/api/keys/{id}/", "Delete an API key.",
        (Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Key identifier"),),
        subgroup="api-deployment key", doc="v1-api-deployments.mdx",
        permission=Permission.FULL_ACCESS, description=_DELETE_NOTE),
)


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #

_PIPE_ID = Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
                 help="Pipeline identifier")

_PIPE_FIELDS: tuple[Param, ...] = (
    Param("pipeline_name", location=ParamLocation.BODY, required=True,
          help="Pipeline name, unique per organization (max 32 chars)"),
    Param("workflow", type=ParamType.UUID, location=ParamLocation.BODY, required=True,
          help="Workflow to run"),
    Param("pipeline_type", location=ParamLocation.BODY, default="DEFAULT",
          choices=["ETL", "TASK", "DEFAULT", "APP"], help="Pipeline type"),
    Param("cron_string", location=ParamLocation.BODY,
          help="UNIX cron schedule; the platform enforces a minimum interval"),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
          replace_semantics=True, help="User IDs to share with"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
)

_pipe_update = _ep("update", "PUT", "/pipeline/{id}/", "Replace a pipeline.",
                   (_PIPE_ID, *_PIPE_FIELDS), subgroup="pipeline", body=BodyKind.JSON,
                   doc="v1-etl-pipelines.mdx", permission=Permission.READ_WRITE)

#: PATCH additionally accepts `active`, which PUT does not.
_pipe_patch = with_params(
    derive_patch(_pipe_update, summary="Partially update a pipeline."),
    Param("active", type=ParamType.BOOL, location=ParamLocation.BODY,
          help="Activate or deactivate the pipeline"),
)

_PIPELINES: tuple[Endpoint, ...] = (
    _ep("list", "GET", "/pipeline/", "List pipelines.",
        (Param("type", choices=["ETL", "TASK", "DEFAULT", "APP"], help="Filter by type"),
         Param("workflow", type=ParamType.UUID, help="Filter by workflow"),
         Param("search", help="Search on pipeline name"),
         Param("ordering",
               choices=["created_at", "last_run_time", "pipeline_name", "run_count"],
               help="Sort field; prefix with '-' for descending"),
         *_PAGE),
        subgroup="pipeline", doc="v1-etl-pipelines.mdx", permission=Permission.READ,
        table_columns=("id", "pipeline_name", "pipeline_type", "active", "last_run_status")),
    _ep("create", "POST", "/pipeline/", "Create a pipeline.", _PIPE_FIELDS,
        subgroup="pipeline", body=BodyKind.JSON, doc="v1-etl-pipelines.mdx",
        permission=Permission.READ_WRITE),
    _ep("get", "GET", "/pipeline/{id}/", "Show one pipeline.", (_PIPE_ID,),
        subgroup="pipeline", doc="v1-etl-pipelines.mdx", permission=Permission.READ),
    _pipe_update,
    _pipe_patch,
    _ep("delete", "DELETE", "/pipeline/{id}/", "Delete a pipeline and its scheduler job.",
        (_PIPE_ID,), subgroup="pipeline", doc="v1-etl-pipelines.mdx",
        permission=Permission.FULL_ACCESS, description=_DELETE_NOTE),
    _ep("execute", "POST", "/pipeline/execute/", "Execute a pipeline.",
        (Param("pipeline_id", type=ParamType.UUID, location=ParamLocation.BODY,
               required=True, help="Pipeline to execute"),
         Param("execution_id", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Execution identifier; generated if omitted")),
        subgroup="pipeline", body=BodyKind.JSON, doc="v1-etl-pipelines.mdx",
        permission=Permission.READ_WRITE),
    _ep("executions", "GET", "/pipeline/{id}/executions/", "List executions of a pipeline.",
        (_PIPE_ID,
         Param("start_date", help="ISO 8601 start of range"),
         Param("end_date", help="ISO 8601 end of range"), *_PAGE),
        subgroup="pipeline", doc="v1-etl-pipelines.mdx", permission=Permission.READ),
    _ep("users", "GET", "/pipeline/{id}/users/", "List users a pipeline is shared with.",
        (_PIPE_ID,), subgroup="pipeline", doc="v1-etl-pipelines.mdx",
        permission=Permission.READ),
    # Distinct from the deployment collection path; both exist, and they differ.
    _ep("postman-collection", "GET", "/pipeline/api/postman_collection/{id}/",
        "Download a Postman collection for a pipeline.",
        (_PIPE_ID, Param("save", client_side=True, help="Write the collection to this path")),
        subgroup="pipeline", doc="v1-etl-pipelines.mdx", permission=Permission.READ,
        description="Returns 400 if the pipeline has no active API key."),
    _ep("list", "GET", "/api/keys/pipeline/{pipeline_id}/", "List API keys for a pipeline.",
        (Param("pipeline_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Pipeline identifier"),),
        subgroup="pipeline key", doc="v1-etl-pipelines.mdx", permission=Permission.READ),
    _ep("create", "POST", "/api/keys/pipeline/{pipeline_id}/",
        "Create an API key for a pipeline.",
        (Param("pipeline_id", type=ParamType.UUID, location=ParamLocation.PATH,
               required=True, help="Pipeline identifier"),
         Param("pipeline", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Pipeline to attach the key to"),
         Param("api", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Deployment to attach the key to"),
         Param("description", location=ParamLocation.BODY, help="Description (max 255 chars)"),
         Param("is_active", type=ParamType.BOOL, location=ParamLocation.BODY, default=True,
               help="Whether the key is usable")),
        subgroup="pipeline key", body=BodyKind.JSON, doc="v1-etl-pipelines.mdx",
        permission=Permission.READ_WRITE,
        constraints=(MutuallyExclusive(("pipeline", "api")),)),
)


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #

_ADAPTER_TYPES = ["LLM", "EMBEDDING", "VECTOR_DB", "X2TEXT", "OCR"]
_AD_ID = Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Adapter instance identifier")

_AD_FIELDS: tuple[Param, ...] = (
    Param("adapter_name", location=ParamLocation.BODY, required=True,
          help="Instance name, unique per name+type+org (max 128 chars)"),
    Param("adapter_id", location=ParamLocation.BODY, required=True,
          help="SDK adapter identifier, e.g. openai_llm"),
    Param("adapter_type", location=ParamLocation.BODY, required=True,
          choices=_ADAPTER_TYPES, help="Adapter category"),
    Param("adapter_metadata", type=ParamType.JSON, location=ParamLocation.BODY,
          required=True, help="Provider configuration; encrypted at rest"),
    Param("description", location=ParamLocation.BODY, help="Description"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
)

_ad_update = _ep("update", "PUT", "/adapter/{id}/", "Replace an adapter instance.",
                 (_AD_ID, *_AD_FIELDS), subgroup="adapter", body=BodyKind.JSON,
                 doc="v1-adapters.mdx", permission=Permission.READ_WRITE)

#: Only PATCH documents `shared_users` for adapters; POST/PUT do not.
_ad_patch = with_params(
    derive_patch(_ad_update, summary="Partially update an adapter instance."),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY,
          multiple=True, replace_semantics=True, help="User IDs to share with"),
)

_ADAPTERS: tuple[Endpoint, ...] = (
    _ep("supported", "GET", "/supported_adapters/", "List adapters supported by the platform.",
        (Param("adapter_type", required=True, choices=_ADAPTER_TYPES,
               help="Adapter category to list"),),
        subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ,
        examples=("unstract platform adapter supported --adapter-type LLM",)),
    _ep("schema", "GET", "/adapter_schema/", "Show the configuration schema for an adapter.",
        (Param("id", required=True, help="SDK adapter identifier, e.g. openai_llm"),),
        subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ),
    _ep("test", "POST", "/test_adapters/", "Test adapter credentials.",
        (Param("adapter_id", location=ParamLocation.BODY, required=True,
               help="SDK adapter identifier"),
         Param("adapter_metadata", type=ParamType.JSON, location=ParamLocation.BODY,
               required=True, help="Provider configuration to test"),
         Param("adapter_type", location=ParamLocation.BODY, required=True,
               choices=_ADAPTER_TYPES, help="Adapter category")),
        subgroup="adapter", body=BodyKind.JSON, doc="v1-adapters.mdx",
        permission=Permission.READ_WRITE),
    _ep("list", "GET", "/adapter/", "List configured adapter instances.",
        (Param("adapter_type", choices=_ADAPTER_TYPES, help="Filter by category"),),
        subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ,
        table_columns=("id", "adapter_name", "adapter_type", "model", "is_available")),
    _ep("create", "POST", "/adapter/", "Create an adapter instance.", _AD_FIELDS,
        subgroup="adapter", body=BodyKind.JSON, doc="v1-adapters.mdx",
        permission=Permission.READ_WRITE),
    _ep("get", "GET", "/adapter/{id}/", "Show one adapter instance.", (_AD_ID,),
        subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ,
        description="Returns decrypted adapter_metadata, including credentials."),
    _ad_update,
    _ad_patch,
    _ep("delete", "DELETE", "/adapter/{id}/", "Delete an adapter instance.", (_AD_ID,),
        subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.FULL_ACCESS,
        description=(
            f"{_DELETE_NOTE} Returns 409 if the adapter is used by a workflow or "
            "Prompt Studio project, and 500 if it is configured as a default."
        )),
    _ep("info", "GET", "/adapter/info/{id}/", "Show adapter summary including context window.",
        (_AD_ID,), subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ),
    _ep("users", "GET", "/adapter/users/{id}/", "List users an adapter is shared with.",
        (_AD_ID,), subgroup="adapter", doc="v1-adapters.mdx", permission=Permission.READ),
    _ep("get", "GET", "/adapter/default_triad/", "Show the organization's default adapters.",
        subgroup="adapter default-triad", doc="v1-adapters.mdx", permission=Permission.READ),
    # The request keys here differ from the response keys of the GET above
    # (llm_default vs default_llm_adapter). That asymmetry is upstream.
    _ep("set", "POST", "/adapter/default_triad/", "Set the organization's default adapters.",
        (Param("llm_default", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Default LLM adapter"),
         Param("embedding_default", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Default embedding adapter"),
         Param("vector_db_default", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Default vector DB adapter"),
         Param("x2text_default", type=ParamType.UUID, location=ParamLocation.BODY,
               help="Default text extractor adapter")),
        subgroup="adapter default-triad", body=BodyKind.JSON, doc="v1-adapters.mdx",
        permission=Permission.READ_WRITE),
)


# --------------------------------------------------------------------------- #
# Connectors
# --------------------------------------------------------------------------- #

_CN_ID = Param("id", type=ParamType.UUID, location=ParamLocation.PATH, required=True,
               help="Connector instance identifier")

_CN_FIELDS: tuple[Param, ...] = (
    Param("connector_name", location=ParamLocation.BODY, required=True,
          help="Instance name, unique per organization (max 128 chars)"),
    Param("connector_id", location=ParamLocation.BODY, required=True,
          help="Connector type identifier"),
    Param("connector_metadata", type=ParamType.JSON, location=ParamLocation.BODY,
          help="Connection configuration; not needed when using the OAuth flow"),
    Param("connector_version", location=ParamLocation.BODY, help="Connector version"),
    Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY, default=False,
          help="Share with the whole organization"),
    Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
          replace_semantics=True, help="User IDs to share with"),
)

_cn_update = _ep("update", "PUT", "/connector/{id}/", "Replace a connector instance.",
                 (_CN_ID, Param("oauth-key", help="Cache key from the OAuth flow"),
                  *_CN_FIELDS),
                 subgroup="connector", body=BodyKind.JSON, doc="v1-connectors.mdx",
                 permission=Permission.READ_WRITE)

_CONNECTORS: tuple[Endpoint, ...] = (
    _ep("supported", "GET", "/supported_connectors/",
        "List connector types supported by the platform.",
        (Param("type", choices=["INPUT", "OUTPUT"], help="Filter by direction"),
         Param("connector_mode", choices=["FILE_SYSTEM", "DATABASE"], help="Filter by mode")),
        subgroup="connector", doc="v1-connectors.mdx", permission=Permission.READ),
    _ep("schema", "GET", "/connector_schema/",
        "Show the configuration schema for a connector type.",
        (Param("id", required=True, help="Connector type identifier"),),
        subgroup="connector", doc="v1-connectors.mdx", permission=Permission.READ),
    _ep("test", "POST", "/test_connectors/", "Test connector credentials.",
        (Param("connector_id", location=ParamLocation.BODY, required=True,
               help="Connector type identifier"),
         Param("connector_metadata", type=ParamType.JSON, location=ParamLocation.BODY,
               required=True, help="Connection configuration to test")),
        subgroup="connector", body=BodyKind.JSON, doc="v1-connectors.mdx",
        permission=Permission.READ_WRITE),
    _ep("list", "GET", "/connector/", "List connector instances.",
        (Param("workflow", type=ParamType.UUID, help="Filter by workflow"),
         Param("created_by", help="Filter by creator user id"),
         Param("connector_type", choices=["INPUT", "OUTPUT"], help="Filter by direction"),
         Param("connector_mode", choices=["FILE_SYSTEM", "DATABASE"], help="Filter by mode")),
        subgroup="connector", doc="v1-connectors.mdx", permission=Permission.READ,
        table_columns=("id", "connector_name", "connector_id", "connector_mode")),
    _ep("create", "POST", "/connector/", "Create a connector instance.",
        (Param("oauth-key", help="Cache key from the OAuth flow, for OAuth connectors"),
         *_CN_FIELDS),
        subgroup="connector", body=BodyKind.JSON, doc="v1-connectors.mdx",
        permission=Permission.READ_WRITE),
    _ep("get", "GET", "/connector/{id}/", "Show one connector instance.", (_CN_ID,),
        subgroup="connector", doc="v1-connectors.mdx", permission=Permission.READ),
    _cn_update,
    derive_patch(_cn_update, summary="Partially update a connector instance."),
    _ep("delete", "DELETE", "/connector/{id}/", "Delete a connector instance.", (_CN_ID,),
        subgroup="connector", doc="v1-connectors.mdx", permission=Permission.FULL_ACCESS,
        description=f"{_DELETE_NOTE} Returns 409 if used by a workflow."),
)

#: Not org-scoped, unlike everything else in this module.
_OAUTH_CACHE_KEY = Endpoint(
    name="oauth-cache-key",
    group="docstudio",
    subgroup="platform connector",
    method="GET",
    path="/api/v1/oauth/cache-key/{backend}",
    api=ApiGroup.PLATFORM,
    summary="Generate a cache key to associate an OAuth flow with a connector.",
    params=(
        Param("backend", location=ParamLocation.PATH, required=True,
              help="OAuth backend identifier, e.g. google-oauth2"),
    ),
    doc_source=f"{_DOCS}/v1-connectors.mdx",
    permission=Permission.READ,
    description="This endpoint is not organization-scoped.",
    no_trailing_slash=True,
)


# --------------------------------------------------------------------------- #
# Groups, users, sharing
# --------------------------------------------------------------------------- #

#: Group identifiers are plain integers, unlike the UUIDs used elsewhere (P10).
_GROUP_ID = Param("id", type=ParamType.INT, location=ParamLocation.PATH, required=True,
                  help="Group identifier (an integer, not a UUID)")

_GROUPS: tuple[Endpoint, ...] = (
    _ep("list", "GET", "/groups/", "List user groups.", subgroup="group",
        doc="v1-user-groups.mdx", permission=Permission.READ,
        table_columns=("id", "name", "description", "member_count")),
    _ep("create", "POST", "/groups/", "Create a user group.",
        (Param("name", location=ParamLocation.BODY, required=True,
               help="Group name, unique per organization"),
         Param("description", location=ParamLocation.BODY, help="Group description")),
        subgroup="group", body=BodyKind.JSON, doc="v1-user-groups.mdx",
        permission=Permission.READ_WRITE,
        description=(
            "The response echoes only name and description. Re-list groups to "
            "obtain the new id."
        )),
    _ep("patch", "PATCH", "/groups/{id}/", "Update a group's name or description.",
        (_GROUP_ID,
         Param("name", location=ParamLocation.BODY, help="New name, unique per organization"),
         Param("description", location=ParamLocation.BODY, help="New description")),
        subgroup="group", body=BodyKind.JSON, doc="v1-user-groups.mdx",
        permission=Permission.READ_WRITE),
    _ep("delete", "DELETE", "/groups/{id}/", "Delete a group.", (_GROUP_ID,),
        subgroup="group", doc="v1-user-groups.mdx", permission=Permission.FULL_ACCESS,
        description=(
            f"{_DELETE_NOTE} Removes every resource share referencing the group; "
            "member accounts themselves are not deleted."
        )),
    _ep("list", "GET", "/groups/{id}/members/", "List members of a group.", (_GROUP_ID,),
        subgroup="group member", doc="v1-user-groups.mdx", permission=Permission.READ),
    _ep("add", "POST", "/groups/{id}/members/", "Add members to a group.",
        (_GROUP_ID,
         Param("user_ids", type=ParamType.INT, location=ParamLocation.BODY, multiple=True,
               required=True, help="User IDs to add; existing members are ignored")),
        subgroup="group member", body=BodyKind.JSON, doc="v1-user-groups.mdx",
        permission=Permission.READ_WRITE),
    # No trailing slash on this path, unlike its siblings.
    _ep("remove", "DELETE", "/groups/{id}/members/{user_id}", "Remove a member from a group.",
        (_GROUP_ID, Param("user_id", type=ParamType.INT, location=ParamLocation.PATH,
                          required=True, help="User to remove")),
        subgroup="group member", doc="v1-user-groups.mdx", permission=Permission.FULL_ACCESS,
        description=_DELETE_NOTE, no_trailing_slash=True),
    _ep("resources", "GET", "/groups/{id}/resources/",
        "List resources shared with a group.", (_GROUP_ID,), subgroup="group",
        doc="v1-user-groups.mdx", permission=Permission.READ),
    _ep("list", "GET", "/users/", "List organization members.", subgroup="user",
        doc="v1-organization-users.mdx", permission=Permission.READ,
        table_columns=("id", "email", "role", "is_admin"),
        description=(
            "Member ids are returned as strings but must be sent as integers in "
            "shared_users; the CLI converts them for you."
        )),
    Endpoint(
        name="share",
        group="docstudio",
        subgroup="platform",
        method="POST",
        path=f"{_BASE}/{{resource}}/{{id}}/share/",
        api=ApiGroup.PLATFORM,
        summary="Share a resource with users, groups, or the whole organization.",
        description=(
            "Each axis REPLACES its existing list rather than appending. To add "
            "one user without dropping the others, read the current shares first "
            "and send the combined list."
        ),
        params=(
            _ORG,
            Param("resource", location=ParamLocation.PATH, required=True,
                  choices=SHARE_RESOURCES,
                  help="Resource type; mapped to the correct URL segment"),
            Param("id", location=ParamLocation.PATH, required=True,
                  help="Resource identifier"),
            Param("shared_users", type=ParamType.INT, location=ParamLocation.BODY,
                  multiple=True, replace_semantics=True, help="User IDs to share with"),
            Param("shared_groups", type=ParamType.INT, location=ParamLocation.BODY,
                  multiple=True, replace_semantics=True, help="Group IDs to share with"),
            Param("shared_to_org", type=ParamType.BOOL, location=ParamLocation.BODY,
                  help="Share with the whole organization"),
        ),
        body=BodyKind.JSON,
        doc_source=f"{_DOCS}/v1-user-groups.mdx",
        permission=Permission.READ_WRITE,
        examples=(
            "unstract platform share --resource api-deployment --id <uuid> --shared-users 2 --shared-users 5",
        ),
    ),
)


ENDPOINTS: tuple[Endpoint, ...] = (
    *_PROMPT_STUDIO,
    *_WORKFLOWS,
    *_API_DEPLOYMENTS,
    *_PIPELINES,
    *_ADAPTERS,
    *_CONNECTORS,
    _OAUTH_CACHE_KEY,
    *_GROUPS,
)
