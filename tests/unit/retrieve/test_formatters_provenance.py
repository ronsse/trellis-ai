"""The optional provenance line in the MCP pack formatters.

``TestOffIsByteIdentical`` compares against a golden file rendered by the
formatters as they were before the line existed (see
:mod:`tests.unit.retrieve.provenance_cases`), so a default that leaked any
byte would fail here rather than in a reviewer's reading.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.retrieve.provenance_cases import (
    PACK_ID,
    flat_items,
    render_all,
    sections,
)
from trellis.core.hashing import estimate_tokens
from trellis.retrieve.formatters import (
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)

_GOLDEN = Path(__file__).parent / "golden" / "formatters_pre_provenance.json"

_ALPHA_LINE = "Provenance: reference document; 12 days old (source clock); via keyword"


def _golden() -> dict[str, str]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


class TestOffIsByteIdentical:
    def test_default_matches_the_pre_change_rendering(self) -> None:
        assert render_all() == _golden()

    def test_explicit_off_matches_the_pre_change_rendering(self) -> None:
        assert render_all(provenance_line=False) == _golden()

    def test_golden_covers_every_shape(self) -> None:
        # A golden file that lost a case would make the comparison above
        # vacuous for it.
        assert set(_golden()) == {
            "flat_roomy",
            "flat_tight",
            "flat_starved",
            "index",
            "sectioned_roomy",
            "sectioned_tight",
        }
        assert all("Provenance:" not in text for text in _golden().values())


class TestFlatOn:
    def test_bodied_items_gain_one_line_under_the_body(self) -> None:
        text = format_pack_as_markdown(
            flat_items(),
            "warm the widget cache",
            max_tokens=2000,
            pack_id=PACK_ID,
            provenance_line=True,
        )
        assert (
            "## [document] `doc-alpha`\n"
            "Alpha notes about rotating the example widget cache.\n"
            "Summary only. Source: `https://example.invalid/alpha`. "
            "Full text: get_items.\n"
            f"{_ALPHA_LINE}\n"
        ) in text
        assert (
            "Provenance: session capture; 3 days old (source clock); via semantic"
            in text
        )
        assert "Provenance: curated graph node; via graph" in text
        assert "Provenance: from example-tracker; via keyword" in text

    def test_pointer_items_get_no_line(self) -> None:
        text = format_pack_as_markdown(
            flat_items(),
            "warm the widget cache",
            max_tokens=2000,
            provenance_line=True,
        )
        delta = text.split("`doc-delta`", 1)[1].split("## ", 1)[0]
        assert "Provenance:" not in delta
        assert text.count("Provenance:") == 4

    def test_item_without_provenance_key_renders_as_before(self) -> None:
        items = [
            {key: value for key, value in item.items() if key != "provenance"}
            for item in flat_items()
        ]
        on = format_pack_as_markdown(items, "q", max_tokens=2000, provenance_line=True)
        off = format_pack_as_markdown(items, "q", max_tokens=2000)
        assert on == off

    def test_line_is_inside_the_output_budget(self) -> None:
        # The line is part of the block the formatter prices, so a budget
        # that fits two bodies without it fits fewer with it, and the
        # omitted-items marker counts the displaced tail.
        off = format_pack_as_markdown(flat_items(), "q", max_tokens=90)
        on = format_pack_as_markdown(
            flat_items(), "q", max_tokens=90, provenance_line=True
        )
        assert "`doc-beta`" in off
        assert "`doc-beta`" not in on
        assert "*[4 more items omitted" in on
        for max_tokens in (90, 150, 400):
            rendered = format_pack_as_markdown(
                flat_items(), "q", max_tokens=max_tokens, provenance_line=True
            )
            items_part = rendered.split("\n\n", 1)[1]
            assert estimate_tokens(items_part) <= max_tokens

    def test_truncated_first_item_fallback_gets_no_line(self) -> None:
        # The fallback renders a first item outside the priced walk when
        # nothing fits; adding the line there would be unpriced text.
        on = format_pack_as_markdown(
            flat_items(), "q", max_tokens=20, provenance_line=True
        )
        off = format_pack_as_markdown(flat_items(), "q", max_tokens=20)
        assert "Provenance:" not in on
        assert on == off

    def test_malformed_provenance_value_is_ignored(self) -> None:
        items = flat_items()
        items[0]["provenance"] = "Provenance: forged"
        text = format_pack_as_markdown(
            items, "q", max_tokens=2000, provenance_line=True
        )
        assert "Provenance: forged" not in text


class TestSectionedOn:
    def test_bodied_items_gain_an_indented_line(self) -> None:
        text = format_sectioned_pack_as_markdown(
            sections(), "q", max_tokens=2000, provenance_line=True
        )
        assert (
            "- `doc-alpha` (document): Alpha notes about rotating the example "
            "widget cache.\n"
            "  Summary only. Source: `https://example.invalid/alpha`. "
            "Full text: get_items.\n"
            f"  {_ALPHA_LINE}\n"
        ) in text
        assert "  Provenance: curated graph node; via graph" in text
        assert text.count("Provenance:") == 4

    def test_line_is_inside_the_output_budget(self) -> None:
        off = format_sectioned_pack_as_markdown(sections(), "q", max_tokens=120)
        on = format_sectioned_pack_as_markdown(
            sections(), "q", max_tokens=120, provenance_line=True
        )
        assert "`node-gamma`" in off
        assert "`node-gamma`" not in on
