# unstract-cli

`unstract` — one CLI for the Unstract suite: extract a document with
LLMWhisperer, run it through a Document Studio API deployment, get structured
JSON back. It also clones one organization's resources into another.

```bash
curl -LsSf https://raw.githubusercontent.com/Zipstack/unstract-cli/main/install.sh | sh
unstract auth login                   # asks for your keys, checks them, stores them
unstract docstudio deployment ls      # what can I run?
```

For an agent or CI, no prompts and no file — the environment is the profile:

```bash
export UNSTRACT_ORG_ID=... UNSTRACT_DEPLOYMENT_KEY=... LLMWHISPERER_API_KEY=...
unstract -o json whisper extract ./doc.pdf
unstract -o json docstudio deployment run invoice-parser ./doc.pdf
```

The installer fetches `uv` if it is missing and installs the CLI with it; `uv`
brings its own Python, so nothing on the machine has to match. Already have
`uv`? `uv tool install git+https://github.com/Zipstack/unstract-cli` is the same
thing. Set `UNSTRACT_CLI_SOURCE` to install a branch or a local checkout
instead.

Or run it without installing: `uvx --from git+https://github.com/Zipstack/unstract-cli unstract --discover groups`.

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

Failures exit non-zero with a stable code. The codes are this CLI's own
convention, not a service's — they are the `ExitCode` enum in
`core/errors.py`, and `--discover full` publishes the table so a caller does not
have to copy it:

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

## Credentials

Three keys, each for one job:

- **LLMWhisperer key** — extracts text (`whisper …`). Minted in the LLMWhisperer
  console.
- **Deployment key** — runs deployments (`deployment run`, `deployment status`).
  Shown on the API deployment's own page in the Unstract UI; one minted under
  **Settings → API Key Manager** covers every deployment in the organisation.
- **Platform key** — identifies the organisation and lists what is in it
  (`auth whoami`, `deployment ls`). Minted by an organisation admin under
  **Settings → Platform API Keys**.

`auth login` takes whichever of the three you have, checks the two it can
(`whoami` for the platform key, the usage endpoint for the LLMWhisperer key; a
deployment key has nothing side-effect-free to call and is stored as given) and
writes them to one profile. Run it again to rotate a key. Without a terminal
pass them as flags — `--platform-key`, `--deployment-key`, `--llmwhisperer-key`,
any one of them `-` to read from stdin.

## Configuration

`~/.unstract/config.toml`, or a project-local `.unstract.toml` found by upward
search, or `$UNSTRACT_CONFIG`, or `--config`. Every setting resolves
**flag > env > profile > built-in default**, and the CLI is fully usable with no
config file at all. The flag tier is the connection options on each product
group — `unstract docstudio --base-url … --org-id … deployment run …`, and
`--base-url`/`--api-key` on `whisper`, `--platform-key` on `auth` — which
override the profile for that one invocation without writing anything.

```toml
default_profile = "cloud-us"

[profiles.cloud-us.llmwhisperer]
base_url = "https://llmwhisperer-api.us-central.unstract.com/api/v2"
api_key = "env:LLMWHISPERER_API_KEY"

[profiles.cloud-us.docstudio]
base_url = "https://us-central.unstract.com"
org_id = "org_ABC123"
api_key = "env:UNSTRACT_DEPLOYMENT_KEY"
platform_key = "env:UNSTRACT_PLATFORM_KEY"

# Only for a deployment whose key differs from the one above.
[profiles.cloud-us.deployments."invoice-parser"]
api_key = "env:INVOICE_PARSER_KEY"
```

`deployment run` and `deployment status` take the API name as `deployment ls`
prints it. The key for a run resolves `--api-key` > `$UNSTRACT_DEPLOYMENT_KEY` >
the deployment's own entry > the profile's `api_key`, so most profiles need no
`deployments` section at all; `config set docstudio api_key <key> --deployment
<api_name>` writes one. `org_id` lives on the `docstudio` block — `auth login`
and `auth whoami` write the one the platform key resolves there. `config init`
writes this shape minus `platform_key` and the `deployments` entry — both are
the exception, not the starting point — plus an `onprem-example` profile to
copy for a self-hosted install; only the *active* profile is ever resolved.

`auth login` writes keys literally; `env:VAR_NAME` indirection is what
`config init` writes and what the example uses, so the file records where a
secret lives rather than the secret itself and stays safe to copy or commit.
Either way the file is created `0600`, and `config doctor` warns when its mode
is wider than that.

`unstract config doctor` reports where each setting resolved from — including
whether an `env:` reference is actually set in the current process — without
echoing any value. `--probe` also checks the keys against the service and, with
a platform key, warns about a `deployments` entry the organisation no longer
has. It exits non-zero when one of its own checks failed, so a setup script can
branch on it.

A project-local `.unstract.toml` **found by upward search** may not supply a
key or `base_url`. Those are ignored, with a warning; everything else in it —
profile selection, `org_id` — applies as usual. A checkout you did not write is
not trusted to name the host your key is sent to. Name the file explicitly
(`--config` or `$UNSTRACT_CONFIG`) and it is honoured in full.

What that protects is the key and the host, not the routing: `org_id` and
profile selection stay repo-controllable by design, so a project file can still
decide *which* organisation a command runs against on a host you trust. Read
one before you run inside a checkout you did not write.

`clone` is the exception, and it is an operator command: a human moving one
organisation's resources into another, holding two admin Platform keys. It is
not part of the document-processing path the rest of this CLI wraps, so an agent
serving a user request should not reach for it unasked. It talks to two
deployments at once, which no single profile describes, so it takes both
endpoints as flags and both keys from `UNSTRACT_SRC_PLATFORM_KEY` /
`UNSTRACT_TGT_PLATFORM_KEY` — two keys for two organisations, so it reads
neither the profile's `platform_key` nor `$UNSTRACT_PLATFORM_KEY`. It exits 0
when nothing failed, which is not the same as everything having moved: oversize
and unsupported documents are skipped by design, and `data.skipped` counts them.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest   # offline; no network, no credentials
uv run ruff check .
```
