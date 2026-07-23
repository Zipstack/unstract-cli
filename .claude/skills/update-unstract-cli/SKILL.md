---
name: update-unstract-cli
description: Update the Unstract CLI's endpoint definitions by cross-referencing the public API documentation. Use when Unstract, LLMWhisperer, or API Hub APIs change, when the CLI is missing a documented endpoint or parameter, or to audit the CLI for drift against the docs. Triggers include "update the CLI", "sync CLI with docs", "check the CLI for API drift", and "add the new endpoint to the CLI".
---

# Update the Unstract CLI from the public API docs

Keep `src/unstract_cli/endpoints/*.py` synchronized with the published API
documentation: detect drift, report it with citations, then apply it.

This is tractable only because of one architectural property: **every command is
generated from a declarative `Endpoint` record.** The command tree, flags, help
text, validation and `--discover` all derive from those records, so a
correct edit to one record propagates everywhere. You never touch command wiring.

## Products and API groups

Unstract is the company and the CLI's name. It builds three products:

| Product | CLI group | API groups it owns |
| --- | --- | --- |
| **Document Studio** | `docstudio` | `platform`, `deployment`, `hitl` |
| **LLMWhisperer** | `whisper` | `llmwhisperer` |
| **API Hub** | `apihub` | `apihub` |

There are exactly three products -- never introduce a fourth. Document Studio's
three API groups have distinct hosts and credentials, so records set
`api=ApiGroup.PLATFORM` (etc.) and the product is derived from that, never
stored twice. Document Studio's wire paths use `platform`/`deployment`; the CLI
preserves them verbatim under the `docstudio` group.

## Sources of truth

| Product | Documentation source | CLI file |
| --- | --- | --- |
| LLMWhisperer | `llmwhisperer-docs/docs/llm_whisperer/apis/*.md` | `whisper.py` |
| API deployment runtime | `unstract-docs/docs/unstract_platform/api_deployment/*.md` | `deployment.py` |
| Platform v1 | `unstract-docs/docs/unstract_platform/api_documentation/versions/v1-*.mdx` | `platform.py` |
| HITL | `unstract-docs/docs/unstract_platform/human_quality_review/*.md` | `hitl.py` |
| API Hub | **No public docs** — `unstract-verticals/src/api_v1/api.py`, `verticals-portal/portal/postman-collection/*.json` | `apihub.py` |

Repos are expected as siblings of `unstract-cli`. If one is missing locally, read
it through the GitHub API (`gh api repos/Zipstack/<repo>/contents/<path>`) rather
than guessing.

Several records carry unusually detailed help text, a `doc_conflict` note, or a
seemingly redundant parameter *because* of a known server defect — the comment
above each explains which. Read it before "simplifying" one: the verbosity is
load-bearing, and the workaround should be removed only once the backend fix
lands, not because it looks like clutter.

Each record's `doc_source` field names the file it was authored from — that is
your anchor for diffing. Most point at a `.mdx`/`.md` docs page. A number
legitimately point at **backend source** instead, because the endpoint has no
public docs page, and cite `backend/.../urls.py` exactly as API Hub records cite
`unstract-verticals` source:

- Prompt Studio Output Manager + task-status — `output list`, `output latest`,
  `task-status` (`backend/prompt_studio/...`).
- Workflow assembly — `tool registry {list,settings-schema}`,
  `workflow tool {list,get,add,set-metadata,remove}`,
  `workflow endpoint {list,set}` (`backend/tool_instance_v2/urls.py`,
  `backend/workflow_manager/endpoint_v2/urls.py`).

These surface in the diff as "in CLI, missing from docs" (info-only); that is
correct and expected — report, never delete. Do **not** reach for `doc_conflict`
to silence them: `doc_conflict` means "a docs page exists but the record
deliberately diverges from it," which is a different thing from "no docs page
exists."

`doc_conflict` suppresses **both** axes of the diff for its record — the
endpoint's absence from the docs *and* any documented parameter the record does
not expose. That is deliberate: a record which deliberately drops a documented
flag would otherwise be reported as param drift on every run, inviting exactly
the "restoration" the annotation exists to prevent. The diff also recognises a
parameter that is *mirrored* rather than exposed: a body field supplied by
`mirror_as` from a path param is present on the wire, so it is not drift.

## Procedure

### 1. Establish scope

If the user named a product or endpoint, work only on that. Otherwise audit
everything. State the scope before starting.

### 2. Parse the documentation

Extract `(method, path, params[])` from each source. Two formats:

