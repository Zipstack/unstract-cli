# Unstract CLI — Implementation Plan

**Companion to:** [`SPEC.md`](./SPEC.md) (Draft v1)
**Status:** Ready to execute
**Scope:** Build sequence, de-risking order, and per-phase acceptance criteria.

> This plan deliberately does **not** restate the endpoint tables (SPEC §6), the file layout (§7), or the phase list (§10). Those are the contract; this document is the *order of operations* and the definition of "done" for each step. Where the two disagree, SPEC.md wins.

---

## 1. The central bet, and how we de-risk it

Everything in the spec rests on one architectural bet:

> The command tree, `--help`, validation, `--dump-commands`, and the Skill's docs-diff are all **generated** from declarative `Endpoint` records (§7.1).

If that abstraction does not hold, the design collapses into per-command special-casing — which would also destroy the Skill's diffability (§8.5), because the definitions would no longer be a faithful description of behaviour. Every sequencing decision below follows from protecting that bet.

**Two command categories.** The bet covers endpoint-backed commands, which is nearly all of them. It is worth stating the exception explicitly, because an endpoint-centric plan makes the non-generated commands easy to lose:

| Category | Commands | Source | Built in |
| --- | --- | --- | --- |
| **Generated** | `whisper`, `deployment`, `platform`, `hitl`, `apihub` | `Endpoint` records → `generate.py` | Phases 2–5 |
| **Hand-authored** | `config {init,list,get,set,use,current,path}`, `completion {bash,zsh,fish}` | Plain Typer commands; map to no API endpoint | Phase 1 (M1.5), Phase 6 |

Both appear in `--dump-commands`; hand-authored ones are flagged as non-endpoint so an agent can tell local operations from remote calls.

Two consequences drive the plan's shape:

1. **Model before breadth.** The `Endpoint`/`Param` schema must be proven against the *hardest* parameter patterns in §6 before we add a hundred endpoints on top of it. See §2.
2. **Walking skeleton before product surfaces.** One trivial endpoint flows end-to-end before any real surface is built. See Phase 1, M1.3.

### 1.1 Risk register

| # | Risk | Impact if unhandled | Mitigation | Resolved by |
| --- | --- | --- | --- | --- |
| R1 | `Param` schema cannot express §6's special cases | Generation degrades into per-command hacks; Skill diff breaks | Pattern enumeration is the *first* task; schema must encode all of §2 | Phase 1 exit |
| R2 | `--output table` has no universal shape for nested JSON | Either ugly output or per-command rendering code | Decide the strategy in Phase 1 — it may add a field to the data model | Phase 1 exit |
| R3 | `--dump-commands` can't recover full flag metadata | The core LLM-discoverability promise (§5.3) fails | **Retired** — verified, see §1.2 | ✅ |
| R4 | Deployment 422 defect (§6.2) misread as failure | `--wait` breaks now, or breaks later when the defect is fixed | Poll on **body `status`**, never HTTP code; assert both in tests | Phase 3 |
| R5 | One-shot retrieval consumed by a retry (§5.6) | Silent, unrecoverable data loss for an agent | Retries never replay a consumed read; `--save` writes before exit | Phase 2 |
| R6 | API Hub public base URL unknown (§11.1) | `apihub` group unusable out of the box | `--base-url` / env required; no baked default | Phase 5 entry |
| R7 | `full_access` may not be UI-creatable (§11.2) | Every `delete` command unusable with a UI-issued key | Confirm at Phase 4 entry; if confirmed, say so in help text | Phase 4 entry |

### 1.2 Pre-verified during planning

**R3 is retired.** Click 8.3.1 / Typer 0.24.1 `Parameter.to_info_dict()` returns exactly what §5.3 requires:

```
{'name': 'mode', 'opts': ['--mode'],
 'type': {'param_type': 'Choice', 'choices': ('form','table')},
 'required': False, 'multiple': False, 'default': 'form',
 'help': 'Processing mode.', 'is_flag': False, ...}
```

Type, choices, default, required-ness, repeatability, and help text all round-trip. `--dump-commands` can therefore be built by walking the generated Click tree.

**Design note.** Even so, `--dump-commands` emits from the **`Endpoint` records plus** the introspected tree, because the records carry what Click cannot know: the underlying HTTP method/path, `doc_source`, permission level, and one-shot semantics. Introspection alone would produce a CLI description; the records make it an *API* description, which is what an agent needs.

---

## 2. Task 0 — Parameter pattern enumeration (do this first)

