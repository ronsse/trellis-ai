"""Tests for the feedback module."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.feedback import (
    PackFeedback,
    compute_item_effectiveness,
    load_feedback_log,
    reconcile_feedback_log_to_event_log,
    record_feedback,
)

# ---------------------------------------------------------------------------
# PackFeedback construction
# ---------------------------------------------------------------------------


class TestPackFeedbackConstruction:
    def test_required_fields_only(self):
        fb = PackFeedback(
            run_id="run-1",
            phase="GENERATE_ASSETS",
            intent="generate_sql",
            outcome="success",
            items_served=["item-a", "item-b"],
        )
        assert fb.run_id == "run-1"
        assert fb.phase == "GENERATE_ASSETS"
        assert fb.intent == "generate_sql"
        assert fb.outcome == "success"
        assert fb.items_served == ["item-a", "item-b"]

    def test_defaults(self):
        fb = PackFeedback(
            run_id="run-1",
            phase="PLAN",
            intent="plan_layers",
            outcome="failure",
            items_served=[],
        )
        assert fb.items_referenced == []
        assert fb.relevance_scores == {}
        assert fb.intent_family == ""
        assert fb.agent_id is None
        assert fb.metadata == {}
        assert fb.timestamp_utc != ""  # auto-populated

    def test_all_fields(self):
        fb = PackFeedback(
            run_id="run-42",
            phase="VALIDATE",
            intent="validate_sql",
            outcome="partial",
            items_served=["x", "y"],
            items_referenced=["x"],
            relevance_scores={"x": 0.9, "y": 0.1},
            intent_family="data_quality",
            timestamp_utc="2026-04-13T00:00:00+00:00",
            agent_id="agent-007",
            metadata={"extra": "value"},
        )
        assert fb.agent_id == "agent-007"
        assert fb.relevance_scores == {"x": 0.9, "y": 0.1}
        assert fb.metadata == {"extra": "value"}

    def test_outcome_values(self):
        for outcome in ("success", "failure", "partial", "unknown"):
            fb = PackFeedback(
                run_id="r",
                phase="p",
                intent="i",
                outcome=outcome,
                items_served=[],
            )
            assert fb.outcome == outcome


class TestPackFeedbackFrozen:
    def test_is_frozen(self):
        fb = PackFeedback(
            run_id="run-1",
            phase="p",
            intent="i",
            outcome="success",
            items_served=["a"],
        )
        with pytest.raises((AttributeError, TypeError)):
            fb.run_id = "mutated"  # type: ignore[misc]

    def test_is_hashable(self):
        # Frozen dataclasses with list/dict fields are not hashable by default,
        # but we confirm equality works when timestamps and feedback_ids are pinned.
        ts = "2026-04-13T00:00:00+00:00"
        fid = "fb_01KABCDEF0000000000000000"
        fb = PackFeedback(
            run_id="run-1",
            phase="p",
            intent="i",
            outcome="success",
            items_served=["a"],
            timestamp_utc=ts,
            feedback_id=fid,
        )
        fb2 = PackFeedback(
            run_id="run-1",
            phase="p",
            intent="i",
            outcome="success",
            items_served=["a"],
            timestamp_utc=ts,
            feedback_id=fid,
        )
        assert fb == fb2


# ---------------------------------------------------------------------------
# JSONL roundtrip
# ---------------------------------------------------------------------------


class TestJsonlRoundtrip:
    def _make_fb(self, run_id: str = "run-1", **kwargs) -> PackFeedback:
        return PackFeedback(
            run_id=run_id,
            phase="GENERATE_ASSETS",
            intent="generate_sql",
            outcome="success",
            items_served=["item-a", "item-b"],
            **kwargs,
        )

    def test_record_creates_file(self, tmp_path: Path):
        fb = self._make_fb()
        result = record_feedback(fb, log_dir=tmp_path)
        assert result.log_path.exists()
        assert result.log_path.name == "pack_feedback.jsonl"
        assert result.feedback_id == fb.feedback_id

    def test_record_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "c"
        fb = self._make_fb()
        result = record_feedback(fb, log_dir=nested)
        assert result.log_path.exists()

    def test_single_roundtrip(self, tmp_path: Path):
        fb = self._make_fb(
            items_referenced=["item-a"],
            relevance_scores={"item-a": 0.8},
            intent_family="generation",
            agent_id="agent-1",
            metadata={"model": "claude-3"},
        )
        record_feedback(fb, log_dir=tmp_path)
        loaded = load_feedback_log(tmp_path)

        assert len(loaded) == 1
        result = loaded[0]
        assert result.run_id == fb.run_id
        assert result.phase == fb.phase
        assert result.intent == fb.intent
        assert result.outcome == fb.outcome
        assert result.items_served == fb.items_served
        assert result.items_referenced == fb.items_referenced
        assert result.relevance_scores == fb.relevance_scores
        assert result.intent_family == fb.intent_family
        assert result.agent_id == fb.agent_id
        assert result.metadata == fb.metadata

    def test_multiple_records_preserved_order(self, tmp_path: Path):
        for i in range(5):
            fb = self._make_fb(run_id=f"run-{i}")
            record_feedback(fb, log_dir=tmp_path)

        loaded = load_feedback_log(tmp_path)
        assert len(loaded) == 5
        for i, fb in enumerate(loaded):
            assert fb.run_id == f"run-{i}"

    def test_load_missing_log_returns_empty(self, tmp_path: Path):
        result = load_feedback_log(tmp_path / "nonexistent")
        assert result == []

    def test_load_empty_lines_skipped(self, tmp_path: Path):
        log_path = tmp_path / "pack_feedback.jsonl"
        log_path.write_text("\n\n\n")
        result = load_feedback_log(tmp_path)
        assert result == []

    def test_appends_not_overwrites(self, tmp_path: Path):
        fb1 = self._make_fb(run_id="run-1")
        fb2 = self._make_fb(run_id="run-2")
        record_feedback(fb1, log_dir=tmp_path)
        record_feedback(fb2, log_dir=tmp_path)
        loaded = load_feedback_log(tmp_path)
        assert len(loaded) == 2
        assert loaded[0].run_id == "run-1"
        assert loaded[1].run_id == "run-2"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestComputeItemEffectiveness:
    def _signal(
        self,
        items_served: list[str],
        outcome: str = "success",
        items_referenced: list[str] | None = None,
        intent_family: str = "",
    ) -> PackFeedback:
        return PackFeedback(
            run_id="run-x",
            phase="p",
            intent="i",
            outcome=outcome,
            items_served=items_served,
            items_referenced=items_referenced or [],
            intent_family=intent_family,
        )

    def test_empty_signals(self):
        result = compute_item_effectiveness([])
        assert result == {}

    def test_single_success(self):
        signals = [self._signal(["item-a", "item-b"], outcome="success")]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["times_served"] == 1
        assert result["item-a"]["success_rate"] == 1.0
        assert result["item-b"]["times_served"] == 1

    def test_single_failure(self):
        signals = [self._signal(["item-a"], outcome="failure")]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["success_rate"] == 0.0

    def test_completed_counts_as_success(self):
        signals = [self._signal(["item-a"], outcome="completed")]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["success_rate"] == 1.0

    def test_partial_does_not_count_as_success(self):
        signals = [self._signal(["item-a"], outcome="partial")]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["success_rate"] == 0.0

    def test_reference_rate(self):
        signals = [
            self._signal(["item-a", "item-b"], items_referenced=["item-a"]),
            self._signal(["item-a", "item-b"], items_referenced=[]),
        ]
        result = compute_item_effectiveness(signals)
        # item-a: referenced once out of 2 deliveries
        assert result["item-a"]["reference_rate"] == 0.5
        assert result["item-a"]["times_referenced"] == 1
        # item-b: never referenced
        assert result["item-b"]["reference_rate"] == 0.0

    def test_intent_families_collected(self):
        signals = [
            self._signal(["item-a"], intent_family="generation"),
            self._signal(["item-a"], intent_family="validation"),
            self._signal(["item-a"], intent_family="generation"),
        ]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["intent_families"] == ["generation", "validation"]

    def test_intent_families_sorted(self):
        signals = [
            self._signal(["item-a"], intent_family="z_family"),
            self._signal(["item-a"], intent_family="a_family"),
        ]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["intent_families"] == ["a_family", "z_family"]

    def test_blank_intent_family_excluded(self):
        signals = [
            self._signal(["item-a"], intent_family=""),
            self._signal(["item-a"], intent_family="  "),
        ]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["intent_families"] == []

    def test_mixed_outcomes_success_rate(self):
        signals = [
            self._signal(["item-a"], outcome="success"),
            self._signal(["item-a"], outcome="success"),
            self._signal(["item-a"], outcome="failure"),
        ]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["times_served"] == 3
        assert result["item-a"]["success_rate"] == pytest.approx(2 / 3)

    def test_multiple_items_independent(self):
        signals = [
            self._signal(["item-a"], outcome="success"),
            self._signal(["item-b"], outcome="failure"),
        ]
        result = compute_item_effectiveness(signals)
        assert result["item-a"]["success_rate"] == 1.0
        assert result["item-b"]["success_rate"] == 0.0

    def test_result_contains_expected_keys(self):
        signals = [self._signal(["item-a"])]
        result = compute_item_effectiveness(signals)
        keys = set(result["item-a"].keys())
        assert {
            "times_served",
            "times_referenced",
            "success_count",
            "success_rate",
            "reference_rate",
            "intent_families",
        } <= keys


# ---------------------------------------------------------------------------
# Event-log bridge (PackFeedback.to_event_payload + record_feedback event_log)
# ---------------------------------------------------------------------------


class TestToEventPayload:
    def test_core_fields(self):
        fb = PackFeedback(
            run_id="run-9",
            phase="GENERATE",
            intent="generate_sql",
            outcome="success",
            items_served=["a", "b", "c"],
            items_referenced=["a", "c"],
            intent_family="asset_generation",
            relevance_scores={"a": 0.9, "b": 0.3, "c": 0.7},
        )
        payload = fb.to_event_payload()
        assert payload["run_id"] == "run-9"
        assert payload["phase"] == "GENERATE"
        assert payload["intent"] == "generate_sql"
        assert payload["intent_family"] == "asset_generation"
        assert payload["outcome"] == "success"
        assert payload["success"] is True
        assert payload["items_served"] == ["a", "b", "c"]
        assert payload["helpful_item_ids"] == ["a", "c"]
        assert payload["relevance_scores"] == {"a": 0.9, "b": 0.3, "c": 0.7}

    def test_non_success_outcomes_map_to_false(self):
        for outcome in ("failure", "partial", "unknown"):
            fb = PackFeedback(
                run_id="r",
                phase="p",
                intent="i",
                outcome=outcome,
                items_served=[],
            )
            payload = fb.to_event_payload()
            assert payload["success"] is False, outcome

    def test_completed_maps_to_success(self):
        fb = PackFeedback(
            run_id="r",
            phase="p",
            intent="i",
            outcome="completed",
            items_served=[],
        )
        assert fb.to_event_payload()["success"] is True

    def test_pack_id_included_when_provided(self):
        fb = PackFeedback(
            run_id="r",
            phase="p",
            intent="i",
            outcome="success",
            items_served=[],
        )
        assert "pack_id" not in fb.to_event_payload()
        assert fb.to_event_payload(pack_id="pack-123")["pack_id"] == "pack-123"

    def test_optional_fields_omitted_when_empty(self):
        fb = PackFeedback(
            run_id="r",
            phase="p",
            intent="i",
            outcome="success",
            items_served=[],
        )
        payload = fb.to_event_payload()
        assert "agent_id" not in payload
        assert "metadata" not in payload

    def test_agent_id_and_metadata_preserved(self):
        fb = PackFeedback(
            run_id="r",
            phase="p",
            intent="i",
            outcome="success",
            items_served=[],
            agent_id="agent-1",
            metadata={"custom": "v"},
        )
        payload = fb.to_event_payload()
        assert payload["agent_id"] == "agent-1"
        assert payload["metadata"] == {"custom": "v"}


class _CapturingEventLog:
    """Minimal EventLog stand-in for tests — records emit() calls.

    Also implements ``get_events`` so record_feedback's idempotency
    check (``_feedback_id_in_event_log``) can scan prior payloads.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(
        self,
        event_type,
        source,
        *,
        entity_id=None,
        entity_type=None,
        payload=None,
        metadata=None,
    ):
        self.events.append(
            {
                "event_type": event_type,
                "source": source,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "payload": payload or {},
                "metadata": metadata or {},
            }
        )

    def get_events(
        self,
        *,
        event_type=None,
        limit: int = 100,
        order: str = "asc",
        **_ignored,
    ):
        from types import SimpleNamespace

        matches = [
            e
            for e in self.events
            if event_type is None or e["event_type"] == event_type
        ]
        if order == "desc":
            matches.reverse()
        return [SimpleNamespace(payload=e["payload"]) for e in matches[:limit]]


