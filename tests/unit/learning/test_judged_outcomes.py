"""The judged-outcome join: what followed each ``memory_op.judged`` row (B1).

Every test seeds a real :class:`SQLiteEventLog` with controlled
``occurred_at`` stamps, because the rules under test are about order
(was the memory served *after* it was judged?) and about which scan a row
fell inside — neither survives a mocked log.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.judged_outcomes import (
    PHASE_1_GATE,
    JudgedOutcomeCell,
    JudgedOutcomesReport,
    summarize_judged_outcomes,
)
from trellis.learning.pack_observations import (
    join_pack_events_with_coverage,
    join_pack_feedback_with_coverage,
)
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

RUNGS = (
    "unservable",
    "never_served",
    "served_ungraded",
    "graded_uncited",
    "cited_unhelpful",
    "cited_helpful",
)


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _ago(*, days: float = 0, hours: float = 0) -> datetime:
    return datetime.now(tz=UTC) - timedelta(days=days, hours=hours)


def _append(
    log: SQLiteEventLog,
    event_type: EventType,
    *,
    at: datetime,
    entity_id: str | None,
    payload: dict[str, Any],
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source="test",
            entity_id=entity_id,
            occurred_at=at,
            recorded_at=at,
            payload=payload,
        )
    )


def _judged(
    log: SQLiteEventLog,
    ref_id: str,
    *,
    at: datetime,
    op_type: JudgedOpType = JudgedOpType.CLASSIFICATION,
    decision: str = "reference",
    ref_type: str = REF_TYPE_DOCUMENT,
) -> None:
    # Built through the real schema so a renamed payload key breaks here,
    # not silently in the reader.
    payload = MemoryOpJudgedPayload(
        op_type=op_type,
        model_id="test-model",
        input_digest=InputDigest(hash="0123abcd", length=42),
        decision=decision,
        confidence=0.9,
        subject_ref=SubjectRef(ref_type=ref_type, ref_id=ref_id),
    ).model_dump(mode="json")
    _append(log, EventType.MEMORY_OP_JUDGED, at=at, entity_id=ref_id, payload=payload)


def _pack(
    log: SQLiteEventLog, pack_id: str | None, items: Iterable[str], *, at: datetime
) -> None:
    injected = [
        {"item_id": item_id, "item_type": "document", "estimated_tokens": 10, "rank": n}
        for n, item_id in enumerate(items)
    ]
    _append(
        log,
        EventType.PACK_ASSEMBLED,
        at=at,
        entity_id=pack_id,
        payload={"intent": "test", "injected_items": injected},
    )


def _feedback(
    log: SQLiteEventLog,
    pack_id: str,
    *,
    at: datetime,
    helpful: Iterable[str] = (),
    unhelpful: Iterable[str] = (),
) -> None:
    _append(
        log,
        EventType.FEEDBACK_RECORDED,
        at=at,
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "helpful_item_ids": list(helpful),
            "unhelpful_item_ids": list(unhelpful),
            "rating": 0.5,
            "success": True,
        },
    )


def _rungs(cell: JudgedOutcomeCell) -> dict[str, int]:
    return {rung: getattr(cell, rung) for rung in RUNGS if getattr(cell, rung)}


def _cell(
    report: JudgedOutcomesReport, op_type: str, decision: str
) -> JudgedOutcomeCell:
    matches = [
        c for c in report.cells if c.op_type == op_type and c.decision == decision
    ]
    assert len(matches) == 1, [(c.op_type, c.decision) for c in report.cells]
    return matches[0]


def _reading(report: JudgedOutcomesReport, name: str) -> Any:
    matches = [r for r in report.gate_readings if r.reading == name]
    assert len(matches) == 1
    return matches[0]


class TestContainment:
    """A document judgment covers its chunks; a chunk judgment covers itself."""

    def test_parent_subject_matches_its_chunk_servings(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "doc1", at=_ago(hours=3))
        _pack(log, "p1", ["doc1#chunk-2", "doc1#chunk-5"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"served_ungraded": 1}
        assert report.totals.servings == 2

    def test_chunk_subject_matches_neither_parent_nor_sibling(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "doc1#chunk-1", at=_ago(hours=3))
        _pack(log, "p1", ["doc1", "doc1#chunk-2"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"never_served": 1}

    def test_a_shared_id_prefix_is_not_containment(self, log: SQLiteEventLog) -> None:
        _judged(log, "doc1", at=_ago(hours=3))
        _pack(log, "p1", ["doc10", "doc1x#chunk-0", "doc1#chunkish"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"never_served": 1}

    def test_ref_type_is_reported_and_never_matched_on(
        self, log: SQLiteEventLog
    ) -> None:
        # Writers spell a document both ``doc`` and ``document``.
        _judged(log, "a", at=_ago(hours=3), ref_type="doc")
        _judged(log, "b", at=_ago(hours=3), ref_type="document")
        _judged(log, "c", at=_ago(hours=3), ref_type="entity")
        _pack(log, "p1", ["a", "b", "c"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert report.totals.served == 3
        assert report.ref_types == {"doc": 1, "document": 1, "entity": 1}


class TestOrder:
    """An outcome cannot precede the decision it grades."""

    def test_a_serving_before_the_judgment_is_not_an_outcome(
        self, log: SQLiteEventLog
    ) -> None:
        _pack(log, "p1", ["a"], at=_ago(hours=3))
        _feedback(log, "p1", at=_ago(hours=2), helpful=["a"])
        _judged(log, "a", at=_ago(hours=1))

        report = summarize_judged_outcomes(log)
        totals = report.totals

        assert _rungs(totals) == {"never_served": 1}
        assert totals.served_before_only == 1
        assert (totals.served, totals.cited) == (0, 0)
        assert (totals.served_any_order, totals.cited_any_order) == (1, 1)

    def test_only_downstream_servings_are_counted(self, log: SQLiteEventLog) -> None:
        _pack(log, "before", ["a"], at=_ago(hours=4))
        _feedback(log, "before", at=_ago(hours=3), helpful=["a"])
        _judged(log, "a", at=_ago(hours=2))
        _pack(log, "after", ["a"], at=_ago(hours=1))

        report = summarize_judged_outcomes(log)
        totals = report.totals

        # The helpful citation came first, so the row is only served.
        assert _rungs(totals) == {"served_ungraded": 1}
        assert totals.servings == 1
        assert totals.served_before_only == 0
        assert totals.cited_any_order == 1

    def test_a_serving_in_the_same_instant_counts(self, log: SQLiteEventLog) -> None:
        at = _ago(hours=2)
        _judged(log, "a", at=at)
        _pack(log, "p1", ["a"], at=at)

        report = summarize_judged_outcomes(log)

        assert report.totals.served == 1


class TestRungs:
    def test_each_rung_and_the_partition(self, log: SQLiteEventLog) -> None:
        start = _ago(hours=5)
        for ref_id in ("never", "ungraded", "uncited", "unhelpful", "helpful"):
            _judged(log, ref_id, at=start)
        _pack(log, "quiet", ["ungraded"], at=_ago(hours=4))
        # Feedback with no item ids grades nothing per item.
        _feedback(log, "quiet", at=_ago(hours=3))
        _pack(log, "graded", ["uncited", "unhelpful", "helpful"], at=_ago(hours=2))
        _feedback(
            log,
            "graded",
            at=_ago(hours=1),
            helpful=["helpful"],
            unhelpful=["unhelpful"],
        )

        report = summarize_judged_outcomes(log)
        totals = report.totals

        assert _rungs(totals) == {
            "never_served": 1,
            "served_ungraded": 1,
            "graded_uncited": 1,
            "cited_unhelpful": 1,
            "cited_helpful": 1,
        }
        assert (totals.served, totals.graded, totals.cited) == (4, 3, 2)
        assert report.attributed_packs == 1
        assert report.pack_targeted_feedback == 2

    def test_the_best_serving_wins_across_packs(self, log: SQLiteEventLog) -> None:
        _judged(log, "a", at=_ago(hours=5))
        _pack(log, "quiet", ["a"], at=_ago(hours=4))
        _pack(log, "graded", ["a", "b"], at=_ago(hours=3))
        _feedback(log, "graded", at=_ago(hours=2), helpful=["b"])

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"graded_uncited": 1}
        assert report.totals.servings == 2

    def test_helpful_wins_and_the_contradiction_is_counted(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "a", at=_ago(hours=5))
        _judged(log, "b", at=_ago(hours=5))
        _pack(log, "p1", ["a", "b"], at=_ago(hours=4))
        _feedback(log, "p1", at=_ago(hours=3), unhelpful=["a"], helpful=["b"])
        _pack(log, "p2", ["a"], at=_ago(hours=2))
        _feedback(log, "p2", at=_ago(hours=1), helpful=["a"])

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"cited_helpful": 2}
        assert report.totals.cited_contradictory == 1

    def test_helpful_wins_inside_one_pack(self, log: SQLiteEventLog) -> None:
        # Two graders disagree about the same serving; analyze value reads
        # that as helpful, and this join has to agree with it.
        _judged(log, "a", at=_ago(hours=4))
        _pack(log, "p1", ["a"], at=_ago(hours=3))
        _feedback(log, "p1", at=_ago(hours=2), unhelpful=["a"])
        _feedback(log, "p1", at=_ago(hours=1), helpful=["a"])

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"cited_helpful": 1}
        assert report.totals.cited_contradictory == 1

    def test_rungs_partition_every_cell_and_rollups_sum(
        self, log: SQLiteEventLog
    ) -> None:
        start = _ago(hours=6)
        decisions = ["reference", "notes", "reference", "research", "notes", "notes"]
        for n, decision in enumerate(decisions):
            _judged(log, f"c{n}", at=start, decision=decision)
        _judged(log, "d0", at=start, op_type=JudgedOpType.DISTILLATION, decision="keep")
        _judged(
            log,
            "sess-1",
            at=start,
            op_type=JudgedOpType.DISTILLATION,
            decision="discard",
            ref_type="session",
        )
        _pack(log, "p1", ["c0", "c1#chunk-0", "c2", "d0"], at=_ago(hours=4))
        _feedback(log, "p1", at=_ago(hours=3), helpful=["c0"], unhelpful=["c2"])
        _pack(log, "p2", ["c3"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        every = [*report.cells, *report.by_op_type, report.totals]
        for cell in every:
            assert sum(getattr(cell, rung) for rung in RUNGS) == cell.judged, cell
            assert cell.cited == cell.cited_helpful + cell.cited_unhelpful
            assert cell.graded == cell.cited + cell.graded_uncited
            assert cell.served == cell.graded + cell.served_ungraded
        for field in ("judged", *RUNGS, "served", "servings", "served_before_only"):
            by_cells = sum(getattr(c, field) for c in report.cells)
            by_ops = sum(getattr(c, field) for c in report.by_op_type)
            assert by_cells == by_ops == getattr(report.totals, field), field
        assert report.totals.judged == 8


class TestUnservable:
    def test_an_unmatched_session_subject_is_unservable(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(
            log,
            "session-1",
            at=_ago(hours=2),
            op_type=JudgedOpType.DISTILLATION,
            decision="discard",
            ref_type="session",
        )

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"unservable": 1}
        assert all(reading.count == 0 for reading in report.gate_readings)

    def test_an_unknown_ref_type_is_presumed_servable(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "w1", at=_ago(hours=2), ref_type="widget")

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"never_served": 1}

    def test_a_session_subject_that_was_served_is_graded_normally(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "s1", at=_ago(hours=3), ref_type="session")
        _pack(log, "p1", ["s1"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"served_ungraded": 1}

    def test_a_session_subject_served_only_before_is_not_unservable(
        self, log: SQLiteEventLog
    ) -> None:
        _pack(log, "p1", ["s1"], at=_ago(hours=3))
        _judged(log, "s1", at=_ago(hours=2), ref_type="session")

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"never_served": 1}
        assert report.totals.served_before_only == 1


class TestPacksAndCitations:
    def test_sectioned_packs_serve_nothing_joinable(self, log: SQLiteEventLog) -> None:
        _judged(log, "a", at=_ago(hours=3))
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            at=_ago(hours=2),
            entity_id="sectioned",
            payload={"sections": [{"items": [{"item_id": "a"}]}]},
        )
        _feedback(log, "sectioned", at=_ago(hours=1), helpful=["a"])

        report = summarize_judged_outcomes(log)

        assert _rungs(report.totals) == {"never_served": 1}
        assert report.sectioned_packs_excluded == 1
        assert report.unjoined_feedback == 1
        assert any("sectioned pack(s) excluded" in note for note in report.notes)

    def test_duplicate_item_in_one_pack_is_one_serving(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "a", at=_ago(hours=3))
        _pack(log, "p1", ["a", "a"], at=_ago(hours=2))

        report = summarize_judged_outcomes(log)

        assert report.totals.servings == 1

    def test_stray_citations_and_those_that_would_have_joined(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "doc1", at=_ago(hours=4))
        _judged(log, "doc2", at=_ago(hours=4))
        _pack(log, "p1", ["doc1#chunk-0", "x"], at=_ago(hours=3))
        _feedback(
            log,
            "p1",
            at=_ago(hours=2),
            # doc1 was served as a chunk; doc2's chunk was never served.
            helpful=["doc1", "x", "zzz"],
            unhelpful=["doc2#chunk-5"],
        )

        report = summarize_judged_outcomes(log)

        assert report.stray_citations == 3
        assert report.stray_citations_matching_judged == 2
        assert _cell(report, "classification", "reference").graded_uncited == 1
        assert report.totals.never_served == 1


class TestGate:
    def test_the_threshold_is_the_one_the_plan_states(self) -> None:
        assert PHASE_1_GATE == 500

    @staticmethod
    def _seed_cited(log: SQLiteEventLog, count: int, *, cite: bool) -> None:
        subjects = [f"m{n}" for n in range(count)]
        for subject in subjects:
            _judged(log, subject, at=_ago(hours=3))
        _pack(log, "p1", [*subjects, "other"], at=_ago(hours=2))
        _feedback(log, "p1", at=_ago(hours=1), helpful=subjects if cite else ["other"])

    def test_passes_strictly_above_the_threshold(self, log: SQLiteEventLog) -> None:
        # 51 cited rows over 3 days is 510 per 30 days.
        self._seed_cited(log, 51, cite=True)

        report = summarize_judged_outcomes(log, days=3)

        assert report.gate_verdict == "passes"
        assert report.gate_number == 510.0
        assert _reading(report, "cited_rows").passes

    def test_exactly_the_threshold_fails(self, log: SQLiteEventLog) -> None:
        self._seed_cited(log, 50, cite=True)

        report = summarize_judged_outcomes(log, days=3)

        assert report.gate_number == 500.0
        assert report.gate_verdict == "fails_every_reading"

    def test_a_looser_reading_passing_is_named(self, log: SQLiteEventLog) -> None:
        self._seed_cited(log, 51, cite=False)

        report = summarize_judged_outcomes(log, days=3)

        assert report.gate_verdict == "fails_strict_reading"
        assert report.gate_number == 0.0
        assert _reading(report, "graded_rows").per_30d == 510.0
        assert _reading(report, "graded_rows").passes

    def test_never_served_rows_count_toward_no_reading(
        self, log: SQLiteEventLog
    ) -> None:
        for n in range(60):
            _judged(log, f"m{n}", at=_ago(hours=3))

        report = summarize_judged_outcomes(log, days=3)

        assert report.totals.never_served == 60
        assert all(reading.count == 0 for reading in report.gate_readings)
        assert report.gate_verdict == "fails_every_reading"

    def test_rows_and_subjects_straddling_the_gate_are_called_out(
        self, log: SQLiteEventLog
    ) -> None:
        for _ in range(51):
            _judged(log, "doc1", at=_ago(hours=3))
        _pack(log, "p1", ["doc1"], at=_ago(hours=2))
        _feedback(log, "p1", at=_ago(hours=1), helpful=["doc1"])

        report = summarize_judged_outcomes(log, days=3)

        assert report.gate_verdict == "passes"
        assert _reading(report, "cited_subjects").per_30d == 10.0
        assert report.totals.distinct_cited_subjects == 1
        assert any("opposite sides of the gate" in note for note in report.notes)

    def test_readings_are_ordered_strictest_first(self, log: SQLiteEventLog) -> None:
        report = summarize_judged_outcomes(log)

        assert report.gate_readings[0].reading == report.gate_reading == "cited_rows"
        assert report.gate_readings[-1].reading == "served_rows_any_order"
        assert len({r.reading for r in report.gate_readings}) == 9


class TestTruncation:
    def test_rows_before_the_evidence_start_are_dropped_and_rates_rescaled(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "old", at=_ago(days=5))
        for n, days_ago in enumerate((4, 3, 2)):
            _pack(log, f"p{n}", ["filler"], at=_ago(days=days_ago))
        _judged(log, "new", at=_ago(days=1, hours=12))
        _pack(log, "p3", ["new"], at=_ago(days=1))
        _feedback(log, "p3", at=_ago(hours=12), helpful=["new"])

        # Four packs against a limit of three: the pack scan keeps the newest
        # three, so the evidence starts at p1 (three days ago).
        report = summarize_judged_outcomes(log, days=10, limit=3)

        assert report.scan.truncated
        assert report.judged_rows_before_coverage == 1
        assert report.totals.judged == 1
        assert 2.99 < report.effective_window_days < 3.01
        assert report.gate_number == 10.0
        assert any("TRUNCATED" in note for note in report.notes)
        assert any("evidence start were dropped" in note for note in report.notes)

    def test_an_untruncated_window_is_the_requested_window(
        self, log: SQLiteEventLog
    ) -> None:
        _judged(log, "a", at=_ago(days=20))
        _judged(log, "b", at=_ago(days=40))

        report = summarize_judged_outcomes(log, days=30)

        assert not report.scan.truncated
        assert report.effective_window_days == 30
        assert report.totals.judged == 1


class TestNotes:
    def test_a_single_decision_op_type_is_named_over_servable_rows(
        self, log: SQLiteEventLog
    ) -> None:
        for ref_id in ("a", "b"):
            _judged(
                log,
                ref_id,
                at=_ago(hours=2),
                op_type=JudgedOpType.DISTILLATION,
                decision="keep",
            )
        _judged(
            log,
            "session-1",
            at=_ago(hours=2),
            op_type=JudgedOpType.DISTILLATION,
            decision="discard",
            ref_type="session",
        )
        _judged(log, "c", at=_ago(hours=2), decision="reference")
        _judged(log, "d", at=_ago(hours=2), decision="notes")

        report = summarize_judged_outcomes(log)

        assert report.single_decision_op_types == {"distillation": "keep"}
        assert any("distillation='keep' (n=2)" in note for note in report.notes)

    def test_malformed_rows_are_counted_and_placed_on_no_rung(
        self, log: SQLiteEventLog
    ) -> None:
        subject = {"ref_type": "doc", "ref_id": "a"}
        for payload in (
            {},
            {"op_type": "classification", "decision": "", "subject_ref": subject},
            {"op_type": 3, "decision": "x", "subject_ref": subject},
            {"op_type": "classification", "decision": "x", "subject_ref": "a"},
            {"op_type": "classification", "decision": "x", "subject_ref": {}},
        ):
            _append(
                log,
                EventType.MEMORY_OP_JUDGED,
                at=_ago(hours=2),
                entity_id="a",
                payload=payload,
            )
        # A row a newer build widened still counts: the reader is lenient.
        _append(
            log,
            EventType.MEMORY_OP_JUDGED,
            at=_ago(hours=2),
            entity_id="a",
            payload={
                "op_type": "classification",
                "decision": "x",
                "subject_ref": {"ref_id": "a"},
                "added_by_a_later_build": True,
            },
        )

        report = summarize_judged_outcomes(log)

        assert report.malformed_judged_events == 5
        assert report.judged_events == 6
        assert report.totals.judged == 1
        assert report.ref_types == {"": 1}
        assert any("placed on no rung" in note for note in report.notes)

    def test_an_empty_log_reports_zero_with_only_the_definition(
        self, log: SQLiteEventLog
    ) -> None:
        report = summarize_judged_outcomes(log)

        assert report.totals.judged == 0
        assert report.gate_number == 0.0
        assert report.gate_verdict == "fails_every_reading"
        assert len(report.notes) == 1
        assert report.notes[0].startswith("Each judged row takes the best rung")


class TestPackEventJoin:
    """The payload join is now a projection of the event join."""

    def test_the_payload_join_is_the_event_join_projected(
        self, log: SQLiteEventLog
    ) -> None:
        _pack(log, "p1", ["old"], at=_ago(hours=5))
        _pack(log, None, ["orphan"], at=_ago(hours=4))
        _pack(log, "p2", ["b"], at=_ago(hours=3))
        newest = _ago(hours=2)
        _pack(log, "p1", ["new"], at=newest)
        _feedback(log, "p1", at=_ago(hours=1), helpful=["new"])
        since = _ago(days=1)

        fb_a, payloads, count_a, cov_a = join_pack_feedback_with_coverage(
            log, since=since, limit=100
        )
        fb_b, events, count_b, cov_b = join_pack_events_with_coverage(
            log, since=since, limit=100
        )

        assert payloads == {pid: event.payload for pid, event in events.items()}
        assert [e.event_id for e in fb_a] == [e.event_id for e in fb_b]
        assert count_a == count_b == 4
        assert cov_a == cov_b
        assert set(events) == {"p1", "p2"}
        assert events["p1"].payload["injected_items"][0]["item_id"] == "new"
        # The event join keeps the timestamp the payload join threw away;
        # the judged-outcome order rule is built on it.
        assert events["p1"].occurred_at == newest
