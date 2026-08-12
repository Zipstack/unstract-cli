# unstract-cli

`unstract` — one CLI for the Unstract suite: extract a document with
LLMWhisperer, run it through a Document Studio API deployment, get structured
JSON back.

```bash
pipx install git+https://github.com/Zipstack/unstract-cli
unstract config init
unstract config doctor
```

## Output contract

stdout always carries exactly one JSON envelope, on success and on failure
alike:

```json
{"ok": true, "data": {...}, "error": null, "meta": {}}
```

Parsing never needs to check whether a terminal is attached. Diagnostics,
warnings and progress go to stderr. `--output table` and `--output raw` are
opt-in renderings of `data` for humans and pipes.

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

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest        # offline; no network, no credentials
ruff check .
```
