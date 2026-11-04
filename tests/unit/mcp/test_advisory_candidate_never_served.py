"""A candidate minted from an advisory is never served, like any other candidate.

The pin: record advisory-sourced candidates through the executor, then ask every
reader. The control is a governed promotion, which the same readers do serve.
"""

from __future__ import annotations

from tests.advisory_candidates import (
    ADVISORY_SENTINEL,
    anti_pattern,
    approach,
    record_advisory_candidates,
)
from tests.mined_precedents import promote_through_executor
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

# Words the candidates carry, none of the needles below, so a reader that echoes
# the query is not mistaken for one that serves a candidate.
QUERY = "prefer the graph strategy and relying on documents"


def _advisories():
    return [
        anti_pattern(ADVISORY_SENTINEL, message=f"{ADVISORY_SENTINEL} says avoid it"),
        approach(ADVISORY_SENTINEL),
    ]


def test_no_reader_serves_an_advisory_sourced_candidate(
    temp_registry: StoreRegistry,
) -> None:
    records = record_advisory_candidates(temp_registry, _advisories())
    assert {r.rule for r in records} == {"advisory_anti_pattern", "advisory_approach"}
    promote_through_executor(temp_registry, trace_id="trace-1", title="Approved lesson")
    # Text only a candidate carries, and the ids a reader could leak.
    needles = [
        ADVISORY_SENTINEL,
        "Association, not a cause",
        "Avoid relying on item",
        "retrieval strategy",
        *(r.candidate_id for r in records),
    ]

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


def test_a_pack_built_after_recording_holds_no_advisory_candidate(
    temp_registry: StoreRegistry,
) -> None:
    records = record_advisory_candidates(temp_registry, _advisories())

    pack = build_pack_builder(temp_registry, surface="test").build(
        QUERY, clearance="restricted"
    )

    assert pack.items == []
    dumped = pack.model_dump_json()
    assert ADVISORY_SENTINEL not in dumped
    assert all(r.candidate_id not in dumped for r in records)
