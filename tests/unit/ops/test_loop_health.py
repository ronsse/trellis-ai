"""Tests for `trellis.ops.loop_health.summarize_loop_health` (e167).

The one claim the brief makes load-bearing: a loop with no event yet
reads "never run", never a zero that looks identical to "ran and found
nothing to do".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trellis.ops.loop_health import summarize_loop_health
from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore


def _stores(tmp_path: Path) -> tuple[SQLiteEventLog, SQLiteTunerStateStore]:
    return (
        SQLiteEventLog(tmp_path / "events.db"),
        SQLiteTunerStateStore(tmp_path / "tuner.db"),
    )


def _row(report, name: str):
    matches = [r for r in report.loops if r.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} row"
    return matches[0]


class TestNeverRunVsZero:
    def test_empty_event_log_reports_never_run_for_every_row(
        self, tmp_path: Path
    ) -> None:
        event_log, tuner_state = _stores(tmp_path)

        report = summarize_loop_health(event_log, tuner_state)

        for row in report.loops:
            if row.name == "tuner":
                # Documented exception: pending_count/promoted_total are
                # live reads, not event-derived, so they stay populated
                # even when the automated tuner pass has never run.
                assert row.last_run_at is None
                assert row.last_status is None
                assert row.counters == {"pending_count": 0, "promoted_total": 0}
                continue
            assert row.last_run_at is None, row.name
            assert row.last_status is None, row.name
            assert row.counters is None, row.name

    def test_a_cycle_that_tags_nothing_is_not_confused_with_never_run(
        self, tmp_path: Path
    ) -> None:
        """A real cycle with real zero counters must read differently from
        no cycle at all — same zero int, different row shape."""
        event_log, tuner_state = _stores(tmp_path)
        event_log.emit(
            EventType.CURATE_CYCLE_COMPLETED,
            source="trellis.worker.curate",
            payload={
                "status": "ok",
                "noise_tagged": 0,
                "noise_refused_non_document": 0,
                "advisories_generated": 0,
                "advisories_refused": 0,
                "advisories_suppressed": 0,
                "advisories_boosted": 0,
                "advisory_store_degraded": False,
                "advisory_store_stale": False,
                "learning_observations": 0,
                "learning_candidates": 0,
                "learning_promotion_ready": {"count": 0},
                "skipped_stages": [],
                "dry_run": False,
            },
        )

        report = summarize_loop_health(event_log, tuner_state)

        noise_row = _row(report, "noise_demotion")
        assert noise_row.last_run_at is not None
        assert noise_row.last_status == "ok"
        assert noise_row.counters == {
            "noise_tagged": 0,
            "noise_refused_non_document": 0,
        }


class TestCurateDerivedRows:
    def test_skipped_stage_reports_skipped_status(self, tmp_path: Path) -> None:
        event_log, tuner_state = _stores(tmp_path)
        event_log.emit(
            EventType.CURATE_CYCLE_COMPLETED,
            source="trellis.worker.curate",
            payload={
                "status": "ok",
                "noise_tagged": 3,
                "advisories_generated": 0,
                "advisories_refused": 0,
                "advisories_suppressed": 0,
                "advisories_boosted": 0,
                "advisory_store_degraded": False,
                "advisory_store_stale": False,
                "learning_observations": 5,
                "learning_candidates": 1,
                "learning_promotion_ready": {"count": 1},
                "skipped_stages": ["learning"],
                "dry_run": False,
            },
        )

        report = summarize_loop_health(event_log, tuner_state)

        assert _row(report, "learning_candidate_scoring").last_status == "skipped"
        assert _row(report, "noise_demotion").last_status == "ok"

    def test_degraded_advisory_store_propagates_to_both_advisory_rows(
        self, tmp_path: Path
    ) -> None:
        event_log, tuner_state = _stores(tmp_path)
        event_log.emit(
            EventType.CURATE_CYCLE_COMPLETED,
            source="trellis.worker.curate",
            payload={
                "status": "degraded",
                "noise_tagged": 0,
                "advisories_generated": 0,
                "advisories_refused": 0,
                "advisories_suppressed": 0,
                "advisories_boosted": 0,
                "advisory_store_degraded": True,
                "advisory_store_stale": False,
                "learning_observations": 0,
                "learning_candidates": 0,
                "learning_promotion_ready": {"count": 0},
                "skipped_stages": [],
                "dry_run": False,
            },
        )

        report = summarize_loop_health(event_log, tuner_state)

        assert _row(report, "advisory_generation").last_status == "degraded"
        assert _row(report, "advisory_fitness").last_status == "degraded"
        # Noise demotion does not read the advisory store, so it must not
        # inherit a status derived from it.
        assert _row(report, "noise_demotion").last_status == "ok"


class TestPrecedentPromotionWindows:
    def test_counts_only_fall_inside_their_window(self, tmp_path: Path) -> None:
        event_log, tuner_state = _stores(tmp_path)
        now = datetime.now(UTC)
        old = now - timedelta(days=40)
        recent = now - timedelta(days=3)
        for occurred_at in (old, recent, recent):
            event_log.append(
                Event(
                    event_type=EventType.PRECEDENT_PROMOTED,
                    source="trellis.learning",
                    entity_id="cand-1",
                    occurred_at=occurred_at,
                    payload={},
                )
            )

        report = summarize_loop_health(event_log, tuner_state, now=now)

        row = _row(report, "precedent_promotion")
        assert row.counters == {"promoted_last_7d": 2, "promoted_last_30d": 2}


class TestTunerRow:
    def test_pending_count_is_a_live_store_read_not_an_event_count(
        self, tmp_path: Path
    ) -> None:
        event_log, tuner_state = _stores(tmp_path)
        tuner_state.put_proposal(
            ParameterProposal(
                proposal_id="p-1",
                scope=ParameterScope(component_id="retrieval"),
                proposed_values={"k": 1},
                tuner="rule_tuner",
                sample_size=10,
                status="pending",
            )
        )

        report = summarize_loop_health(event_log, tuner_state)

        row = _row(report, "tuner")
        assert row.counters["pending_count"] == 1
        # No TUNE_CYCLE_COMPLETED event exists, so the run-level fields
        # stay "never run" even though live state is non-empty.
        assert row.last_run_at is None
        assert row.last_status is None

    def test_tune_cycle_event_populates_last_run_fields(self, tmp_path: Path) -> None:
        event_log, tuner_state = _stores(tmp_path)
        event_log.emit(
            EventType.TUNE_CYCLE_COMPLETED,
            source="trellis.worker.tune",
            payload={
                "tuner_name": "rule_tuner",
                "enabled": True,
                "dry_run": False,
                "proposals_considered": 4,
                "auto_promoted": 1,
                "rolled_back": 0,
                "pending_manual": 3,
                "outcomes": [],
            },
        )

        report = summarize_loop_health(event_log, tuner_state)

        row = _row(report, "tuner")
        assert row.last_run_at is not None
        assert row.last_status == "ok"
        assert row.counters["last_run_proposals_considered"] == 4
        assert row.counters["last_run_auto_promoted"] == 1


class TestFeedbackIntakeWindows:
    def test_counts_only_fall_inside_their_window(self, tmp_path: Path) -> None:
        event_log, tuner_state = _stores(tmp_path)
        now = datetime.now(UTC)
        old = now - timedelta(days=40)
        recent = now - timedelta(days=3)
        for occurred_at in (old, recent, recent):
            event_log.append(
                Event(
                    event_type=EventType.FEEDBACK_RECORDED,
                    source="mcp:record_feedback",
                    occurred_at=occurred_at,
                    payload={},
                )
            )

        report = summarize_loop_health(event_log, tuner_state, now=now)

        row = _row(report, "feedback_intake")
        assert row.counters == {"feedback_last_7d": 2, "feedback_last_30d": 2}


class TestRowMetadata:
    def test_every_row_declares_actuates_and_what_it_changes(
        self, tmp_path: Path
    ) -> None:
        event_log, tuner_state = _stores(tmp_path)

        report = summarize_loop_health(event_log, tuner_state)

        assert len(report.loops) == 7
        names = {row.name for row in report.loops}
        assert names == {
            "noise_demotion",
            "advisory_generation",
            "advisory_fitness",
            "learning_candidate_scoring",
            "precedent_promotion",
            "tuner",
            "feedback_intake",
        }
        for row in report.loops:
            assert isinstance(row.actuates, bool)
            assert row.what_it_changes
            assert row.description
