# Unstract CLI — Specification

**Status:** Draft v1
**Repo:** `Zipstack/unstract-cli`
**Binary:** `unstract`

---

## 1. Purpose

**Unstract** is the company, and `unstract` is the name of this CLI. Unstract builds exactly **three products**, and this one tool covers all of them:

| Product | Group | API groups | Surface covered |
| --- | --- | --- | --- |
| **Document Studio** | `docstudio` | `platform`, `deployment`, `hitl` | Prompt Studio, Workflows, API Deployments, ETL/Task Pipelines, Adapters, Connectors, User Groups, Org Users; deployed-API execution; Human Quality Review |
| **LLMWhisperer** | `whisper` | `llmwhisperer` | Text extraction, status/retrieve, detail, highlights, usage, webhook management |
| **API Hub** (internal name: *Verticals*) | `apihub` | `apihub` | Vertical extraction (bank statement, table discovery/extraction), doc-splitter, status/retrieve |

> **Document Studio's APIs use `platform`/`deployment` paths on the wire and `UNSTRACT_*` environment variables.** The CLI surfaces them under the `docstudio` group but preserves those wire paths and variable names verbatim.

Document Studio owns three API groups because each has its own base URL and credentials. They stay distinct for auth and config purposes while belonging to one product.

The primary consumer is an **LLM agent operating in an autonomous workflow**. A human at a terminal is the secondary consumer. Every design decision below resolves in favour of machine legibility, deterministic behaviour, and self-description via `--help`.

### 1.1 Goals

1. One binary, one auth/config model, one output contract across three products (five API groups) that today have three incompatible authentication schemes and four different base URLs.
2. An agent can discover the full capability surface **without external documentation** — from `--help` output and a machine-readable command index alone.
3. Every documented API parameter is reachable as a flag. No capability is CLI-only or API-only.
4. The CLI's endpoint definitions are a **single declarative source of truth**, mechanically diffable against the public docs so the bundled Claude Skill can keep it current.

### 1.2 Non-goals

- Not a replacement for `unstract-python-client` / `llm-whisperer-python-client` as an embedding library. The CLI is a process-level interface.
- No interactive TUI, wizards, or prompts. See §5.2.
- Not a workflow orchestrator. It exposes primitives; agents compose them.

---

## 2. Key decisions

### D1 — Direct REST with declarative endpoint definitions (not client-library wrapping)

The CLI implements HTTP directly against documented endpoints. Each endpoint is a declarative record (`endpoint`, `method`, `params`, `auth`, `output`) from which the command tree, flags, help text, and validation are **generated**.

*Rationale.* The official Python clients cover LLMWhisperer and API-deployment execution only. The Platform v1 management surface — the bulk of the command tree — has no client and would be raw REST regardless; wrapping would produce two inconsistent internal layers. Decisively: the "cross-reference public docs" Skill requirement is only tractable if each command maps 1:1 onto a documented endpoint+parameter set in a form that can be **diffed**. A table of endpoint definitions is diffable against documentation; a wrapper around a third-party client's Python API is not. Polling/retry *logic* may be borrowed from the clients, but the HTTP layer is owned here.

### D2 — Profile-based configuration

Three incompatible auth schemes, plus region-specific and on-prem hosts, plus `org_id` as a **URL path segment** rather than a flag. Configuration uses named profiles (kubectl/aws style) holding per-product host, key, and org. See §4.

### D3 — Python + Click (a deliberately minimal dependency set)

Click gives an introspectable command tree, which `--discover` (§5.3) reads directly, and its command objects are built at runtime from the declarative `Endpoint` records rather than from static function signatures. Click is used **directly**, not through Typer: Typer's decorator API assumes statically-declared signatures, whereas the parameters here are known only at runtime from data, so Typer added a dependency without being used and was removed.

The runtime dependency set is kept as small as the feature set allows, to limit supply-chain exposure: `click` (dynamic command tree), `httpx` (TLS, redirects, connection reuse, and multipart/form-data encoding — the one piece with no stdlib equivalent), `pyyaml` (`--output yaml`) and `tomli-w` (config writer; reading uses the stdlib `tomllib`). "Absolute zero" is not a goal: the remaining packages are widely-deployed and audited, and replacing them (a hand-rolled multipart encoder, a hand-rolled TOML writer) would substitute bespoke, unaudited code for that — a worse trade for a security-motivated posture. Reproducible, hash-pinned installs (a lockfile with hashes) address the "compromised release on PyPI" threat more directly than shaving audited packages from the set.

### D4 — API Hub is code-derived, and this is a documented gap

API Hub has **no public documentation site**. Its contract is derived from `Zipstack/unstract-verticals` (`src/api_v1/api.py`) and the Postman collections in `Zipstack/verticals-portal` (`portal/postman-collection/`). The Skill therefore **cannot** validate API Hub commands against public docs; it must fall back to source-of-truth diffing against the repo. This gap is explicit in §8.4 rather than left implicit.

---

## 3. Command tree

Grouped by product, then API group, then resource, then action. Verb-last, consistent across groups.

```
unstract
├── whisper                       # LLMWhisperer v2
│   ├── extract                   # POST /whisper   (+ --wait convenience)
│   ├── status                    # GET  /whisper-status
│   ├── retrieve                  # GET  /whisper-retrieve
│   ├── detail                    # GET  /whisper-detail
│   ├── highlights                # GET  /highlights
│   ├── usage                     # GET  /get-usage-info
│   ├── usage-by-tag              # GET  /usage
│   └── webhook {create,get,update,delete}    # /whisper-manage-callback
│
├── docstudio                     # Document Studio
│   └── deployment                # Deployed API workflow execution
│   ├── run                       # POST /deployment/api/{org}/{api_name}/
│   ├── status                    # GET  /deployment/api/{org}/{api_name}/?execution_id=
│   └── highlight                 # GET  /deployment/api/{org}/{api_name}/highlight/
│
│   └── platform                  # Platform Management API v1
│   ├── prompt-studio {list,get,create,update,patch,delete,
│   │                  export-project,import-project,sync-prompts,
│   │                  export-tool,export-info,
│   │                  file {upload,get,delete},
│   │                  prompt {create,get,update,patch,delete,reorder},
│   │                  profile {list,set-default,create,get,update,patch,delete},
│   │                  index-document,fetch-response,single-pass,task-status,
│   │                  output {list,latest},
│   │                  users,check-deployment-usage,
│   │                  select-choices,adapter-choices,retrieval-strategies}
│   ├── workflow {list,get,create,update,patch,delete,execute,toggle-active,
│   │             can-update,clear-file-marker,schema,users,
│   │             tool {list,get,add,set-metadata,remove},   # attach a registry tool
│   │             endpoint {list,set},                       # set SOURCE/DEST to API
│   │             execution {list,get,logs},
│   │             file-history {list,get,delete,clear}}
│   ├── tool registry {list,settings-schema}   # find a tool's function_name
│   ├── api-deployment {list,get,create,update,patch,delete,users,
│   │                   by-prompt-studio-tool,postman-collection,
│   │                   key {list,create,get,update,delete}}
│   ├── pipeline {list,get,create,update,patch,delete,execute,executions,
│   │             users,postman-collection,
│   │             key {list,create}}
│   ├── adapter {list,get,create,update,patch,delete,info,users,
│   │            supported,schema,test,
│   │            default-triad {get,set}}
│   ├── connector {list,get,create,update,patch,delete,
│   │              supported,schema,test,oauth-cache-key}
│   ├── group {list,create,patch,delete,
│   │          member {list,add,remove}, resources}
│   ├── user list
│   └── share                     # POST /{resource}/{id}/share/
│
│   └── hitl                      # Human Quality Review (Enterprise)
│   ├── approved get              # GET /mr/api/{org}/approved/result/{class_id}/
│   ├── bulk-download             # same endpoint, --download-files / --page / --email
│   └── download-status           # GET /mr/api/{org}/approved/download-status/{job_id}/
│
├── apihub                        # API Hub / Verticals
│   ├── extract                   # POST /api/v1/extract?vertical=&sub_vertical=
│   ├── status                    # GET  /api/v1/status?file_hash=
│   ├── retrieve                  # GET  /api/v1/retrieve?file_hash=
│   └── doc-splitter {upload,status,download}
│
├── config {init,list,get,set,use,current,path,doctor}
├── completion {bash,zsh,fish}
└── --discover / --version / --help
```

