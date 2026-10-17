"""The graph page gives each canonical entity type its own fixed colour."""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api
from trellis.schemas.well_known import CANONICAL_ENTITY_TYPES

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _type_to_css_var(type_name: str) -> str:
    """Mirrors entityTypeCssVar() in index.html: PascalCase -> --entity-kebab."""
    return "--entity-" + re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", type_name).lower()


def test_fixed_colours_cover_exactly_the_canonical_entity_types() -> None:
    # Pass 6 moved the type -> colour map off a JS `Map` literal and onto
    # `--entity-<kebab-type>` CSS tokens (one set per theme block); the type
    # roster itself lives in the WELL_KNOWN_ENTITY_TYPES array.
    page = INDEX_HTML.read_text(encoding="utf-8")
    roster = re.search(r"const WELL_KNOWN_ENTITY_TYPES = \[(.*?)\];", page, re.DOTALL)
    assert roster is not None, "WELL_KNOWN_ENTITY_TYPES not found in index.html"
    types = re.findall(r"'([^']+)'", roster.group(1))
    assert sorted(types) == sorted(CANONICAL_ENTITY_TYPES)

    # Pull every --entity-* declaration out of the bare (dark) :root block --
    # the first theme block in the file -- and resolve each canonical type's
    # token to its hex value there.
    root_block = re.search(r":root \{(.*?)\n        \}", page, re.DOTALL)
    assert root_block is not None, "bare :root block not found"
    tokens = dict(
        re.findall(r"(--entity-[a-z0-9-]+):\s*(#[0-9a-fA-F]{6});", root_block.group(1))
    )

    colours = []
    for t in types:
        var = _type_to_css_var(t)
        assert var in tokens, f"{var} missing from the dark :root block for type {t!r}"
        colours.append(tokens[var])

    # A shared colour would make two types read as one in the legend.
    assert len(set(colours)) == len(colours)
