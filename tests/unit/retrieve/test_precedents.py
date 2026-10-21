"""Tests for ``trellis.retrieve.precedents.list_precedents``.

The regression of interest is the silent-truncation bug fixed in PR-8:
when a ``domain`` filter was supplied, the previous shape paged events
with ``limit`` first and applied the Python-side filter second, so a
large window of non-matching rows could starve the result list. The
new shape pushes ``domain`` into ``payload_filters`` so the limit
applies AFTER the filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.mined_precedents import (
    MinerShape,
    emit_historical_miner_row,
    promote_through_executor,
)
from trellis.retrieve.precedents import (
    MINED_CANDIDATE_ACTION,
    MINER_EVENT_SOURCE,
    is_miner_candidate_event,
    list_precedents,
)
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def graph_store(tmp_path: Path):
    return SQLiteGraphStore(tmp_path / "graph.db")


def test_list_precedents_no_filter(event_log: SQLiteEventLog) -> None:
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing", "title": "Refund flow"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-2",
        payload={"domain": "shipping", "title": "Address validation"},
    )

    result = list_precedents(event_log)
    assert len(result) == 2


def test_list_precedents_domain_filter_pushed_to_sql(
    event_log: SQLiteEventLog,
) -> None:
    """Domain filter narrows results via SQL predicate."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing", "title": "Refund flow"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-2",
        payload={"domain": "shipping", "title": "Address validation"},
    )

    result = list_precedents(event_log, domain="billing")
    assert len(result) == 1
    assert result[0]["entity_id"] == "prec-1"
    assert result[0]["domain"] == "billing"


def test_list_precedents_limit_applies_after_filter(
    event_log: SQLiteEventLog,
) -> None:
    """Regression: with the old post-fetch shape, a small ``limit`` and a
    domain filter could return fewer than ``limit`` matches. The new
    shape pushes the predicate into SQL so the cap applies *after* the
    filter and the user gets a full page.
    """
    # Emit 20 non-matching rows first (so they sit at the oldest end
    # under default ASC ordering and would fill the limit window first).
    for i in range(20):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="noise",
            entity_id=f"noise-{i}",
            payload={"domain": "shipping", "title": f"shipping-{i}"},
        )
    for i in range(5):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="match",
            entity_id=f"match-{i}",
            payload={"domain": "billing", "title": f"billing-{i}"},
        )

    result = list_precedents(event_log, domain="billing", limit=3)
    assert len(result) == 3
    assert all(row["domain"] == "billing" for row in result)


def test_list_precedents_no_match(event_log: SQLiteEventLog) -> None:
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing"},
    )
    result = list_precedents(event_log, domain="nonexistent")
    assert result == []