### 3.1 Convenience composition

Both LLMWhisperer and Unstract are execute → poll → retrieve. Raw sub-commands are always available, **and** a `--wait` flag drives the poll loop to a terminal state so agents need not script it:

```bash
unstract whisper extract --file invoice.pdf --mode form --wait --output json
unstract docstudio deployment run --api-name invoice-api --file invoice.pdf --wait --save out.json
unstract apihub extract --vertical table --sub-vertical bank_statement --file stmt.pdf --wait
unstract docstudio platform prompt-studio fetch-response --tool-id <t> --document-id <d> --id <p> --wait
```

`--wait` accepts `--poll-interval` (default 3s) and `--timeout` (default 300s). On timeout the process exits `7` (§5.4) with the handle (`whisper_hash` / `execution_id` / `file_hash` / `task_id`) on stdout so the agent can resume.

**One-shot polling (deployment run).** For `deployment run --wait`, the status endpoint *is* the one-shot result store: the poll that first observes `COMPLETED` also consumes and acknowledges the result. So `--wait` returns *that terminal poll's body* as the result and issues no second read — a re-read would return HTTP 406. This is the one execute→poll→retrieve variant where the poll *is* the retrieve; it defines no separate `retrieve_endpoint`. Because the read is destructive and unrepeatable, `deployment run` takes `--save`, which persists the result on that single read. Reading the terminal state correctly is load-bearing: the status GET returns a top-level `status` field (its `message` holds the result), while the run POST nests `execution_status` under `message`; the poll must match the status endpoint's shape or it fails to recognise `COMPLETED`, consumes the result on that read, and 406s on the next poll.

Prompt Studio's `fetch-response` and `single-pass` are execute → poll → retrieve too, but the pieces live in different places: the async call returns a `task_id`, `task-status` reports completion (not the value), and the extracted value lands in the Output Manager. `--wait` composes all three — poll `task-status` to `completed`, then read the result from `prompt-studio output list`, keyed by the original request's `tool_id`. `fetch-response --wait` narrows to the exact prompt + document it ran; `single-pass --wait` returns the tool-wide single-pass rows. Without `--wait`, retrieve the result yourself with `prompt-studio output list --tool-id <t>` (see §6.3).

---

## 4. Configuration & authentication

### 4.1 Resolution order

For every setting, strictly: **command-line flag → environment variable → profile in config file → built-in default.**

### 4.2 Config file

Named profiles; `--profile/-p` or `UNSTRACT_PROFILE` selects one.

**Multiple config files are supported**, since different work needs different settings. The file itself is resolved, highest precedence first:

1. `--config PATH`
2. `$UNSTRACT_CONFIG`
3. the nearest `.unstract.toml`, found by walking up from the working directory (as `git` and `ruff` do; the search stops at `$HOME` so a stray file above it cannot capture every invocation)
4. `$XDG_CONFIG_HOME/unstract/config.toml`, else `~/.config/unstract/config.toml`

These compose with profiles rather than replacing them: a project file may itself define several profiles. A committed `.unstract.toml` is safe because credentials use `env:` indirection.

**Block layout has exactly one accepted form**: the API group nested under its product, e.g. `[profiles.<name>.docstudio.platform]` or `[profiles.<name>.llmwhisperer]`. No aliases, no flat fallback. A block written any other way is ignored rather than silently half-applied — a config that looks applied but is not surfaces later as a missing-credential error with no obvious cause. `config get`/`config set` name the same targets: `docstudio.platform` (or `docstudio platform`), `docstudio.deployment`, `docstudio.hitl`, `llmwhisperer`, `apihub`.

```toml
default_profile = "cloud-us"

[profiles.cloud-us]
  [profiles.cloud-us.whisper]
  base_url = "https://llmwhisperer-api.us-central.unstract.com/api/v2"
  api_key  = "env:LLMWHISPERER_API_KEY"     # indirection: never store secrets inline

  [profiles.cloud-us.platform]
  base_url = "https://us-central.unstract.com"
  org_id   = "org_XXXXXXXX"
  api_key  = "env:UNSTRACT_PLATFORM_KEY"

  [profiles.cloud-us.deployment]
  base_url = "https://us-central.unstract.com"
  org_id   = "org_XXXXXXXX"
  api_key  = "env:UNSTRACT_DEPLOYMENT_KEY"

  [profiles.cloud-us.apihub]
  base_url = "https://api-hub.unstract.com"
  api_key  = "env:UNSTRACT_APIHUB_KEY"

[profiles.cloud-eu]
  [profiles.cloud-eu.whisper]
  base_url = "https://llmwhisperer-api.eu-west.unstract.com/api/v2"
```

Values may use `env:VAR_NAME` indirection so the file itself holds no secrets. Config files with secrets inline are created `0600`; the CLI warns if permissions are broader.

An `env:` ref resolves from the CLI **process's** environment only. A variable `export`ed in a login shell that the CLI did not inherit resolves to nothing, and auth then fails with a "missing credential" error while a literal value in the file would have worked — a costly, invisible trap. `config doctor` exists to make this visible: it reports, per API group, exactly where each credential resolves from (override / named env var / profile literal / profile `env:` ref) and, for an `env:` ref, whether the variable is present in *this* process. With `--check` (on by default) it makes one authenticated call per configured group and reports whether the resolved credential actually works — never echoing the secret or the payload.

### 4.3 Environment variables

| Variable | Purpose |
| --- | --- |
| `UNSTRACT_PROFILE` | Active profile name |
| `LLMWHISPERER_API_KEY` | LLMWhisperer `unstract-key` header |
| `LLMWHISPERER_BASE_URL` | Region / on-prem override |
| `UNSTRACT_PLATFORM_KEY` | Platform v1 Bearer token |
| `UNSTRACT_DEPLOYMENT_KEY` | API-deployment Bearer token |
| `UNSTRACT_ORG_ID` | Org identifier (URL path segment) |
| `UNSTRACT_BASE_URL` | Platform host (cloud region / on-prem) |
| `UNSTRACT_APIHUB_KEY`, `UNSTRACT_APIHUB_BASE_URL` | API Hub |
| `UNSTRACT_ANTHROPIC_API_KEY` | API Hub `X-Anthropic-API-Key` passthrough |
| `UNSTRACT_OUTPUT` | Default output format |
| `NO_COLOR` | Disable ANSI styling |

Env vars are the expected mechanism in CI and agent sandboxes; the CLI is fully usable with **zero config file**.

### 4.4 Auth schemes per product

| Product | Header | Org handling |
| --- | --- | --- |
| LLMWhisperer | `unstract-key: <key>` | n/a |
| API Deployment | `Authorization: Bearer <key>` | `org_id` + `api_name` in URL path |
| Platform v1 | `Authorization: Bearer <platform-key>` | `org_id` in URL path |
| HITL | `Authorization: Bearer <key>` | `org_id` + `class_id` in URL path |
| API Hub | `apikey: <key>` at the Kong gateway; optional BYO-key passthrough `X-LLMWhisperer-API-Key`, `X-Anthropic-API-Key` | n/a |

**API Hub tenancy.** External callers send only `apikey`. The Kong plugin `subscription-metadata-injector` looks that key up in Redis and injects `X-Subscription-Id`, `X-Subscription-Name`, `X-User-Id`, and `X-Product-Id` downstream. The CLI therefore **must not** send those headers — they are gateway-supplied and would be overwritten. It sends `apikey`, plus the optional bring-your-own-key headers when the user supplies their own LLMWhisperer/Anthropic credentials.

**Platform key permission levels** (`read` / `read_write` / `full_access`) are surfaced in help text for destructive commands: `DELETE` requires `full_access`, enforced server-side at middleware. The CLI states this in `--help` rather than letting agents discover it via a 403.

---

## 5. LLM-friendliness requirements

These are testable requirements, not aspirations.

### 5.1 Output contract

