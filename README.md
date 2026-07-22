# Unstract CLI

**Unstract** is the company, and `unstract` is the name of this CLI. It builds
three products, and this one tool covers all of them:

| Product | Group | Covers |
| --- | --- | --- |
| **Document Studio** | `docstudio` | Document extraction platform — Platform Management API, deployed API workflows, Human Quality Review. *Formerly named Unstract.* |
| **LLMWhisperer** | `whisper` | Convert documents to LLM-ready text |
| **API Hub** | `apihub` | Vertical extraction — bank statements, tables, document splitting |

Built for **LLM agents first**: machine-readable discovery, stable exit codes,
structured errors, and no interactive prompts anywhere.

```bash
unstract whisper extract --file invoice.pdf --mode form --wait --save result.json
```

## Install

```bash
uv pip install -e ".[dev]"     # development
pip install unstract-cli       # once published
```

Requires Python 3.12+.

## Quick start

The CLI works with **no config file** — environment variables are enough:

```bash
export LLMWHISPERER_API_KEY=your-key
unstract whisper usage
```

For repeated use, create profiles:

```bash
unstract config init          # writes ~/.config/unstract/config.toml (mode 0600)
unstract config use cloud-eu  # switch regions
unstract config current       # show what is resolved right now

# Read and write settings. The target names an API group through its product,
# so a setting always says which product it configures. Either separator works.
unstract config set docstudio.platform org_id org_ABC123
unstract config set docstudio platform org_id org_ABC123
unstract config get llmwhisperer base_url
```

Valid targets: `docstudio.platform`, `docstudio.deployment`, `docstudio.hitl`,
`llmwhisperer`, `apihub`.

Config blocks have **exactly one accepted layout** — the API group nested under
its product. There are no aliases and no flat fallback, so a block written any
other way is ignored rather than half-applied:

```toml
[profiles.cloud-us.docstudio.platform]   # ✓ read
[profiles.cloud-us.platform]             # ✗ ignored
[profiles.cloud-us.whisper]              # ✗ ignored (use llmwhisperer)
```

Settings resolve in one order, everywhere: **flag → environment variable →
profile → built-in default**. Credentials in the config file use `env:VAR_NAME`
indirection, so the file records *where* secrets live rather than the secrets
themselves.

### Multiple configs

Nothing is global-only. There are three independent ways to select a config, and
they layer — different environments, tenants or projects can each have their own:

| Mechanism | Scope | Use for |
| --- | --- | --- |
| **Profiles** in one file (`--profile`) | per invocation | regions and tenants that share a machine |
| **`.unstract.toml`** in a project | per directory tree | settings a repo commits for everyone working in it |
| **`--config PATH`** / `$UNSTRACT_CONFIG` | per invocation | throwaway or CI configs, or fully separate environments |

Config *file* resolution, highest precedence first:

1. `--config PATH`
2. `$UNSTRACT_CONFIG`
3. the nearest `.unstract.toml`, found by walking up from the working directory
   (like `git` or `ruff`; the search stops at `$HOME`)
4. `$XDG_CONFIG_HOME/unstract/config.toml`, else `~/.config/unstract/config.toml`

```bash
unstract --config ./staging.toml whisper usage    # a specific file
unstract --profile cloud-eu whisper usage         # a profile within the active file
cd my-project && unstract whisper usage           # picks up ./.unstract.toml
```

A project file is ordinary config, so it can hold several profiles too:

```toml
default_profile = "dev"

[profiles.dev.docstudio.platform]
base_url = "https://dev.internal/"
org_id   = "org_dev"
api_key  = "env:UNSTRACT_PLATFORM_KEY"

[profiles.prod.docstudio.platform]
base_url = "https://us-central.unstract.com"
org_id   = "org_prod"
api_key  = "env:UNSTRACT_PROD_KEY"
```

Because credentials use `env:` indirection, such a file is safe to commit.

## For agents

Discover the entire surface without documentation. Start cheap, then drill in —
selection (`--group` / `--command`) and verbosity (`--detail`) are independent,
so any combination works:

```bash
unstract --discover                                  # all 143, names + summaries
unstract --discover --group docstudio                # one product
unstract --discover --command 'whisper webhook'      # one subtree
unstract --discover --command 'whisper extract' --detail full
unstract --discover --detail full                    # everything
```

At `--detail full` each entry carries the underlying HTTP method and path, every
flag with type, default, enum and required-ness, plus whether the command
supports `--wait` and whether its result is one-shot.

**Token cost** matters when an agent reads this into context, so the default is
the cheap index:

| Invocation | Tokens |
| --- | --- |
| `--discover` (default) | ~4,500 |
| `--group whisper` | ~470 |
| `--command 'whisper extract' --detail full` | ~1,900 |
| `--group whisper --detail full` | ~4,200 |
| `--detail full` (everything) | ~50,000 |

Output is compact JSON when piped and pretty-printed on a terminal.

### Output

`--output json|yaml|table|raw`. **JSON is the default whenever stdout is not a
TTY**, so piping needs no flags. Payloads go to stdout and nothing else;
diagnostics and errors go to stderr, so `unstract ... | jq` always parses.

### Exit codes

| Code | Meaning | Code | Meaning |
| --- | --- | --- | --- |
| 0 | success | 5 | validation error |
| 1 | generic error | 6 | rate limited |
| 2 | usage error | 7 | timed out waiting |
| 3 | auth failure | 8 | server error |
| 4 | not found | 9 | result already consumed |

Failures also emit a JSON object on stderr with `code`, `message`, `hint` and
`retryable`, so an agent can self-correct rather than retry blindly.

### One-shot results

Some results can be retrieved **exactly once** — LLMWhisperer retrieval, the
deployment status API, and HITL dequeues. A second read cannot recover them.
Those commands are marked `one_shot` in `--discover`, and all accept
`--save PATH`, which writes the payload to disk atomically before exiting. A
consumed result exits `9`.

### Waiting

Execute → poll → retrieve flows accept `--wait` so an agent needn't script the
loop:

```bash
unstract docstudio deployment run --api-name invoice-api --file invoice.pdf --wait
```

On timeout the CLI exits `7` and prints the job handle, so work can resume
without reprocessing the document.

## Command groups

| Command | Product | Covers |
| --- | --- | --- |
| `docstudio platform` | Document Studio | Prompt Studio, workflows, deployments, pipelines, adapters, connectors, groups, sharing |
| `docstudio deployment` | Document Studio | Run deployed API workflows; status; highlight data |
| `docstudio hitl` | Document Studio | Human Quality Review: approved results, bulk download |
| `whisper` | LLMWhisperer | Extraction, status, retrieve, highlights, usage, webhooks |
| `apihub` | API Hub | Bank statements, tables, document splitting |
| `config` | *local only* | Profile management (no network calls) |

Document Studio exposes three API groups, each with its own host and
credentials, so they remain separate under one product.

Every group and command has full `--help` with worked examples.

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy src        # types
```

### Architecture

Commands are **generated from declarative `Endpoint` records** in
`src/unstract_cli/endpoints/`. The command tree, flags, help text, validation and
`--discover` all derive from those records, so adding an endpoint means
adding one record and help text cannot drift from behaviour.

That single source of truth is also what makes the bundled Claude Skill
(`.claude/skills/update-unstract-cli/`) possible: it cross-references the public
API documentation and updates the records when the APIs change.

See [`SPEC.md`](./SPEC.md) for the full specification and
[`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) for the build sequence.
