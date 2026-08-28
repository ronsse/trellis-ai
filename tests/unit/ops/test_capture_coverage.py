"""Capture-coverage tests.

The load-bearing ones are the *variance* tests. This repo's recurring defect
is a measurement wired to a constant — a reference rate that could only read
1.00, a noise filter that excluded nothing, ``confidence`` that was a literal
in a prompt exemplar — so the suite's first job is to prove that every number
and every state this module reports can actually take more than one value.
``TestTheMetricIsNotAConstant`` is that proof and should be the last thing
anyone deletes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.ops.capture_coverage import (
    MIN_ELIGIBLE_SESSIONS,
    SUPPRESSED_DEGRADED,
    SUPPRESSED_STALE,
    SUPPRESSED_THIN_SAMPLE,
    SUPPRESSED_UNOBSERVED,
    CaptureCoverageReport,
    summarize_capture_coverage,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _sweep_payload(**overrides: Any) -> dict[str, Any]:
    """A sweep funnel payload with every field the emitter writes."""
    payload: dict[str, Any] = {
        "transcripts_root": "/home/u/.claude/projects",
        "dry_run": False,
        "reconcile_enabled": False,
        "source_system": "claude-code",
        "sessions_seen": 100,
        "sessions_skipped_watermark": 80,
        "sessions_parsed": 20,
        "sessions_triggered": 20,
        "sessions_sampled_out": 0,
        "sessions_skipped_ephemeral": 0,
        "sessions_skipped_empty": 0,
        "sessions_judge_unavailable": 0,
        "sessions_with_memory": 10,
        "malformed_lines": 0,
        "candidates_distilled": 30,
        "candidates_rejected_worthiness": 2,
        "candidates_rejected_injection": 0,
        "candidates_blocked_scan": 1,
        "candidates_reconciled_noop": 0,
        "memories_written": 25,
        "memories_skipped_unchanged": 0,
        "scan_hits_by_class": {},
        "warning_kinds": {},
    }
    payload.update(overrides)
    return payload


def _emit_sweep(event_log: SQLiteEventLog, **overrides: Any) -> None:
    event_log.emit(
        EventType.CAPTURE_SWEEP_COMPLETED,
        "worker:session-capture",
        entity_id="capture:claude-code",
        entity_type="capture_sweep",
        payload=_sweep_payload(**overrides),
    )


def _emit_memory_stored(
    event_log: SQLiteEventLog,
    session_id: str,
    *,
    source: str = "worker:session-capture",
) -> None:
    event_log.emit(
        EventType.MEMORY_STORED,
        source,
        entity_id=f"capture:claude-code:{session_id}",
        entity_type="document",
        payload={"doc_id": "d", "metadata": {"session_id": session_id}},
    )


class TestStatesAreDistinguished:
    """not-implemented / not-deployed / deployed-but-disabled are not one number.

    The acceptance criterion this module exists for: a coverage metric that
    reads 0.0 in all three cases sends the operator to debug a pipeline that
    was never deployed.
    """

    def test_no_sweep_ever_is_unobserved_not_zero(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "unobserved"
        assert report.capture_rate is None  # NOT 0.0
        assert report.suppressed_reason == SUPPRESSED_UNOBSERVED
        assert any("absence of measurement" in note for note in report.notes)

    def test_sweeps_outside_the_window_are_stale_not_zero(
        self, tmp_path: Path
    ) -> None:
        """The pipeline ran and stopped — a different fix from 'it never ran'."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        old = datetime.now(tz=UTC) - timedelta(days=40)
        event_log.append(
            Event(
                event_type=EventType.CAPTURE_SWEEP_COMPLETED,
                source="worker:session-capture",
                occurred_at=old,
                recorded_at=old,
                payload=_sweep_payload(),
            )
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "stale"
        assert report.capture_rate is None
        assert report.suppressed_reason == SUPPRESSED_STALE
        assert report.last_sweep_at is not None

    def test_sweeps_running_but_adjudicating_nothing_is_degraded(
        self, tmp_path: Path
    ) -> None:
        """The #255 shape: exits zero, captures nothing, reports success."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_parsed=12,
            sessions_triggered=0,
            sessions_judge_unavailable=12,
            sessions_with_memory=0,
            memories_written=0,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "degraded"
        assert report.capture_rate is None
        assert report.suppressed_reason == SUPPRESSED_DEGRADED
        assert "judge was unreachable" in report.degraded_reason

    def test_healthy_sweeps_are_measured_with_a_rate(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(event_log, sessions_triggered=20, sessions_with_memory=10)
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "measured"
        assert report.capture_rate == pytest.approx(0.5)
        assert report.eligible_sessions == 20


class TestDegradedReasonNamesTheStage:
    """'Adjudicated nothing' has several causes that call for opposite fixes."""

    def test_empty_parses_are_flagged_as_a_reader_regression(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_parsed=30,
            sessions_skipped_empty=30,
            sessions_triggered=0,
            sessions_with_memory=0,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert "#332" in report.degraded_reason
        assert "not a sampling decision" in report.degraded_reason

    def test_sampled_out_is_named_as_a_knob_not_a_defect(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_parsed=30,
            sessions_sampled_out=30,
            sessions_triggered=0,
            sessions_with_memory=0,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert "SAMPLE_DENOMINATOR" in report.degraded_reason

    def test_all_watermark_skipped_names_the_skip_counts(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_seen=100,
            sessions_skipped_watermark=90,
            sessions_skipped_ephemeral=10,
            sessions_parsed=0,
            sessions_triggered=0,
            sessions_with_memory=0,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert "watermark-skipped" in report.degraded_reason
        assert "ephemeral" in report.degraded_reason


class TestDenominator:
    def test_denominator_is_triggered_not_seen(self, tmp_path: Path) -> None:
        """``sessions_seen`` is dominated by watermark skips of old work.

        100 seen / 10 with a memory would read 10%; the sessions that got a
        chance were the 20 triggered ones, so the rate is 50%.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_seen=100,
            sessions_skipped_watermark=80,
            sessions_parsed=20,
            sessions_triggered=20,
            sessions_with_memory=10,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.eligible_sessions == 20
        assert report.capture_rate == pytest.approx(0.5)

    def test_sampling_a_knob_does_not_move_the_rate(self, tmp_path: Path) -> None:
        """Sampled-out sessions are outside the denominator on purpose.

        Were they inside it, coverage would be capped at
        ``1/sample_denominator`` and would move whenever the operator turned
        a cost knob — a metric measuring a config value, not health.
        """
        log_a = SQLiteEventLog(tmp_path / "a.db")
        _emit_sweep(
            log_a,
            sessions_parsed=20,
            sessions_sampled_out=0,
            sessions_triggered=20,
            sessions_with_memory=10,
        )
        log_b = SQLiteEventLog(tmp_path / "b.db")
        _emit_sweep(
            log_b,
            sessions_parsed=100,
            sessions_sampled_out=80,
            sessions_triggered=20,
            sessions_with_memory=10,
        )
        assert (
            summarize_capture_coverage(log_a, days=7).capture_rate
            == summarize_capture_coverage(log_b, days=7).capture_rate
        )

    def test_thin_sample_suppresses_the_rate(self, tmp_path: Path) -> None:
        """One session producing nothing is not a 0%-coverage system."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_parsed=1,
            sessions_triggered=1,
            sessions_with_memory=0,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "measured"
        assert report.capture_rate is None
        assert report.suppressed_reason == SUPPRESSED_THIN_SAMPLE
        assert report.eligible_sessions == 1  # the count is still reported

    def test_rate_appears_at_the_minimum(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(
            event_log,
            sessions_parsed=MIN_ELIGIBLE_SESSIONS,
            sessions_triggered=MIN_ELIGIBLE_SESSIONS,
            sessions_with_memory=MIN_ELIGIBLE_SESSIONS,
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.capture_rate == pytest.approx(1.0)
        assert report.suppressed_reason == ""


class TestDryRunsExcluded:
    def test_dry_run_sweeps_do_not_drag_coverage_down(self, tmp_path: Path) -> None:
        """A dry run writes nothing, so its numerator is structurally zero."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(event_log, sessions_triggered=20, sessions_with_memory=10)
        for _ in range(3):
            _emit_sweep(
                event_log,
                dry_run=True,
                sessions_triggered=20,
                sessions_with_memory=0,
            )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.dry_run_sweeps_excluded == 3
        assert report.funnel.sweeps == 1
        assert report.capture_rate == pytest.approx(0.5)


class TestFunnelAggregation:
    def test_sums_across_sweeps(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for _ in range(3):
            _emit_sweep(event_log, sessions_triggered=7, sessions_with_memory=3)
        report = summarize_capture_coverage(event_log, days=7)
        assert report.funnel.sweeps == 3
        assert report.eligible_sessions == 21
        assert report.sessions_with_memory == 9
        assert report.capture_rate == pytest.approx(9 / 21, rel=1e-3)

    def test_missing_payload_fields_do_not_crash(self, tmp_path: Path) -> None:
        """A sweep emitted by an older build carries fewer keys."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.CAPTURE_SWEEP_COMPLETED,
            "worker:session-capture",
            payload={"sessions_triggered": 8, "sessions_with_memory": 4},
        )
        report = summarize_capture_coverage(event_log, days=7)
        assert report.capture_rate == pytest.approx(0.5)
        assert report.funnel.sessions_seen == 0

    def test_booleans_are_not_summed_as_integers(self, tmp_path: Path) -> None:
        """``bool`` is an ``int`` subclass — a real footgun for this loop."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_sweep(event_log, sessions_triggered=8, sessions_with_memory=4)
        report = summarize_capture_coverage(event_log, days=7)
        # ``reconcile_enabled`` / ``dry_run`` are bools in the payload and
        # share no name with a funnel field, but the guard is what stops a
        # future bool-named-like-a-counter from silently incrementing one.
        assert report.funnel.sessions_with_memory == 4


class TestStoredMemoryCrossCheck:
    def test_counts_distinct_capture_sessions(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_memory_stored(event_log, "s1")
        _emit_memory_stored(event_log, "s1")  # same session, two memories
        _emit_memory_stored(event_log, "s2")
        report = summarize_capture_coverage(event_log, days=7)
        assert report.sessions_with_stored_memory == 2

    def test_ignores_other_sources(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_memory_stored(event_log, "s1", source="cli:ingest-corpus")
        report = summarize_capture_coverage(event_log, days=7)
        assert report.sessions_with_stored_memory == 0

    def test_unobserved_still_reports_that_capture_is_writing(
        self, tmp_path: Path
    ) -> None:
        """A build too old to emit the funnel still writes memories.

        The honest reading is "capture runs, its funnel is unreported" — not
        "capture is broken", and not a coverage of zero.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _emit_memory_stored(event_log, "s1")
        report = summarize_capture_coverage(event_log, days=7)
        assert report.state == "unobserved"
        assert report.sessions_with_stored_memory == 1
        assert any("only its funnel is unreported" in n for n in report.notes)


class TestFailsSoft:
    def test_empty_log_never_raises(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        assert summarize_capture_coverage(event_log, days=7).state == "unobserved"

    def test_non_dict_metadata_is_skipped(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.MEMORY_STORED,
            "worker:session-capture",
            payload={"metadata": "not-a-dict"},
        )
        assert summarize_capture_coverage(event_log, days=7).sessions_with_stored_memory == 0

    def test_mock_event_log_with_no_events(self) -> None:
        event_log = MagicMock()
        event_log.get_events.return_value = []
        report = summarize_capture_coverage(event_log, days=7)
        assert isinstance(report, CaptureCoverageReport)
        assert report.capture_rate is None


class TestTheMetricIsNotAConstant:
    """Proof the metric can move. Delete these last.

    A measurement that cannot be shown to take more than one value is
    indistinguishable from a literal, and this repo has shipped four of those
    (a reference rate pinned at 1.00, a noise filter that excluded nothing, a
    hard-coded feedback rating, ``confidence`` baked into a prompt exemplar).
    Each assertion below fails if its dimension collapses to a constant.
    """

    def test_capture_rate_takes_many_distinct_values(self, tmp_path: Path) -> None:
        observed = set()
        for i, (triggered, with_memory) in enumerate(
            [(10, 0), (10, 3), (10, 5), (10, 7), (10, 10), (8, 1)]
        ):
            event_log = SQLiteEventLog(tmp_path / f"rate{i}.db")
            _emit_sweep(
                event_log,
                sessions_triggered=triggered,
                sessions_with_memory=with_memory,
            )
            observed.add(summarize_capture_coverage(event_log, days=7).capture_rate)
        assert len(observed) == 6, f"rate collapsed to {observed}"
        assert 0.0 in observed and 1.0 in observed

    def test_every_state_is_reachable(self, tmp_path: Path) -> None:
        """All four states, or the three-way distinction is decorative."""
        states = set()

        empty = SQLiteEventLog(tmp_path / "s_none.db")
        states.add(summarize_capture_coverage(empty, days=7).state)

        stale = SQLiteEventLog(tmp_path / "s_stale.db")
        old = datetime.now(tz=UTC) - timedelta(days=40)
        stale.append(
            Event(
                event_type=EventType.CAPTURE_SWEEP_COMPLETED,
                source="worker:session-capture",
                occurred_at=old,
                recorded_at=old,
                payload=_sweep_payload(),
            )
        )
        states.add(summarize_capture_coverage(stale, days=7).state)

        degraded = SQLiteEventLog(tmp_path / "s_degraded.db")
        _emit_sweep(degraded, sessions_triggered=0, sessions_with_memory=0)
        states.add(summarize_capture_coverage(degraded, days=7).state)

        measured = SQLiteEventLog(tmp_path / "s_measured.db")
        _emit_sweep(measured, sessions_triggered=10, sessions_with_memory=5)
        states.add(summarize_capture_coverage(measured, days=7).state)

        assert states == {"unobserved", "stale", "degraded", "measured"}

    def test_suppressed_reason_takes_every_slug(self, tmp_path: Path) -> None:
        reasons = set()

        empty = SQLiteEventLog(tmp_path / "r_none.db")
        reasons.add(summarize_capture_coverage(empty, days=7).suppressed_reason)

        stale = SQLiteEventLog(tmp_path / "r_stale.db")
        old = datetime.now(tz=UTC) - timedelta(days=40)
        stale.append(
            Event(
                event_type=EventType.CAPTURE_SWEEP_COMPLETED,
                source="worker:session-capture",
                occurred_at=old,
                recorded_at=old,
                payload=_sweep_payload(),
            )
        )
        reasons.add(summarize_capture_coverage(stale, days=7).suppressed_reason)

        degraded = SQLiteEventLog(tmp_path / "r_degraded.db")
        _emit_sweep(degraded, sessions_triggered=0, sessions_with_memory=0)
        reasons.add(summarize_capture_coverage(degraded, days=7).suppressed_reason)

        thin = SQLiteEventLog(tmp_path / "r_thin.db")
        _emit_sweep(thin, sessions_triggered=2, sessions_with_memory=1)
        reasons.add(summarize_capture_coverage(thin, days=7).suppressed_reason)

        healthy = SQLiteEventLog(tmp_path / "r_ok.db")
        _emit_sweep(healthy, sessions_triggered=10, sessions_with_memory=5)
        reasons.add(summarize_capture_coverage(healthy, days=7).suppressed_reason)

        assert reasons == {
            SUPPRESSED_UNOBSERVED,
            SUPPRESSED_STALE,
            SUPPRESSED_DEGRADED,
            SUPPRESSED_THIN_SAMPLE,
            "",
        }

    def test_the_332_regression_is_visible_in_the_funnel(
        self, tmp_path: Path
    ) -> None:
        """The regression this metric exists to catch, before and after.

        #332: ``resolve_thread`` dropped every turn of a pure-sidechain
        transcript, so 61% of the corpus parsed to zero turns. Those sessions
        failed ``should_distill`` and were counted as ``sessions_sampled_out``
        — a reader regression wearing a sampling decision's clothes. Split
        out, the same corpus reads unmistakably.
        """
        healthy = SQLiteEventLog(tmp_path / "before.db")
        _emit_sweep(
            healthy,
            sessions_parsed=100,
            sessions_skipped_empty=0,
            sessions_sampled_out=20,
            sessions_triggered=80,
            sessions_with_memory=40,
        )
        broken = SQLiteEventLog(tmp_path / "after.db")
        _emit_sweep(
            broken,
            sessions_parsed=100,
            sessions_skipped_empty=61,
            sessions_sampled_out=8,
            sessions_triggered=31,
            sessions_with_memory=15,
        )

        before = summarize_capture_coverage(healthy, days=7)
        after = summarize_capture_coverage(broken, days=7)

        # The rate barely moves — 0.50 vs 0.48 — which is exactly why a rate
        # alone could not have caught this. The funnel is what shows it.
        assert before.capture_rate == pytest.approx(0.5)
        assert after.capture_rate == pytest.approx(15 / 31, rel=1e-3)
        assert before.funnel.sessions_skipped_empty == 0
        assert after.funnel.sessions_skipped_empty == 61
        assert any("#332" in note for note in after.notes)
        assert not any("#332" in note for note in before.notes)
