# AGENT_BRIEF — running the POC CLI

Where the CLI is and how to start it. Nothing else on purpose — the point of handing you this
file alone is to find out how far the CLI gets you by itself.

Branch: `poc/openapi-pipeline`.

## The two CLIs

| Script | Transport |
|---|---|
| `poc/cli_generated.py` | SDKs generated from the OpenAPI specs in `specs/` |
| `poc/cli_published.py` | the published PyPI clients |

They are meant to expose the same commands and produce the same output. There is no installed
`unstract` binary; run the scripts with the interpreter below.

## Setup

```bash
cd ~/zipstuff/unstract-cli

uv venv build/poc-venv
uv pip install --python build/poc-venv/bin/python click requests httpx attrs tenacity
uv pip install --python build/poc-venv/bin/python -e ~/zipstuff/llm-whisperer-python-client
uv pip install --python build/poc-venv/bin/python unstract-client

bash tools/gen_sdk.sh          # regenerates build/sdk_* from the committed specs
export PYTHONPATH=build:poc

build/poc-venv/bin/python poc/cli_generated.py --help
```

Two offline checks, no network, under a second each:

```bash
build/poc-venv/bin/python poc/test_compat.py
build/poc-venv/bin/python poc/test_llmw_compat.py
```

## Credentials

Never paste a key into a file, a commit, or a report.

- **LLMWhisperer:** `set -a && . ~/zipstuff/llm-whisperer-python-client/.env && set +a` — a staging
  key, and the only one to use. **Do not use `~/zipstuff/llm-whisperer-python-client/tests/.env`;
  that is production.**
- **Document Studio:** an API deployment and its key. `RUNBOOK.md` §5 has the SQL to find one on a
  local stack.

## Rules

- **Never edit `build/`** — it is regenerated wholesale. Fixes belong in `tools/annotations.py`
  or `specs/overlay/llmwhisperer.yaml`.
- Re-run both compat checks after touching anything in `poc/`.

`RUNBOOK.md` explains how the pipeline is built and how to regenerate it.
