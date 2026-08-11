# RUNBOOK — OpenAPI → SDK → CLI pipeline

Branch: `poc/openapi-pipeline`. Written for someone with **no prior context** on these
repos. Every command below was run and its output checked on 2026-08-10.

If your job is to *exercise* the CLI rather than build or change it, `AGENT_BRIEF.md` has the
setup and the credential rules and deliberately nothing more.

---

## 1. What this is

A proof that an SDK and a CLI can be kept in step with two backends without anyone
hand-maintaining an API contract. Three layers:

```
  Django backend (unstract)              Flask service (unstract-llm-whisperer)
  routes + DRF serializers               route handlers
            │                                       │
   drf-spectacular introspection            AST walk of the source
            │                                       │
            ▼                                       ▼
   specs/docstudio.json                    specs/llmwhisperer.json      ← committed, diffable
            │                                       │
            └──────── openapi-python-client ────────┘
                             │
                             ▼
                    build/sdk_*/  (httpx)                               ← generated, gitignored
                             │
                             ▼
          poc/facade.py   ·   poc/llmw_facade.py                      ← hand-written
                             │
                             ▼
                    poc/cli_generated.py                                ← hand-written
              deployment · whisper · webhook
```

**Below the facade line: generated, never hand-edited.** If generated code is wrong, the
fix goes in the backend annotation or the overlay, never in `build/`.

## 2. What is in this repo

| Path | Generated? | What it is |
|---|---|---|
| `tools/gen_docstudio_spec.py` | hand | Runs drf-spectacular against an `unstract` checkout. |
| `tools/annotations.py` | hand | The `@extend_schema_view` block for `DeploymentExecution`, applied at **runtime** so no backend repo is touched. Written in its final form — graduating it is a paste into `api_v2/api_deployment_views.py`. |
| `tools/gen_llmw_spec.py` | hand | AST-walks the LLMWhisperer Flask controller into an OpenAPI spec. |
| `tools/gen_sdk.sh` | hand | Runs `openapi-python-client` over both specs into `build/`. |
| `tools/openapi-client.yaml` | hand | Generator config. Deliberately near-empty. |
| `specs/docstudio.json` | **generated, committed** | The Document Studio contract. Diff it to see backend drift. |
| `specs/llmwhisperer.json` | **generated, committed** | The LLMWhisperer contract. |
| `specs/overlay/llmwhisperer.yaml` | hand | Everything the AST walk cannot infer — bodies, responses, tags. |
| `poc/facade.py` | hand | Document Studio: retry, poll, sync/async POST rule, dict return shapes, exception translation. |
| `poc/llmw_facade.py` | hand | LLMWhisperer: all 11 public methods of `LLMWhispererClientV2`, signature-identical. Retry, the `wait_for_completion` poll loop, the error-dict contract, and `get_highlight_rect` (which makes no HTTP call at all). |
| `poc/cli_generated.py` | hand | `deployment`, `whisper` and `webhook` command groups over the generated SDKs. Flags are derived from the generated model / `_get_kwargs` signature. |
| `poc/cli_published.py` | hand | The same `deployment`, `whisper` and `webhook` commands over the published clients. The baseline. Its `whisper` flags come from the published method signature where the generated CLI's come from the spec, so the two flag sets are themselves a drift signal. |
| `poc/compare.sh` | hand | Runs both against a live backend and diffs the JSON. |
| `poc/test_compat.py` | hand | The Document Studio exception-compatibility check. |
| `poc/test_llmw_compat.py` | hand | The LLMWhisperer drop-in check: method surface, constructor, signatures, defaults, wire names, webhook body, no injected defaults, exception translation. Offline. |
| `build/` | **generated** | SDKs and venvs. Gitignored. Delete it any time. |

## 3. Prerequisites

- A checkout of `unstract` at `~/zipstuff/unstract` with a working `backend/.venv`
  (override with `--source`).
