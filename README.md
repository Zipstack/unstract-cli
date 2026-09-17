# unstract-cli

`unstract` runs LLMWhisperer text extraction and Unstract API deployments from
the terminal. Pass `-o json` and every command prints one JSON envelope, so a
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
uv tool install --prerelease allow unstract-cli   # or: pip install --pre unstract-cli
unstract --version
```

Only release candidates are on PyPI so far, hence the pre-release flag.

## Get your keys

| Key | Where it is minted | What it does |
| --- | --- | --- |
| **Platform key** | An organisation admin, under **Settings → Platform API Keys** in the Unstract UI | Identifies the organisation and lists what is in it (`auth whoami`, `deployment ls`); cannot run a deployment |
| **Deployment key** | The API deployment's own page in the Unstract UI; an organisation admin mints one covering every deployment in the organisation under **Settings → Global API Deployment Keys** | Runs deployments (`deployment run`, `deployment status`) |
| **LLMWhisperer key** | The LLMWhisperer console | Extracts text (`whisper …`) |

You need only the keys for what you run. A platform key and a deployment key
together cover the whole Unstract side.

## Set up

```bash
unstract auth login
```

The wizard asks, in order:

1. `docstudio base URL [https://us-central.unstract.com]:` — Enter keeps the
   cloud host. Self-hosted? Type your own, e.g. `https://unstract.example.com`.
2. `llmwhisperer base URL [https://llmwhisperer-api.us-central.unstract.com/api/v2]:`
   — the same for LLMWhisperer.
3. Platform key, deployment key, LLMWhisperer key — hidden input, Enter skips
   one; at least one is needed.

It checks the platform key and the LLMWhisperer key against their services
(a deployment key has nothing side-effect-free to call, so it is stored as
given), resolves your organisation, and writes `~/.unstract/config.toml`
owner-only. Run it again to rotate a key; a login that stores a different host
asks before dropping keys that were checked against the old one.

Then verify:

```bash
unstract auth whoami             # which organisation the platform key belongs to
unstract config doctor --probe   # where each setting resolved from, keys checked
```

## Without a terminal: CI, containers, agents

No prompts and no file — the environment is the profile:

```bash
export UNSTRACT_PLATFORM_KEY=...      # optional: auth whoami, deployment ls
export UNSTRACT_DEPLOYMENT_KEY=...    # deployment run / status
export UNSTRACT_ORG_ID=...            # the organisation id auth whoami reports
export LLMWHISPERER_API_KEY=...       # whisper …
export UNSTRACT_BASE_URL=https://unstract.example.com   # self-hosted only
export LLMWHISPERER_BASE_URL=https://whisperer.example.com/api/v2   # self-hosted only

unstract -o json auth whoami
```

Or store them once, non-interactively — keys as flags, any one of them `-` to
read from stdin, and `--base-url` for a self-hosted host:

```bash
printf '%s' "$UNSTRACT_PLATFORM_KEY" | unstract auth login \
  --platform-key - \
  --deployment-key "$UNSTRACT_DEPLOYMENT_KEY" \
  --llmwhisperer-key "$LLMWHISPERER_API_KEY"
```

`unstract config init` writes a starter file that references those variables
(`api_key = "env:UNSTRACT_DEPLOYMENT_KEY"`) instead of holding secrets, so it is
safe to commit. Every setting resolves **flag > environment > profile >
built-in default**.

## Use it

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

## Configuration

`~/.unstract/config.toml`, or `$UNSTRACT_CONFIG`, or `--config`, or a
project-local `.unstract.toml` found by upward search (which may select a
profile and an `org_id` but may not supply a key or a host — name the file
explicitly and it is honoured in full).

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

`auth login` writes keys literally; `env:VAR_NAME` keeps them out of the file.
The connection flags on each group (`--base-url`, `--api-key`, `--org-id`,
`--platform-key`) override the profile for one invocation without writing
anything. `config doctor` reports where each setting resolved from without
echoing a value, and exits non-zero when one of its checks fails.

`clone` moves one organisation's resources into another, holding two admin
platform keys as `UNSTRACT_SRC_PLATFORM_KEY` / `UNSTRACT_TGT_PLATFORM_KEY`. It
is an operator command, not part of the document-processing path.

## Development

```bash
uv sync --extra dev
uv run pytest   # offline; no network, no credentials
uv run ruff check .
```