class TestRecordFeedbackEventLogBridge:
    def _feedback(self) -> PackFeedback:
        return PackFeedback(
            run_id="run-b",
            phase="GEN",
            intent="generate",
            outcome="success",
            items_served=["x", "y"],
            items_referenced=["x"],
            intent_family="asset_generation",
        )

    def test_file_only_by_default(self, tmp_path: Path):
        """event_log not provided → only JSONL write happens (existing behavior)."""
        log_dir = tmp_path / "feedback"
        record_feedback(self._feedback(), log_dir=log_dir)
        assert (log_dir / "pack_feedback.jsonl").exists()

    def test_emits_when_event_log_provided(self, tmp_path: Path):
        from trellis.stores.base.event_log import EventType

        captured = _CapturingEventLog()
        record_feedback(
            self._feedback(),
            log_dir=tmp_path,
            event_log=captured,
            pack_id="pack-abc",
        )
        assert len(captured.events) == 1
        evt = captured.events[0]
        assert evt["event_type"] == EventType.FEEDBACK_RECORDED
        assert evt["source"] == "feedback.record"
        assert evt["entity_id"] == "pack-abc"
        assert evt["entity_type"] == "pack"
        assert evt["payload"]["pack_id"] == "pack-abc"
        assert evt["payload"]["success"] is True
        assert evt["payload"]["helpful_item_ids"] == ["x"]

    def test_emit_without_pack_id_sets_none_entity(self, tmp_path: Path):
        captured = _CapturingEventLog()
        record_feedback(self._feedback(), log_dir=tmp_path, event_log=captured)
        evt = captured.events[0]
        assert evt["entity_id"] is None
        assert evt["entity_type"] is None
        assert "pack_id" not in evt["payload"]

    def test_emit_failure_is_non_fatal(self, tmp_path: Path):
        """File write must succeed even when the event log explodes."""

        class BrokenEventLog:
            def emit(self, *args, **kwargs):
                msg = "eventlog down"
                raise RuntimeError(msg)

        result = record_feedback(
            self._feedback(),
            log_dir=tmp_path,
            event_log=BrokenEventLog(),
            pack_id="p",
        )
        assert result.log_path.exists()
        # JSONL file received the write despite the event-log failure
        assert result.log_path.read_text(encoding="utf-8").strip() != ""
        # Emission failure is now visible to the caller via the result —
        # the silent-divergence gap (2.3) is closed for in-process callers.
        assert result.event_log_emitted is False
        assert result.event_log_error is not None
        assert not result.event_log_in_sync


