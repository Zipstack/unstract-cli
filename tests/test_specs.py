"""The vendored specs are the ones the pinned clients were generated from.

A spec copied from anywhere else derives flags the released client cannot
carry, and the failure surfaces at the call rather than here.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from unstract_cli.core.params import SPEC_FILES

PROVENANCE = json.loads(
    (resources.files("unstract_cli") / "specs" / "provenance.json").read_text("utf-8")
)
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@pytest.mark.parametrize("filename", sorted(SPEC_FILES.values()))
def test_each_vendored_spec_is_the_pinned_one(filename):
    blob = (resources.files("unstract_cli") / "specs" / filename).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROVENANCE[filename]["sha256"]


def test_every_vendored_spec_has_a_provenance_entry():
    assert set(PROVENANCE) == set(SPEC_FILES.values())


@pytest.mark.parametrize("filename", sorted(SPEC_FILES.values()))
def test_each_vendored_spec_names_the_client_pin_it_was_synced_for(filename):
    pins = tomllib.loads(PYPROJECT.read_text("utf-8"))["project"]["dependencies"]
    recorded = PROVENANCE[filename]["client"]
    name = recorded.split("==")[0]
    (pinned,) = (pin for pin in pins if pin.split("==")[0] == name)
    assert recorded == pinned, (
        f"{filename} was synced for {recorded} but pyproject.toml pins {pinned}: "
        "re-sync the spec and update provenance.json with it"
    )
