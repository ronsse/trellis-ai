"""The MCP twin of ``tests/unit/api/test_precedent_source_end_to_end.py`` (#1080).

A precedent stored by a real promotion, whose source is relabelled
``restricted`` afterwards, is read back through ``get_lessons`` and
``get_items`` by a ``read`` caller and by a ``restricted`` one. ``get_items`` is
pinned as a known gap (strict xfail): it serves the document at the precedent's
id, whose text carries the source's id, title and excerpt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import trellis.mcp.server as server_mod
from tests.promoted_precedent import (
    FAMILY,
    SOURCE_EXCERPT,
    SOURCE_ID,
    SOURCE_TITLE,
    promote,
    relabel_source_restricted,
)
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import READ, RESTRICTED, as_http

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_lessons = unwrap_tool(server_mod.get_lessons)
get_items = unwrap_tool(server_mod.get_items)


def test_get_lessons_withholds_the_row_below_the_label(
    temp_registry: StoreRegistry,
) -> None:
    promote(temp_registry)
    with as_http(READ):
        before_relabel = get_lessons()
    assert FAMILY in before_relabel, "the premise: an open source's precedent is served"

    relabel_source_restricted(temp_registry)
    with as_http(READ):
        below = get_lessons()
    assert SOURCE_ID not in below
    assert FAMILY not in below
    with as_http(RESTRICTED):
        above = get_lessons()
    assert FAMILY in above


@pytest.mark.xfail(
    strict=True,
    reason=(
        "get_items resolves the document stored at the precedent's id before "
        "the node, and that document's text names the source's id, title and "
        "opening excerpt. A relabel after promotion leaves it readable. Drop "
        "this marker with the fix."
    ),
)
def test_get_items_serves_nothing_derived_from_the_source(
    temp_registry: StoreRegistry,
) -> None:
    precedent_id = promote(temp_registry)
    relabel_source_restricted(temp_registry)
    with as_http(READ):
        below = get_items([precedent_id])
    for needle in (SOURCE_ID, SOURCE_TITLE, SOURCE_EXCERPT):
        assert needle not in below, needle
