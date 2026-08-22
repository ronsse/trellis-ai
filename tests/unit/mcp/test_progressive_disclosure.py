"""Tests for the progressive-disclosure retrieval surface (#305).

Three layers that have to compose: an *index* pack (ids + read cost, no
bodies), ``get_graph`` doc-pointers as the traversal hop, and ``get_items``
as the batch fetch — with ``pack_id`` attribution surviving all three.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import (
    get_context as _get_context,
)
from trellis.mcp.server import (
    get_graph as _get_graph,
)
from trellis.mcp.server import (
    get_items as _get_items,
)
from trellis.mcp.server import (
    save_experience as _save_experience,
)
from trellis.mcp.server import (
    search as _search,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_graph = unwrap_tool(_get_graph)
get_items = unwrap_tool(_get_items)
save_experience = unwrap_tool(_save_experience)
search = unwrap_tool(_search)

#: Distinctive body prose. An index rendering must never contain it.
_BODY = "Restart the pgbouncer sidecar before draining the primary. "

#: Per-document filler that makes each seeded body genuinely distinct —
#: near-identical bodies get collapsed by the pack's dedup, which would
#: make an index-vs-full item-count comparison measure dedup, not budget.
_SUBJECTS = (
    "replication lag",
    "connection pooling",
    "vacuum scheduling",
    "wal shipping",
    "index bloat",
    "checkpoint tuning",
    "autovacuum limits",
    "statement timeouts",
    "lock contention",
    "partition pruning",
    "query planning",
    "buffer eviction",
    "replica promotion",
    "sequence drift",
    "toast compression",
    "logical decoding",
    "connection storms",
    "tablespace layout",
    "backup retention",
    "collation upgrades",
)


def _seed_documents(registry: StoreRegistry, count: int) -> list[str]:
    store = registry.knowledge.document_store
    return [
        store.put(
            f"doc-{i}",
            f"failover runbook {i}: {_SUBJECTS[i]}. "
            + f"{_SUBJECTS[i]} notes. {_BODY}" * 20,
            {"title": f"Failover runbook {i}"},
        )
        for i in range(count)
    ]


def _pack_id_from(rendered: str) -> str:
    """Pull the ``pack_id`` a rendered pack advertises for citation."""
    marker = "**pack_id:** `"
    assert marker in rendered
    return rendered.split(marker, 1)[1].split("`", 1)[0]


# ---------------------------------------------------------------------------
# Layer 1 — index-mode packs
# ---------------------------------------------------------------------------


class TestIndexModeRetrieval:
    def test_get_context_index_lists_ids_without_bodies(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 3)
        result = get_context("failover runbook", index=True)
        assert _BODY.strip() not in result
        for i in range(3):
            assert f"`doc-{i}`" in result
            assert f"Failover runbook {i}" in result

    def test_index_is_cheaper_than_the_full_pack_it_stands_for(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 6)
        full = get_context("failover runbook", session_id="a")
        index = get_context("failover runbook", session_id="b", index=True)
        assert len(index) < len(full)

    def test_index_surveys_more_items_under_the_same_budget(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 12)
        full = get_context("failover runbook", max_tokens=400, session_id="a")
        index = get_context(
            "failover runbook", max_tokens=400, session_id="b", index=True
        )
        assert index.count("- `doc-") > full.count("## [document]")

    def test_index_pack_is_a_real_pack_with_attribution(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 2)
        result = get_context("failover runbook", index=True)
        pack_id = _pack_id_from(result)

        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert len(events) == 1
        assert events[0].entity_id == pack_id
        payload = events[0].payload
        assert payload["index_mode"] is True
        # The ids the agent can cite are the ids the pack recorded.
        assert set(payload["injected_item_ids"]) == {"doc-0", "doc-1"}
        assert "record_feedback(" in result

    @pytest.mark.parametrize(
        ("max_tokens", "intent"),
        [
            (200, "failover runbook"),
            # A long intent makes the renderer's heading overhead large
            # relative to the budget — the case where a builder budgeting
            # the same lines against the undiscounted max_tokens admits a
            # tail the rendering then drops.
            (120, "failover runbook " * 8),
        ],
    )
    def test_every_id_charged_as_served_is_an_id_the_agent_is_shown(
        self, temp_registry: StoreRegistry, max_tokens: int, intent: str
    ) -> None:
        # A pack item that is recorded but never rendered is invisible
        # twice over: session dedup suppresses it for the rest of the
        # session, and the learning join grades it as served-unreferenced.
        _seed_documents(temp_registry, 20)
        result = get_context(intent, max_tokens=max_tokens, index=True)
        payload = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )[0].payload

        assert payload["injected_item_ids"]
        for item_id in payload["injected_item_ids"]:
            assert f"`{item_id}`" in result
        assert result.count("- `doc-") == len(payload["injected_item_ids"])

    def test_a_tiny_budget_still_answers_with_one_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        # "No context found" about a corpus that has some is the one
        # wrong answer; a one-line survey is the right degradation.
        _seed_documents(temp_registry, 5)
        result = get_context("failover runbook", max_tokens=30, index=True)
        assert result.count("- `doc-") == 1

    def test_normal_retrieval_still_marks_a_non_index_serve(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 1)
        get_context("failover runbook")
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert events[0].payload["index_mode"] is False

    def test_index_points_at_the_fetch_tool(self, temp_registry: StoreRegistry) -> None:
        _seed_documents(temp_registry, 1)
        assert "get_items(" in get_context("failover runbook", index=True)

    def test_index_with_sections_is_rejected(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError) as excinfo:
            get_context(
                "failover runbook",
                sections=[{"name": "All"}],
                index=True,
            )
        assert excinfo.value.error.code == INVALID_PARAMS
        assert excinfo.value.error.data == {"fields": ["index", "sections"]}

    def test_search_index_mode(self, temp_registry: StoreRegistry) -> None:
        _seed_documents(temp_registry, 3)
        result = search("failover", index=True)
        assert _BODY.strip() not in result
        assert "`doc-0`" in result

    def test_empty_index_retrieval_says_so(self, temp_registry: StoreRegistry) -> None:
        assert "No context found" in get_context("nothing matches this", index=True)


# ---------------------------------------------------------------------------
# Layer 2 — graph doc-pointers
# ---------------------------------------------------------------------------


class TestGraphEvidencePointers:
    def test_get_graph_renders_followable_document_ids(
        self, temp_registry: StoreRegistry
    ) -> None:
        doc_id = temp_registry.knowledge.document_store.put(
            None, "the evidence body", {"title": "Evidence"}
        )
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node(
            "svc-api",
            "service",
            {"name": "API Gateway"},
            document_ids=[doc_id],
        )
        result = get_graph("svc-api")
        assert "Evidence documents (1)" in result
        assert f"`{doc_id}`" in result

    def test_neighbor_pointers_are_rendered_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        graph = temp_registry.knowledge.graph_store
        graph.upsert_node("svc-api", "service", {"name": "API Gateway"})
        graph.upsert_node(
            "svc-auth", "service", {"name": "Auth"}, document_ids=["doc-evidence"]
        )
        graph.upsert_edge("svc-api", "svc-auth", "depends_on")
        result = get_graph("svc-api", depth=1)
        assert "docs: `doc-evidence`" in result

    def test_node_without_evidence_renders_no_pointer_section(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.graph_store.upsert_node(
            "svc-api", "service", {"name": "API Gateway"}
        )
        assert "Evidence documents" not in get_graph("svc-api")


# ---------------------------------------------------------------------------
# Layer 3 — get_items batch fetch
# ---------------------------------------------------------------------------


class TestGetItemsValidation:
    def test_empty_ids_rejected(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_items([])
        assert excinfo.value.error.code == INVALID_PARAMS
        assert excinfo.value.error.data == {"field": "item_ids"}

    def test_too_many_ids_rejected(self) -> None:
        with pytest.raises(McpError) as excinfo:
            get_items([f"d{i}" for i in range(51)])
        assert excinfo.value.error.code == INVALID_PARAMS
        assert "too many item_ids" in excinfo.value.error.message

    def test_blank_id_entries_rejected(self) -> None:
        for ids in (["ok", "   "], ["ok", ""]):
            with pytest.raises(McpError) as excinfo:
                get_items(ids)
            assert excinfo.value.error.code == INVALID_PARAMS


class TestGetItemsResolution:
    def test_fetches_document_bodies(self, temp_registry: StoreRegistry) -> None:
        _seed_documents(temp_registry, 2)
        result = get_items(["doc-0", "doc-1"])
        assert _BODY.strip() in result
        assert "## [document] `doc-0`" in result
        assert "## [document] `doc-1`" in result

    def test_fetches_entities_with_their_evidence_pointers(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.graph_store.upsert_node(
            "svc-api",
            "service",
            {"name": "API Gateway", "owner": "platform"},
            document_ids=["doc-7"],
        )
        result = get_items(["svc-api"])
        assert "## [entity] `svc-api`" in result
        assert "API Gateway" in result
        assert "owner" in result
        # A fetched entity is itself a hop, not a dead end.
        assert "`doc-7`" in result

    def test_fetches_traces(self, temp_registry: StoreRegistry) -> None:
        trace = {
            "source": "agent",
            "intent": "drain the primary",
            "context": {"agent_id": "test-agent"},
            "steps": [
                {"step_type": "action", "name": "drain", "result": {"status": "ok"}}
            ],
            "outcome": {"status": "success"},
        }
        saved = save_experience(json.dumps(trace))
        trace_id = saved.split("Trace saved:")[1].strip()
        result = get_items([trace_id])
        assert f"## [trace] `{trace_id}`" in result
        assert "drain the primary" in result

    def test_unknown_ids_are_reported_not_silently_dropped(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 1)
        result = get_items(["doc-0", "ghost-id"])
        assert "## [document] `doc-0`" in result
        assert "not found: `ghost-id`" in result

    def test_repeated_ids_are_charged_once(self, temp_registry: StoreRegistry) -> None:
        _seed_documents(temp_registry, 1)
        result = get_items(["doc-0", "doc-0", " doc-0 "])
        assert result.count("## [document] `doc-0`") == 1

    def test_over_budget_items_are_listed_for_refetch(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 3)
        result = get_items(["doc-0", "doc-1", "doc-2"], max_tokens=500)
        assert "## [document] `doc-0`" in result
        assert "over token budget" in result
        assert "re-fetch with a larger max_tokens" in result

    def test_a_lone_over_budget_item_is_omitted_not_truncated(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 1)
        result = get_items(["doc-0"], max_tokens=100)
        # No prefix, and the audit event must not claim it was served.
        assert "## [document] `doc-0`" not in result
        assert "re-fetch with a larger max_tokens: `doc-0`" in result
        payload = TestGetItemsAttribution._fetch_event(temp_registry).payload
        assert payload["served_item_ids"] == []
        assert payload["omitted_item_ids"] == ["doc-0"]


class TestGetItemsAttribution:
    @staticmethod
    def _fetch_event(registry: StoreRegistry):
        events = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ITEMS_FETCHED, limit=10
        )
        assert len(events) == 1
        return events[0]

    def test_served_ids_are_recorded_against_the_originating_pack(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 2)
        index = get_context("failover runbook", index=True)
        pack_id = _pack_id_from(index)

        get_items(["doc-0", "doc-1"], pack_id=pack_id)

        event = self._fetch_event(temp_registry)
        assert event.entity_id == pack_id
        assert event.entity_type == "pack"
        assert event.payload["pack_id"] == pack_id
        assert event.payload["served_item_ids"] == ["doc-0", "doc-1"]
        assert event.payload["not_found_item_ids"] == []
        assert event.payload["omitted_item_ids"] == []

    def test_fetch_without_a_pack_is_still_recorded(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 1)
        get_items(["doc-0"])
        event = self._fetch_event(temp_registry)
        assert event.payload["pack_id"] is None
        assert event.entity_id is None
        assert event.payload["served_item_ids"] == ["doc-0"]

    def test_record_distinguishes_served_from_omitted_and_missing(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed_documents(temp_registry, 3)
        get_items(["doc-0", "doc-1", "doc-2", "ghost"], max_tokens=500)
        payload = self._fetch_event(temp_registry).payload
        assert payload["requested_item_ids"] == ["doc-0", "doc-1", "doc-2", "ghost"]
        assert payload["not_found_item_ids"] == ["ghost"]
        assert payload["served_item_ids"]
        assert payload["omitted_item_ids"]
        # Every requested id lands in exactly one bucket.
        buckets = (
            payload["served_item_ids"]
            + payload["omitted_item_ids"]
            + payload["not_found_item_ids"]
        )
        assert sorted(buckets) == sorted(payload["requested_item_ids"])

    def test_response_tokens_are_metered(self, temp_registry: StoreRegistry) -> None:
        _seed_documents(temp_registry, 1)
        get_items(["doc-0"], max_tokens=4000)
        payload = self._fetch_event(temp_registry).payload
        assert payload["budget_tokens"] == 4000
        assert payload["response_tokens"] > 0

        usage = temp_registry.operational.event_log.get_events(
            event_type=EventType.TOKEN_TRACKED, limit=10
        )
        assert any(e.payload.get("operation") == "get_items" for e in usage)


class TestProgressiveDisclosureWorkflow:
    def test_index_then_graph_then_fetch_keeps_one_pack_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The acceptance sketch from #305, end to end."""
        doc_id = temp_registry.knowledge.document_store.put(
            "doc-evidence",
            f"failover runbook. {_BODY * 20}",
            {"title": "Failover runbook"},
        )
        temp_registry.knowledge.graph_store.upsert_node(
            "svc-api",
            "service",
            {"name": "API Gateway failover"},
            document_ids=[doc_id],
        )

        # 1. Survey an index — ids and read costs, no bodies.
        index = get_context("failover runbook", index=True)
        assert _BODY.strip() not in index
        pack_id = _pack_id_from(index)

        # 2. Traverse from an entity to its evidence pointers.
        graph = get_graph("svc-api")
        assert f"`{doc_id}`" in graph

        # 3. Fetch the chosen body, attributed to the serving pack.
        fetched = get_items([doc_id], pack_id=pack_id)
        assert _BODY.strip() in fetched

        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ITEMS_FETCHED, limit=10
        )
        assert events[0].payload["pack_id"] == pack_id
        assert events[0].payload["served_item_ids"] == [doc_id]