# ---------------------------------------------------------------------------
# Gap 2.3 — JSONL ↔ EventLog divergence / double-count / reconciliation
# ---------------------------------------------------------------------------


class TestFeedbackIdIdempotency:
    """feedback_id prevents double-counting on replay."""

    def _fb(self) -> PackFeedback:
        return PackFeedback(
            run_id="run-dup",
            phase="p",
            intent="i",
            outcome="success",
            items_served=["a"],
        )

    def test_feedback_id_is_auto_generated(self):
        fb = self._fb()
        assert fb.feedback_id.startswith("fb_")
        assert len(fb.feedback_id) > len("fb_")

    def test_feedback_id_in_event_payload(self):
        fb = self._fb()
        payload = fb.to_event_payload()
        assert payload["feedback_id"] == fb.feedback_id

    def test_same_feedback_recorded_twice_emits_once(self, tmp_path: Path):
        """Replay protection: if the exact same PackFeedback object is
        recorded again (same feedback_id), the second EventLog emit is
        skipped — duplicate-detection bridges the two sources."""
        captured = _CapturingEventLog()
        fb = self._fb()

        r1 = record_feedback(fb, log_dir=tmp_path, event_log=captured)
        r2 = record_feedback(fb, log_dir=tmp_path, event_log=captured)

        assert r1.event_log_emitted is True
        assert r1.event_log_skipped_as_duplicate is False
        assert r2.event_log_emitted is False
        assert r2.event_log_skipped_as_duplicate is True
        assert r2.event_log_in_sync  # still counts as in-sync
        assert len(captured.events) == 1
        # JSONL appends are not deduped — the file is the audit trail.
        loaded = load_feedback_log(tmp_path)
        assert len(loaded) == 2
        assert loaded[0].feedback_id == loaded[1].feedback_id

    def test_distinct_feedback_ids_both_emit(self, tmp_path: Path):
        captured = _CapturingEventLog()
        fb1 = self._fb()
        fb2 = self._fb()  # fresh feedback_id
        assert fb1.feedback_id != fb2.feedback_id

        record_feedback(fb1, log_dir=tmp_path, event_log=captured)
        record_feedback(fb2, log_dir=tmp_path, event_log=captured)

        assert len(captured.events) == 2


