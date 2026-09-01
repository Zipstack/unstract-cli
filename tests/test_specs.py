"""The vendored specs are the ones the pinned clients were generated from.

A spec copied from anywhere else derives flags the released client cannot
carry, and the failure surfaces at the call rather than here.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources

import pytest

from unstract_cli.core.params import SPEC_FILES

PROVENANCE = json.loads(
    (resources.files("unstract_cli.specs") / "provenance.json").read_text("utf-8")
)


@pytest.mark.parametrize("filename", sorted(SPEC_FILES.values()))
def test_each_vendored_spec_is_the_pinned_one(filename):
    blob = (resources.files("unstract_cli.specs") / filename).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == PROVENANCE[filename]["sha256"]


def test_every_vendored_spec_has_a_provenance_entry():
    assert set(PROVENANCE) == set(SPEC_FILES.values())