The `Endpoint`/`Param` sketch in §7.1 ends in `...`, and the ellipsis hides the hard part. Before writing `generate.py`, confirm the schema expresses every pattern present in §6. **This is the load-bearing design work of the whole project.**

| # | Pattern | Instances in §6 | Schema requirement |
| --- | --- | --- | --- |
| P1 | Mutually exclusive (XOR) | `whisper extract` `--file`\|`--url`; `apihub extract` `--file`\|`--use-cached-file-hash`; `key create` `--api`\|`--pipeline` | Endpoint-level `constraints=[MutuallyExclusive(...)]`, validated pre-flight → exit 2 |
| P2 | At-least-one-of | `file-history clear` (≥1 filter) | `constraints=[AtLeastOneOf(...)]` |
| P3 | Enum → path-segment mapping | `share --resource` (`api-deployment` → `api/deployment`) | `Param.choices` as `{friendly: wire}` mapping, not a flat list |
| P4 | Repeatable | `--file`, `--presigned-url`, `--shared-users`, `--user-id`, `--ids` | `Param.multiple=True` |
| P5 | Arbitrary key=value passthrough | `apihub --ext-param KEY=VALUE` (§6.5) | `Param.freeform_prefix="ext_"`; merged into query at request build |
| P6 | Path param w/ profile default | `--org-id` (defaults from profile), `--api-name` (required, no default) | `location="path"` + `default_from="profile.<product>.org_id"` |
| P7 | Location variants | query / JSON body / multipart / binary octet-stream / header | `Param.location ∈ {query,body,path,header,form}`; `Endpoint.body ∈ {json,multipart,binary_file,text,none}` |
| P8 | PATCH = PUT minus required | Prompt Studio, workflow, pipeline, adapter, connector | **Derive**: `Endpoint.derive_patch_from=<put_endpoint>` strips required-ness. Never duplicate records — duplication is how definitions drift |
| P9 | Conditional applicability (help-only) | `--median-filter-size` / `--gaussian-blur-radius` (`low_cost` only); `--allow-rotated-text` (form/high_quality/table only) | `Param.applies_when="mode=low_cost"` — rendered into help, **never enforced** (server owns that rule) |
| P10 | Int vs UUID identifiers | `group` IDs are `int`; nearly everything else is UUID | `Param.type` distinguishes; validation surfaces mismatch as exit 2, not a 404 |
| P11 | Trailing-slash sensitivity | `profilemanager` vs `profile-manager`; `groups/{id}/members/{user_id}` (no slash) | `Endpoint.path` is **literal and authoritative**; no normalization anywhere in the stack |
| P12 | Replace-vs-append semantics | `shared_users`, `shared_groups` (replace) | `--add-user`/`--remove-user` are read-modify-write helpers layered *above* the raw flag; raw flag documented as replace |

**Exit criterion:** a written mapping from each of P1–P12 to a concrete field on `Endpoint`/`Param`, with one representative record hand-written per pattern. If a pattern cannot be expressed declaratively, that is a design finding to resolve *now*, not at Phase 4.

---

## 3. Phases

Phases match SPEC §10. Each has entry conditions, work items, and a **testable** exit criterion.

### Phase 0 — Scaffolding

Repo skeleton per §7, matching sibling-repo conventions (verified: `unstract` and `unstract-verticals` both use Python 3.12, ruff `line-length = 90`, mypy).

- `pyproject.toml`: `requires-python = ">=3.12"`, deps `typer`, `httpx`, `pyyaml`, `rich`; dev deps `pytest`, `respx`, `ruff`, `mypy`. Stdlib `tomllib` for reading, `tomli-w` for writing config.
- **Pin `click` and `typer` to compatible major versions.** R3's retirement rests specifically on Click's `Parameter.to_info_dict()` shape (§1.2); a future Click major that reshapes that dict would silently degrade `--dump-commands`. Treat `to_info_dict()` as a tested contract surface — a Phase 1 test asserts the keys we depend on still exist, so a dependency bump fails loudly rather than quietly.
- Console entry point `unstract = unstract_cli.__main__:main`.
- Package skeleton per §7 (empty modules), `.claude/skills/` placeholder, pre-commit with ruff + mypy.
- CI: lint, type-check, test on 3.12.

**Exit:** `unstract --version` runs; `ruff` and `mypy` pass clean on the skeleton.

---

### Phase 1 — Core (the whole spine, proven on one endpoint)

**Entry:** Task 0 complete.

This is the phase that matters. Everything after it is mostly adding records.