class TestFeedbackReconciliation:
    """reconcile_feedback_log_to_event_log backfills the EventLog from
    the JSONL file — closes divergence when an earlier emit failed or
    a file-only capture is being promoted."""

    def _fb(self, run_id: str = "r") -> PackFeedback:
        return PackFeedback(
            run_id=run_id,
            phase="p",
            intent="i",
            outcome="success",
            items_served=["a"],
        )

    def test_reconcile_emits_missing_entries(self, tmp_path: Path):
        # Phase 1: write JSONL without an event log (divergence).
        fb1 = self._fb("r1")
        fb2 = self._fb("r2")
        record_feedback(fb1, log_dir=tmp_path)
        record_feedback(fb2, log_dir=tmp_path)

        # Phase 2: reconcile into a fresh event log.
        captured = _CapturingEventLog()
        result = reconcile_feedback_log_to_event_log(tmp_path, captured)

        assert result.scanned == 2
        assert result.already_present == 0
        assert result.emitted == 2
        assert result.failed == 0
        emitted_ids = {e["payload"]["feedback_id"] for e in captured.events}
        assert emitted_ids == {fb1.feedback_id, fb2.feedback_id}

    def test_reconcile_is_idempotent(self, tmp_path: Path):
        """Running reconcile twice must not double-emit."""
        fb = self._fb()
        record_feedback(fb, log_dir=tmp_path)

        captured = _CapturingEventLog()
        first = reconcile_feedback_log_to_event_log(tmp_path, captured)
        second = reconcile_feedback_log_to_event_log(tmp_path, captured)

        assert first.emitted == 1
        assert second.emitted == 0
        assert second.already_present == 1
        assert len(captured.events) == 1

    def test_reconcile_skips_entries_already_in_event_log(self, tmp_path: Path):
        """If an entry was already emitted (e.g., live path succeeded),
        reconciliation doesn't re-emit."""
        captured = _CapturingEventLog()
        fb_live = self._fb("live")
        fb_only_file = self._fb("only_file")

        # First was emitted live. Second never was (imagine event_log was down).
        record_feedback(fb_live, log_dir=tmp_path, event_log=captured)
        record_feedback(fb_only_file, log_dir=tmp_path)  # no event_log

        result = reconcile_feedback_log_to_event_log(tmp_path, captured)

        assert result.scanned == 2
        assert result.already_present == 1
        assert result.emitted == 1
        assert len(captured.events) == 2

    def test_reconcile_tracks_failures(self, tmp_path: Path):
        fb = self._fb()
        record_feedback(fb, log_dir=tmp_path)

        class BrokenEventLog:
            def emit(self, *args, **kwargs):
                msg = "persistent outage"
                raise RuntimeError(msg)

            def get_events(self, **_ignored):
                return []

        result = reconcile_feedback_log_to_event_log(tmp_path, BrokenEventLog())  # type: ignore[arg-type]
        assert result.scanned == 1
        assert result.emitted == 0
        assert result.failed == 1
        assert result.missing_feedback_ids == [fb.feedback_id]

    def test_reconcile_empty_log_is_noop(self, tmp_path: Path):
        captured = _CapturingEventLog()
        result = reconcile_feedback_log_to_event_log(tmp_path, captured)
        assert result.scanned == 0
        assert result.emitted == 0
        assert result.already_present == 0
        assert len(captured.events) == 0

    def test_reconcile_applies_pack_id_lookup(self, tmp_path: Path):
        fb = self._fb()
        record_feedback(fb, log_dir=tmp_path)

        captured = _CapturingEventLog()
        reconcile_feedback_log_to_event_log(
            tmp_path,
            captured,
            pack_id_lookup={fb.feedback_id: "pack-xyz"},
        )

        assert captured.events[0]["entity_id"] == "pack-xyz"
        assert captured.events[0]["entity_type"] == "pack"
        assert captured.events[0]["payload"]["pack_id"] == "pack-xyz"


