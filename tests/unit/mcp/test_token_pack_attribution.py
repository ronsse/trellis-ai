"""``TOKEN_TRACKED`` must name the pack whose render it priced.

Without ``pack_id`` a token event records *that* a retrieval cost N
tokens but not *which* retrieval, so response cost cannot be attributed
to the pack it paid for and ``trellis analyze value`` has no call-level
half. These tests pin the plumbing end to end — a unit test on
``track_token_usage`` alone would pass while the MCP path forgot to pass
the id, which is exactly how the field came to be missing.
"""

from __future__ import annotations

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import get_graph as _get_graph
from trellis.mcp.server import get_sectioned_context as _get_sectioned_context
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_graph = unwrap_tool(_get_graph)
get_sectioned_context = unwrap_tool(_get_sectioned_context)


def _seed(registry: StoreRegistry, count: int = 3) -> None:
    store = registry.knowledge.document_store
    for index in range(count):
        store.put(
            f"doc-{index}",
            f"Postgres connection pooling note {index}. The pgbouncer "
            "transaction mode drops session state, so prepared statements "
            "must be disabled on the client.",
            {"title": f"pooling note {index}"},
        )


def _token_events(registry: StoreRegistry) -> list[Event]:
    return registry.operational.event_log.get_events(
        event_type=EventType.TOKEN_TRACKED, limit=50
    )


def _pack_ids(registry: StoreRegistry) -> list[str]:
    return [
        event.entity_id or ""
        for event in registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=50
        )
    ]


class TestFlatPackAttribution:
    def test_get_context_stamps_the_assembled_pack_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        get_context("postgres connection pooling")

        events = _token_events(temp_registry)
        assert len(events) == 1
        pack_id = events[0].payload["pack_id"]
        assert pack_id
        # The id must be the pack actually emitted, not a fresh ulid.
        assert pack_id in _pack_ids(temp_registry)

    def test_pack_id_joins_response_cost_to_the_pack(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The join the value analyzer performs, exercised for real."""
        _seed(temp_registry)
        get_context("postgres connection pooling")

        token_event = _token_events(temp_registry)[0]
        pack_events = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED,
            entity_id=token_event.payload["pack_id"],
            limit=1,
        )
        assert len(pack_events) == 1
        assert token_event.payload["response_tokens"] > 0

    def test_distinct_calls_get_distinct_pack_ids(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        get_context("postgres connection pooling")
        get_context("prepared statements pgbouncer")

        ids = [event.payload["pack_id"] for event in _token_events(temp_registry)]
        assert len(ids) == 2
        assert len(set(ids)) == 2


class TestSectionedPackAttribution:
    def test_sectioned_context_stamps_its_pack_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry)
        get_sectioned_context(
            "postgres connection pooling",
            [{"name": "Notes"}, {"name": "Gotchas"}],
        )

        events = _token_events(temp_registry)
        assert len(events) == 1
        assert events[0].payload["pack_id"] in _pack_ids(temp_registry)


class TestPackFreeOperations:
    def test_get_graph_records_no_pack_id(self, temp_registry: StoreRegistry) -> None:
        """A pack-free tool records absence rather than a fabricated id."""
        temp_registry.knowledge.graph_store.upsert_node(
            "ent-1", "concept", {"name": "pgbouncer"}
        )
        get_graph("ent-1")

        events = _token_events(temp_registry)
        assert len(events) == 1
        assert events[0].payload["pack_id"] is None
