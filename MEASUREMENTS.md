# Phase 6 — measurements

All measured 2026-08-10 against local checkouts of `unstract` (`aac5edef9`),
`unstract-llm-whisperer`, `unstract-python-client` and `llm-whisperer-python-client`, with
the local docker-compose stack running. SLOC = non-blank, non-comment, non-docstring lines.

## The table from `02-poc-plan`

| Question | Result |
|---|---|
| **Hand-written LOC per command, generated vs published** | Generated path: **243 SLOC** (`facade.py` 175 + `cli_generated.py` 68) for 2 commands exposing **all 12** execution params. Published path: **32 SLOC** (`cli_published.py`) for the same 2 commands exposing **2 of 12** — on top of the published client's **328 SLOC**, which the facade replaces. Like for like, the hand-written transport shrinks **328 → 175 (−47%)** while its parameter coverage goes **2 → 12**. |
| **Cost of a backend param addition** | **Zero hand-written lines.** Added `poc_new_param = CharField(...)` to `ExecutionRequestSerializer`, regenerated: spec grew 4 generated lines, the SDK model gained the field, and `--poc-new-param` appeared in `deployment execute --help` and reached the multipart body — no edit anywhere in this repo. **Target met.** |
| **Is regeneration deterministic?** | **Yes, both.** `gen_docstudio_spec.py` twice → `diff` empty. `gen_llmw_spec.py` twice → `diff` empty. Both write with `sort_keys=True`; keep it. |
| **Diff noise on a real change** | Renaming one route → **137-line diff in a 436-line spec**, only **4 lines** of which are the path keys. Paths are top-level sorted keys, so a rename relocates the whole operation block. Field additions diff cleanly (4 lines). See `GAPS.md` §9. |
| **Does the annotation backlog dominate?** | Not yet, but it is **2× the recorded estimate**. `DeploymentExecution` (1 view, 2 operations) costs **72 SLOC** — ~31 per operation, sitting on the kill criterion's "roughly 30 lines average". `01-current-state`'s ~30 was measured against an annotation that produced a non-working SDK. See `GAPS.md` §10. |
| **Total maintenance surface** | **555 SLOC** forever: `gen_llmw_spec.py` 238, `overlay/llmwhisperer.yaml` 139, `gen_docstudio_spec.py` 87, `annotations.py` 72, `gen_sdk.sh` 18, `openapi-client.yaml` 1. Against **6,684 lines of generated SDK** and 1,435 lines of committed spec. Ratio of hand-written to generated: **1 : 12**. |

## Kill criteria

| Criterion | Status |
|---|---|
| Regeneration not deterministic and not cheaply fixable | **not hit** — deterministic on both generators |
| Overlay grows faster than the generated surface | **not hit** — 139-line overlay produces a 999-line spec and a 47-file SDK |
| Annotating an endpoint costs more than ~30 lines on average | **borderline** — 31 SLOC per operation, exactly on the line |
| The generated SDK needs post-processing to be usable | **not hit** — `build/` is never touched; every fix landed in the annotation or the overlay |

**No kill criterion was hit. The POC held.**

## Falsifiable claim: the two CLIs agree

`poc/compare.sh` runs `deployment execute` and `deployment status` through both
implementations against a live backend, each with its own execution (the status GET is
one-shot). Normalising `execution_id`, `file_execution_id`, `workflow_start_time`,
`total_elapsed_time` and the status endpoint URL:

```
execute: IDENTICAL
status:  IDENTICAL
```

Including the deeply nested failure payload — `metadata.usage`, `total_pages_processed`,
per-file `error` — none of which the annotation declares. `to_dict()` round-trips undeclared
keys through `additional_properties`.

## Known-answer test on the LLMWhisperer walk

`POST /whisper` must surface the five parameters the service reads that neither the
published client nor PR #1's records know about. All five found, correctly typed:

```
checkbox_confidence_threshold  number  0.3
derotate_threshold             number  10.0
ignore_vertical_text           boolean false
min_table_width                number  0.0
watermark_angle_threshold      number  25.0
```

The generated SDK exposes **27 query parameters** on `extract`; the published
`whisper()` sends **20**, two of which never reach the service (`GAPS.md` §6).

## Live staging verification (2026-08-10, `globe.unstract.com`)

Everything above was measured against a local stack. This section is the same pipeline
against the remote staging deployment, which is what ADR 0005 requires before an annotation
counts as done.

Unless noted, each check ran **through the CLI**, not by calling a client directly — so the
CLI, the facade and the generated SDK are all exercised in the same call.

| Check | Ran via | Result |
|---|---|---|
| `deployment execute`, published vs generated | both CLIs | **IDENTICAL** (UUID-normalised) |
| Upload fidelity, read back from the staging DB | both CLIs | **IDENTICAL** — same `file_name`, `file_size` 2047673, `mime_type application/octet-stream`, exactly one `workflow_file_execution` row each |
| `deployment status`, success path | both CLIs | Identical except LLM free-text and the token counts derived from it — see below |
| `deployment status`, 422 error path | both CLIs | Identical, including undeclared nested keys (`metadata.usage`, `hitl`, `total_pages_processed`, per-file `error`) |
| LLMWhisperer `whisper` + poll | generated CLI | 595 chars from a 1-page PDF, 16.5s — **later shown to be missing words**, see the input diff below |
| LLMWhisperer `get_usage_info` | generated CLI | Live quota returned |

