"""A failed retrieval axis has to reach the surface ``get_context`` renders
markdown for (#775 gate, finding F2).

``POST /api/v1/packs`` reports a failed axis in its JSON ``axes`` block
(#775). ``get_context``'s default (flat) path renders markdown sized for an
LLM context window instead -- there is no JSON block for an agent to fall
back on, so a caller who never inspects
``PACK_ASSEMBLED.strategy_failures`` sees nothing. This module pins:

1. a failing strategy adds exactly one line naming it, with no exception
   text, in both the populated-pack and the empty-pack reply;
2. a clean pack's reply carries no such line and its header shape is
   unchanged.

Out of scope (per the gate's F2 scope note): ``get_context(sections=...)``,
``get_objective_context`` and ``get_task_context`` share
``_sectioned_context`` -- a separate helper this change does not touch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)

INTENT = "alpha bravo runbook"
_AXIS_FAILURE_SENTINEL = "SENTINEL_MCP_AXIS_FAILURE_3b7a1d"


def _axis_item(item_id: str) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt="alpha bravo runbook drain queue",
        relevance_score=0.5,
    )


def _make_axis_strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _make_failing_axis_strategy(name: str, message: str) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.side_effect = RuntimeError(message)
    return strategy


def _builder_one_failing_one_surviving(
    *_args: object, **_kwargs: object
) -> PackBuilder:
    """Keyword raises, graph survives with a result -- same shape as
    tests/unit/retrieve/test_pack_builder_failures.py's partial-failure
    case and tests/unit/api/test_routes.py's REST pin for #775."""
    bad = _make_failing_axis_strategy("keyword", _AXIS_FAILURE_SENTINEL)
    good = _make_axis_strategy("graph", [_axis_item("d1")])
    return PackBuilder(strategies=[bad, good])


def _builder_failing_axis_empty_survivor(
    *_args: object, **_kwargs: object
) -> PackBuilder:
    """Keyword raises; graph runs but finds nothing -- reproduces the
    re-measurement probe's most misleading reply: "No context found" with
    no signal that anything failed (a *total* strategy failure instead
    raises ``PackAssemblyError``, already surfaced as ``INTERNAL_ERROR``
    and out of scope here)."""
    bad = _make_failing_axis_strategy("keyword", _AXIS_FAILURE_SENTINEL)
    empty = _make_axis_strategy("graph", [])
    return PackBuilder(strategies=[bad, empty])


def test_get_context_reports_failed_axis_for_a_populated_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_one_failing_one_surviving
    )

    result = get_context(INTENT)
    lines = result.split("\n")

    # Pin the exact line, not just a substring: a note that also carried
    # exception text would still contain this prefix and would still leave
    # the sentinel absent, so a substring-only check cannot tell "names
    # only" from "names plus some other, non-matching text" apart.
    assert "**Retrieval axis failed:** keyword." in lines
    assert result.count("Retrieval axis failed") == 1
    assert _AXIS_FAILURE_SENTINEL not in result


def test_get_context_reports_failed_axis_for_an_empty_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    """The defect's most severe instance: keyword fails and graph (the only
    surviving axis) returns zero items, so the pack is empty -- it used to
    read as "No context found", identical to a genuinely empty corpus, with
    no hint that an axis never ran at all."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_failing_axis_empty_survivor
    )

    result = get_context(INTENT)
    lines = result.split("\n")

    assert lines[0] == f"No context found for: {INTENT}"
    assert "**Retrieval axis failed:** keyword." in lines
    assert _AXIS_FAILURE_SENTINEL not in result


def test_get_context_clean_pack_has_no_axis_note(
    temp_registry: StoreRegistry,
) -> None:
    store = temp_registry.knowledge.document_store
    store.put(
        "doc-a",
        "alpha bravo runbook drain queue " * 10,
        {"content_tags": {"domain": "alpha"}},
    )

    result = get_context(INTENT)
    lines = result.split("\n")

    assert lines[0] == f"# Context for: {INTENT}"
    assert lines[1].startswith("**pack_id:**")
    # No axis note and no withholding note inserted: the header's third
    # line is the blank separator that precedes the first item block,
    # exactly as it was before this change.
    assert lines[2] == ""
    assert "Retrieval axis failed" not in result


def test_get_context_clean_empty_pack_is_unchanged(
    temp_registry: StoreRegistry,
) -> None:
    result = get_context(INTENT)

    assert result == f"No context found for: {INTENT}"