- `--output {json,yaml,table,raw}`; **`json` is the default when stdout is not a TTY**, `table` when it is. `UNSTRACT_OUTPUT` overrides.
- On **success**, only the result goes to stdout — no banners, spinners, or progress. Diagnostics go to stderr.
- On **failure**, stdout carries nothing but the structured error envelope, and only when stdout is not a TTY (see §5.5). A result and an error never share an invocation's stdout: the command either emits a result and exits `0`, or emits an error and exits non-zero — never both. So a consumer that pipes stdout to a JSON parser always sees exactly one valid object, on either path.
- `raw` emits only the payload (extracted text, file bytes) for piping.
- `--quiet` suppresses stderr diagnostics; `-v/-vv` increases them.

### 5.2 Never interactive

No command prompts for input under any circumstance. Missing required input is an error with a message naming the exact flag or env var to supply. Secrets are read from env or config, never a TTY prompt. This is a hard constraint: an autonomous agent cannot answer a prompt.

### 5.3 Discoverability

- Complete `--help` at **every** level, with a one-line description, full flag list with types/defaults/enums, and at least one worked example per leaf command.
- Enumerated values are listed in help text verbatim (e.g. `--mode {native_text,low_cost,high_quality,form,table}`).
- `unstract --discover` emits a machine-readable JSON index, generated from the same endpoint definitions that drive execution so it cannot drift. It has three verbosity tiers, cheapest first, selected by `--detail`:
  - **`groups`** (default, ~1k tokens): a map of the ~15 navigable command groups, each with a one-line summary, its command count, and the exact `drill` command that lists it. This is the entry point — an agent reads it, then follows one `drill` into the subtree it needs instead of pulling every command into context. Each `drill` is the exact `--discover --command '<path>'` prefix and is **verified by test to return the count it advertises** (discovery must never advertise a path the parser won't honour).
  - **`summary`** (~5.8k tokens): every command with its name and one-line summary.
  - **`full`** (~50k tokens): adds each command's flags (type, default, enum, required-ness), HTTP method and path, `--wait`/one-shot semantics, and permissions.
  - Selection (`--group`, `--command` prefix) and verbosity (`--detail`) are independent; a filtered query degrades `groups` to `summary` since the filter already narrows to a subtree. The `groups` overview and the unfiltered `full`/`summary` views carry the global boilerplate (exit-code table, output/error conventions); narrow queries omit it, where it would outweigh the answer.
- `unstract <group> --help` lists sub-commands with descriptions; unknown commands produce a "did you mean" suggestion on stderr.

### 5.4 Exit codes

Stable and documented, so an agent can branch without parsing prose:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Generic / unexpected error |
| `2` | Usage error (bad flags, missing required argument) |
| `3` | Authentication or authorization failure (401 / 403) |
| `4` | Not found (404) |
| `5` | Validation error rejected by the API (400 / 422) |
| `6` | Rate limited / quota exceeded (429) |
| `7` | Timed out waiting for a terminal state (`--wait`) |
| `8` | Remote server error (5xx), or a 2xx the server returned in a broken state — e.g. a create that accepts the request but leaves a linking field null (§6.3, `prompt create`) |
| `9` | Result already consumed (see §5.6) |

### 5.5 Structured errors

Every failure emits a JSON object on **stderr** (even in `table` mode). When stdout is **not a TTY** — the agent / wrapper case — the same envelope is **also written to stdout**, so a pipeline feeding stdout to a JSON parser sees a valid object rather than an empty stream (a `JSONDecodeError` otherwise). On a TTY, stdout stays clean and the error appears on stderr only.

This mirroring covers **every** error path, not merely usage/validation errors: our own structured errors, Click's own parse errors (`No such option`, missing argument — rendered through the same envelope at the console entry point), and the `--discover` no-match case. It is safe because success and failure never share stdout (§5.1).

Example envelope:

```json
{
  "error": {
    "code": "validation_error",
    "http_status": 422,
    "message": "Pipeline 'testetl' is inactive, please activate the pipeline",
    "details": [{"code": "error", "detail": "...", "attr": null}],
    "endpoint": "POST /deployment/api/{org_id}/{api_name}/",
    "hint": "Activate the pipeline in the Unstract UI, or use `unstract docstudio platform pipeline patch --active true`.",
    "retryable": false
  }
}
```

`hint` and `retryable` exist specifically so an agent can self-correct rather than retry blindly.

### 5.6 One-time-retrieval semantics (agent footgun)

Both LLMWhisperer `/whisper-retrieve` and the Unstract deployment status API return results **exactly once**; a second call yields "already delivered" / HTTP 406. An agent that re-runs a step will silently lose data.

Mitigations, all mandatory:
- `--save <path>` on every retrieval command, writing the payload to disk atomically **before** exit.
- `--wait` implies persistence: the retrieved result is always written to `--save` if given, and the destructive read is performed exactly once.
- Help text for these commands states the one-shot behaviour explicitly.
- A consumed result produces exit code `9` with a `hint` naming the likely cause.

### 5.7 Reliability

- Automatic retry with exponential backoff + jitter on `429` and `5xx`; never on `4xx`. `--max-retries` (default 3), `--no-retry`.
- `--dry-run` prints the request (method, resolved URL, headers with secrets redacted, body summary) as JSON and exits `0` without sending. Lets an agent verify a call before a destructive operation.
- `--timeout` per request.
- Secrets are **always** redacted from logs, errors, and `--dry-run` output.

---

## 6. Endpoint reference

The tables below are the contract the Skill (§8) maintains. `Params → flags` uses kebab-case flag names derived from API parameter names (`page_seperator` → `--page-separator`, with the API's spelling preserved on the wire).

**JSON-valued flags** (`--data`, `--custom-data`, `--adapter-metadata`, `--connector-metadata`, `--source-settings`, `--destination-settings`, …) accept either inline JSON or an `@path/to/file.json` reference — large payloads such as an exported prompts file are painful to pass inline and hit shell argument limits. The value is parsed to a real object before it is sent, so a JSON body reaches the API as a nested object, not a quoted string. Invalid JSON fails locally with exit `2`.

### 6.1 LLMWhisperer v2

Base: `https://llmwhisperer-api.{us-central,eu-west}.unstract.com/api/v2` · Auth: `unstract-key`

| Command | Endpoint | Method | Parameters |
| --- | --- | --- | --- |
| `whisper extract` | `/whisper` | POST | `--file` \| `--url` (sets `url_in_post=true`); `--mode {native_text,low_cost,high_quality,form,table}` (default `form`); `--output-mode {layout_preserving,text}` (default `layout_preserving`); `--page-separator` (default `<<<`); `--pages-to-extract` (e.g. `1-5,7,21-`); `--median-filter-size` (int, `low_cost` only); `--gaussian-blur-radius` (int, `low_cost` only); `--line-splitter-tolerance` (float, default `0.4`); `--line-splitter-strategy` (default `left-priority`); `--horizontal-stretch-factor` (float, default `1.0`); `--mark-vertical-lines` / `--mark-horizontal-lines` (bool); `--lang` (default `eng`); `--tag` (default `default`); `--file-name`; `--use-webhook`; `--webhook-metadata`; `--add-line-nos` (bool); `--allow-rotated-text` (bool, default `true`); `--word-confidence-threshold` (float, default `0.3`) |
| `whisper status` | `/whisper-status` | GET | `--whisper-hash` (required) |
| `whisper retrieve` | `/whisper-retrieve` | GET | `--whisper-hash` (required); `--text-only` (bool, default `false`); `--save` |
| `whisper detail` | `/whisper-detail` | GET | `--whisper-hash` (required) |
| `whisper highlights` | `/highlights` | GET | `--whisper-hash` (required); `--lines` (required, e.g. `1-5,7,21-`) |
| `whisper usage` | `/get-usage-info` | GET | — |
| `whisper usage-by-tag` | `/usage` | GET | `--tag` (required); `--from-date` `YYYY-MM-DD`; `--to-date` `YYYY-MM-DD` (defaults to last 30 days) |
| `whisper webhook create` | `/whisper-manage-callback` | POST | `--webhook-name`, `--url`, `--auth-token` (all body fields) |
| `whisper webhook get` | `/whisper-manage-callback` | GET | `--webhook-name` (required) |
| `whisper webhook update` | `/whisper-manage-callback` | PUT | `--webhook-name`, `--url`, `--auth-token` |
| `whisper webhook delete` | `/whisper-manage-callback` | DELETE | `--webhook-name` (required) |

Statuses: `accepted`, `processing`, `processed`, `error`, `retrieved`. `--wait` polls `/whisper-status` until `processed`, then retrieves.

### 6.2 Unstract API Deployments (runtime)

Base: `https://us-central.unstract.com` · Auth: `Authorization: Bearer` · Path: `/deployment/api/{org_id}/{api_name}/`

| Command | Endpoint | Method | Parameters |
| --- | --- | --- | --- |
| `deployment run` | `/deployment/api/{org_id}/{api_name}/` | POST | `--api-name`\* (path); `--org-id` (path, defaults from profile); `--file` (repeatable, ≤32 combined); `--presigned-url` (repeatable, AWS S3 HTTPS only); `--timeout` (0–300, default 0 = async); `--include-metadata` (bool); `--tags` (currently 1 tag; must start with a letter); `--llm-profile-id` (UUID); `--custom-data` (JSON object, addressable as `{{custom_data.key}}`); `--hitl-queue-name`; `--save` (with `--wait`, persists the one-shot result on its single read) |
| `deployment status` | `/deployment/api/{org_id}/{api_name}/` | GET | `--api-name`\* (path); `--org-id` (path, defaults from profile); `--execution-id`\*; `--include-metadata` (bool); `--save` |
| `deployment highlight` | `/deployment/api/{org_id}/{api_name}/highlight/` | GET | `--api-name`\* (path); `--org-id` (path, defaults from profile); `--whisper-hash`\*; `--line-numbers`\* (comma-separated); `--text-extractor-name`\* (e.g. `llm-whisperer-v2`) |

> **Path parameters are flags too.** `org_id` and `api_name` are URL path segments, not query/body parameters, but the §1.1 contract ("every documented parameter is reachable as a flag") covers them. `--org-id` resolves from the active profile when omitted; `--api-name` has no sensible default — one profile serves many deployments — so it is always required. The same rule applies to `--org-id` in §6.3 (Platform v1) and §6.4 (HITL, alongside the required `--class-id`).

Execution statuses: `PENDING`, `EXECUTING`, `COMPLETED`, `STOPPED`, `ERROR`.

> **Implementation note.** `EXECUTING` and `PENDING` currently return **HTTP 422**, not 200 — a documented server-side defect scheduled for correction. The CLI **must branch on the response-body `status` field, never on the HTTP status code**, so behaviour is unchanged when the fix ships. Synchronous mode (`timeout > 0`) is deprecated upstream; `--wait` uses `timeout=0` + polling.
>
> **Status-field shape (load-bearing).** The status GET returns a **top-level `status`** field (`{status, message}`, where `message` carries the result), whereas the run POST nests `execution_status` under `message`. The poll reads the status endpoint, so it matches `status` first. A field mismatch here means the terminal state is not recognised, the one-shot result is consumed unread, and the next poll returns HTTP 406 — this is exactly how `deployment run --wait` once lost results (§3.1 one-shot polling). `run --wait` returns the terminal poll's body and takes `--save` to persist it.

### 6.3 Unstract Platform Management API v1

Base: `{host}/api/v1/unstract/{org_id}` · Auth: `Authorization: Bearer <platform-api-key>`
Permissions: `read` → GET/HEAD/OPTIONS; `read_write` → all but DELETE; `full_access` → all.
Pagination (list endpoints): `--page` (default 1), `--page-size` (default 50, max 1000).

#### Prompt Studio — `unstract docstudio platform prompt-studio`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `list` | `/prompt-studio/` | GET | — |
| `create` | `/prompt-studio/` | POST | `--tool-name`\*, `--description`\*, `--author`\*, `--icon`, `--preamble`, `--postamble`, `--summarize-context`, `--single-pass-extraction-mode`, `--enable-challenge`, `--enable-highlight`, `--custom-data`, `--shared-users`, `--shared-to-org` |
| `get` | `/prompt-studio/{tool_id}/` | GET | `--tool-id`\* |
| `update` / `patch` | `/prompt-studio/{tool_id}/` | PUT / PATCH | as `create`; PATCH all optional |
| `delete` | `/prompt-studio/{tool_id}/` | DELETE | `--tool-id`\* (409 if exported and in use) |
| `export-project` | `/prompt-studio/project-transfer/{tool_id}` | GET | `--tool-id`\*, `--save` |
| `import-project` | `/prompt-studio/project-transfer/` | POST | `--file`\* (multipart) |
| `sync-prompts` | `/prompt-studio/{tool_id}/sync-prompts/` | POST | `--tool-id`\*, `--data`\* (export JSON), `--create-copy` |
| `export-tool` | `/prompt-studio/export/{tool_id}` | POST | `--tool-id`\*, `--is-shared-with-org`, `--user-id` (repeatable), `--force-export` |
| `export-info` | `/prompt-studio/export/{tool_id}` | GET | `--tool-id`\* (204 if never exported) |
| `file upload` | `/prompt-studio/file/{tool_id}` | POST | `--tool-id`\*, `--file`\* (repeatable) |
| `file get` | `/prompt-studio/file/{tool_id}` | GET | `--tool-id`\*, `--document-id`\*, `--view-type {ORIGINAL,EXTRACT,SUMMARIZE}` |
| `file delete` | `/prompt-studio/file/{tool_id}` | DELETE | `--tool-id`\*, `--document-id`\* |
| `prompt create` | `/prompt-studio/prompt-studio-prompt/{tool_id}/` | POST | `--tool-id`\* (**also sent in the body**, see note), `--prompt-key`\*, `--enforce-type {text,number,email,date,boolean,json,line-item,table}` (`date` is locale-ambiguous — see note), `--prompt`, `--sequence-number`, `--prompt-type {PROMPT,NOTES}`, `--active` |
| `prompt get/update/patch/delete` | `/prompt-studio/prompt/{prompt_id}/` | GET/PUT/PATCH/DELETE | `--prompt-id`\* + fields above |
| `prompt reorder` | `/prompt-studio/prompt/reorder/` | POST | `--start-sequence-number`\*, `--end-sequence-number`\*, `--prompt-id`\* |
| `profile list` | `/prompt-studio/prompt-studio-profile/{tool_id}/` | GET | `--tool-id`\* |
| `profile set-default` | `/prompt-studio/prompt-studio-profile/{tool_id}/` | PATCH | `--tool-id`\*, `--default-profile`\* |
| `profile create` | `/prompt-studio/profilemanager/{tool_id}` | POST | `--tool-id`\*, `--profile-name`\*, `--vector-store`\*, `--embedding-model`\*, `--llm`\*, `--x2text`\*, `--chunk-size` (**`0` = whole-document / no-RAG**; see note), `--chunk-overlap`, `--retrieval-strategy {simple,subquestion,fusion,recursive,router,keyword_table,automerging}`, `--similarity-top-k` (max 4 profiles/project) |
| `profile get/update/patch/delete` | `/prompt-studio/profile-manager/{profile_id}/` | GET/PUT/PATCH/DELETE | `--profile-id`\* + fields above |
| `index-document` | `/prompt-studio/index-document/{tool_id}` | POST | `--tool-id`\*, `--document-id`\* |
| `fetch-response` | `/prompt-studio/fetch_response/{tool_id}` | POST | `--tool-id`\*, `--document-id`\*, `--id`\* (prompt), `--run-id`, `--profile-manager` (may be needed even when a default appears set), `--wait` |
| `single-pass` | `/prompt-studio/single-pass-extraction/{tool_id}` | POST | `--tool-id`\*, `--document-id`\*, `--run-id`, `--wait` |
| `task-status` | `/prompt-studio/{tool_id}/task-status/{task_id}` | GET | `--tool-id`\*, `--task-id`\* — status only (`processing`/`completed`/`failed`); the value lands in the Output Manager |
| `output list` | `/prompt-studio/prompt-output/` | GET | `--tool-id`\*, `--prompt-id`, `--document-id`, `--profile-id`, `--is-single-pass-extract` — reads extraction results (backend-sourced, no `.mdx`) |
| `output latest` | `/prompt-studio/prompt-output/latest-by-keys/` | GET | `--tool-id`\* — latest output per prompt key (may return `{}`; prefer `output list`) |
| `users` | `/prompt-studio/users/{tool_id}` | GET | `--tool-id`\* |
| `check-deployment-usage` | `/prompt-studio/{tool_id}/check_deployment_usage/` | GET | `--tool-id`\* |
| `select-choices` | `/prompt-studio/select_choices/` | GET | — |
| `adapter-choices` | `/prompt-studio/adapter-choices/` | GET | — |
| `retrieval-strategies` | `/prompt-studio/{tool_id}/get_retrieval_strategies/` | GET | `--tool-id`\* |

> **`prompt create` sends `tool_id` in the body as well as the path.** The backend's `create_prompt` persists the request body verbatim and ignores the URL `pk`, so a `tool_id` absent from the body is saved as `NULL` — the prompt exists but links to no project and is unreachable (returns 201 with `tool_id: null`, then a 404 on read). The CLI mirrors the path value into the body to link it correctly, and treats a 201 whose response `tool_id` is null as a failure (exit `8`, §5.4) rather than reporting success. The real fix is a backend change.
>
> **`prompt create` also takes `--profile-manager`.** `fetch_response` resolves a prompt's LLM profile from the prompt's own `profile_manager` field and does **not** fall back to the project's default profile, unlike `index-document` and `single-pass`, which both do. A prompt created without one is therefore unrunnable, and fails with a message naming the *project* default — which is genuinely set, making the error actively misleading. Setting it at creation avoids the trap. The backend fix is to fall back to `ProfileManager.get_default_llm_profile(tool)` when the prompt's field is null, matching the two sibling paths; the flag then becomes convenience rather than a requirement.
>
> **`--chunk-size 0` means whole-document / no-RAG.** It skips embedding and the vector DB entirely — the correct mode for short documents, and the escape hatch when the vector DB is unreachable (indexing otherwise 500s on a dead vector DB even for a one-page file). Set `--chunk-overlap 0` alongside it. `--vector-store` and `--embedding-model` stay **required** even so: both columns are `NOT NULL` server-side, so the values are stored but never queried. Pass any valid adapter id. Making them genuinely optional requires a backend change (nullable columns).
>
> **`--enforce-type date` is locale-ambiguous.** Date normalization reads `DD/MM/YYYY` as `MM/DD/YYYY` (`01/08/2025` → `2025-01-08`). For non-US date sources prefer `--enforce-type text`.
>
> **Retrieving results.** `fetch-response` / `single-pass` return `202 {task_id, run_id, status:accepted}` and never return the value directly — it lands in the Output Manager. Read it with `output list` (or `output latest`), or use `--wait` (§3.1) to poll and return it in one call.

#### Workflows — `unstract docstudio platform workflow`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `list` | `/workflow/` | GET | `--project`, `--workflow-owner`, `--is-active`, `--order-by {asc,desc}` |
| `create` | `/workflow/` | POST | `--workflow-name`\* (≤128, unique/org), `--description` (≤490), `--deployment-type {DEFAULT,ETL,TASK,API,APP}`, `--source-settings` (JSON), `--destination-settings` (JSON), `--max-file-execution-count` (≥1), `--shared-to-org`, `--shared-users` |
| `get` / `update` / `patch` / `delete` | `/workflow/{id}/` | GET/PUT/PATCH/DELETE | `--id`\* + fields above (update/delete require owner) |
| `execute` | `/workflow/execute/` | POST | `--workflow-id`\*, `--execution-action {START,NEXT,STOP,CONTINUE}`, `--execution-id` (required for NEXT/STOP/CONTINUE), `--log-guid`, `--file` (repeatable) |
| `toggle-active` | `/workflow/active/{id}/` | PUT | `--id`\* |
| `can-update` | `/workflow/{id}/can-update/` | GET | `--id`\* |
| `clear-file-marker` | `/workflow/{id}/clear-file-marker/` | GET | `--id`\* (mutating GET — flagged in help) |
| `schema` | `/workflow/schema/` | GET | `--type {src,dest}` (default `src`), `--entity {file,api,db}` (default `file`) |
| `users` | `/workflow/{id}/users/` | GET | `--id`\* |
| `execution list` | `/workflow/{id}/execution/` | GET | `--id`\* |
| `execution get` | `/workflow/execution/{id}/` | GET | `--id`\* |
| `execution logs` | `/workflow/execution/{id}/logs/` | GET | `--id`\*, `--file-execution-id` (literal `null` for non-file logs), `--log-level {DEBUG,INFO,WARN,ERROR}`, `--ordering`, `--page`, `--page-size` |
| `file-history list` | `/workflow/{workflow_id}/file-histories/` | GET | `--workflow-id`\*, `--status` (CSV), `--execution-count-min/max`, `--file-path` (prefix), `--page`, `--page-size` |
| `file-history get/delete` | `/workflow/{workflow_id}/file-histories/{id}/` | GET/DELETE | `--workflow-id`\*, `--id`\* |
| `file-history clear` | `/workflow/{workflow_id}/file-histories/clear/` | POST | `--workflow-id`\*, ≥1 of: `--ids` (≤100), `--status`, `--execution-count-min/max`, `--file-path` |

#### Workflow assembly — attach a tool and configure endpoints (deployment prerequisites)

A workflow is only deployable once a tool is attached and both of its endpoints are set to `API`. These commands expose that assembly, which previously required direct API calls. **These endpoints have no public v1 docs page**; their `doc_source` cites the backend routes (like API Hub, §8.4).

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `tool registry list` | `/tool/` | GET | — (each tool's `function_name` is the registry id used below; **not** the Prompt Studio `tool_id`) |
| `tool registry settings-schema` | `/tool_settings_schema/` | GET | `--function-name`\* (reveals required adapter settings, e.g. `challenge_llm`) |
| `workflow tool list` | `/tool_instance/` | GET | `--workflow` (filter) |
| `workflow tool get` | `/tool_instance/{id}/` | GET | `--id`\* (read `metadata` before patching it) |
| `workflow tool add` | `/tool_instance/` | POST | `--workflow`\*, `--tool`\* (registry id) — a workflow holds ≤1 tool; **set the default triad first** (below); attaching activates the workflow |
| `workflow tool set-metadata` | `/tool_instance/{id}/` | PATCH | `--id`\*, `--metadata`\* (JSON; **REPLACES** wholesale — send the complete object, accepts `@file.json`) |
| `workflow tool remove` | `/tool_instance/{id}/` | DELETE | `--id`\* (needs `full_access` — `read_write` cannot delete a tool instance even though it can create one) |
| `workflow endpoint list` | `/workflow/endpoint/` | GET | `--workflow`, `--endpoint-type {SOURCE,DESTINATION}`, `--connection-type` |
| `workflow endpoint set` | `/workflow/endpoint/{id}/` | PATCH | `--id`\*, `--connection-type {API,FILESYSTEM,DATABASE,MANUALREVIEW}`\* |

> **The deploy-a-tool sequence.** `export-tool` (§Prompt Studio) publishes the project to the registry → `tool registry list` gives its `function_name` → `workflow create` (auto-creates both endpoints, `connection_type` null) → `adapter default-triad set` (**must precede** `tool add`, see below) → `workflow tool add` → `workflow endpoint set` both SOURCE and DESTINATION to `API` → `api-deployment create` → `api-deployment key create` → `deployment run`. For an API deployment, endpoints set to `API` (or `MANUALREVIEW`) need no connector/credentials.
>
> **Ordering trap — set the default triad first (CAPTURE2 GAP 3).** `workflow tool add` seeds the tool instance's adapter settings from the org **default triad**. With no triad set, creation returns HTTP 500 *but still persists a half-configured row*; a retry then 400s with "can't have more than one tool." Run `adapter default-triad set` before `workflow tool add`.
>
> **`challenge_llm` trap (CAPTURE2 BUG 4).** An exported tool's settings schema may list `challenge_llm` as required even when the project has `enable_challenge=false`. A tool instance whose `metadata.challenge_llm` is empty then fails deploy-time validation with an opaque enum error (visible only in `workflow execution logs`). Fix it with a read-modify-write: `workflow tool get` → add a valid LLM adapter id (from `settings-schema`'s enum) to the **existing** `metadata` object → `workflow tool set-metadata --metadata @edited.json`. `set-metadata` **replaces** metadata wholesale (the backend does not merge), so sending only the one key would wipe `prompt_registry_id` and orphan the tool — always send the complete object.
>
> **Not shipped: a one-shot `deploy-api` convenience.** CAPTURE2 floated a single command that runs the whole sequence. Deliberately deferred: the individual commands above make the flow CLI-only, and a mega-command would bury the ordering/preflight decisions (triad, endpoint config) that the user needs to see. Revisit if the assembly proves rote in practice.

#### API Deployments (management) — `unstract docstudio platform api-deployment`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `list` | `/api/deployment/` | GET | `--workflow`, `--search`, `--page`, `--page-size` |
| `create` | `/api/deployment/` | POST | `--workflow`\*, `--display-name` (≤30), `--description` (≤255), `--api-name` (`^[a-zA-Z0-9_-]+$`, ≤30, unique/org), `--is-active`, `--shared-to-org`, `--shared-users` — returns `api_key`, one active deployment per workflow |
| `get` / `update` / `patch` / `delete` | `/api/deployment/{id}/` | GET/PUT/PATCH/DELETE | `--id`\* (owner only for writes) |
| `users` | `/api/deployment/{id}/users/` | GET | `--id`\* |
| `by-prompt-studio-tool` | `/api/deployment/by-prompt-studio-tool/` | GET | `--tool-id`\* |
| `postman-collection` | `/api/postman_collection/{id}/` | GET | `--id`\*, `--save` (409 if no active key) |
| `key list` / `key create` | `/api/keys/api/{api_id}/` | GET / POST | `--api-id`\*; create: exactly one of `--api` / `--pipeline`, `--description` (≤255), `--is-active` |
| `key get` / `key update` / `key delete` | `/api/keys/{id}/` | GET/PUT/DELETE | `--id`\*, `--is-active`, `--description` |

#### ETL / Task Pipelines — `unstract docstudio platform pipeline`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `list` | `/pipeline/` | GET | `--type {ETL,TASK,DEFAULT,APP}`, `--workflow`, `--search`, `--ordering {created_at,last_run_time,pipeline_name,run_count}` (`-` prefix = desc), `--page`, `--page-size` |
| `create` | `/pipeline/` | POST | `--pipeline-name`\* (≤32, unique/org), `--workflow`\*, `--pipeline-type`, `--cron-string` (min interval enforced), `--shared-users`, `--shared-to-org` |
| `get` / `update` / `patch` / `delete` | `/pipeline/{id}/` | GET/PUT/PATCH/DELETE | `--id`\*; PATCH also `--active` |
| `execute` | `/pipeline/execute/` | POST | `--pipeline-id`\*, `--execution-id` |
| `executions` | `/pipeline/{id}/executions/` | GET | `--id`\*, `--start-date`, `--end-date` (ISO 8601), `--page`, `--page-size` |
| `users` | `/pipeline/{id}/users/` | GET | `--id`\* |
| `postman-collection` | `/pipeline/api/postman_collection/{id}/` | GET | `--id`\*, `--save` (400 if no active key) |
| `key list` / `key create` | `/api/keys/pipeline/{pipeline_id}/` | GET / POST | `--pipeline-id`\*; create: exactly one of `--pipeline` / `--api`, `--description`, `--is-active` |

#### Adapters — `unstract docstudio platform adapter`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `supported` | `/supported_adapters/` | GET | `--adapter-type {LLM,EMBEDDING,VECTOR_DB,X2TEXT,OCR}`\* |
| `schema` | `/adapter_schema/` | GET | `--id`\* (SDK identifier, e.g. `openai_llm`) |
| `test` | `/test_adapters/` | POST | `--adapter-id`\*, `--adapter-metadata`\* (JSON), `--adapter-type`\* |
| `list` | `/adapter/` | GET | `--adapter-type` |
| `create` | `/adapter/` | POST | `--adapter-name`\* (≤128), `--adapter-id`\*, `--adapter-type`\*, `--adapter-metadata`\* (JSON, encrypted at rest), `--description`, `--shared-to-org` |
| `get` | `/adapter/{id}/` | GET | `--id`\* (returns decrypted metadata) |
| `update` / `patch` / `delete` | `/adapter/{id}/` | PUT/PATCH/DELETE | `--id`\*; PATCH also `--shared-users` (replaces list); delete 409 if in use, 500 if default |
| `info` | `/adapter/info/{id}/` | GET | `--id`\* (includes `context_window_size`) |
| `users` | `/adapter/users/{id}/` | GET | `--id`\* |
| `default-triad get` | `/adapter/default_triad/` | GET | — |
| `default-triad set` | `/adapter/default_triad/` | POST | `--llm-default`, `--embedding-default`, `--vector-db-default`, `--x2text-default` (note: request keys differ from response keys) |

#### Connectors — `unstract docstudio platform connector`

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `supported` | `/supported_connectors/` | GET | `--type {INPUT,OUTPUT}`, `--connector-mode {FILE_SYSTEM,DATABASE}` |
| `schema` | `/connector_schema/` | GET | `--id`\* |
| `test` | `/test_connectors/` | POST | `--connector-id`\*, `--connector-metadata`\* (JSON) |
| `list` | `/connector/` | GET | `--workflow`, `--created-by`, `--connector-type`, `--connector-mode` |
| `create` | `/connector/` | POST | `--connector-name`\* (≤128), `--connector-id`\*, `--connector-metadata` (JSON), `--connector-version`, `--shared-to-org`, `--shared-users`, `--oauth-key` (query) |
| `get` / `update` / `patch` / `delete` | `/connector/{id}/` | GET/PUT/PATCH/DELETE | `--id`\*; delete 409 if used by a workflow |
| `oauth-cache-key` | `/api/v1/oauth/cache-key/{backend}` | GET | `--backend`\* (e.g. `google-oauth2`); **not org-scoped** |

#### Groups, users, sharing

| Command | Endpoint | Method | Key parameters |
| --- | --- | --- | --- |
| `group list` / `group create` | `/groups/` | GET / POST | `--name`\* (unique/org), `--description` |
| `group patch` / `group delete` | `/groups/{id}/` | PATCH / DELETE | `--id`\* (**int**, not UUID); DELETE needs `full_access` |
| `group member list` / `add` | `/groups/{id}/members/` | GET / POST | `--id`\*; add: `--user-ids`\* (ints, idempotent) |
| `group member remove` | `/groups/{id}/members/{user_id}` | DELETE | `--id`\*, `--user-id`\* (no trailing slash; needs `full_access`) |
| `group resources` | `/groups/{id}/resources/` | GET | `--id`\* |
| `user list` | `/users/` | GET | — (`id` returned as **string**; cast to int for `shared_users`) |
| `share` | `/{resource}/{id}/share/` | POST | `--resource`\* (**enum**, see below), `--id`\*, `--shared-users`, `--shared-groups`, `--shared-to-org` (each axis is **replace**, not append) |

> **`--resource` is an enum, never free text.** The URL path segment is not guessable from the friendly resource name, so an agent passing `api-deployment` would get a 404. The flag accepts friendly names and maps them to exact path segments, with the mapping enumerated in `--help` and `--discover`:
>
> | `--resource` value | Path segment |
> | --- | --- |
> | `adapter` | `adapter` |
> | `connector` | `connector` |
> | `workflow` | `workflow` |
> | `pipeline` | `pipeline` |
> | `api-deployment` | `api/deployment` |
> | `prompt-studio` | `prompt-studio` |

> **Sharing semantics.** `shared_users` replaces rather than appends. The CLI provides `--add-user` / `--remove-user` convenience flags that read current state first and send the merged list, and documents the replace behaviour of the raw flag in help text.

### 6.4 Human Quality Review (Enterprise)

Base: `{host}/mr/api/{org_id}` · Auth: `Authorization: Bearer`

| Command | Endpoint | Method | Parameters |
| --- | --- | --- | --- |
| `hitl approved get` | `/approved/result/{class_id}/` | GET | `--class-id`\*, `--hitl-queue-name`, `--save` — **dequeue: consumes one item per call** |
| `hitl bulk-download` | `/approved/result/{class_id}/` | GET | `--class-id`\*, `--page` (default 1), `--page-size` (1–500, default 50), `--download-files` (bool), `--email` (async notification) |
| `hitl download-status` | `/approved/download-status/{job_id}/` | GET | `--job-id`\* |

Pushing to HITL is `unstract docstudio deployment run --hitl-queue-name <name>` (§6.2), which returns `QUEUED` + `execution_id`.

### 6.5 API Hub (Verticals)

Auth: `apikey: <key>` (Kong gateway). Optional passthrough: `--llmwhisperer-key` → `X-LLMWhisperer-API-Key`, `--anthropic-key` → `X-Anthropic-API-Key` (bring-your-own-key). Subscription/user headers are gateway-injected and never sent by the CLI (§4.4).
**Source of truth: code + Postman collections, not public docs** (see §8.4).

| Command | Endpoint | Method | Parameters |
| --- | --- | --- | --- |
| `apihub extract` | `/api/v1/extract` | POST | `--vertical`\* (e.g. `table`), `--sub-vertical`\* (`bank_statement`, `discover_tables`, `extract_table`), `--file` \| `--use-cached-file-hash`; **conversion passthrough** (`conv_*`): `--conv-mode` (default `high_quality`), `--conv-output-mode` (default `layout_preserving`), `--conv-lang`, `--conv-tag`, `--conv-filename`, `--conv-page-separator`, `--conv-pages-to-extract`, `--conv-median-filter-size`, `--conv-gaussian-blur-radius`, `--conv-mark-vertical-lines`, `--conv-mark-horizontal-lines`, `--conv-line-splitter-strategy`, `--conv-line-splitter-tolerance`, `--conv-horizontal-stretch-factor`; **extraction** (`ext_*`): `--ext-section-name`, `--ext-compress-double-space`, `--ext-headers`, `--ext-start-page`, `--ext-end-page`, `--ext-page-filter-strategy`, `--ext-use-bank-schema`, `--ext-pattern {generic_table,indent_as_groups}`, `--ext-table-no`, `--ext-cache-result`, `--ext-cache-text` |
| `apihub status` | `/api/v1/status` | GET | `--file-hash`\* |
| `apihub retrieve` | `/api/v1/retrieve` | GET | `--file-hash`\*, `--output-mode {raw,full}` (default `full`), `--sub-vertical`, `--save` |
| `apihub doc-splitter upload` | `/doc-splitter/documents/upload` | POST | `--file`\* |
| `apihub doc-splitter status` | `/doc-splitter/jobs/status` | GET | `--job-id`\* |
| `apihub doc-splitter download` | `/doc-splitter/jobs/download` | GET | `--job-id`\*, `--save` |

Statuses: `QUEUED_FOR_WHISPER` → `QUEUED_FOR_EXTRACTION` → `COMPLETED`. The `conv_*` prefix maps onto LLMWhisperer parameters; `ext_*` parameters are forwarded verbatim to the vertical worker, so the CLI accepts an escape hatch `--ext-param KEY=VALUE` (repeatable) for parameters newer than the CLI.

---

## 7. Architecture

```
unstract_cli/
├── __main__.py             # entry point
├── app.py                  # Click root group; command tree built from definitions
├── config/
│   ├── profile.py          # profile model, resolution order (flag > env > file > default)
│   └── loader.py           # TOML load/save, env: indirection, permission checks
├── core/
│   ├── http.py             # request execution, retry/backoff, redaction
│   ├── errors.py           # HTTP status → exit code + structured error mapping
│   ├── output.py           # json/yaml/table/raw renderers, TTY detection
│   ├── poll.py             # --wait state machines (body-status based, never HTTP code)
│   └── generate.py         # endpoint definition → Click command
├── endpoints/              # ← SINGLE SOURCE OF TRUTH (the Skill's edit target)
│   ├── whisper.py
│   ├── deployment.py
│   ├── platform_prompt_studio.py
│   ├── platform_workflow.py
│   ├── platform_deployment.py
│   ├── platform_pipeline.py
│   ├── platform_adapter.py
│   ├── platform_connector.py
│   ├── platform_groups.py
│   ├── hitl.py
│   └── apihub.py
└── skills/                 # bundled Claude Skill (§8)
```

### 7.1 Endpoint definition shape

```python
Endpoint(
    name="extract",
    group="whisper",
    method="POST",
    path="/whisper",
    product="llmwhisperer",
    summary="Convert a document to LLM-ready text.",
    doc_source="llmwhisperer-docs/docs/llm_whisperer/apis/whisper.md",  # skill anchor
    params=[
        Param("mode", str, default="form",
              choices=["native_text", "low_cost", "high_quality", "form", "table"],
              location="query", help="Processing mode."),
        Param("word_confidence_threshold", float, default=0.3, location="query",
              help="Minimum OCR confidence (0-1); works only with form/high_quality/table."),
        ...
    ],
    body="binary_file",
    returns="whisper_hash",
    poll=PollSpec(status_endpoint="whisper.status",
                  terminal_success=("processed",), terminal_failure=("error",),
                  handle_field="whisper_hash", handle_param="whisper_hash",
                  retrieve_endpoint="whisper.retrieve", one_shot=True),
)
```

`PollSpec.status_field` may be a single field name **or a tuple of candidates tried in order**, because the run POST and the status GET can spell the same state differently — the deployment poll uses `("status", "execution_status")` so the status GET's top-level `status` wins while the run POST's nested `execution_status` still resolves (§6.2). A mismatch here silently consumes a one-shot result; the tuple is what prevents it.

`PollSpec` also carries the fields that make a two-step retrieve work when the result store is not keyed by the poll handle: `retrieve_carry` forwards identifiers from the *original* request into the retrieve call (each entry a py_name, or a `(source, dest)` pair that renames it), `retrieve_extra` injects a constant flag the original request did not carry, and `retrieve_omits_handle` drops the handle from a retrieve endpoint that has no such parameter. Prompt Studio's `--wait` uses all three: it reads the Output Manager by the original `tool_id`, renames `id → prompt_id`, and omits the `task_id` handle (§3.1, §6.3). When `one_shot=True` and no `retrieve_endpoint` is set, the poll that first reaches a terminal state *is* the retrieve: its body is returned as the result and no second read is issued (deployment run, §6.2).

Records may also mirror a PATH identifier into the body — `Param.mirror_to_body` under the same name, or `Param.mirror_as="<body_name>"` where the body spells the identifier differently — and assert non-null response fields on success (`Endpoint.require_response_fields`). `prompt create` uses the first pair to defeat the tool-linking defect (§6.3); `api-deployment key create` uses the rename, sending its `api_id` path value as the body's `api` so the caller supplies one identifier rather than the same value twice. `Param.default_from` accepts a whitespace-separated **fallback chain** resolved left to right (`"deployment.org_id platform.org_id"`), for settings that legitimately live in two blocks where one is typically empty; credentials deliberately do not chain, since borrowing a key across API groups would be credential confusion. `Constraint` also covers conditional requirement (`RequiredUnless`) alongside `MutuallyExclusive` / `AtLeastOneOf`. `ParamType.JSON` values are parsed automatically (inline or `@file.json`, §6).

The command tree, `--help`, validation, `--discover`, and the docs-diff in §8 all derive from these records. Adding an endpoint means adding one record — no separate command wiring, so help text cannot drift from behaviour.

> **Note (doc drift, pre-existing):** the file tree above lists per-resource modules (`platform_prompt_studio.py`, `platform_workflow.py`, …); the implementation consolidated the Platform surface into a single `endpoints/platform.py`. This predates the changes described here and is left as-is pending a broader §7 refresh.

---

## 8. Claude Skill: `update-unstract-cli`

Location: `unstract-cli/.claude/skills/update-unstract-cli/SKILL.md`

### 8.1 Purpose

Keep the CLI's endpoint definitions synchronized with the public API documentation by cross-referencing the docs repos, detecting drift, and applying the corresponding edits to `endpoints/`.

### 8.2 Inputs

| Source | Repo / path | Covers |
| --- | --- | --- |
| LLMWhisperer docs | `llmwhisperer-docs/docs/llm_whisperer/apis/*.md` | §6.1 |
| Unstract API deployment docs | `unstract-docs/docs/unstract_platform/api_deployment/*.md` | §6.2 |
| Unstract Platform v1 docs | `unstract-docs/docs/unstract_platform/api_documentation/versions/v1-*.mdx` | §6.3 |
| HITL docs | `unstract-docs/docs/unstract_platform/human_quality_review/*.md` | §6.4 |
| API Hub source | `unstract-verticals/src/api_v1/api.py`, `verticals-portal/portal/postman-collection/*.json` | §6.5 (no public docs — see §8.4) |

Repo paths are configurable; the Skill falls back to the GitHub API when a repo is not checked out locally.

### 8.3 Procedure

1. **Parse the documentation.** Extract endpoint tables from Markdown and `<ApiEndpoint>` / `<ApiSection>` MDX components into a normalized set of `(method, path, params[])` records. Each `Endpoint.doc_source` anchors a definition to its documentation file.
2. **Parse the CLI definitions.** Load `endpoints/*.py` into the same normalized shape.
3. **Diff** on three axes:
   - endpoints present in docs but missing from the CLI (**new capability**);
   - endpoints in the CLI but absent from docs (**possible removal or deprecation** — report, never auto-delete);
   - per-parameter drift: added, removed, renamed, or changed type / default / enum / required-ness. A documented parameter supplied by mirroring (`Param.mirror_as`, §7.1) is **not** drift — it reaches the wire under its documented name without a flag of its own. A record carrying `doc_conflict` is exempt from this axis as well as the previous one, so a deliberately dropped parameter is not re-reported every run.
4. **Report** as a structured table before editing: each difference with its doc citation (file + heading) and the proposed CLI change.
5. **Apply** by editing `endpoints/*.py` only. Because commands are generated (§7.1), no command-wiring, help-text, or `--discover` changes are needed — this is why the diff can be applied mechanically.
6. **Verify:** run `unstract --discover` and confirm it parses and includes the new surface; run the test suite; run `ruff`/`mypy`.
7. **Summarize:** what changed, what needs human judgement (renames, breaking changes), and what was intentionally skipped.

### 8.4 Known limitation — API Hub

API Hub has **no public documentation site**. The Skill cannot cross-reference it against public docs and instead diffs against `unstract-verticals/src/api_v1/api.py` (`request.args.get(...)` calls) and the Postman collections in `verticals-portal`. This is lower-fidelity: it recovers parameter names and defaults but not prose descriptions. The Skill must **flag API Hub changes for human review** rather than applying them silently, and the `--ext-param KEY=VALUE` escape hatch (§6.5) exists precisely so agents are not blocked by this lag.

### 8.5 Safety rules

- Never delete a command solely because it is absent from the docs — documentation lags implementation. Report and let a human decide.
- Never invent parameters not present in a source; every change cites a specific file and heading.
- Treat renames as breaking: propose an alias with a deprecation note rather than a silent rename.
- Preserve hand-written `help` text where it is richer than the docs; add rather than overwrite.
- **Docs are not infallible.** Where an endpoint index and its detail page disagree, prefer the detail page and the official client libraries, and check §11 "Resolved during drafting" for known documentation defects before proposing a change. A definition may carry a `doc_conflict` note recording a deliberate divergence; the Skill must not revert one without explicit human confirmation. Such a note covers a dropped parameter as well as a divergent path — `api-deployment key create` omits the documented `api`/`pipeline` body flags because the route already fixes the resource and the value is mirrored from the path (§7.1).

---

## 9. Testing

| Layer | Approach |
| --- | --- |
| Definition integrity | Every `Endpoint` has a unique `(group, name)`, valid `doc_source`, and complete param metadata |
| Command generation | Tree builds; `--help` renders at every level; `--discover` is valid JSON and covers every endpoint |
| HTTP | `respx`/`responses` fixtures per endpoint using the documented sample payloads |
| Exit codes | Table-driven: each HTTP status maps to the §5.4 code |
| Polling | `--wait` reaches terminal state; branches on **body status**, not HTTP code (asserted explicitly against the 422 defect). Prompt Studio `--wait` polls `task-status` then retrieves from the Output Manager keyed on the original `tool_id`, narrowed to the run's prompt + document. Deployment `--wait` recognises the status GET's top-level `status`, returns the terminal poll's body on the *first* terminal poll (asserted `call_count == 1` — no double-consume), and `run --wait --save` persists it |
| One-JSON-document output | A response body of several concatenated JSON objects is recovered into a single array, so `\| json` never breaks; a genuinely malformed body falls back to raw text |
| JSON parameters | A `ParamType.JSON` flag resolves to a nested object in the body (not a quoted string); accepts inline JSON and `@file.json`; invalid JSON exits `2`; a FORM-located JSON field is re-serialized to a string so multipart encoding still works |
| One-shot semantics | Second retrieve yields exit `9`; `--save` persists before exit |
| Error stream | On failure with stdout not a TTY, the structured envelope appears on **both** stdout and stderr; on a TTY, stderr only. Covers our errors, Click's own parse errors (subprocess-tested at the real entry point), and `--discover` no-match |
| Response guards | A 2xx that leaves a `require_response_fields` field null (e.g. `prompt create` → `tool_id`) is treated as a failure (exit `8`), not success |
| Redaction | No secret appears in any output stream, including `--dry-run`, `-vv`, and `config doctor` |
| Non-interactivity | No command reads stdin unless explicitly asked (`--file -`); no TTY prompts |

---

## 10. Delivery phases

| Phase | Scope |
| --- | --- |
| **1** | Core: config/profiles, HTTP layer, output/errors/exit codes, definition→command generation, `--discover` |
| **2** | LLMWhisperer (§6.1) + `--wait` + one-shot handling — smallest complete product surface |
| **3** | API Deployments runtime (§6.2) + HITL (§6.4) |
| **4** | Platform Management v1 (§6.3) — largest surface |
| **5** | API Hub (§6.5) incl. `--ext-param` escape hatch |
| **6** | Claude Skill (§8), packaging, shell completions |

---

## 11. Open questions

1. **API Hub public base URL.** Tenancy is resolved (§4.4): callers send `apikey`; Kong injects subscription/user headers from Redis. The externally routable hostname still needs confirmation from the deployment charts — `--base-url` / `UNSTRACT_APIHUB_BASE_URL` is required until then.
2. **Platform API `full_access` permission level.** The v1 overview lists `read`, `read_write`, `full_access`, but the Platform-Keys page exposes only `read` and `read_write` in its UI enum. Since `DELETE` commands require `full_access`, confirm whether it is UI-creatable — if not, every `delete` command is unusable with a UI-issued key and help text must say so.

### Resolved during drafting

- **Multi-doc chat APIs are excluded from v1** (confirmed). `/md/file/upload`, `/md/file/search`, and `/md/chat` (base `https://us-central.unstract.com/api/v1`, auth `Authorization: Bearer <platform_key>`) are documented but carry `draft: true`. Draft endpoints may change without notice, and including them would make the update-Skill churn against an unstable contract. When the draft flag is lifted, they belong under a new `unstract chat {upload,search,ask}` group. **The Skill must not auto-add these endpoints** while `draft: true` is present in their front matter — treat the flag as an exclusion marker.
- **`/whisper-detail` vs `/whisper-details`.** The docs index says `/whisper-details`; the endpoint page and the official `llm-whisperer-python-client` (`client_v2.py:349`) both use **`/whisper-detail`** (singular). Pinned to the singular form; the docs index is wrong, and the Skill must not "correct" this back from the index page.
