"""The workflow files parse, and say what they mean.

A workflow is only parsed when it is dispatched, so a file that cannot be read
sits in the repository looking fine until the day someone needs it to run. That
day is a release. These read the files the way GitHub does, at PR time.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted(
    (Path(__file__).resolve().parents[1] / ".github/workflows").glob("*.yml")
)


class _StrictLoader(yaml.SafeLoader):
    """Refuses what `yaml.safe_load` accepts silently."""


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode) -> dict:
    """Duplicate keys are an error, not a last-one-wins merge.

    A step that declares `env:` twice keeps only the second, so a variable the
    run block reads is quietly never set -- and the file still parses here while
    GitHub rejects it outright. Neither outcome may reach main.
    """
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise AssertionError(f"duplicate key {key!r} at {key_node.start_mark}")
        seen.add(key)
    return yaml.constructor.SafeConstructor.construct_mapping(loader, node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def _load(path: Path) -> dict:
    with path.open() as handle:
        return yaml.load(handle, Loader=_StrictLoader)


def test_there_are_workflows_to_check() -> None:
    """Guards the glob: an empty directory would pass every check below."""
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_parses_with_no_duplicate_keys(path: Path) -> None:
    assert _load(path)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_declares_a_trigger_and_a_job(path: Path) -> None:
    """`on` is the YAML 1.1 boolean `True` once parsed, which is the key GitHub
    means and the one a hand-written check usually misses.
    """
    document = _load(path)

    assert document.get(True) or document.get("on"), f"{path.name}: no trigger"
    assert document.get("jobs"), f"{path.name}: no jobs"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_step_that_names_a_secret_can_read_it(path: Path) -> None:
    """A `run` block reading `$GITHUB_TOKEN` needs the step or the job to set it;
    nothing fails at parse time when it does not, the command just gets an empty
    value and the step fails halfway through a release.
    """
    document = _load(path)

    for job_name, job in (document.get("jobs") or {}).items():
        job_env = set(job.get("env") or {})
        for step in job.get("steps") or []:
            script = step.get("run") or ""
            if "$GITHUB_TOKEN" not in script and "${GITHUB_TOKEN" not in script:
                continue
            available = job_env | set(step.get("env") or {})
            named = step.get("name", step.get("uses", "?"))
            assert "GITHUB_TOKEN" in available, f"{path.name}: {job_name}: {named}"


def test_the_release_publishes_only_after_everything_revertible_is_done() -> None:
    """A tag, a branch and a release can all be deleted; a published version
    cannot. Publishing first turns any later failure into a release on PyPI
    that no tag in the repository names."""
    steps = _load(Path(__file__).resolve().parents[1] / ".github/workflows/release.yml")[
        "jobs"
    ]["release-and-publish"]["steps"]
    names = [step.get("name", "") for step in steps]

    assert names.index("Publish to PyPI") > names.index(
        "Commit version bump and create release"
    )