- **Markdown** (LLMWhisperer, API deployment, HITL): parameters live in tables
  with `Parameter | Type | Default | Required | Description` columns. The endpoint
  and method are in the summary table at the top of the page.
- **MDX** (Platform v1): parameters live in `<ApiEndpoint>` component props —
  `method`, `path`, `description`, and arrays `pathParams`, `queryParams`,
  `requestBody`, `responseBody`. This is JSX, not Markdown: parse the props, not
  table cells. `<ApiSection>` groups them.

### 3. Parse the current records

Read the relevant `endpoints/*.py`. `unstract --discover` gives the same
information as JSON and is often easier to diff against — but remember it reflects
the *installed* package, so re-install after editing if you use it.

Filter it rather than reading the whole thing: unfiltered `--detail full` is
~50k tokens.

```bash
unstract --discover --group whisper --detail full
unstract --discover --command 'whisper extract' --detail full
```

### 4. Diff on three axes

1. **In docs, missing from CLI** → new capability to add.
2. **In CLI, missing from docs** → *report only, never delete.* Documentation lags
   implementation constantly; an endpoint absent from the docs is far more often
   an undocumented feature than a removed one.
3. **Parameter drift** → added, removed, renamed, or changed type / default /
   enum / required-ness.

### 5. Report before editing

Present a table: each difference, its doc citation (file + heading), and the
proposed record change. Wait for confirmation on anything ambiguous. Never apply
a breaking change without flagging it as such.

### 6. Apply

Edit `endpoints/*.py` only. Follow the conventions already in those files:

- `Param.name` is the **API's spelling**, used verbatim on the wire — including
  typos like `page_seperator`. Rename the *flag* with `flag="--page-separator"`,
  never the name.
- Choose the right `location` (`QUERY`/`BODY`/`PATH`/`HEADER`/`FORM`) and the
  endpoint's `body` kind.
- Every `Param` needs `help`; every `Endpoint` needs `summary` and `doc_source`.
- Reuse the pattern encodings rather than inventing new ones: `MutuallyExclusive`,
  `AtLeastOneOf`, `RequiredUnless`, `choices={friendly: wire}`, `multiple`,
  `default_from`, `freeform_prefix`, `applies_when`, `replace_semantics`,
  `derive_patch`, `with_params`, `mirror_to_body` / `mirror_as`,
  `require_response_fields` (treat a 2xx that leaves a field null as a failure).
- **Mirroring a PATH id into the body.** `mirror_to_body=True` copies a path
  parameter into the JSON body under the same name (`prompt create`'s `tool_id`,
  which the backend otherwise persists as NULL). `mirror_as="<body_name>"` does
  the same but renames it, for a server that wants one identifier twice under two
  spellings — `api-deployment key create` takes `api_id` in the URL and the same
  value as `api` in the body, so mirroring means the caller passes only
  `--api-id`. Prefer this over asking the user to repeat a value.
- **`RequiredUnless`** expresses "required, except in one configuration" — a
  field that is mandatory in general but genuinely unread when some other flag
  holds a sentinel value. It is exported and tested but **no shipped record uses
  it today**: the case it was built for (`profile create --chunk-size 0`) turned
  out to be server-enforced, so relaxing it client-side would only have moved a
  fast exit-2 to a slow remote 400. Reach for it only when you have confirmed the
  *server* accepts the omission.
- **`default_from` accepts a fallback chain**, whitespace-separated, first
  resolved value winning: `default_from="deployment.org_id platform.org_id"`.
  Use it where two config blocks genuinely hold the same value and one is
  typically empty — never to borrow a credential across API groups, which would
  be credential confusion rather than convenience.
- **JSON parameters parse themselves.** A `Param(type=ParamType.JSON, …)` value is
  parsed to a real object before it is sent, accepting inline JSON *or*
  `@path/to/file.json`. Never add manual `json.loads`; just set the type.
