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