### The one diff on the success path

Both runs returned `COMPLETED 200` with `status: Success`. Three fields differ, all
downstream of the LLM:

```
result.output.about   different phrasing of the same summary
completion_tokens     32  vs  18
cost_in_dollars       0.0012106  vs  0.0010706
elapsed_time          140.3s  vs  20.4s
```

Everything that would move if the *request* differed is identical:

```
prompt_tokens          347  ==  347      <- both clients delivered a byte-identical prompt
embedding_tokens       231  ==  231
total_pages_processed    1  ==    1
tool_name/output_type  structure_tool / JSON
status                 Success == Success
```

`prompt_tokens` matching is the load-bearing evidence. The completion differs because LLM
generation is nondeterministic; the token count and cost follow from it. Not a client
difference.

### A parity pair needs a deterministic workflow

An earlier attempt on a different deployment diverged: published `200/COMPLETED`, generated
`422/ERROR`. The staging DB confirms the *server* recorded those two states, so both clients
reported faithfully. Cause was a server-side race — of 148 staging executions in 30 days
where every file errored, 147 were marked ERROR and exactly 1 COMPLETED, and that 1 was the
published run.

Do not use a slow or flaky workflow for parity. Better: compare the two clients on
**deterministic failures** — bad key → 401, garbage `execution_id` → 4xx, consumed status
endpoint → 406. Sub-second, repeatable in CI, and strictly better evidence, since every
silent bug this POC found was in error or encoding handling rather than the 200 path.

## The input diff (2026-08-10, same day)

The gap logged below as "not covered" was closed by giving `cli_published.py` the same
`whisper` and `webhook` groups, deriving their flags from the published method signature the
way the generated CLI derives them from the spec. Comparing the two **query strings** — not
the two outputs — found three defects in one pass (`GAPS.md` §14, §15).

The consequential one: the generated client sends every parameter default the spec declares,
and the published client does not.

```
allow_rotated_text=True         checkbox_confidence_threshold=0.3   derotate_threshold=10.0
ignore_vertical_text=False      min_table_width=0.0                 watermark_angle_threshold=25.0
word_confidence_threshold=0.3
```

Bisected against the published baseline, one parameter per run, `--wait-timeout 600`:

```
none                             matches_published=True
allow_rotated_text               matches_published=True
checkbox_confidence_threshold    matches_published=True
derotate_threshold               matches_published=True
ignore_vertical_text             matches_published=True
min_table_width                  matches_published=True
watermark_angle_threshold        matches_published=True
word_confidence_threshold        matches_published=False    <- the only one
url / url_in_post / file_name / line_splitter_strategy   matches_published=True
```

Each side is deterministic — two runs of the same side are byte-identical — so this is a
parameter effect, not OCR noise. Words scoring below the threshold disappeared from
`result_text` and from `confidence_metadata`.

### After the fix

`whisper extract`, same PDF, both CLIs:

```
result_text identical : True
confidence_metadata   : True
envelope identical    : True
```

Only `whisper_metadata.avg_page_processing_time` differs (4.33 vs 4.0 — server-side timing).
`whisper usage` is byte-identical. The full query-string diff is now two deliberate
entries, both LW-406:

```
file_name              pub=<absent>        gen=''
filename               pub=''              gen=<absent>
line_spitter_strategy  pub='left-priority' gen=<absent>
line_splitter_strategy pub=<absent>        gen='left-priority'
```

Deployment re-verified after the same fix on `ExecuteRequest`: both `COMPLETED`, differing
only in per-run IDs, elapsed times and LLM free text.
`total_tokens − completion_tokens = 1458` on both sides — identical prompts.

### The spec-side alternative, measured

Stripping every `default:` from the spec before generating makes the generator emit `UNSET`
and omit unset parameters with no facade code at all:

```
whisper params sent when caller sets nothing:  []
retrieve:                                      ['whisper_hash']
signature still exposes 28 params              <- CLI flag derivation unaffected
```

50 defaults stripped, one pipeline step, fixes every backend at once. Not taken: it also
removes the server's default from `--help` and from the generated signature, which is real
documentation, and it drops a parameter a caller deliberately set to the default value. The
facade filter is exact; this is cheaper. Revisit if the per-method cost bites.

### Not covered

`poc/cli_published.py` now covers `whisper` and `webhook`, so both backends have a side by
side. What still has no differential coverage is **malformed and boundary inputs** — the
absolute-vs-relative status endpoint of `GAPS.md` §13 remains the only known instance, found
by accident rather than by a test.

## Surfaces

```
specs/docstudio.json       2 paths ·  4 operations ·  6 schemas ·   436 lines
specs/llmwhisperer.json   13 paths · 16 operations · 53 params ·   999 lines
build/sdk_docstudio       18 files
build/sdk_llmwhisperer    47 files
generated Python total                                            6,684 lines
```

## Unannotated baseline, for contrast

`gen_docstudio_spec.py --no-annotate` on the same routes:

```
operationId: root_create / root_retrieve      (collide across any two such views)
requestBody: absent
component schemas: 0
```

Introspection alone yields nothing usable for this endpoint — consistent with
`01-current-state`'s "unable to guess serializer".