- A checkout of `unstract-llm-whisperer` at `~/zipstuff/unstract-llm-whisperer`
  (override with `--source`). Only its **source** is read; the service is never started.
- `uv` on PATH.

**No database is required.** Measured 2026-08-11 with the DB pointed at a dead port
(`127.0.0.1:1`) before `django.setup()`: both the default `deployment` urlconf and the full
`backend.urls_v2` produce specs **byte-identical** to a run against a live Postgres. The app
that connects at startup logs the failure and continues, and the two viewsets whose
`get_queryset()` drf-spectacular calls fail with a live DB too — `FileHistoryViewSet` and
`WorkflowEndpointViewSet` degrade the same way either way. So CI needs the backend venv and
nothing else: no Postgres service, no testcontainers.

This holds as long as every `AppConfig.ready()` keeps *catching* its database errors. One
that raises instead would break generation — which the drift gate reports as a failure, so
no separate guard is needed.

### Environment gotchas — read before debugging anything

1. **`backend/.env` contains an inline JSON value.** It breaks shell `export`, `set -a`,
   and `uv --env-file`. `gen_docstudio_spec.py` loads it with `python-dotenv` for exactly
   this reason. Do not "simplify" that to a shell source.
2. **`DB_SCHEMA=public`.** `.env` defaults to schema `unstract`, which does not exist on a
   fresh DB and makes `django.setup()` fail with an opaque error against a *live* DB. The
   script forces `public`. Harmless when no DB is reachable at all.
3. **`DJANGO_SETTINGS_MODULE=backend.settings.test`.** Other settings modules pull in
   cloud plugins that may not be present.
4. **Which urlconf you pass decides what you get.** See the table below. Getting this
   wrong produces a spec whose paths the server does not serve, and the failure is a 401,
   not a 404 — you will chase auth for an hour.

| `--urlconf` | Produces |
|---|---|
| `deployment` *(default)* | The execute + status routes **mounted at their real prefix** (`/deployment/api/{org}/{api}/`). This is what the CLI needs. |
| `api_v2.execution_urls` | The same routes **without** the `deployment/` prefix. Structurally wrong against a real server. Do not use. |
| `backend.urls_v2` | The tenant routes (`/api/v1/unstract/{org}/…`) — 171 paths, 257 operations, 101 schemas. Large; the stretch goal, not the POC. |
| `backend.base_urls` | Everything. |

Run the generator **with the backend's own interpreter**, not a venv of this repo — it
needs the backend's installed apps.

## 4. Regenerate everything from scratch

```bash
cd ~/zipstuff/unstract-cli

# 0. one-time: drf-spectacular into the backend venv, WITHOUT touching its pyproject.toml
uv pip install --python ~/zipstuff/unstract/backend/.venv/bin/python drf-spectacular

# 1. Document Studio spec  (~40s; most of it is Django app loading)
~/zipstuff/unstract/backend/.venv/bin/python tools/gen_docstudio_spec.py
#   -> specs/docstudio.json: 2 paths, 4 operations, 6 schemas

# 2. LLMWhisperer spec  (instant; pure AST, no imports, no service)
python3 tools/gen_llmw_spec.py
#   -> specs/llmwhisperer.json: 13 routes walked, 13 paths, 16 operations, 53 params

# 3. both SDKs  (creates build/gen-venv on first run)
bash tools/gen_sdk.sh
#   -> generated build/sdk_docstudio (18 files)
#   -> generated build/sdk_llmwhisperer (46 files)
```

Both specs are **deterministic** — regenerating and diffing yields no change. That is what
makes the committed artifact a usable drift signal, so keep `sort_keys=True` in both
generators.

### Pinned inputs — what has to stay fixed for a diff to mean anything

A drift signal is only as good as the things it holds constant. Two inputs are load-bearing:

