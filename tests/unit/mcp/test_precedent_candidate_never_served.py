"""A recorded candidate precedent is never served by any MCP reader or pack path.

The pin: record candidates through the executor, then ask every reader. The
control is a governed promotion, which the same readers do serve.
"""

from __future__ import annotations

from tests.mined_precedents import promote_through_executor
from tests.precedent_candidates import SENTINEL, record_candidates, recovering_trace
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import get_lessons as _get_lessons
from trellis.mcp.server import search as _search
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.precedents import list_precedents
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_lessons = unwrap_tool(_get_lessons)
search = unwrap_tool(_search)

QUERY = "load the quarterly export schema mismatch"


def test_no_reader_serves_a_recorded_candidate(temp_registry: StoreRegistry) -> None:
    records = record_candidates(temp_registry, recovering_trace())
    promote_through_executor(temp_registry, trace_id="trace-1", title="Approved lesson")
    needles = [SENTINEL, *(r.candidate_id for r in records)]

    lessons = get_lessons()
    listed = list_precedents(temp_registry.operational.event_log)
    context = get_context(QUERY)
    found = search(QUERY)

    # Control: the readers work, and serve the promotion.
    assert "Approved lesson" in lessons
    assert [p["title"] for p in listed] == ["Approved lesson"]
    for needle in needles:
        for served in (lessons, str(listed), context, found):
            assert needle not in served


def test_a_pack_built_after_recording_holds_no_candidate(
    temp_registry: StoreRegistry,
) -> None:
    records = record_candidates(temp_registry, recovering_trace())

    pack = build_pack_builder(temp_registry, surface="test").build(
        QUERY, clearance="restricted"
    )

    assert pack.items == []
    dumped = pack.model_dump_json()
    assert SENTINEL not in dumped
    assert all(r.candidate_id not in dumped for r in records)