class TestLoadFeedbackLogBackwardCompat:
    """Pre-feedback_id JSONL rows get synthesized ids on load so they
    remain reconcilable (not matchable to an original event, but not a
    parse error either)."""

    def test_load_pre_feedback_id_rows(self, tmp_path: Path):
        import json

        log_path = tmp_path / "pack_feedback.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "run_id": "legacy",
                    "phase": "p",
                    "intent": "i",
                    "outcome": "success",
                    "items_served": ["a"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        loaded = load_feedback_log(tmp_path)
        assert len(loaded) == 1
        # Fresh ULID was minted since the file row had no feedback_id.
        assert loaded[0].feedback_id.startswith("fb_")
        assert loaded[0].run_id == "legacy"


class TestFeedbackIdScanLimitRegression:
    """Regression: ``_feedback_id_in_event_log`` must find recent feedback
    even when the EventLog has more than the scan limit's worth of older
    rows. Earlier the helper relied on the default ``ORDER BY ASC`` of
    ``get_events`` and a ``limit=10_000`` cap — on a busy log, the
    matching recent feedback fell off the back of the scan window and
    the helper falsely returned False, causing duplicate emissions and
    broken reconcile idempotency.
    """

    def test_recent_feedback_visible_past_scan_limit(self, tmp_path: Path):
        """Seed >10K older FEEDBACK_RECORDED events, then ask whether a
        feedback added *afterwards* is present. Under the bug this was
        False; under ``order='desc'`` it is True."""
        from trellis.feedback.recording import _feedback_id_in_event_log
        from trellis.stores.event_log import (
            Event,
            EventType,
            SQLiteEventLog,
        )

        event_log = SQLiteEventLog(tmp_path / "events.db")
        try:
            scan_limit = 10_000
            for i in range(scan_limit + 50):
                event_log.append(
                    Event(
                        event_type=EventType.FEEDBACK_RECORDED,
                        source="seed",
                        payload={"feedback_id": f"fb_seed_{i}"},
                    )
                )
            # The target feedback is the very last write — under ASC scan
            # ordering with a 10K limit it would never be reached.
            target_id = "fb_target_recent"
            event_log.append(
                Event(
                    event_type=EventType.FEEDBACK_RECORDED,
                    source="seed",
                    payload={"feedback_id": target_id},
                )
            )

            assert _feedback_id_in_event_log(event_log, target_id) is True
            # Sanity: a feedback_id that was never written stays False.
            assert _feedback_id_in_event_log(event_log, "fb_never_written") is False
        finally:
            event_log.close()
