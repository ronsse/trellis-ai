"""A failed retrieval axis has to reach the surface ``get_context`` renders
markdown for (#775 gate, finding F2; #783 gate, finding F1).

``POST /api/v1/packs`` reports a failed axis in its JSON ``axes`` block
(#775). ``get_context``'s default (flat) path renders markdown sized for an
LLM context window instead -- there is no JSON block for an agent to fall
back on, so a caller who never inspects
``PACK_ASSEMBLED.strategy_failures`` sees nothing. This module pins:

1. a failing strategy adds exactly one line naming it, with no exception
   text, in the populated-pack reply (plain and index), the empty-pack
   one-liner, and the formatter-rendered empty pack the holdout uses;
2. a clean pack's reply carries no such line and its header shape is
   unchanged;
3. both hold for the four tools built on ``_sectioned_context``
   (``get_context(sections=...)``, ``get_objective_context``,
   ``get_task_context``, ``get_sectioned_context``).

A ``misconfigured`` semantic axis (#783 gate, follow-up F2) is a fourth,
independent state ``describe_axes`` reports: an embedder resolved but the
vector backend never initialised, so the axis never reaches ``available``
at all and ``format_failed_axes_note`` — built from ``axes["failed"]`` —
cannot see it. This module also pins that every tool above adds a second,
separate line for that state, that the two lines coexist when both apply,
and that a clean pack, a pack with only a failed axis, and a pack whose
semantic axis was built (and ran or raised) gain no such line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import get_objective_context as _get_objective_context
from trellis.mcp.server import get_sectioned_context as _get_sectioned_context
from trellis.mcp.server import get_task_context as _get_task_context
from trellis.mcp.server import search as _search
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_objective_context = unwrap_tool(_get_objective_context)
get_task_context = unwrap_tool(_get_task_context)
get_sectioned_context = unwrap_tool(_get_sectioned_context)
search = unwrap_tool(_search)

INTENT = "alpha bravo runbook"
_AXIS_FAILURE_SENTINEL = "SENTINEL_MCP_AXIS_FAILURE_3b7a1d"
_CUSTOM_SECTIONS = [{"name": "docs", "content_types": ["document"], "max_items": 5}]
_MISCONFIGURED_SEMANTIC_LINE = (
    "**Semantic retrieval misconfigured:** the vector store did not"
    " initialise; results are keyword and graph only."
)


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


def _builder_clean_without_semantic(*_args: object, **_kwargs: object) -> PackBuilder:
    """Keyword and graph both run and both find something; no axis failed.

    Used only together with ``_configure_embedder`` -- that combination is
    what ``describe_axes`` reads as "misconfigured" rather than
    "not_configured" (tests/unit/retrieve/test_builder_factory.py's
    ``TestDescribeAxes.test_absent_with_an_embedder_is_misconfigured``)."""
    keyword = _make_axis_strategy("keyword", [_axis_item("d1")])
    graph = _make_axis_strategy("graph", [_axis_item("d2")])
    return PackBuilder(strategies=[keyword, graph])


def _builder_empty_without_semantic(*_args: object, **_kwargs: object) -> PackBuilder:
    """Keyword and graph both run and both find nothing; no axis failed."""
    keyword = _make_axis_strategy("keyword", [])
    graph = _make_axis_strategy("graph", [])
    return PackBuilder(strategies=[keyword, graph])


def _builder_held_out_empty_without_semantic(
    *_args: object, **_kwargs: object
) -> PackBuilder:
    keyword = _make_axis_strategy("keyword", [])
    graph = _make_axis_strategy("graph", [])
    return PackBuilder(strategies=[keyword, graph], holdout_rate=1.0)


def _configure_embedder(
    monkeypatch: pytest.MonkeyPatch, registry: StoreRegistry
) -> None:
    """Force ``registry.embedding_fn`` truthy without touching config.

    ``embedding_fn`` is a read-only property with internal lazy caching
    (``StoreRegistry._embedding_fn_cache``, sentinel-valued until resolved)
    and no setter, so the cache is set directly -- the one-layer-up
    equivalent of ``describe_axes``'s own ``embedder_configured=True``
    parameter in tests/unit/retrieve/test_builder_factory.py, needed here
    because ``_flat_context``/``_sectioned_context`` read the registry
    property themselves rather than taking a bool.
    """
    monkeypatch.setattr(registry, "_embedding_fn_cache", lambda text: [0.1])


@pytest.mark.parametrize("index", [False, True])
def test_get_context_reports_failed_axis_for_a_populated_pack(
    temp_registry: StoreRegistry, monkeypatch, index: bool
) -> None:
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_one_failing_one_surviving
    )

    result = get_context(INTENT, index=index)
    lines = result.split("\n")

    # Pin the exact line, not just a substring: a note that also carried
    # exception text would still contain this prefix and would still leave
    # the sentinel absent, so a substring-only check cannot tell "names
    # only" from "names plus some other, non-matching text" apart.
    assert "**Retrieval axis failed:** keyword." in lines
    assert result.count("Retrieval axis failed") == 1
    assert _AXIS_FAILURE_SENTINEL not in result
    # No embedder was configured in this scenario, so the missing semantic
    # axis reads "not_configured", not "misconfigured" -- this must not
    # print regardless.
    assert "Semantic retrieval misconfigured" not in result


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
    assert "Semantic retrieval misconfigured" not in result


def test_get_context_reports_failed_axis_for_a_held_out_empty_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    """With the pack holdout on, an empty pack renders through the
    formatter instead of the one-liner; the note must survive that path."""

    def _builder(*_args: object, **_kwargs: object) -> PackBuilder:
        bad = _make_failing_axis_strategy("keyword", _AXIS_FAILURE_SENTINEL)
        empty = _make_axis_strategy("graph", [])
        return PackBuilder(strategies=[bad, empty], holdout_rate=1.0)

    monkeypatch.setattr(server_mod, "_build_pack_builder", _builder)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert lines[0] == f"# Context for: {INTENT}"
    assert "**Retrieval axis failed:** keyword." in lines
    assert _AXIS_FAILURE_SENTINEL not in result
    assert "Semantic retrieval misconfigured" not in result


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
    assert "Semantic retrieval misconfigured" not in result


def test_get_context_clean_empty_pack_is_unchanged(
    temp_registry: StoreRegistry,
) -> None:
    result = get_context(INTENT)

    assert result == f"No context found for: {INTENT}"


# ---------------------------------------------------------------------------
# A misconfigured semantic axis (#783 gate, follow-up F2): an embedder
# resolved but the vector backend never initialised, so the axis never
# reaches ``available`` and ``format_failed_axes_note`` cannot see it.
# ---------------------------------------------------------------------------


def test_get_context_reports_misconfigured_semantic_for_a_populated_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_clean_without_semantic
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert _MISCONFIGURED_SEMANTIC_LINE in lines
    assert result.count("Semantic retrieval misconfigured") == 1
    assert "Retrieval axis failed" not in result


def test_get_context_reports_misconfigured_semantic_for_an_empty_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_empty_without_semantic
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert lines[0] == f"No context found for: {INTENT}"
    assert _MISCONFIGURED_SEMANTIC_LINE in lines


def test_get_context_reports_misconfigured_semantic_for_a_held_out_empty_pack(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    """With the pack holdout on, an empty pack renders through the
    formatter instead of the one-liner; the misconfigured note must survive
    that path too (mirrors test_get_context_reports_failed_axis_for_a_held_
    out_empty_pack for the failed-axis case)."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_held_out_empty_without_semantic
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert lines[0] == f"# Context for: {INTENT}"
    assert _MISCONFIGURED_SEMANTIC_LINE in lines