class TestGraphStoreExcludesDemoted:
    """``graph_store`` is the opt-in check that reads past the immutable
    ``PRECEDENT_PROMOTED`` event to the node's *current* lifecycle stamp.

    Defective subject: a ``list_precedents`` that drops the
    ``lifecycle.get("state") == "demoted"`` branch (or the ``graph_store
    is not None`` guard around it) serves every row from
    ``test_demoted_precedent_is_excluded`` regardless of the node's
    current state — the first assertion below then fails.
    """

    def test_demoted_precedent_is_excluded(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(
            node_id="prec-1",
            node_type="precedent",
            properties={"name": "Refund flow", LIFECYCLE_KEY: {"state": "demoted"}},
        )
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="prec-1",
            payload={"domain": "billing", "title": "Refund flow"},
        )

        result = list_precedents(event_log, graph_store=graph_store)
        assert result == []

    def test_archived_precedent_is_excluded(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """``retention.prune`` with ``lifecycle_states=["demoted"]`` archives
        a demoted precedent; listing must not serve it again once archived.
        """
        graph_store.upsert_node(
            node_id="prec-1",
            node_type="precedent",
            properties={"name": "Refund flow", LIFECYCLE_KEY: {"state": "archived"}},
        )
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="prec-1",
            payload={"domain": "billing", "title": "Refund flow"},
        )

        result = list_precedents(event_log, graph_store=graph_store)
        assert result == []

    def test_non_demoted_precedent_is_included(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(
            node_id="prec-1",
            node_type="precedent",
            properties={"name": "Refund flow", LIFECYCLE_KEY: {"state": "current"}},
        )
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="prec-1",
            payload={"domain": "billing", "title": "Refund flow"},
        )

        result = list_precedents(event_log, graph_store=graph_store)
        assert [row["entity_id"] for row in result] == ["prec-1"]

    def test_a_node_with_no_lifecycle_stamp_is_included(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """A node that predates the demote feature carries no lifecycle
        key at all; absence must read as "not demoted", the same
        fail-open rule :func:`trellis.retrieve.lifecycle.is_demoted`
        follows."""
        graph_store.upsert_node(
            node_id="prec-1", node_type="precedent", properties={"name": "Refund flow"}
        )
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="prec-1",
            payload={"domain": "billing", "title": "Refund flow"},
        )

        result = list_precedents(event_log, graph_store=graph_store)
        assert [row["entity_id"] for row in result] == ["prec-1"]

    def test_a_trace_mined_precedent_with_no_graph_node_is_included(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """Trace-mined precedents have no graph node at all (only the
        ``PRECEDENT_PROMOTED`` event, ``entity_id`` = the trace id) —
        ``graph_store.get_node`` returns ``None`` and the row must still
        be served."""
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="trace-1",
            payload={"domain": "billing", "title": "Mined from a trace"},
        )

        result = list_precedents(event_log, graph_store=graph_store)
        assert [row["entity_id"] for row in result] == ["trace-1"]

    def test_limit_is_not_short_when_demoted_rows_sit_in_the_window(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """The lifecycle filter must apply BEFORE ``limit``, not after.

        Five precedents are promoted, newest-first: two demoted
        (``demoted-1``, ``demoted-2``) land inside the naive
        ``limit=3`` window (the two most recent events), with three
        servable ones (``servable-1..3``) sitting just behind them.

        Defective subject: a ``list_precedents`` that fetches
        ``limit=3`` once and filters after (the old shape) sees
        ``[demoted-2, demoted-1, servable-3]``, drops the two demoted
        rows, and returns only ``["servable-3"]`` -- one row short of
        ``limit`` even though three servable precedents exist. The
        fixed shape widens its fetch until it finds ``limit`` servable
        rows (or exhausts the log), returning all three.
        """
        for i in range(1, 4):
            graph_store.upsert_node(
                node_id=f"servable-{i}",
                node_type="precedent",
                properties={
                    "name": f"Pattern {i}",
                    LIFECYCLE_KEY: {"state": "current"},
                },
            )
            event_log.emit(
                EventType.PRECEDENT_PROMOTED,
                source="test",
                entity_id=f"servable-{i}",
                payload={"domain": "billing", "title": f"Pattern {i}"},
            )
        for i in range(1, 3):
            graph_store.upsert_node(
                node_id=f"demoted-{i}",
                node_type="precedent",
                properties={"name": f"Stale {i}", LIFECYCLE_KEY: {"state": "demoted"}},
            )
            event_log.emit(
                EventType.PRECEDENT_PROMOTED,
                source="test",
                entity_id=f"demoted-{i}",
                payload={"domain": "billing", "title": f"Stale {i}"},
            )

        result = list_precedents(event_log, limit=3, graph_store=graph_store)

        assert len(result) == 3
        entity_ids = {row["entity_id"] for row in result}
        assert entity_ids == {"servable-1", "servable-2", "servable-3"}

    def test_without_graph_store_a_demoted_precedent_still_appears(
        self, event_log: SQLiteEventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """Omitting ``graph_store`` preserves the pre-demote-feature
        behaviour: the event-log scan alone, with no lifecycle awareness."""
        graph_store.upsert_node(
            node_id="prec-1",
            node_type="precedent",
            properties={"name": "Refund flow", LIFECYCLE_KEY: {"state": "demoted"}},
        )
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id="prec-1",
            payload={"domain": "billing", "title": "Refund flow"},
        )

        result = list_precedents(event_log)
        assert [row["entity_id"] for row in result] == ["prec-1"]


class TestMinedCandidatesAreNotPromoted:
    """A candidate the precedent miner wrote is not a promoted precedent.

    The miner once emitted its ungoverned, unapproved candidates as
    ``PRECEDENT_PROMOTED``; the event log is immutable, so those rows
    outlive the fix and ``list_precedents`` (hence ``get_lessons``,
    ``GET /precedents`` and ``trellis retrieve precedents``) has to skip them.
    """

    @pytest.fixture
    def registry(self, tmp_path: Path) -> StoreRegistry:
        return StoreRegistry(stores_dir=tmp_path / "stores")

    @pytest.mark.parametrize("with_graph", [False, True])
    @pytest.mark.parametrize("shape", ["generated", "extracted"])
    def test_a_historical_miner_row_is_not_listed(
        self, registry: StoreRegistry, with_graph: bool, shape: MinerShape
    ) -> None:
        log = registry.operational.event_log
        emit_historical_miner_row(log, entity_id="mined-1", shape=shape)

        graph = registry.knowledge.graph_store if with_graph else None
        assert list_precedents(log, graph_store=graph) == []

    @pytest.mark.parametrize("with_graph", [False, True])
    def test_a_governed_promotion_is_still_listed(
        self, registry: StoreRegistry, with_graph: bool
    ) -> None:
        log = registry.operational.event_log
        emit_historical_miner_row(log, entity_id="mined-1")
        promote_through_executor(registry, trace_id="trace-1", title="Real lesson")

        graph = registry.knowledge.graph_store if with_graph else None
        listed = list_precedents(log, graph_store=graph)

        assert [row["title"] for row in listed] == ["Real lesson"]

    @pytest.mark.parametrize("source", ["test", "cli", "trellis.learning"])
    def test_a_promotion_from_any_other_emitter_is_still_listed(
        self, event_log: SQLiteEventLog, source: str
    ) -> None:
        """The exclusion names the miner; it is not "anything but the
        promote handler". A ``PRECEDENT_PROMOTED`` row from some other
        emitter is still a promotion."""
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source=source,
            entity_id="prec-1",
            payload={"title": "From elsewhere"},
        )

        assert [r["title"] for r in list_precedents(event_log)] == ["From elsewhere"]

    @pytest.mark.parametrize("with_graph", [False, True])
    def test_miner_rows_do_not_use_up_the_limit(
        self, registry: StoreRegistry, with_graph: bool
    ) -> None:
        """Newer miner rows sit ahead of the one real promotion in the
        newest-first window; a fetch of ``limit`` rows then a filter would
        return nothing."""
        log = registry.operational.event_log
        promote_through_executor(registry, trace_id="trace-1", title="Real lesson")
        for i in range(6):
            emit_historical_miner_row(log, entity_id=f"mined-{i}")

        graph = registry.knowledge.graph_store if with_graph else None
        listed = list_precedents(log, limit=1, graph_store=graph)

        assert [row["title"] for row in listed] == ["Real lesson"]

    def test_the_predicate_matches_the_literals_old_rows_carry(self) -> None:
        """Old rows are immutable, so the predicate's constants can never
        change without orphaning them."""
        assert MINER_EVENT_SOURCE == "precedent_miner"
        assert MINED_CANDIDATE_ACTION == "precedent_candidate_generated"

    def test_the_predicate_reads_the_action_as_well_as_the_source(
        self, event_log: SQLiteEventLog
    ) -> None:
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="some-fork",
            entity_id="prec-1",
            payload={"action": MINED_CANDIDATE_ACTION, "title": "Candidate"},
        )

        [event] = event_log.get_events(event_type=EventType.PRECEDENT_PROMOTED)
        assert is_miner_candidate_event(event)
        assert list_precedents(event_log) == []
