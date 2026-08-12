# Runbook

Maintainer procedures. For what the CLI does and how to configure it, see the
[README](README.md); this file covers the things that are done *to* the CLI —
installing a build, moving the client pins, proving a build against real
services, and cutting a release.

## Install

### From a published ref

```bash
pipx install git+https://github.com/Zipstack/unstract-cli
unstract --version
```

Pin the ref when reproducing a report:

```bash
pipx install "git+https://github.com/Zipstack/unstract-cli@<tag-or-sha>"
```

`pipx` puts each install in its own virtualenv, which matters here: the two
clients are pinned to exact commits, and a shared environment would let another
package's resolver move them.

### Other names for the same CLI

- `unstract-cli` — a second console script this package always owns.
- `python -m unstract_cli` — works from a source checkout with no install at all.

`unstract-client` released before this CLI installed a console script called
`unstract` too. An environment that still holds one of those versions gives the
name to whichever package was installed last, so check what answers before
filing a bug about a missing command:

```bash
command -v unstract && unstract --version
```

### From a checkout

```bash
uv venv && uv pip install -e '.[dev]'
pytest          # offline: no network, no credentials
ruff check .
```

## Moving the client pins

The CLI derives its flags from the vendored specs intersected with the pinned
clients' signatures, and takes flag help from those clients' docstrings. Moving
a pin therefore changes the CLI's surface without a line of CLI code changing.
That is the intent, so the check is that the change was the intended one:

1. Update the `unstract-client` and/or `llmwhisperer-client` ref in
   `pyproject.toml`.
2. Refresh the vendored spec if the service's spec moved too — see
   [`src/unstract_cli/specs/README.md`](src/unstract_cli/specs/README.md).
   A spec and a client from different commits is exactly the state
   `tests/test_contract.py` exists to catch.
3. `uv pip install -e '.[dev]' && pytest`.
4. Diff the surface before and after:

   ```bash
   python -m unstract_cli -o json --discover full > after.json
   ```

   Every added or removed flag should be one you can name a reason for.
   `tests/test_contract.py` pins the spec parameters no command can reach; that
   set should only ever shrink, and only on purpose.

Both pins move to released versions before this ships publicly.

## Live gate

The offline suite proves the CLI is self-consistent. It cannot prove the
services agree, and the defects worth catching here have all been of that kind:
a payload shaped differently from the spec, a status code meaning something
other than it appears to, geometry that divides by a value the service reports
as zero. Run this against a real tenant before tagging a release.

### Credentials

Supply them through the environment, never on the command line and never in a
file inside this repository:

```bash
export LLMWHISPERER_API_KEY=...
export UNSTRACT_DEPLOYMENT_KEY=...
export UNSTRACT_BASE_URL=https://<host>
export UNSTRACT_ORG_ID=org_...
```

Use a staging tenant. Passing `--api-key` works and warns, because a key on the
command line lands in shell history and in the process list.

### Checklist

Run against a document you can re-send; several of these submit real work.

| # | Command | Pass |
|---|---|---|
| 1 | `config doctor --probe` | every setting reports where it resolved from; the LLMWhisperer probe answers live |
| 2 | `whisper extract <pdf>` | polls to completion, returns text |
| 3 | `whisper extract <pdf> --no-wait` then `whisper status <hash>` then `whisper retrieve <hash>` | the handle survives the round trip |
| 4 | `whisper retrieve <hash>` a second time | refused, exit 9, and the error names the one-shot read |
| 5 | `whisper highlights <hash> --target-width 800 --target-height 1000` | bounding boxes for the lines that carry geometry, and no traceback for the lines that do not |
| 6 | `whisper usage` | quota returned |
| 7 | `docstudio deployment run <alias-or-api-name> <pdf>` | polls to completion, returns structured JSON |
| 8 | `docstudio deployment run <target> <pdf> --no-wait`, then `docstudio deployment status <target> <execution_id>` from the run envelope | the handle survives the round trip |
| 9 | any command with `-o raw` | one field, not the envelope |
| 10 | any command with `-o json` and a wrong key | exit 3, JSON envelope on stdout, no traceback |
| 11 | any command with `-o json` and a path that does not exist | exit 2, JSON envelope on stdout |
| 12 | any command with no `-o` | a table, in a terminal and through a pipe alike |
| 13 | `clone --source-url ... --target-url ... --dry-run` | the plan is reported and nothing is written to the target |

Two properties matter more than any single row, because they are what a caller
depends on and what breaks quietly:

- **With `-o json`, stdout is one envelope in every case above, including the
  failures.**
  A traceback on stderr with empty stdout is a bug even when the exit code is
  right.
- **A flag passed explicitly reaches the wire, including when its value is
  falsy.** `--no-include-metadata` must produce a different payload than passing
  nothing at all. A flag that is silently dropped looks identical to a flag that
  worked.

### Interpreting a failure

A live failure is a finding about the CLI, the client, or the service, in that
order of likelihood — check which layer the response actually came from before
changing anything. Fixes go in the facade or the spec; never in a generated
directory, whose contents are replaced wholesale on the next generation.

## Release

1. Live gate green against staging.
2. `pytest` and `ruff check .` clean.
3. Both client pins on released versions, not commits.
4. Tag, then verify the tag installs clean in an environment that has nothing
   else in it:

   ```bash
   pipx install --force "git+https://github.com/Zipstack/unstract-cli@<tag>"
   unstract-cli --version
   unstract-cli --discover groups
   ```

5. `--discover groups` on the fresh install should match the checkout's. It is
   the cheapest proof that the built wheel carries the specs — they are package
   data, and package data is what a build configuration silently drops.
