# RUNBOOK — OpenAPI → SDK → CLI pipeline

Branch: `poc/openapi-pipeline`. Written for someone with **no prior context** on these
repos. Every command below was run and its output checked on 2026-08-10.

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
                    poc/facade.py                                       ← hand-written
                             │
                             ▼
                    poc/cli_generated.py                                ← hand-written
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
| `poc/facade.py` | hand | Retry, poll, sync/async POST rule, dict return shapes, exception translation. |
| `poc/cli_generated.py` | hand | Two commands over the generated SDK. Flags are derived from the generated model. |
| `poc/cli_published.py` | hand | The same two commands over the published `unstract-client`. The baseline. |
| `poc/compare.sh` | hand | Runs both against a live backend and diffs the JSON. |
| `poc/test_compat.py` | hand | The exception-compatibility check. Run it after touching the facade. |
| `build/` | **generated** | SDKs and venvs. Gitignored. Delete it any time. |

## 3. Prerequisites

- A checkout of `unstract` at `~/zipstuff/unstract` with a working `backend/.venv`
  (override with `--source`).
- A checkout of `unstract-llm-whisperer` at `~/zipstuff/unstract-llm-whisperer`
  (override with `--source`). Only its **source** is read; the service is never started.
- A Postgres reachable at `localhost:5432` with the `unstract` schema set. The local
  docker-compose stack provides this: `docker compose -f docker/docker-compose.yaml up -d db`.
  **The backend imports its apps at generation time and one viewset touches the DB**, so
  generation is not purely static.
- `uv` on PATH.

### Environment gotchas — read before debugging anything

1. **`backend/.env` contains an inline JSON value.** It breaks shell `export`, `set -a`,
   and `uv --env-file`. `gen_docstudio_spec.py` loads it with `python-dotenv` for exactly
   this reason. Do not "simplify" that to a shell source.
2. **`DB_SCHEMA=public`.** `.env` defaults to schema `unstract`, which does not exist on a
   fresh DB and makes `django.setup()` fail with an opaque error. The script forces
   `public`.
3. **`DJANGO_SETTINGS_MODULE=backend.settings.test`.** Other settings modules pull in
   cloud plugins that may not be present.
4. **Which urlconf you pass decides what you get.** See the table below. Getting this
   wrong produces a spec whose paths the server does not serve, and the failure is a 401,
   not a 404 — you will chase auth for an hour.

| `--urlconf` | Produces |
|---|---|
| `deployment` *(default)* | The execute + status routes **mounted at their real prefix** (`/deployment/api/{org}/{api}/`). This is what the CLI needs. |
| `api_v2.execution_urls` | The same routes **without** the `deployment/` prefix. Structurally wrong against a real server. Do not use. |
| `backend.urls_v2` | The 307 tenant routes (`/api/v1/unstract/{org}/…`). Large; the stretch goal, not the POC. |
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
#   -> generated build/sdk_llmwhisperer (47 files)
```

Both specs are **deterministic** — regenerating and diffing yields no change. That is what
makes the committed artifact a usable drift signal, so keep `sort_keys=True` in both
generators.

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

### Compatibility check — run this after any facade change

```bash
PYTHONPATH=build:poc build/poc-venv/bin/python poc/test_compat.py   # -> compat OK
```

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
build/sdk_llmwhisperer   47 files
```

The LLMWhisperer walk is validated against a known answer: `POST /whisper` must expose the
five parameters the service reads but neither the published client nor PR #1's records know
about — `min_table_width`, `derotate_threshold`, `checkbox_confidence_threshold`,
`watermark_angle_threshold`, `ignore_vertical_text`. It finds all five.

## 8. What was NOT tested

- The generated LLMWhisperer SDK has **never been run against the live service** — no local
  instance exists and the hosted API is metered. Its request shape was verified by
  inspecting `_get_kwargs` output (correct path, correct query keys, `true`/`false` bool
  encoding that matches the service's `.lower() == "true"` parsing), not by a round trip.
- Only `POST /deployment/api/…` and its status `GET` were exercised end to end.
- The 307-route tenant spec (`--urlconf backend.urls_v2`) was not regenerated in this pass.

## 9. Where the findings are

`GAPS.md` — what broke, what the approach costs, and what is unsustainable about it.