#### M1.1 — Data model
`Endpoint`, `Param`, `PollSpec`, `Constraint` types encoding all of P1–P12. Frozen dataclasses; no runtime mutation (the records are a contract, and the Skill edits them as source).

#### M1.2 — Config & profiles (§4)
- TOML load from `~/.config/unstract/config.toml`, `UNSTRACT_CONFIG` override.
- Resolution chain **flag → env → profile → default**, implemented once as a single resolver used by every parameter. Not re-implemented per command.
- `env:VAR_NAME` indirection; `0600` on write; warn on broader permissions.
- Per-product config blocks (`whisper`, `platform`, `deployment`, `apihub`) — they have genuinely different hosts and keys.
- **Fully usable with zero config file** (§4.3) — asserted by a test that runs with only env vars set.

#### M1.3 — Walking skeleton ⭐
The single most important milestone. Take **`whisper usage`** — GET, no parameters, no path params, trivial response — and drive it end-to-end:

```
Endpoint record → generate.py → Typer command → auth injection
  → http.py → response → output.py → exit code
```

Nothing else is built until this works. It proves the spine with the least possible code.

#### M1.4 — Cross-cutting spine
Built once, against the skeleton, because these touch every command:

- **Auth injection** — strategy per product (§4.4): `unstract-key`, `Bearer`, `apikey`. API Hub sends `apikey` **only**; the `X-Subscription-*` / `X-User-Id` headers are Kong-injected and must never be sent (§4.4).
- **Path-param substitution** — literal paths, profile-default resolution for `--org-id`.
- **`http.py`** — httpx, retry with exponential backoff + jitter on 429/5xx only, never on 4xx; `--max-retries`, `--no-retry`, `--timeout`.
- **`errors.py`** — HTTP status → §5.4 exit code + §5.5 structured JSON on stderr, with `hint` and `retryable`.
- **`output.py`** — json/yaml/table/raw; **TTY detection** (json when not a TTY); stdout carries payload *only*, diagnostics to stderr.
- **Redaction** — a single choke point applied to logs, errors, and `--dry-run`. Centralized so it cannot be forgotten per-command.
- **`--dry-run`** — resolved request as JSON, exit 0, nothing sent.

#### M1.5 — `config` command group (hand-authored)
`config {init,list,get,set,use,current,path}` (§3). These map to **no API endpoint** — they operate on the M1.2 config layer — so they are hand-authored Typer commands rather than generated ones. They belong in Phase 1 because they are how a user or agent bootstraps every other command.

- `init` scaffolds a config file at the default path (`0600`), pre-populated with cloud-us/cloud-eu profile stubs using `env:` indirection — never inline secrets.
- `use` sets `default_profile`; `current` prints the resolved active profile; `path` prints the file location.
- `list`/`get`/`set` read and write profile values.
- Per §5.2, `init` never prompts: an existing file is left untouched unless `--force` is given.

#### M1.6 — R2 spike: `--output table`
Decide and implement the strategy for heterogeneous nested JSON. Recommended: generic rule (list-of-objects → columns from shared keys; single object → key/value pairs; nested → JSON-encoded cell), with an optional `Endpoint.table_columns` hint for responses where the generic rule reads poorly. **If a hint field is needed, it must be added to the model now**, not retrofitted.

#### M1.7 — `--dump-commands`
Emit records + introspected tree (per §1.2). Include: command path, summary, HTTP method + path, every flag with type/default/enum/required/repeatable, `doc_source`, permission level, one-shot flag.

**Exit criteria (all testable):**
1. `unstract whisper usage` succeeds end-to-end against a respx fixture.
2. `unstract --dump-commands` emits valid JSON containing that command with complete flag metadata.
3. Exit-code table (§5.4) asserted table-driven: each HTTP status → correct code.
4. Zero-config operation works with env vars only.
5. No secret appears in `--dry-run`, `-vv`, or error output.
6. Schema expresses P1–P12 (one representative record each, unit-tested).
7. `config init` writes a `0600` file; `config use` switches the default profile; `config current` reflects the change. Both categories appear in `--dump-commands`, with hand-authored commands flagged as non-endpoint.

---

### Phase 2 — LLMWhisperer (§6.1)

**Entry:** Phase 1 exit criteria met.

Smallest *complete* product surface — 11 commands — and the first real test of "adding an endpoint is adding a record."

