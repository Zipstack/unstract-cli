# AGENT_BRIEF — testing the POC CLI with no prior context

Written for an agent or person handed this repo and nothing else. Read this **before**
`RUNBOOK.md`; that file explains how the pipeline is built, this one explains what you can
actually run and what will waste your time.

Branch: `poc/openapi-pipeline`.

---

## 1. What this is in five lines

Two CLIs that do the same twelve things, one over hand-written published clients
(`poc/cli_published.py`) and one over SDKs generated from an OpenAPI spec
(`poc/cli_generated.py`). The point of the POC is that **the two agree**. Most useful work here
is running both and diffing, not running one and judging the output.

There is no installed `unstract` binary. Both CLIs are scripts run with an explicit interpreter.

## 2. What exists — the whole surface

Twelve commands. Nothing else is implemented.

| Group | Commands | Backend |
|---|---|---|
| `deployment` | `execute`, `status` | Document Studio (Django) |
| `whisper` | `extract`, `status`, `retrieve`, `detail`, `highlights`, `usage` | LLMWhisperer (Flask) |
| `webhook` | `register`, `update`, `get`, `delete` | LLMWhisperer |

Both scripts expose the same three groups.

## 3. What does NOT exist — read this before using PR #1's `E2E.md`

`E2E.md` on the `pr-graft` branch (PR #1) describes a different, much larger CLI. Against this
POC its scenarios **cannot be completed**, and failing to complete them is not a finding:

| `E2E.md` expects | Status here |
|---|---|
| A config file, `unstract config init`, temp config per run | **does not exist** — configuration is env vars and flags only |
| Creating a Prompt Studio project | **does not exist** |
| Exporting a project to a custom tool | **does not exist** |
| Deploying a project as an API | **does not exist** |
| Deleting created resources | **does not exist** — nothing here creates a deletable resource except webhooks |
| "Extract total bill amount from an invoice" | partially — `whisper extract` returns text; interpreting it needs an API deployment that already exists |
| An installed `unstract` binary | **does not exist** — run the scripts directly |

If you were asked to run `E2E.md`, say so and stop; the scenarios target the PR #1 codebase, not
this branch. §6 lists what is genuinely runnable.

## 4. Setup

```bash
cd ~/zipstuff/unstract-cli

uv venv build/poc-venv
uv pip install --python build/poc-venv/bin/python click requests httpx attrs tenacity
uv pip install --python build/poc-venv/bin/python -e ~/zipstuff/llm-whisperer-python-client
uv pip install --python build/poc-venv/bin/python unstract-client

bash tools/gen_sdk.sh          # regenerates build/sdk_* from the committed specs
export PYTHONPATH=build:poc
```

Offline checks — run these first, they need no network and take under a second:

```bash
build/poc-venv/bin/python poc/test_compat.py       # -> compat OK
build/poc-venv/bin/python poc/test_llmw_compat.py  # -> llmw compat OK
```

If either fails before you have changed anything, **stop and report it** — most likely one of
the published clients moved (see `GAPS.md` §17).

### Credentials

Never paste a key into a file, a commit or a report.

- **LLMWhisperer:** `set -a && . ~/zipstuff/llm-whisperer-python-client/.env && set +a`.
  This is a staging key and is the only one to use. Do **not** use
  `~/zipstuff/llm-whisperer-python-client/tests/.env` — that is production.
- **Document Studio:** needs a local or staging API deployment plus its key. `RUNBOOK.md` §5
  has the SQL to find one on a local stack, and the URL shape
  (`http://localhost:8000/deployment/api/<org_id>/<api_name>/` — port 8000, not 80).

## 5. What `--help` gives you, and what it does not

Be aware of this before relying on it. Top level:

```
Commands:
  deployment
  webhook
  whisper
```

No descriptions — not for groups, not for commands. Flag help is machine-derived:

```
--word-confidence-threshold FLOAT   (generated) default=0.3
--mode TEXT                         (generated) default='form'
```

That tells you a parameter's name, type and default. It does **not** tell you what it does,
which values are legal, or which parameters interact. The cause is measurable: the specs carry
**0 of 27** parameter descriptions for `whisper` and **1 of 12** for the deployment request
body, so there is nothing for the CLI to display.

**Do not guess parameter semantics from the flag name.** The real descriptions live in the
published client's docstring, which documents 25 of 25 parameters:

```bash
build/poc-venv/bin/python -c "
from unstract.llmwhisperer.client_v2 import LLMWhispererClientV2 as C
print(C.whisper.__doc__)"
```

For the deployment side, read `ExecutionRequestSerializer` in the `unstract` backend.

## 6. Scenarios that are actually runnable

Give each side its **own** run every time. Both status/retrieve endpoints are one-shot —
reading a completed result acknowledges it and the next read fails (`406 Result already
acknowledged`). Sharing one execution between the two CLIs makes the second look broken.

```bash
F=~/zipstuff/llm-whisperer-python-client/tests/test_data/utf_8_chars.pdf

# 1. text extraction, both sides, in parallel
build/poc-venv/bin/python poc/cli_generated.py whisper extract $F --wait          > gen.json &
build/poc-venv/bin/python poc/cli_published.py whisper extract $F --wait-timeout 600 > pub.json &
wait
# compare extraction.result_text and extraction.confidence_metadata

# 2. account info — fast, deterministic, no extraction
build/poc-venv/bin/python poc/cli_generated.py whisper usage
build/poc-venv/bin/python poc/cli_published.py whisper usage

# 3. webhook lifecycle (creates and deletes a real resource — clean up after)
build/poc-venv/bin/python poc/cli_generated.py webhook register NAME --callback-url URL
build/poc-venv/bin/python poc/cli_generated.py webhook get NAME
build/poc-venv/bin/python poc/cli_generated.py webhook delete NAME

# 4. deployment, both sides
UNSTRACT_API_URL=... UNSTRACT_API_KEY=... poc/compare.sh /path/to/sample.txt

# 5. error paths — the best value per second, and where every silent bug in this POC lived
#    bad key -> 401, garbage execution_id -> 4xx, an already-read status endpoint -> 406
```

Scenario 5 is worth more than scenario 1. Every defect this POC found was in error handling,
encoding, or which parameters got sent — never in the happy-path 200.

## 7. Telling a real difference from noise

Four things differ between two runs and **none of them is a defect**:

| Field | Why |
|---|---|
| `whisper_hash`, `execution_id`, `file_execution_id` | per-run identifiers |
| `whisper_metadata.avg_page_processing_time`, `elapsed_time` | server-side timing |
| One or two entries in `confidence_metadata` | OCR confidence on low-scoring glyphs is **not** stable run to run. Reproduced generated-vs-generated. Diff the count of differing entries, not equality |
| LLM free text, `completion_tokens`, `cost_in_dollars` | generation is nondeterministic. `prompt_tokens` matching is the evidence that both clients sent the same request |

Two more traps:

- **Staging can take minutes** on a photo-heavy PDF. A timeout is not a failure; raise
  `--wait-timeout` before concluding anything. `utf_8_chars.pdf` is the fast, deterministic file.
- **A workflow can genuinely reach two different terminal states.** One earlier pair diverged
  (`200/COMPLETED` vs `422/ERROR`) because the *server* recorded both; 1 of 148 staging
  executions in 30 days. Both clients reported faithfully. Use a deterministic deployment, or
  prefer error-path comparisons.

## 8. If you are asked to change something

- **Never edit `build/`.** It is regenerated wholesale. Fixes go in `tools/annotations.py`
  (Document Studio) or `specs/overlay/llmwhisperer.yaml` (LLMWhisperer).
- Re-run both compat checks after any change to `poc/*facade*.py`. They are the only thing
  standing between a network blip and an uncaught exception in the tool runtime.
- The compat checks are **per-method**. A facade method you add is uncovered until you add it
  to them.
- Read `RUNBOOK.md` §6 for the "you want to X → do Y" table before editing anything.

## 9. Where everything else is

| Question | File |
|---|---|
| How the pipeline works, how to regenerate, environment traps | `RUNBOOK.md` |
| Every number, with the command that produced it | `MEASUREMENTS.md` |
| What broke, and what is unsustainable | `GAPS.md` |
| Why any of it is shaped this way | the knowledge base — `INDEX.md` routes; ADRs hold decisions |
