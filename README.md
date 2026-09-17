# unstract-cli
[![PyPI - Downloads](https://img.shields.io/pypi/dm/unstract-cli)](https://pypi.org/project/unstract-cli/)
[![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2FZipstack%2Funstract-cli%2Fmain%2Fpyproject.toml)
](https://pypi.org/project/unstract-cli/)
[![PyPI - Version](https://img.shields.io/pypi/v/unstract-cli)](https://pypi.org/project/unstract-cli/)

`unstract` runs [LLMWhisperer](https://docs.unstract.com/llmwhisperer/) text
extraction and [Unstract](https://docs.unstract.com/unstract/) API deployments
from the terminal. Pass `-o json` and every command prints one JSON envelope, so a
shell script or a coding agent can drive it.

Full reference: <https://docs.unstract.com/unstract/unstract_platform/cli/unstract_cli/>

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/Zipstack/unstract-cli/main/install.sh | sh
```

The installer fetches [`uv`](https://docs.astral.sh/uv/) if it is missing and
installs the CLI with it; `uv` brings its own Python. With `uv` or `pip`
already there:

```bash
uv tool install unstract-cli   # or: pip install unstract-cli
unstract --version
```

## Get your keys

| Key | Where it is minted | What it does |
| --- | --- | --- |
| **Platform key** | An organisation admin, under **Settings → Platform API Keys** in the Unstract UI | Platform related operations and to identify the organization |
| **Deployment key** | The API deployment's own page in the Unstract UI or an organisation admin mints one under **Settings → Global API Deployment Keys** | Runs deployments (`deployment run`, `deployment status`) |
| **LLMWhisperer key** | The LLMWhisperer console | Extracts text (`whisper …`) |

## Set up

```bash
unstract auth login
```

A wizard asks for each product's URL (Enter keeps the cloud host; self-hosted,
type your own) and API keys, then writes `~/.unstract/config.toml`. Then verify:

```bash
unstract auth whoami             # which organisation the platform key belongs to
unstract config doctor --probe   # where each setting resolved from, keys checked
```

## Usage

```bash
# Extract text from a document (path or URL); waits for the result
unstract whisper extract invoice.pdf -o raw > invoice.txt

# What deployments can I run?
unstract docstudio deployment ls

# Run one and wait for the structured result
unstract docstudio deployment run invoice-parser invoice.pdf

# Long job: submit, then check later
unstract docstudio deployment run invoice-parser invoice.pdf --no-wait
unstract docstudio deployment status invoice-parser <execution_id>
```

`--help` on any command lists its options; `unstract --discover full` prints
the whole command tree, every flag and the exit-code table as JSON.

## Configuration

`~/.unstract/config.toml`, or `$UNSTRACT_CONFIG`, or `--config`, or a
project-local `.unstract.toml` is found by upward search. Here's an example config that uses environment variables for the API keys.

```toml
default_profile = "cloud-us"

[profiles.cloud-us.docstudio]
base_url = "https://us-central.unstract.com"
org_id = "org_ABC123"
platform_key = "env:UNSTRACT_PLATFORM_KEY"
api_key = "env:UNSTRACT_DEPLOYMENT_KEY"

[profiles.cloud-us.llmwhisperer]
base_url = "https://llmwhisperer-api.us-central.unstract.com/api/v2"
api_key = "env:LLMWHISPERER_API_KEY"

# Only for a deployment whose key differs from the profile's.
[profiles.cloud-us.deployments."invoice-parser"]
api_key = "env:INVOICE_PARSER_KEY"
```

Every setting resolves **flag > environment > profile > built-in default**.
`auth login` writes keys literally; `env:VAR_NAME` keeps them out of the file.
The connection flags on each group (`--base-url`, `--api-key`, `--org-id`,
`--platform-key`) override the profile for one invocation without writing
anything. `config doctor` reports where each setting resolved from without
echoing a value, and exits non-zero when one of its checks fails.

### Environment variables

The same settings without a file, for CI, containers and agents:

```bash
export UNSTRACT_PLATFORM_KEY=...      # auth whoami, deployment ls
export UNSTRACT_DEPLOYMENT_KEY=...    # deployment run / status
export UNSTRACT_ORG_ID=...            # the organisation id auth whoami reports
export LLMWHISPERER_API_KEY=...       # whisper …
export UNSTRACT_BASE_URL=...          # self-hosted only
export LLMWHISPERER_BASE_URL=...      # self-hosted only
```

`auth login` also takes each key as a flag (`--platform-key`, `--deployment-key`,
`--llmwhisperer-key`; `-` reads it from stdin) and the host as `--base-url`, so
it runs without a terminal too.

## Output for scripts and agents

**Parsing anything? Pass `-o json`.** stdout then carries exactly one envelope,
on success and on failure alike, and diagnostics go to stderr:

```json
{"ok": true, "data": {...}, "error": null, "meta": {"contract_version": 1}}
```

Ignore fields you do not recognise; refuse a `meta.contract_version` above the
one you were written against. `-o raw` prints one field unwrapped. When a
coding agent is driving (detected from the environment it sets) json is the
default; `--agent yes|no` forces that, and an explicit `-o` wins over both.

Failures exit non-zero with a stable code:

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | generic failure |
| 2 | usage error |
| 3 | authentication failed |
| 4 | not found |
| 5 | validation failed — also a completed run in which a document failed; the full result, successful documents included, is in `error.details` |
| 6 | rate limited |
| 7 | timed out (the job handle is in the error payload — resume, do not resubmit) |
| 8 | server error |
| 9 | result already consumed (one-shot read; use `--save` next time) |
| 10 | the result was read but could not be saved — it is in `error.details` |
| 130 | interrupted (128 + SIGINT) — the user stopped it, not a failure |

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

## Questions and Feedback

On Slack, [join great conversations](https://join-slack.unstract.com/) around LLMs, their ecosystem and leveraging them to automate the previously unautomatable!

[Unstract Cloud](https://unstract.com/): Signup and Try!
