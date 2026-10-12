"""Unit tests for trellis.learning.precedent_evidence_gate.

Covers the module's two layers: the pure gate decision
(``judge_precedent_demotion``) and the event-log join that feeds it
(``gather_precedent_citation_evidence``), plus the report-only run loop
(``evaluate_precedent_demotions``) that ties discovery, the lifecycle
skip check and the gate together without writing anything.

The four non-regression acceptance tests below are load-bearing for the
rule this module exists to enforce: demotion requires evidence of
unhelpfulness, never absence of evidence of helpfulness, and that
evidence must postdate the precedent's own promotion and come from a
pack shape capable of attributing it at all. The fifth (Q4 regression)
pins an existing property of ``_scan_already_promoted_entity_ids`` and
lives in ``test_candidate_auto_promote.py`` instead, since it is not new
behaviour this module adds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.learning.precedent_evidence_gate import (
    DISPOSITION_REFUSED,
    DISPOSITION_SKIPPED,
    DISPOSITION_WOULD_DEMOTE,
    MIN_UNHELPFUL_CITATIONS,
    REFUSED_CONTESTED,
    REFUSED_INSUFFICIENT,
    REFUSED_NO_EVIDENCE,
    REFUSED_NO_UNHELPFUL,
    SKIPPED_ALREADY_DEMOTED,
    SKIPPED_ARCHIVED,
    SKIPPED_NOT_GRAPH_BACKED,
    PrecedentCitationEvidence,
    discover_current_precedents,
    discover_current_precedents_with_coverage,
    evaluate_precedent_demotions,
    gather_precedent_citation_evidence,
    join_precedent_citation_events,
    judge_precedent_demotion,
    precedent_skip_reason,
    report_to_dict,
)
from trellis.mutate.commands import Operation
from trellis.mutate.retention import ARCHIVED_STATE
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def graph_store(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(tmp_path / "graph.db")


def _ev(
    entity_id: str = "p1", *, helpful: int = 0, unhelpful: int = 0, appearances: int = 5
) -> PrecedentCitationEvidence:
    return PrecedentCitationEvidence(
        entity_id=entity_id,
        appearances=appearances,
        helpful_count=helpful,
        unhelpful_count=unhelpful,
    )


def _emit_promote(event_log: EventLog, entity_id: str, *, at: datetime) -> None:
    event_log.append(
        Event(
            event_type=EventType.MUTATION_EXECUTED,
            source="test",
            entity_id=entity_id,
            payload={
                "operation": Operation.PRECEDENT_PROMOTE.value,
                "status": "success",
            },
            occurred_at=at,
        )
    )


def _emit_pack_assembled(
    event_log: EventLog,
    *,
    pack_id: str,
    item_ids: list[str],
    at: datetime,
    sectioned: bool = False,
) -> None:
    """Emit a PACK_ASSEMBLED event.

    ``sectioned=True`` omits ``injected_items`` entirely, matching what
    ``PackBuilder.build_sectioned`` actually emits (see the module
    docstring's "Attribution coverage gap") -- never a
    ``pack.sections[].items`` stand-in, which is exactly the fallback
    acceptance test 4 forbids the join from inventing.
    """
    payload: dict[str, object] = {
        "intent": "test",
        "domain": "x",
        "items_count": len(item_ids),
    }
    if not sectioned:
        payload["injected_items"] = [
            {"item_id": item_id, "item_type": "precedent", "rank": rank + 1}
            for rank, item_id in enumerate(item_ids)
        ]
    event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
            occurred_at=at,
        )
    )


def _emit_feedback(
    event_log: EventLog,
    *,
    pack_id: str,
    at: datetime,
    helpful: list[str] | None = None,
    unhelpful: list[str] | None = None,
    feedback_id: str | None = None,
) -> None:
    payload: dict[str, object] = {"pack_id": pack_id, "rating": 0.5, "success": True}
    if helpful:
        payload["helpful_item_ids"] = list(helpful)
    if unhelpful:
        payload["unhelpful_item_ids"] = list(unhelpful)
    event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=feedback_id or f"fb-{pack_id}",
            payload=payload,
            occurred_at=at,
        )
    )


class TestJudgePrecedentDemotion:
    """Pure decision rule — mirrors ``demotion_gate._judge``'s shape."""

    def test_missing_evidence_refuses_rather_than_admits(self) -> None:
        """Acceptance test 1: zero attributed packs since promotion (no
        evidence record at all) must refuse, not admit."""
        decision = judge_precedent_demotion(None, "p1")
        assert decision.admitted is False
        assert decision.reason == REFUSED_NO_EVIDENCE
        assert decision.unhelpful_count == 0

    def test_zero_unhelpful_refuses_regardless_of_helpful_count(self) -> None:
        """Acceptance test 2: ``unhelpful_citations == 0`` refuses no
        matter how low (including zero) ``helpful_citations`` is — the
        rule never admits on an absence of helpfulness."""
        decision = judge_precedent_demotion(_ev(unhelpful=0, helpful=0), "p1")
        assert decision.admitted is False
        assert decision.reason == REFUSED_NO_UNHELPFUL

        decision_with_helpful = judge_precedent_demotion(
            _ev(unhelpful=0, helpful=50), "p1"
        )
        assert decision_with_helpful.admitted is False
        assert decision_with_helpful.reason == REFUSED_NO_UNHELPFUL

    def test_refuses_a_single_unhelpful_citation_below_the_floor(self) -> None:
        decision = judge_precedent_demotion(_ev(unhelpful=1), "p1")
        assert decision.reason == REFUSED_INSUFFICIENT

    def test_refuses_when_helpful_ties_unhelpful(self) -> None:
        decision = judge_precedent_demotion(_ev(helpful=2, unhelpful=2), "p1")
        assert decision.admitted is False
        assert decision.reason == REFUSED_CONTESTED

    def test_refuses_when_helpful_outweighs_unhelpful(self) -> None:
        decision = judge_precedent_demotion(_ev(helpful=5, unhelpful=2), "p1")
        assert decision.reason == REFUSED_CONTESTED

    def test_admits_at_the_floor_with_no_helpful(self) -> None:
        decision = judge_precedent_demotion(
            _ev(unhelpful=MIN_UNHELPFUL_CITATIONS, helpful=0), "p1"
        )
        assert decision.admitted is True
        assert decision.reason == ""

    def test_threshold_is_honoured_when_raised(self) -> None:
        decision = judge_precedent_demotion(
            _ev(unhelpful=2), "p1", min_unhelpful_citations=3
        )
        assert decision.reason == REFUSED_INSUFFICIENT

    def test_decision_carries_the_counts_that_produced_it(self) -> None:
        decision = judge_precedent_demotion(_ev(helpful=1, unhelpful=3), "p1")
        assert (decision.helpful_count, decision.unhelpful_count) == (1, 3)
        assert decision.entity_id == "p1"


class TestGatherPrecedentCitationEvidence:
    """The PACK_ASSEMBLED + FEEDBACK_RECORDED join, scoped to one entity."""

    def test_no_attributed_packs_returns_none(self, event_log: EventLog) -> None:
        """Acceptance test 1, at the join layer: a precedent with zero
        attributed packs since promotion has no evidence record at all,
        not a zero-valued one."""
        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)
        assert evidence is None

    def test_citation_before_promotion_contributes_nothing(
        self, event_log: EventLog
    ) -> None:
        """Acceptance test 3: a citation/feedback pair that occurred
        before ``promoted_at`` must not be counted, because the window
        this join reads starts at ``since=promoted_at``."""
        before = _T0 - timedelta(days=1)
        _emit_pack_assembled(event_log, pack_id="pack-old", item_ids=["p1"], at=before)
        _emit_feedback(event_log, pack_id="pack-old", at=before, unhelpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is None

    def test_sectioned_pack_contributes_nothing(self, event_log: EventLog) -> None:
        """Acceptance test 4: a sectioned pack (no ``injected_items``)
        plus feedback naming it contributes zero rows -- the join must
        not fall back to any other field to recover attribution."""
        _emit_pack_assembled(
            event_log,
            pack_id="pack-sectioned",
            item_ids=["p1"],
            at=_T0 + timedelta(hours=1),
            sectioned=True,
        )
        _emit_feedback(
            event_log,
            pack_id="pack-sectioned",
            at=_T0 + timedelta(hours=1),
            unhelpful=["p1"],
        )

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is None

    def test_positive_join_counts_helpful_and_unhelpful_separately(
        self, event_log: EventLog
    ) -> None:
        at = _T0 + timedelta(hours=1)
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["p1"], at=at)
        _emit_feedback(event_log, pack_id="pack-1", at=at, unhelpful=["p1"])

        at2 = _T0 + timedelta(hours=2)
        _emit_pack_assembled(event_log, pack_id="pack-2", item_ids=["p1"], at=at2)
        _emit_feedback(event_log, pack_id="pack-2", at=at2, helpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is not None
        assert evidence.appearances == 2
        assert evidence.helpful_count == 1
        assert evidence.unhelpful_count == 1

    def test_citation_at_exactly_promoted_at_counts(self, event_log: EventLog) -> None:
        """``since`` is an inclusive floor (``occurred_at >= since``), so
        a citation recorded in the same instant as promotion still
        counts -- the exclusion in test 3 is strictly-before, not
        at-or-before."""
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["p1"], at=_T0)
        _emit_feedback(event_log, pack_id="pack-1", at=_T0, unhelpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is not None
        assert evidence.unhelpful_count == 1

    def test_feedback_naming_an_unknown_pack_contributes_nothing(
        self, event_log: EventLog
    ) -> None:
        at = _T0 + timedelta(hours=1)
        _emit_feedback(event_log, pack_id="pack-missing", at=at, unhelpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is None

    def test_pack_that_never_served_this_entity_contributes_nothing(
        self, event_log: EventLog
    ) -> None:
        at = _T0 + timedelta(hours=1)
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["other"], at=at)
        _emit_feedback(event_log, pack_id="pack-1", at=at, unhelpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is None


class TestJoinPrecedentCitationEvents:
    """The shared, run-once join -- ``gather_precedent_citation_evidence``
    is now a thin per-entity wrapper over this."""

    def test_returns_the_feedback_and_pack_events_untouched(
        self, event_log: EventLog
    ) -> None:
        at = _T0 + timedelta(hours=1)
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["p1"], at=at)
        _emit_feedback(event_log, pack_id="pack-1", at=at, unhelpful=["p1"])

        feedback_events, pack_events, coverage = join_precedent_citation_events(
            event_log, since=_T0
        )

        assert len(feedback_events) == 1
        assert "pack-1" in pack_events
        assert coverage.truncated is False

    def test_truncated_join_reports_truncated(self, event_log: EventLog) -> None:
        """Fix 2's other scan: a capped PACK_ASSEMBLED/FEEDBACK_RECORDED
        read can under-count unhelpful citations the same way a
        truncated discovery scan can hide a precedent entirely. This is
        the coverage ``gather_precedent_citation_evidence`` discards --
        pinned here, at the source, so that discard is a choice made
        once and not a spreading habit."""
        at1 = _T0 + timedelta(hours=1)
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["p1"], at=at1)
        _emit_feedback(event_log, pack_id="pack-1", at=at1, unhelpful=["p1"])
        at2 = _T0 + timedelta(hours=2)
        _emit_pack_assembled(event_log, pack_id="pack-2", item_ids=["p1"], at=at2)
        _emit_feedback(event_log, pack_id="pack-2", at=at2, unhelpful=["p1"])

        _feedback_events, _pack_events, coverage = join_precedent_citation_events(
            event_log, since=_T0, limit=1
        )

        assert coverage.truncated is True