- All §6.1 records: `extract`, `status`, `retrieve`, `detail`, `highlights`, `usage`, `usage-by-tag`, `webhook {create,get,update,delete}`.
- `--file` XOR `--url` (P1); `url_in_post=true` set automatically when `--url` is used.
- Binary `application/octet-stream` upload path (P7).
- **`--wait`** state machine (`core/poll.py`): poll `/whisper-status` until `processed`/`error`, then retrieve. `--poll-interval` (3s), `--timeout` (300s). On timeout → exit 7 with `whisper_hash` on stdout so the agent can resume.
- **One-shot handling (R5)**, mandatory: `--save` writes atomically *before* exit; a consumed result → exit 9 with an explanatory `hint`; retry logic must never replay a consumed read; help text states the one-shot behaviour.
- Pin `/whisper-detail` singular, carrying a `doc_conflict` note (§11 resolved) so the Skill won't "correct" it back.

**Exit:**
1. All 11 commands appear in `--dump-commands` with full metadata.
2. `--wait` reaches a terminal state against respx fixtures.
3. Second `retrieve` of the same hash → exit 9; `--save` file exists and is complete.
4. Every §6.1 parameter is reachable as a flag (asserted by diffing flags against the record set).

---

### Phase 3 — Deployments runtime (§6.2) + HITL (§6.4)

**Entry:** Phase 2 exit criteria met.

- `deployment {run,status,highlight}` with `--api-name` required and `--org-id` profile-defaulted (P6).
- Multipart upload, ≤32 files combined across `--file` and `--presigned-url`; validate client-side → exit 2 rather than a wasted round trip.
- **R4 — the 422 defect.** `--wait` polls on the **response-body `status` field, never the HTTP status code**. Tests must assert *both* current behaviour (`422` + `EXECUTING`) and post-fix behaviour (`200` + `EXECUTING`) resolve identically. This is the single highest-value test in the suite.
- `--wait` uses `timeout=0` + polling (sync mode is deprecated upstream).
- HITL: `approved get` (dequeue — **consumes one item per call**, same one-shot treatment as R5), `bulk-download`, `download-status`.

**Exit:**
1. `--wait` handles 422-with-EXECUTING and 200-with-EXECUTING identically (explicit test).
2. >32 files rejected client-side with exit 2.
3. `hitl approved get` documents and implements dequeue semantics; `--save` persists before exit.

---

### Phase 4 — Platform Management v1 (§6.3)

**Entry:** Phase 3 exit; **R7 confirmed** — check whether `full_access` keys are UI-creatable. If not, every `delete` command's help text must state that it requires a key the UI cannot issue.

Largest surface (~80 endpoints) but the *least* new machinery — if Phase 1 was done right, this is predominantly record authoring plus fixtures.

- Groups: prompt-studio, workflow, api-deployment, pipeline, adapter, connector, group, user, share.
- **P8 in practice:** derive every PATCH from its PUT. Duplicated records here would be the main drift risk.
- **P11 in practice:** the trailing-slash and `profilemanager`/`profile-manager` inconsistencies are real and load-bearing. Add a test asserting each literal path matches §6.3 exactly.
- `share --resource` enum → path-segment mapping (P3).
- `--add-user`/`--remove-user` read-modify-write helpers over replace-semantics flags (P12).
- Pagination (`--page`, `--page-size`) as a shared param group.
- Permission levels surfaced in help for destructive commands (§4.4).

**Exit:**
1. Every §6.3 endpoint present in `--dump-commands`.
2. Literal-path test passes (no normalization; slash inconsistencies preserved).
3. PATCH records are derived, not duplicated (asserted structurally).
4. `share` rejects an unmapped `--resource` with exit 2, not a 404.

---

### Phase 5 — API Hub (§6.5)

**Entry:** Phase 4 exit; **R6** — obtain the public base URL, or confirm none exists yet. No default is baked in; `--base-url`/`UNSTRACT_APIHUB_BASE_URL` is required until resolved.

- `apihub {extract,status,retrieve}`, `doc-splitter {upload,status,download}`.
- `conv_*` and `ext_*` parameter families; **`--ext-param KEY=VALUE`** escape hatch (P5) — this exists precisely because API Hub has no public docs and the Skill will lag (§8.4).
- Auth: `apikey` only, plus optional BYO `--llmwhisperer-key` / `--anthropic-key`. **Assert in tests that no `X-Subscription-*` or `X-User-Id` header is ever emitted.**
- `--wait` over `QUEUED_FOR_WHISPER → QUEUED_FOR_EXTRACTION → COMPLETED`.

