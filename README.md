# Unstract CLI

The `unstract` CLI tool covers all products built by **Unstract** team:

| Product | Group | Covers |
| --- | --- | --- |
| **Document Studio** | `docstudio` | Document extraction platform — Platform Management API, deployed API workflows, Human Quality Review |
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

### Reproducible, hash-pinned installs (recommended)

Because supply-chain attacks target the *release you download* — a compromised
version pushed to an index — the strongest defence is to install only exact,
content-verified artifacts. A lockfile with hashes pins every package in the
dependency tree (transitive ones included) so a swapped-out release fails the
install instead of executing.

The repo commits [`uv.lock`](./uv.lock) for exactly this. Regenerate it whenever
`pyproject.toml` changes, and review the diff:

```bash
uv lock                        # resolve + write uv.lock with hashes for the whole tree
uv lock --check                # CI: fail if uv.lock is stale vs pyproject.toml
```

Install from the lockfile — never re-resolving, so what runs is what was
reviewed:

```bash
uv sync --frozen --extra dev   # dev: exact env from uv.lock, with test/lint tools
uv sync --frozen               # runtime-only (dev tools are an extra, so omitted here)
```

The dev tools (pytest, ruff, mypy) are declared as the `dev` **extra**, so they
install only when you ask for `--extra dev`; a plain `uv sync` gives the runtime
set alone.

If you deploy with `pip` rather than `uv`, export the lock to a hashed
requirements file and install with `--require-hashes` (which refuses any package
whose hash is absent or mismatched):

```bash
uv export --frozen --no-dev --no-emit-project -o requirements.txt
pip install --require-hashes --no-deps -r requirements.txt
```

`--no-deps` is deliberate: the exported file already lists the *complete*,
resolved tree, so pip must not reach out and resolve anything itself. Use a
recent pip (`pip install --upgrade pip` first); older pip verifies hashes
inconsistently when a package builds from an sdist.

**Practices worth keeping:**

- **Commit `uv.lock`** and gate CI on `uv lock --check`, so a dependency can only
  change through a reviewed pull request, never silently at install time.
- **Install with `--frozen` / `--require-hashes`** everywhere — locally, in CI, and
  in production — so no environment re-resolves to a different version.
- **Pin the Python too** (`.python-version`, already `>=3.12`); the interpreter is
  part of the supply chain.
- **Verify before bumping.** When a lock update changes a package, read the diff
  and the upstream changelog rather than accepting the resolution blind.

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

Document Studio's three API groups each have their own block, but they address
one organization, so `org_id` falls back to `docstudio.platform` when the
`deployment` or `hitl` block does not set it — configure it once. Credentials
never fall back: keys are per-group, and silently reusing one across groups
would be credential confusion rather than convenience.

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

Discover the entire surface without documentation. Start with the group map, then
drill into exactly the subtree you need — selection (`--group` / `--command`) and
verbosity (`--detail`) are independent, so any combination works:

```bash
unstract --discover                                  # the ~15 groups, with counts (default)
unstract --discover --command 'docstudio platform prompt-studio'   # one subtree, names + summaries
unstract --discover --detail summary                 # every command, names + summaries
unstract --discover --command 'whisper extract' --detail full
unstract --discover --detail full                    # everything
```

The default is a **map of the navigable groups** — each with a one-line summary, a
command count, and the exact `drill` command that lists it. An agent reads that
first (~1k tokens), then follows one `drill` into the subtree it actually needs
rather than pulling all 156 commands into context. At `--detail full` each command
carries the underlying HTTP method and path, every flag with type, default, enum
and required-ness, plus whether it supports `--wait` and whether its result is
one-shot.

**Token cost** matters when an agent reads this into context, so the default is
the cheapest useful view:

| Invocation | Tokens |
| --- | --- |
| `--discover` (default, group map) | ~1,100 |
| `--command 'whisper extract' --detail full` | ~1,900 |
| `--detail summary` (all commands, one-liners) | ~5,800 |
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

### Dependencies

Kept deliberately small, to limit supply-chain exposure: **click** (dynamic
command tree + `--discover`), **httpx** (TLS, redirects, multipart uploads),
**pyyaml** (`--output yaml`), **tomli-w** (config writer; reading uses the stdlib
`tomllib`). Config parsing needs no third-party TOML reader. The set stops here
rather than at zero on purpose — replacing httpx or the TOML writer means shipping
unaudited network/serialisation code, a worse trade than depending on
widely-reviewed packages. The remaining risk — a *compromised release* of one of
those packages — is addressed by hash-pinned installs, not by removing audited
dependencies: see [Reproducible, hash-pinned installs](#reproducible-hash-pinned-installs-recommended).

See [`SPEC.md`](./SPEC.md) for the full specification and
[`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) for the build sequence.
