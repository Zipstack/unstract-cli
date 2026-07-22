"""Output rendering (SPEC.md §5.1).

The table renderer is the human-facing format, so the property that matters most
is that it is **lossless**: a value shown in a table must be the whole value.
Truncation is the kind of defect you only discover after acting on a half-read
id or URL.
"""

from __future__ import annotations

import json

import pytest

from unstract_cli.core.output import (
    OutputFormat,
    render,
    render_table,
)

LONG_NOTE = (
    "Credentials use env: indirection, so this file holds no secrets. "
    "Set the referenced environment variables to authenticate."
)


class TestTableIsLossless:
    def test_long_value_is_wrapped_not_truncated(self):
        out = render_table({"note": LONG_NOTE}, max_width=80)
        assert "..." not in out
        # Every word survives, even though the cell spans several lines.
        flattened = " ".join(out.split())
        for word in LONG_NOTE.split():
            assert word in flattened

    @pytest.mark.parametrize("width", [40, 60, 80, 100, 200])
    def test_lossless_at_any_width(self, width):
        out = render_table({"note": LONG_NOTE}, max_width=width)
        assert "..." not in out
        assert "authenticate" in out, "the tail of the value must always survive"

    def test_unbroken_token_is_hard_wrapped(self):
        """A long URL has no spaces to wrap on; it must break rather than overflow."""
        url = "https://ex" + "y" * 120 + ".com/end"
        out = render_table({"url": url}, max_width=60)
        assert "..." not in out
        # Reassembling the value column's lines must reproduce the URL exactly.
        value_lines = [ln.split(maxsplit=1)[-1] for ln in out.splitlines()[2:]]
        assert "".join(part.strip() for part in value_lines) == url
        assert all(len(line) <= 60 for line in out.splitlines())


class TestTableLayout:
    def test_respects_terminal_width(self):
        out = render_table({"note": LONG_NOTE}, max_width=60)
        assert all(len(line) <= 60 for line in out.splitlines())

    def test_columns_stay_aligned_across_wrapped_rows(self):
        rows = [
            {"id": "a1", "description": LONG_NOTE},
            {"id": "b2", "description": "short"},
        ]
        lines = render_table(rows, max_width=70).splitlines()
        # The id column is narrow, so every continuation line begins with the
        # blank gutter that keeps the grid readable.
        continuation = [ln for ln in lines[2:] if ln and not ln[0].isalnum()]
        assert continuation, "expected wrapped continuation lines"
        assert all(ln.startswith(" ") for ln in continuation)

    def test_narrow_column_not_squeezed_for_a_wide_neighbour(self):
        """Only the widest column gives up space, so ids stay readable."""
        rows = [{"id": "9c1e-4f2a", "description": LONG_NOTE}]
        out = render_table(rows, max_width=60)
        assert "9c1e-4f2a" in out, "the narrow id column must not wrap"

    def test_key_value_shape_for_single_object(self):
        out = render_table({"a": 1, "b": 2})
        assert out.splitlines()[0].split() == ["key", "value"]

    def test_list_of_objects_uses_keys_as_columns(self):
        out = render_table([{"id": "x", "name": "y"}])
        assert out.splitlines()[0].split() == ["id", "name"]

    def test_empty_input(self):
        assert render_table([]) == "(no results)"

    def test_booleans_render_lowercase(self):
        assert "false" in render_table({"replaced_existing": False})


class TestOtherFormats:
    def test_json_is_untouched_by_table_wrapping(self):
        """Only the human format wraps; machine formats stay verbatim."""
        payload = {"note": LONG_NOTE}
        assert json.loads(render(payload, OutputFormat.JSON)) == payload

    def test_raw_returns_the_payload_alone(self):
        out = render({"result_text": "EXTRACTED"}, OutputFormat.RAW, raw_field="result_text")
        assert out == "EXTRACTED"

    def test_yaml_round_trips(self):
        import yaml

        payload = {"note": LONG_NOTE, "n": 1}
        assert yaml.safe_load(render(payload, OutputFormat.YAML)) == payload
