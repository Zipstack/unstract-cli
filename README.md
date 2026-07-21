# Unstract CLI

A single, LLM-friendly command-line interface across the Unstract product suite:
LLMWhisperer text extraction, deployed API workflows, the Platform Management
API, Human Quality Review, and API Hub.

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
```

Settings resolve in one order, everywhere: **flag → environment variable →
profile → built-in default**. Credentials in the config file use `env:VAR_NAME`
indirection, so the file records *where* secrets live rather than the secrets
themselves.

## For agents

Discover the entire surface without documentation:

```bash
unstract --dump-commands      # full command tree as JSON
```

Each entry carries the underlying HTTP method and path, every flag with type,
default, enum and required-ness, plus whether the command supports `--wait`, and
whether its result is one-shot.

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
Those commands are marked `one_shot` in `--dump-commands`, and all accept
`--save PATH`, which writes the payload to disk atomically before exiting. A
consumed result exits `9`.

### Waiting

Execute → poll → retrieve flows accept `--wait` so an agent needn't script the
loop:

```bash
unstract deployment run --api-name invoice-api --file invoice.pdf --wait
```

On timeout the CLI exits `7` and prints the job handle, so work can resume
without reprocessing the document.

## Command groups

| Group | Covers |
| --- | --- |
| `whisper` | LLMWhisperer v2: extraction, status, retrieve, highlights, usage, webhooks |
| `deployment` | Run deployed API workflows; status; highlight data |
| `platform` | Prompt Studio, workflows, deployments, pipelines, adapters, connectors, groups, sharing |
| `hitl` | Human Quality Review: approved results, bulk download |
| `apihub` | Vertical extraction: bank statements, tables, document splitting |
| `config` | Local profile management (no network calls) |

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
`--dump-commands` all derive from those records, so adding an endpoint means
adding one record and help text cannot drift from behaviour.

That single source of truth is also what makes the bundled Claude Skill
(`.claude/skills/update-unstract-cli/`) possible: it cross-references the public
API documentation and updates the records when the APIs change.

See [`SPEC.md`](./SPEC.md) for the full specification and
[`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) for the build sequence.