- **`--wait` retrieval is declarative.** A `PollSpec` drives execute → poll →
  retrieve. When the result store is not keyed by the poll handle (e.g. Prompt
  Studio's Output Manager is keyed by `tool_id`, not `task_id`), use
  `retrieve_carry` to forward original-request identifiers (a py_name, or a
  `(source, dest)` pair to rename one), `retrieve_extra` for a constant flag the
  original call did not carry, and `retrieve_omits_handle` when the retrieve
  endpoint has no handle parameter.
- **Match `status_field` to the STATUS endpoint's body, not the run response.**
  An execute response and its status endpoint often spell the state differently
  (deployment: the run POST nests `execution_status` under `message`; the status
  GET returns a top-level `status`). The poll reads the *status* endpoint, so
  `status_field` must match that — pass a tuple like `("status", "execution_status")`
  to accept both. Getting this wrong silently consumes a one-shot result and 406s
  on the next poll; verify the real status-GET payload shape, not just the run
  response, and write a fixture that mirrors it.
- **One-shot poll = the poll *is* the retrieve.** When the status endpoint is
  itself the single-read result store (deployment run), set `one_shot=True` and
  **no** `retrieve_endpoint`: the terminal poll's body is the result, and a second
  read would 406. Such commands need a client-side `--save` (a `client_side=True`
  Param) so the result persists on that one read.
- **Never duplicate a PATCH record**: derive it from its PUT with `derive_patch`,
  adding PATCH-only fields via `with_params`.
- Paths are **literal**. Do not normalise trailing slashes or "fix"
  `profilemanager` vs `profile-manager` — those inconsistencies are real, and
  correcting them produces 404s. A path with no trailing slash must set
  `no_trailing_slash=True` so the omission reads as intent; a test enforces this.

### 7. Verify

```bash
uv run pytest          # includes definition-integrity and flag-coverage tests
uv run ruff check .
uv run mypy src
unstract --discover | python -c "import json,sys; json.load(sys.stdin)"
```

Add a respx test for any new endpoint, using the sample payload from the docs.

### 8. Summarize

Report what changed, what needs human judgement, and what you deliberately
skipped.

## Safety rules

These exist because each one has a specific failure mode behind it.

1. **Never delete a command just because the docs omit it.** Report it. Docs lag
   implementation, and deleting a working command breaks users' scripts.
2. **Never invent a parameter.** Every addition cites a specific file and heading.
   If the docs are ambiguous, say so and ask.
3. **Treat renames as breaking.** Add the new flag, keep the old one as a
   deprecated alias, and say so in the report. Never rename silently.
4. **Preserve richer hand-written help.** Where a record's help text says more
   than the docs, add to it — do not overwrite it with a thinner description.
5. **Honour `doc_conflict`.** A record carrying that field records a *deliberate*
   divergence from the docs, verified against another source. Never revert one
   without explicit human confirmation. Two live examples:

   - **A wrong docs page.** `whisper detail` uses `/whisper-detail` (singular).
     The docs *index* says `/whisper-details` (plural), but the endpoint page and
     the official `llm-whisperer-python-client` both use the singular. The index
     is wrong. Do not "fix" it.
   - **Deliberately dropped parameters.** `api-deployment key create` and
     `pipeline key create` each omit two documented body params. The docs list
     both `api` and `pipeline`; the record mirrors the one the route already
     fixes (`api_id` → `api`) and drops the other, because
     `/api/keys/api/{api_id}/` cannot create a pipeline key — that is the
     sibling command. Restoring either flag re-introduces the original
     complaint, which was having to pass the same id twice.

6. **`draft: true` means exclude.** Docs pages with that front matter (currently
   the multi-doc chat APIs `/md/file/upload`, `/md/file/search`, `/md/chat`)
   describe unstable contracts. Do not add them. If the flag is removed upstream,
   propose adding them as a new `chat` group and let a human decide.
7. **API Hub changes need human review.** There are no public docs for it; the
   contract is inferred from source and Postman collections, which recover
   parameter names but not intent. Report proposed changes; do not apply them
   silently. Note that `--ext-param KEY=VALUE` already lets users reach new
   `ext_*` parameters without a CLI release, so the pressure to guess is low.
8. **Docs are not infallible.** Where an index page and a detail page disagree,
   prefer the detail page and the official client libraries. Record the decision
   as a `doc_conflict` note so the next run does not re-litigate it.

## Worked example

*A new `--ocr-engine` parameter appears in the LLMWhisperer extraction docs.*

1. Read `llmwhisperer-docs/docs/llm_whisperer/apis/whisper.md`; find the row in
   the parameters table: `ocr_engine | string | tesseract | No | OCR engine to use`.
2. Confirm `whisper extract` in `endpoints/whisper.py` has no such `Param`.
3. Report: "`whisper.md` documents `ocr_engine` (string, default `tesseract`,
   optional); `whisper extract` lacks it. Proposed: add to `_EXTRACT_PARAMS`."
4. Apply — one `Param` in `_EXTRACT_PARAMS`, with `help` from the description and
   `choices` if the docs enumerate values.
5. Verify: `--discover` now lists `--ocr-engine` with its default. Tests and
   type checks pass. No other file changed — the flag, help text and index all
   came from that single record.