def test_get_context_reports_both_failed_and_misconfigured_axes(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    """A failed keyword axis and a misconfigured semantic axis are
    independent states a single build can hit together (the failing
    stand-in already lacks a semantic strategy); both lines render, each
    exactly once, and neither carries the other's exception text."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_one_failing_one_surviving
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert "**Retrieval axis failed:** keyword." in lines
    assert _MISCONFIGURED_SEMANTIC_LINE in lines
    assert result.count("Retrieval axis failed") == 1
    assert result.count("Semantic retrieval misconfigured") == 1
    assert _AXIS_FAILURE_SENTINEL not in result


def test_search_reports_misconfigured_semantic_axis(
    temp_registry: StoreRegistry, monkeypatch
) -> None:
    """``search`` shares ``_flat_context`` with ``get_context`` but had no
    direct coverage of the axis-note behaviour in this module; pin it
    separately so a future split of the two paths cannot silently drop the
    note from one of them."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_clean_without_semantic
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = search(INTENT)
    lines = result.split("\n")

    assert _MISCONFIGURED_SEMANTIC_LINE in lines


@pytest.mark.parametrize("semantic_raises", [False, True])
def test_get_context_built_semantic_axis_is_never_misconfigured(
    temp_registry: StoreRegistry, monkeypatch, semantic_raises: bool
) -> None:
    """With an embedder configured and the semantic strategy built, the
    axis is "ran" or "failed", never "misconfigured": a semantic axis that
    raised is named by the failed-axis line alone, and one that ran adds
    no line at all."""
    keyword = _make_axis_strategy("keyword", [_axis_item("d1")])
    semantic = (
        _make_failing_axis_strategy("semantic", _AXIS_FAILURE_SENTINEL)
        if semantic_raises
        else _make_axis_strategy("semantic", [_axis_item("d2")])
    )
    monkeypatch.setattr(
        server_mod,
        "_build_pack_builder",
        lambda *_args, **_kwargs: PackBuilder(strategies=[keyword, semantic]),
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = get_context(INTENT)
    lines = result.split("\n")

    assert "Semantic retrieval misconfigured" not in result
    assert _AXIS_FAILURE_SENTINEL not in result
    if semantic_raises:
        assert "**Retrieval axis failed:** semantic." in lines
    else:
        assert "Retrieval axis failed" not in result


# ---------------------------------------------------------------------------
# The four tools sharing ``_sectioned_context`` (#783 gate, F1)
# ---------------------------------------------------------------------------

_SECTIONED_CALLS: dict[str, Callable[[], str]] = {
    "get_context_sections": lambda: get_context(INTENT, sections=_CUSTOM_SECTIONS),
    "get_objective_context": lambda: get_objective_context(INTENT),
    "get_task_context": lambda: get_task_context(INTENT),
    "get_sectioned_context": lambda: get_sectioned_context(INTENT, _CUSTOM_SECTIONS),
}


@pytest.mark.parametrize("tool_name", sorted(_SECTIONED_CALLS))
def test_sectioned_tools_report_failed_axis_in_exactly_one_line(
    temp_registry: StoreRegistry, monkeypatch, tool_name: str
) -> None:
    """One line, axis name only, no exception text, as on the flat path.

    The objective and task presets filter out the fixture's item, so those
    two cases also pin the reply whose every section is empty."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_one_failing_one_surviving
    )

    result = _SECTIONED_CALLS[tool_name]()
    lines = result.split("\n")

    assert "**Retrieval axis failed:** keyword." in lines
    assert result.count("Retrieval axis failed") == 1
    assert _AXIS_FAILURE_SENTINEL not in result
    assert "Semantic retrieval misconfigured" not in result


@pytest.mark.parametrize("tool_name", sorted(_SECTIONED_CALLS))
def test_sectioned_tools_report_misconfigured_semantic_axis(
    temp_registry: StoreRegistry, monkeypatch, tool_name: str
) -> None:
    """The misconfigured-semantic line reaches all four sectioned tools too,
    one line, not combined with a failed-axis note that does not apply."""
    monkeypatch.setattr(
        server_mod, "_build_pack_builder", _builder_clean_without_semantic
    )
    _configure_embedder(monkeypatch, temp_registry)

    result = _SECTIONED_CALLS[tool_name]()
    lines = result.split("\n")

    assert _MISCONFIGURED_SEMANTIC_LINE in lines
    assert result.count("Semantic retrieval misconfigured") == 1
    assert "Retrieval axis failed" not in result


def test_get_sectioned_context_clean_pack_has_no_axis_note(
    temp_registry: StoreRegistry,
) -> None:
    """A clean reply gains no line, blank or otherwise: the separator after
    ``pack_id`` runs straight into the first section heading."""
    store = temp_registry.knowledge.document_store
    store.put(
        "doc-a",
        "alpha bravo runbook drain queue " * 10,
        {"content_tags": {"domain": "alpha"}},
    )

    result = get_sectioned_context(INTENT, _CUSTOM_SECTIONS)
    lines = result.split("\n")

    assert lines[0] == f"# Context for: {INTENT}"
    assert lines[1].startswith("**pack_id:**")
    assert lines[2:4] == ["", "## docs"]
    assert "Retrieval axis failed" not in result
    assert "Semantic retrieval misconfigured" not in result