| Input | Pinned where | Why |
|---|---|---|
| `openapi-python-client` | `GENERATOR` in `tools/gen_sdk.sh` (**0.29.0**) | Unpinned, a generator upgrade and a backend change produce the same kind of diff and cannot be told apart. `gen_sdk.sh` re-installs if the venv drifts. |
| The published clients | **not pinned yet** — installed `-e` from local checkouts | `test_llmw_compat.py` compares the facade against whatever is in the working tree. In CI this must be a released version, or the check silently measures someone's branch. |

The second one is not theoretical. See §9.

To see what introspection alone gives you (nothing usable), add `--no-annotate`:

```bash
~/zipstuff/unstract/backend/.venv/bin/python tools/gen_docstudio_spec.py --no-annotate --out /tmp/baseline.json
# operationIds become root_create / root_retrieve, no requestBody, 0 schemas
```

## 5. Run the CLIs

```bash
uv venv build/poc-venv
uv pip install --python build/poc-venv/bin/python click requests httpx attrs unstract-client

export UNSTRACT_API_URL='http://localhost:8000/deployment/api/<org_id>/<api_name>/'
export UNSTRACT_API_KEY='<api key>'

# generated SDK — note PYTHONPATH must include build/ and poc/
PYTHONPATH=build:poc build/poc-venv/bin/python poc/cli_generated.py deployment execute FILE --timeout -1
PYTHONPATH=build:poc build/poc-venv/bin/python poc/cli_generated.py deployment status '<status_check_api_endpoint>'

# published client — the baseline
build/poc-venv/bin/python poc/cli_published.py deployment execute FILE --timeout -1
```

`status_check_api_endpoint` comes back **relative** (`/deployment/api/…`). The published
client prefixes its own `base_url`, so handing it an absolute URL yields
`https://host…https://host…` and a retry loop of `ConnectionError`. Pass it back exactly as
returned. The generated facade happens to accept both — see `GAPS.md` §13.

Find a local deployment + key:

```sql
-- docker exec unstract-db psql -U unstract_dev -d unstract_db
select d.api_name, o.organization_id, k.api_key
from unstract.api_deployment d
join unstract.organization o on o.id = d.organization_id
join unstract.api_deployment_key k on k.api_id = d.id
where d.is_active and k.is_active;
```

The URL is `http://localhost:8000/deployment/api/<organization_id>/<api_name>/`.
Port 80 (the proxy) does **not** route this path; use 8000.

### Side-by-side comparison

```bash
UNSTRACT_API_URL=... UNSTRACT_API_KEY=... poc/compare.sh /path/to/sample.txt
```

It gives each side its own execution on purpose: **the status GET is one-shot.** Reading a
completed result acknowledges it, and the next read returns `406 Result already
acknowledged`. Sharing one execution between the two CLIs makes the second look broken.

Ignoring per-run IDs and timestamps, the two produce **identical** output for both commands.

### LLMWhisperer commands

```bash
uv pip install --python build/poc-venv/bin/python -e ~/zipstuff/llm-whisperer-python-client

set -a && . ~/zipstuff/llm-whisperer-python-client/.env && set +a   # base URL + key
export PYTHONPATH=build:poc

build/poc-venv/bin/python poc/cli_generated.py whisper usage
build/poc-venv/bin/python poc/cli_generated.py whisper extract FILE.pdf --wait
build/poc-venv/bin/python poc/cli_generated.py whisper retrieve <hash> --text-only
build/poc-venv/bin/python poc/cli_generated.py webhook register NAME --callback-url URL

# same commands over the published client — swap the script name to diff the two
build/poc-venv/bin/python poc/cli_published.py whisper extract FILE.pdf --wait-timeout 600
```

Give each side its own run and compare `extraction.result_text` and
`extraction.confidence_metadata`. `whisper_hash` and `whisper_metadata.avg_page_processing_time`
always differ. Staging can take several minutes on a photo PDF, so raise `--wait-timeout`
before concluding anything from a timeout.

The published `llmwhisperer-client` is installed **only for its exception class** —
`llmw_facade.py` raises the real `LLMWhispererClientException` so downstream `except` clauses
keep matching.