class TestPrecedentSkipReason:
    def test_missing_node_is_skipped_not_judged(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        """A trace-/miner-sourced precedent never gets a graph entity
        (``PrecedentPromoteHandler``'s docstring), so
        ``PrecedentDemoteHandler`` refuses it outright
        (``target_not_graph_backed``) -- "would_demote" here would
        describe a write that handler can never make."""
        assert (
            precedent_skip_reason(graph_store, "does-not-exist")
            == SKIPPED_NOT_GRAPH_BACKED
        )

    def test_node_with_no_lifecycle_record_is_judged(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        node_id = graph_store.upsert_node(
            node_id="p1", node_type="precedent", properties={}
        )
        assert precedent_skip_reason(graph_store, node_id) is None

    def test_current_state_is_judged(self, graph_store: SQLiteGraphStore) -> None:
        node_id = graph_store.upsert_node(
            node_id="p1",
            node_type="precedent",
            properties={LIFECYCLE_KEY: {"state": "current"}},
        )
        assert precedent_skip_reason(graph_store, node_id) is None

    def test_demoted_state_is_skipped(self, graph_store: SQLiteGraphStore) -> None:
        node_id = graph_store.upsert_node(
            node_id="p1",
            node_type="precedent",
            properties={LIFECYCLE_KEY: {"state": "demoted"}},
        )
        assert precedent_skip_reason(graph_store, node_id) == SKIPPED_ALREADY_DEMOTED

    def test_archived_state_is_skipped(self, graph_store: SQLiteGraphStore) -> None:
        node_id = graph_store.upsert_node(
            node_id="p1",
            node_type="precedent",
            properties={LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        assert precedent_skip_reason(graph_store, node_id) == SKIPPED_ARCHIVED


class TestDiscoverCurrentPrecedents:
    def test_maps_entity_to_its_promotion_time(self, event_log: EventLog) -> None:
        _emit_promote(event_log, "p1", at=_T0)

        promoted_at = discover_current_precedents(event_log)

        assert promoted_at == {"p1": _T0}

    def test_latest_promotion_wins_on_reproposal(self, event_log: EventLog) -> None:
        """Newest-first scan: a re-promoted entity's *latest*
        ``precedent.promote`` time is the one the citation window starts
        from, not the first promotion ever."""
        later = _T0 + timedelta(days=30)
        _emit_promote(event_log, "p1", at=_T0)
        _emit_promote(event_log, "p1", at=later)

        promoted_at = discover_current_precedents(event_log)

        assert promoted_at == {"p1": later}

    def test_empty_log_returns_empty_map(self, event_log: EventLog) -> None:
        assert discover_current_precedents(event_log) == {}


class TestDiscoverCurrentPrecedentsWithCoverage:
    def test_untruncated_scan_is_not_truncated(self, event_log: EventLog) -> None:
        _emit_promote(event_log, "p1", at=_T0)

        promoted_at, coverage = discover_current_precedents_with_coverage(event_log)

        assert promoted_at == {"p1": _T0}
        assert coverage.truncated is False
        assert coverage.note == ""

    def test_truncated_scan_reports_truncated_with_a_note(
        self, event_log: EventLog
    ) -> None:
        """A full-looking read (``len(events) >= limit``) means older
        promotions may have been dropped entirely -- the same
        hand-detection the function's own docstring describes, since
        its ``payload_filters`` read can't use ``scan_events``."""
        _emit_promote(event_log, "p1", at=_T0)
        _emit_promote(event_log, "p2", at=_T0 + timedelta(hours=1))

        promoted_at, coverage = discover_current_precedents_with_coverage(
            event_log, limit=1
        )

        assert len(promoted_at) == 1
        assert coverage.truncated is True
        assert coverage.note
        assert coverage.scanned == 1
        assert coverage.limit == 1

    def test_discover_current_precedents_drops_the_coverage(
        self, event_log: EventLog
    ) -> None:
        """The thin wrapper returns the same map as the coverage-carrying
        function, just without the coverage -- pinning that the split
        didn't change its contract for existing callers."""
        _emit_promote(event_log, "p1", at=_T0)
        _emit_promote(event_log, "p2", at=_T0 + timedelta(hours=1))

        promoted_at, _coverage = discover_current_precedents_with_coverage(
            event_log, limit=1
        )
        assert discover_current_precedents(event_log, limit=1) == promoted_at


class TestEvaluatePrecedentDemotions:
    def test_would_demote_end_to_end(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)
        at = _T0 + timedelta(hours=1)
        _emit_pack_assembled(event_log, pack_id="pack-1", item_ids=["p1"], at=at)
        _emit_feedback(event_log, pack_id="pack-1", at=at, unhelpful=["p1"])
        at2 = _T0 + timedelta(hours=2)
        _emit_pack_assembled(event_log, pack_id="pack-2", item_ids=["p1"], at=at2)
        _emit_feedback(event_log, pack_id="pack-2", at=at2, unhelpful=["p1"])

        report = evaluate_precedent_demotions(event_log, graph_store)

        assert report.would_demote_count == 1
        outcome = report.outcomes[0]
        assert outcome.entity_id == "p1"
        assert outcome.disposition == DISPOSITION_WOULD_DEMOTE
        assert outcome.unhelpful_count == 2

    def test_skipped_precedent_is_never_handed_to_the_gather_join(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """A demoted precedent needs no citation read at all -- the skip
        check keeps it out of ``to_judge`` entirely, so the shared join
        (``join_precedent_citation_events``, which ``evaluate_precedent_demotions``
        now calls once per run instead of once per precedent) never runs
        when every discovered precedent is skipped."""
        graph_store.upsert_node(
            node_id="p1",
            node_type="precedent",
            properties={LIFECYCLE_KEY: {"state": "demoted"}},
        )
        _emit_promote(event_log, "p1", at=_T0)

        with patch(
            "trellis.learning.precedent_evidence_gate.join_precedent_citation_events"
        ) as mock_join:
            report = evaluate_precedent_demotions(event_log, graph_store)

        mock_join.assert_not_called()
        assert report.skipped_count == 1
        outcome = report.outcomes[0]
        assert outcome.disposition == DISPOSITION_SKIPPED
        assert outcome.reason == SKIPPED_ALREADY_DEMOTED

    def test_precedent_with_no_graph_node_is_skipped_not_demoted(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """Fix 1, end to end: a trace-/miner-sourced precedent (promoted
        but never given a graph node) is skipped with
        ``SKIPPED_NOT_GRAPH_BACKED``, not judged and reported as
        ``would_demote`` for a write ``PrecedentDemoteHandler`` would
        refuse (``target_not_graph_backed``)."""
        _emit_promote(event_log, "p1", at=_T0)

        report = evaluate_precedent_demotions(event_log, graph_store)

        assert report.skipped_count == 1
        assert report.would_demote_count == 0
        outcome = report.outcomes[0]
        assert outcome.entity_id == "p1"
        assert outcome.disposition == DISPOSITION_SKIPPED
        assert outcome.reason == SKIPPED_NOT_GRAPH_BACKED

    def test_per_precedent_window_still_applies_after_shared_join(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """Mutant proof for Fix 3: the shared join's ``since`` is the
        *earliest* ``promoted_at`` among the precedents judged this run
        (here, p1's), so its raw read can include citations that predate
        a *later*-promoted precedent's (p2's) own window.
        ``_attribute_precedent_citation_evidence`` must still filter each
        precedent to its own ``promoted_at`` on top of that shared read --
        removing or widening that per-row guard would let p2 be demoted
        on citations that occurred before p2 was even promoted."""
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        graph_store.upsert_node(node_id="p2", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)
        p2_promoted_at = _T0 + timedelta(hours=10)
        _emit_promote(event_log, "p2", at=p2_promoted_at)

        # Two unhelpful citations for p2, both strictly before p2's own
        # promotion but after p1's (the shared join's ``since``) -- in
        # window for the one shared read, out of window for p2 itself.
        early1 = _T0 + timedelta(hours=1)
        _emit_pack_assembled(
            event_log, pack_id="pack-early-1", item_ids=["p2"], at=early1
        )
        _emit_feedback(event_log, pack_id="pack-early-1", at=early1, unhelpful=["p2"])
        early2 = _T0 + timedelta(hours=2)
        _emit_pack_assembled(
            event_log, pack_id="pack-early-2", item_ids=["p2"], at=early2
        )
        _emit_feedback(event_log, pack_id="pack-early-2", at=early2, unhelpful=["p2"])

        report = evaluate_precedent_demotions(event_log, graph_store)

        p2_outcome = next(o for o in report.outcomes if o.entity_id == "p2")
        assert p2_outcome.disposition == DISPOSITION_REFUSED
        assert p2_outcome.reason == REFUSED_NO_EVIDENCE
        assert p2_outcome.unhelpful_count == 0

    def test_truncated_discovery_scan_is_surfaced_on_report_and_event(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """Fix 2, end to end: a truncated discovery scan can hide an
        older precedent entirely, not merely under-count one it already
        knows about -- both the report and the emitted event must say
        so, not just ``discover_current_precedents_with_coverage``
        itself."""
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        graph_store.upsert_node(node_id="p2", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)
        _emit_promote(event_log, "p2", at=_T0 + timedelta(hours=1))

        report = evaluate_precedent_demotions(
            event_log, graph_store, promotion_scan_limit=1
        )

        assert report.scan.truncated is True
        assert report.notes
        assert "TRUNCATED" in report.notes[0]

        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(events) == 1
        assert events[0].payload["scan"]["truncated"] is True

    def test_report_to_dict_carries_truncation(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        graph_store.upsert_node(node_id="p2", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)
        _emit_promote(event_log, "p2", at=_T0 + timedelta(hours=1))

        report = evaluate_precedent_demotions(
            event_log, graph_store, promotion_scan_limit=1
        )
        data = report_to_dict(report)

        assert data["scan"]["truncated"] is True
        assert data["notes"]

    def test_no_evidence_refuses_not_skips(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)

        report = evaluate_precedent_demotions(event_log, graph_store)

        assert report.refused_count == 1
        assert report.outcomes[0].disposition == DISPOSITION_REFUSED
        assert report.outcomes[0].reason == REFUSED_NO_EVIDENCE

    def test_exactly_one_event_emitted_per_run(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0)

        report = evaluate_precedent_demotions(event_log, graph_store)

        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(events) == 1
        assert events[0].event_id == report.event_id
        assert events[0].payload["mode"] == "report_only"
        assert events[0].payload["counts"]["total"] == 1

    def test_empty_log_still_emits_one_event_with_zero_counts(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        report = evaluate_precedent_demotions(event_log, graph_store)

        assert report.outcomes == []
        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(events) == 1

    def test_report_to_dict_carries_the_attribution_caveat(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        report = evaluate_precedent_demotions(event_log, graph_store)
        data = report_to_dict(report)
        assert "get_lessons" in data["attribution_caveat"]
        assert "sectioned" in data["attribution_caveat"]


class TestPortedConstants:
    def test_thresholds_match_the_classify_gate_they_were_copied_from(self) -> None:
        """Copied by value, so drift is only catchable by asserting it.

        ``trellis.learning`` must not import ``trellis.classify`` (see
        the module docstring), so this is the only thing standing
        between the two gates and a silent divergence.
        """
        from trellis.classify import demotion_gate

        assert MIN_UNHELPFUL_CITATIONS == demotion_gate.MIN_UNHELPFUL_CITATIONS
        assert REFUSED_NO_EVIDENCE == demotion_gate.REFUSED_NO_EVIDENCE
        assert REFUSED_NO_UNHELPFUL == demotion_gate.REFUSED_NO_UNHELPFUL
        assert REFUSED_INSUFFICIENT == demotion_gate.REFUSED_INSUFFICIENT
        assert REFUSED_CONTESTED == demotion_gate.REFUSED_CONTESTED

    def test_learning_does_not_import_classify_or_retrieve_at_module_scope(
        self,
    ) -> None:
        """The copy exists to keep this edge out of the import graph."""
        import ast
        import pathlib

        module_file = __import__(
            "trellis.learning.precedent_evidence_gate", fromlist=["__file__"]
        ).__file__
        assert module_file
        source = pathlib.Path(module_file).read_text()
        imported = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(
            module.startswith(("trellis.classify", "trellis.retrieve"))
            for module in imported
        )