**Exit:**
1. `--ext-param foo=bar` reaches the wire as `ext_foo=bar`.
2. No gateway-injected header is ever sent (explicit negative test).
3. `--wait` traverses the three-state progression.

---

### Phase 6 — Claude Skill, packaging, completions

**Entry:** Phases 1–5 exit criteria met.

The Skill (§8) is built **last on purpose**: it operates on `endpoints/*.py`, so it needs the full record set to be meaningfully testable.

- `.claude/skills/update-unstract-cli/SKILL.md` implementing §8.3's seven steps.
- Doc parsers: Markdown tables (LLMWhisperer, API deployment, HITL) and `<ApiEndpoint>`/`<ApiSection>` MDX components (Platform v1). MDX is the harder parser — it is JSX-ish, not Markdown, so parse component props rather than table cells.
- Record parser producing the same normalized `(method, path, params[])` shape for a symmetric diff.
- Three-axis diff (new / missing / per-param drift), reported with doc citations **before** any edit.
- Safety rules from §8.5 encoded explicitly: never auto-delete; never invent params; renames → alias + deprecation, never silent; preserve richer hand-written help; honour `doc_conflict` notes; treat `draft: true` as an exclusion marker (multi-doc chat).
- API Hub changes **flagged for human review**, never auto-applied (§8.4).
- Shell completions; packaging and publish workflow.

**Exit:**
1. **Regression harness:** run the Skill against the *current* docs → it must report **zero drift**. This is the strongest possible correctness signal, because the records were authored from exactly these docs.
2. Synthetic drift test: add a parameter to a fixture doc → the Skill detects it, cites the file, and proposes the correct record edit.
3. A `doc_conflict`-marked definition (`/whisper-detail`) is **not** reverted when the Skill sees the conflicting index page.
4. After a Skill-applied edit, `--dump-commands` still parses and the suite passes.

---

## 4. Testing strategy

Per SPEC §9, with the sequencing that matters:

| Layer | When | Notes |
| --- | --- | --- |
| Definition integrity | Phase 1, grows each phase | Unique `(group,name)`; valid `doc_source`; complete param metadata |
| Command generation | Phase 1 | Tree builds; `--help` renders at every level; `--dump-commands` valid JSON |
| HTTP per endpoint | Each phase | `respx` fixtures built from the **documented sample payloads** in the docs repos |
| Exit codes | Phase 1 | Table-driven across §5.4 |
| Polling | Phases 2, 3, 5 | Body-status not HTTP code; 422 defect asserted explicitly |
| One-shot | Phases 2, 3 | Second read → exit 9; `--save` persists before exit |
| Redaction | Phase 1 | No secret in any stream, including `--dry-run` and `-vv` |
| Non-interactivity | Phase 1 | No TTY prompt anywhere; stdin only via explicit `--file -` |
| Skill round-trip | Phase 6 | Zero-drift regression against current docs |

**Two invariants worth asserting globally**, because they are the promises an agent relies on and they are easy to break in any single command:

- **Stdout purity** — for every command, in `--output json`, stdout parses as JSON and nothing else is written to it.
- **Flag coverage** — every `Param` in every record surfaces as a CLI flag (mechanically checkable, and it *is* SPEC goal §1.1.3).

---

## 5. Sequencing summary

```
Task 0  Parameter pattern enumeration (P1–P12)      ← load-bearing design
Phase 0 Scaffolding
Phase 1 Core spine, proven on `whisper usage`       ← de-risks the whole design
Phase 2 LLMWhisperer + --wait + one-shot
Phase 3 Deployments + HITL                          ← R4: the 422 defect
Phase 4 Platform v1 (~80 endpoints)                 ← mostly records, if Phase 1 held
Phase 5 API Hub + --ext-param
Phase 6 Claude Skill + packaging                    ← zero-drift regression
```

The plan front-loads *design* risk (Task 0, Phase 1) and back-loads *volume* (Phase 4). If Phase 1's exit criteria are met honestly, Phase 4 is the least risky phase in the project despite being the largest — that is the intended payoff of the generation bet.

---

## 6. Open questions gated to phases

| Question (SPEC §11) | Gates | If unresolved |
| --- | --- | --- |
| API Hub public base URL | Phase 5 entry | Ship with no default; `--base-url`/env required. Not a blocker for Phases 0–4 |
| `full_access` UI-creatable? | Phase 4 entry | Ship `delete` commands with help text stating the requirement and the limitation |

Neither blocks the critical path. Both must be answered *at their phase entry*, not deferred silently past it.
