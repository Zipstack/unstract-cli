---
name: bump-client-pins
description: Bump the exact `unstract-client` / `llmwhisperer-client` pins, re-sync the vendored specs to match, and cut a CLI release. Use whenever a new version of either client is released, when `tests/test_specs.py` or `tests/test_contract.py` fails, when the CLI is missing a flag for an endpoint the API already has, or when someone asks to "bump the client", "update the pins", "refresh the specs", or "release the CLI". Reach for this even when the request sounds like a plain dependency bump — the pins, the vendored specs and `provenance.json` have to move together or the CLI derives flags the pinned client cannot carry.
---

# Bumping the client pins

The CLI derives its flags and help text from two published clients and from
vendored copies of the specs those clients were generated from. That makes a pin
bump three coupled edits, not one: the pin, the spec, and the provenance record.
Move one without the others and the tests say so — which is the point of them.

## The pieces

| Thing | Where |
|---|---|
| Exact pins | `pyproject.toml`, `[project].dependencies` |
| Vendored specs | `src/unstract_cli/specs/{docstudio,llmwhisperer}.json` |
| Provenance | `src/unstract_cli/specs/provenance.json` (upstream repo, commit, sha256) |
| Coherence tests | `tests/test_specs.py`, `tests/test_contract.py`, `tests/derived_flags.json` |
| Release | `.github/workflows/release.yml`, `workflow_dispatch` |

`src/unstract_cli/specs/README.md` explains the vendoring rule in place; read it
if any of the below is unclear.

## The sequence

1. **Move the pins** in `pyproject.toml` to the released versions you are
   upgrading to. They are exact (`==`) on purpose: the CLI's published surface is
   derived from these clients, so a client that moves reshapes the CLI.

2. **Relock:** `uv lock` then `uv sync --extra dev --python 3.12`. CI installs
   from `uv.lock`, not from a fresh resolve, so a lockfile left behind means the
   gate tests a dependency set nobody ships.

3. **Re-sync each vendored spec from the commit the pinned client was generated
   from.** The chain is: the client repo's release tag → its `tools/gen_sdk.sh`,
   which records the upstream service repo, path and revision the spec was copied
   from → the spec file committed in that client at that tag. Copy that file here
   byte-for-byte. Copying from anywhere else — upstream `main`, a newer service
   commit — is what `tests/test_contract.py` guards: a spec parameter the pinned
   client has no argument for cannot become a flag.

4. **Update `provenance.json`** for each spec you moved: the upstream `commit`
   the client recorded, and the `sha256` of the file you just wrote
   (`sha256sum src/unstract_cli/specs/<file>`). This is the record that lets the
   next person tell a current copy from a stale one.

5. **Run the tests:** `uv run pytest -q`.

   - `test_specs.py` fails if a vendored file stops matching its pinned sha256,
     or if a spec has no provenance entry. It is the cheap check that steps 3 and
     4 actually agree.
   - `test_contract.py` fails if a spec parameter the pinned client cannot accept
     would have become a flag, and separately if the derived flags stop matching
     `tests/derived_flags.json`.

6. **If `derived_flags.json` fails, read the difference before refreshing it.**
   The failure names the flags that moved. A flag missing from the new set is a
   flag the CLI has stopped offering; a narrowed choice or changed type is a value
   the CLI used to take and now rejects. Once you have decided the change is
   intended, refresh it deliberately:

   ```bash
   UNSTRACT_CLI_REFRESH_FLAG_SNAPSHOT=1 uv run pytest -q tests/test_contract.py
   ```

   and commit the snapshot in the same PR, so the diff shows what the CLI's
   surface gained or lost.

7. **Lint:** `uv run ruff check . && uv run ruff format --check .` — the release
   run repeats exactly this, so a failure here is a failure there.

## Versioning and release

Choose the bump by what changed for CLI users: **minor** for new or changed
flags and commands, **patch** for fixes that leave the surface identical.

Do not bump `__version__` in `src/unstract_cli/__init__.py` in your PR. The
in-repo value names the last released version; `release.yml` reads it, applies
the bump chosen at dispatch and commits the result itself.

Release by dispatching **Release Tag and Publish Package** on `main`:

- `version_bump: none` publishes the version already in the repo — what the
  first release of a version needs.
- `pre_release: true` publishes `<next-version>rcN` and deliberately leaves the
  committed version alone, counting N up from the rc tags already published for
  that target. Dispatch again with it off to promote the same version to stable.
- It publishes to PyPI **before** it tags and releases, because publishing is the
  only step that cannot be undone: a failure before it leaves nothing to
  unpublish, and one after it is retried by hand against a live artifact.

## Upstream

If a client pin is missing an endpoint the service already offers, the fix is in
that client, not here — see the `spec-upgrade` skill in `unstract-python-client`
and `llm-whisperer-python-client`. Bump the pin here once it is released.