`whisper extract` derives its 25 parameter flags from the generated `extract._get_kwargs`
signature, so a parameter added to the Flask handler becomes a CLI flag with no edit to
`cli_generated.py`. Same zero-line claim as `deployment execute`, over a backend that has no
serializers to introspect.

### Compatibility checks — run these after any facade change

```bash
PYTHONPATH=build:poc build/poc-venv/bin/python poc/test_compat.py       # -> compat OK
PYTHONPATH=build:poc build/poc-venv/bin/python poc/test_llmw_compat.py  # -> llmw compat OK
```

`test_llmw_compat.py` asserts the LLMWhisperer facade is a drop-in: same 11 public methods,
identical signatures and defaults, a constructor matching published positionally and by
keyword, the two misspelled published parameter names resolving to the query keys the service
actually reads, a webhook body carrying the three required keys, **no query parameter the
published client would not send**, and no httpx exception escaping. It needs no network.

That last one is the check that matters most and the one nothing else covers. `_get_kwargs`
writes every spec-declared default into the request; sending a default is not the same as
omitting it, and on staging one of them silently dropped low-confidence words from the
extracted text (`GAPS.md` §14). The equivalent assertion for Document Studio lives in
`test_compat.py` — the multipart body must carry exactly `files`, `include_metadata`,
`timeout`. **If you add a facade method, add it to these two checks**; they are per-method,
so a new method is uncovered by default.

It asserts that `httpx.ConnectError` / `TimeoutException` escaping the generated transport
are re-raised as `requests.ConnectionError` / `requests.Timeout`. This is not cosmetic:
`unstract/sdk1`'s LLMWhisperer x2text adapter (`.../x2text/llm_whisperer_v2/src/helper.py`)
catches those two `requests` classes by name to turn a network blip into a clean 503/504.
httpx's are not subclasses, so without the translation those handlers silently stop
matching — no import error, no test failure, just an uncaught exception in the tool runtime
the first time a network blips. `requests` stays a dependency **purely for its exception
classes**.

## 6. How to change things

| You want to… | Do this |
|---|---|
| expose a new backend param on the CLI | Add the field to `ExecutionRequestSerializer` in the backend. Regenerate. **Zero lines here.** |
| fix a wrong type/shape in the generated Docstudio SDK | Edit `tools/annotations.py`. Never `build/`. |
| add an LLMWhisperer response shape | Edit `specs/overlay/llmwhisperer.yaml`, keyed by handler function name. |
| change the CLI command tree | Change `operation_id` / `tags` in the annotation or overlay — the generator names modules `api/<tag>/<operation_id>`. |
| add retry/poll/one-shot behaviour | `poc/facade.py`. None of it is generatable. |

### Graduating the annotation

`tools/annotations.py`'s `annotate()` body is written to be pasted into
`backend/api_v2/api_deployment_views.py` as-is (dedent one level, move the imports to the
top of the file, drop the function wrapper). That is the whole backend migration for this
endpoint. Nothing else in this repo changes.

## 7. Known-good outputs

```
specs/docstudio.json      2 paths · 4 operations · 6 schemas · 436 lines
specs/llmwhisperer.json  13 paths · 16 operations · 53 params · 999 lines
build/sdk_docstudio      18 files
build/sdk_llmwhisperer   46 files
```

The LLMWhisperer walk is validated against a known answer: `POST /whisper` must expose the
five parameters the service reads but neither the published client nor PR #1's records know
about — `min_table_width`, `derotate_threshold`, `checkbox_confidence_threshold`,
`watermark_angle_threshold`, `ignore_vertical_text`. It finds all five.

## 8. Verified against staging, and what is still untested

Run against `globe.unstract.com` on 2026-08-10. Numbers in `MEASUREMENTS.md`.

