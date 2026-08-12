# unstract-cli

`unstract` — one CLI for the Unstract suite: extract a document with
LLMWhisperer, run it through a Document Studio API deployment, get structured
JSON back. It also clones one organization's resources into another.

```bash
pipx install git+https://github.com/Zipstack/unstract-cli
unstract config init
unstract config doctor
```

## Output

`unstract` prints a table by default — in a terminal and in a pipe alike, so
what you see while trying something is what a script sees running it.

**Parsing anything? Pass `-o json`.** stdout then carries exactly one envelope,
on success and on failure alike:

```json
{"ok": true, "data": {...}, "error": null, "meta": {"contract_version": 1}}
```

`-o json` output depends on nothing but the command and its arguments — not the
terminal, not the config, not the environment. `-o raw` prints one field
unwrapped, for piping a document's text somewhere else. Diagnostics, warnings
and progress always go to stderr.

Consuming the JSON: ignore fields you do not recognise, and refuse a
`meta.contract_version` above the one you were written against. `unstract
--discover full` publishes the whole contract alongside every command and flag.

If a coding agent is driving (detected from the environment it sets), the
*default* becomes json. `--agent yes|no` forces that either way, and an explicit
`-o` always wins over both.

Failures exit non-zero with a stable code:

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | generic failure |
| 2 | usage error |
| 3 | authentication failed |
| 4 | not found |
| 5 | validation failed |
| 6 | rate limited |
| 7 | timed out (the job handle is in the error payload — resume, do not resubmit) |
| 8 | server error |
| 9 | result already consumed (one-shot read; use `--save` next time) |
| 10 | the result was read but could not be saved — it is in `error.details` |
| 130 | interrupted (128 + SIGINT) — the user stopped it, not a failure |

## Configuration

`~/.unstract/config.toml`, or a project-local `.unstract.toml` found by upward
search, or `$UNSTRACT_CONFIG`, or `--config`. Every setting resolves
**flag > env > profile > built-in default**, and the CLI is fully usable with no
config file at all.

```toml
default_profile = "cloud-us"

[profiles.cloud-us.llmwhisperer]
base_url = "https://llmwhisperer-api.us-central.unstract.com/api/v2"
api_key = "env:LLMWHISPERER_API_KEY"

[profiles.cloud-us.docstudio]
base_url = "https://us-central.unstract.com"
org_id = "org_ABC123"
api_key = "env:UNSTRACT_DEPLOYMENT_KEY"

[profiles.cloud-us.deployments.invoices]
api_name = "invoice-parser"
```

Credentials use `env:VAR_NAME` indirection, so the file records where a secret
lives rather than the secret itself. `unstract config doctor` reports where each
setting resolved from — including whether an `env:` reference is actually set in
the current process — without echoing any value.

`clone` is the exception: it talks to two deployments at once, which no single
profile describes, so it takes both endpoints as flags and both admin Platform
keys from `UNSTRACT_SRC_PLATFORM_KEY` / `UNSTRACT_TGT_PLATFORM_KEY`.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest        # offline; no network, no credentials
ruff check .
```
