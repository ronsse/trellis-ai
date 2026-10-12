"""The graph page gives each canonical entity type its own fixed colour."""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from trellis.schemas.well_known import CANONICAL_ENTITY_TYPES

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def test_fixed_colours_cover_exactly_the_canonical_entity_types() -> None:
    page = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(
        r"const WELL_KNOWN_TYPE_COLORS = new Map\(\[(.*?)\]\);", page, re.DOTALL
    )
    assert block is not None, "WELL_KNOWN_TYPE_COLORS not found in index.html"
    entries = [line for line in block.group(1).splitlines() if line.strip()]
    pairs = re.findall(r"\['([^']+)', '(#[0-9a-f]{6})'\]", block.group(1))

    # Every entry parsed: one written in another style cannot skip the checks.
    assert len(pairs) == len(entries)
    assert sorted(t for t, _ in pairs) == sorted(CANONICAL_ENTITY_TYPES)
    # A shared colour would make two types read as one in the legend.
    assert len({c for _, c in pairs}) == len(pairs)