**Verified.** Document Studio execute and status are output-identical to the published client
on both a success and a 422 error path, with upload fidelity confirmed by reading
`workflow_file_execution` back from the staging DB. LLMWhisperer `whisper extract` is
output-identical too — `result_text` and `confidence_metadata` byte for byte, with only the
server's own `avg_page_processing_time` differing — as is `whisper usage`. Every check went
through the CLI, so CLI, facade and generated SDK were exercised together.

That LLMWhisperer parity only holds **after** the injected-defaults fix. The first side-by-side
run disagreed, and the two sides were sending different parameters (`GAPS.md` §14).

Re-verified 2026-08-11 after the facade was brought back in step with a newer published
client (§9): `result_text` identical at 4,013 characters, `confidence_metadata` identical
except one entry. That entry is **jitter, not a client difference** — a repeat run of the
*generated* side against itself differs in the same single entry, while two published runs
were identical. Expect run-to-run noise in per-word confidence on low-scoring glyphs and do
not read a one-entry diff as a regression.

**Still untested:**

- **No differential test on malformed or boundary inputs.** Parity checks compare two clients
  on *valid* inputs. The facade was found to accept an absolute status endpoint where the
  published client requires a relative one (`GAPS.md` §13) — signature checks passed straight
  over it, and it surfaced by accident rather than by a test.
- Of the LLMWhisperer surface, only `whisper`, `whisper_status`, `whisper_retrieve` and
  `get_usage_info` have made a live call. The webhook and insights operations have not.
- Only `POST /deployment/api/…` and its status `GET` are annotated. The `/highlight/`
  sub-route that the API deployment also serves is **not in the spec**.
- The 307-route tenant spec (`--urlconf backend.urls_v2`) was not regenerated in this pass.

### Picking a deployment for a parity run

Use a workflow that reaches a terminal state fast and deterministically. One earlier pair
diverged (published `200/COMPLETED`, generated `422/ERROR`) purely because the *server*
recorded two different states — a race that hit 1 of 148 staging executions in 30 days. The
clients reported faithfully; the harness just could not prove it without a DB query.

Better still, do not run a workflow. Compare the two clients on deterministic failures — bad
key → 401, garbage `execution_id` → 4xx, an already-consumed status endpoint → 406. Those run
in under a second, need no extraction, and exercise the error handling where every silent bug
in this POC actually lived.

## 9. The gate fired for real, on the first day it existed

On 2026-08-11 the `llm-whisperer-python-client` checkout was pulled forward 2.5 months
(`9862b8f` → `3832713`, merging PR #33). `test_llmw_compat.py` failed immediately:

```
AssertionError: whisper lost word_confidence_threshold
```

The published `whisper()` had grown one parameter and now sends it unconditionally with a
default of `0.3`. The fix was **one line in two places** in `poc/llmw_facade.py` — the
signature and the params dict. Both CLIs then exposed `--word-confidence-threshold` with
**zero edits**, because `cli_published.py` derives flags from the published signature and
`cli_generated.py` derives them from the spec.

Three things this pins down, and they are the argument for the whole layering:

1. **The generated transport and the CLI auto-expose. The facade does not** — because it
   *re-declares* every signature. Re-declaration is the thing that blocks auto-exposure, and
   it is a choice: mature SDKs inherit or re-export instead and get new endpoints for free.
   Ours is justified where a published contract must be pinned, and not justified for surface
   that has no published equivalent.
2. **The offline check is what makes that safe.** No network, no key, sub-second, and it
   named the exact missing parameter.
3. **This is why the published client has to be pinned in CI.** The failure arrived from a
   `git pull` in an unrelated repo, not from a change in this one.

It also revises `GAPS.md` §14. The bisect that found `word_confidence_threshold` was run
against a published client 2.5 months stale, which did not send the parameter at all. The
mechanism it proved is unchanged and still holds — the service treats *absent* and *0.3*
differently, so sending a spec default is not the same as omitting it — but for this one
parameter the two clients were at different versions rather than disagreeing about defaults.

## 10. Where the findings are

`GAPS.md` — what broke, what the approach costs, and what is unsustainable about it.
