"""C2 Phase 5 — telemetry-failure tests for `trellis.feedback.recording`.

Pins the 3 GRACEFUL-DEGRADATION sites and the 1 DEFECT fix in
``src/trellis/feedback/recording.py``:

* L178 — EventLog ``emit`` raises → JSONL still written; error captured
  on the result; ``feedback_event_emit_failed`` logged.
* L198 — OutcomeStore emit raises → JSONL still written; error captured
  on the result; ``feedback_outcome_emit_failed`` logged.
* L278 — reconcile loop emit raises → loop continues; failure id
  recorded on ``ReconcileResult``; ``feedback_reconcile_emit_failed``
  logged.
* L304 (DEFECT fix) — ``_parse_timestamp`` malformed input now logs
  ``feedback_timestamp_parse_failed`` instead of swallowing silently.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import (
    _parse_timestamp,
    reconcile_feedback_log_to_event_log,
    record_feedback,
)


@pytest.fixture
def log_output() -> Iterator[list[dict]]:
    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.BoundLogger,
        processors=saved.get("processors", []),
    )
    try:
        with capture_logs() as cap:
            yield cap
    finally:
        structlog.configure(**saved)


def _events_with_key(cap: list[dict], event_key: str) -> list[dict]:
    return [e for e in cap if e.get("event") == event_key]


def _feedback() -> PackFeedback:
    return PackFeedback(
        run_id="run-phase5",
        phase="GEN",
        intent="generate",
        outcome="success",
        items_served=["a"],
    )


class _BrokenEventLog:
    """EventLog whose ``emit`` always raises. ``get_events`` returns []
    so the duplicate-check path is no-op."""

    def emit(self, *_args, **_kwargs):
        msg = "eventlog down"
        raise RuntimeError(msg)

    def get_events(self, **_kwargs):
        return []


class _BrokenReconcileEventLog(_BrokenEventLog):
    """Same as `_BrokenEventLog` but used in the reconcile path."""


class _ScriptedEventLog:
    """First call records, subsequent calls raise — exercises mid-loop
    failure in reconcile."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._calls = 0

    def emit(self, event_type, source, **kwargs):
        self._calls += 1
        if self._calls > 1:
            msg = "second emit fails"
            raise RuntimeError(msg)
        self.events.append({"event_type": event_type, "source": source, **kwargs})

    def get_events(self, **_kwargs):
        return []


class TestEventLogEmitFailureGraceful:
    """L178 — primary op (JSONL append) succeeds even when EventLog blows."""

    def test_jsonl_persisted_and_error_surfaced(
        self,
        tmp_path: Path,
        log_output: list[dict],
    ) -> None:
        broken = _BrokenEventLog()
        result = record_feedback(
            _feedback(),
            log_dir=tmp_path,
            event_log=broken,
            pack_id="p1",
        )

        # Primary op: JSONL file written.
        assert result.log_path.exists()
        assert result.log_path.read_text(encoding="utf-8").strip() != ""

        # Error captured on the result struct (rubric: caller has signal).
        assert result.event_log_emitted is False
        assert isinstance(result.event_log_error, RuntimeError)

        # structlog received the failure (rubric (b)).
        events = _events_with_key(log_output, "feedback_event_emit_failed")
        assert events, log_output
        assert events[0].get("log_level") == "error"


class TestOutcomeEmitFailureGraceful:
    """L198 — outcome emit failure must not break JSONL write.

    ``record_outcome`` itself catches Store errors internally; this test
    pins the *outer* L198 except by forcing ``_emit_outcome`` (the bridge
    helper) to raise — exercising the contract that any failure in the
    outcome bridge is converted to an ``outcome_error`` on the result.
    """

    def test_jsonl_persisted_and_error_surfaced(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        log_output: list[dict],
    ) -> None:
        # Patch _emit_outcome to explode, simulating any bridge-side
        # failure (schema construction, occurred_at parse, etc.).
        from trellis.feedback import recording as recording_module

        def _boom(*_args, **_kwargs):
            msg = "outcome bridge down"
            raise RuntimeError(msg)

        monkeypatch.setattr(recording_module, "_emit_outcome", _boom)

        class _StubOutcomeStore:
            def append(self, *_args, **_kwargs):
                # Should never be reached because _emit_outcome raises first.
                msg = "should not be called"  # pragma: no cover
                raise AssertionError(msg)

        result = record_feedback(
            _feedback(),
            log_dir=tmp_path,
            outcome_store=_StubOutcomeStore(),
            pack_id="p1",
        )

        # Primary op succeeded.
        assert result.log_path.exists()
        assert result.outcome_emitted is False
        assert isinstance(result.outcome_error, RuntimeError)

        events = _events_with_key(log_output, "feedback_outcome_emit_failed")
        assert events, log_output
        assert events[0].get("log_level") == "error"


class TestReconcileMidLoopFailureGraceful:
    """L278 — reconcile loop continues past a failing emit."""

    def test_reconcile_records_failure_and_keeps_draining(
        self,
        tmp_path: Path,
        log_output: list[dict],
    ) -> None:
        # Stage two feedback rows via the JSONL-only path.
        fb1 = PackFeedback(
            run_id="r1",
            phase="p",
            intent="i",
            outcome="success",
            items_served=[],
        )
        fb2 = PackFeedback(
            run_id="r2",
            phase="p",
            intent="i",
            outcome="success",
            items_served=[],
        )
        record_feedback(fb1, log_dir=tmp_path)
        record_feedback(fb2, log_dir=tmp_path)

        # Reconcile with an event log that fails on the second emit.
        scripted = _ScriptedEventLog()
        result = reconcile_feedback_log_to_event_log(tmp_path, scripted)

        # Primary op drained both rows; one emitted, one failed.
        assert result.scanned == 2
        assert result.emitted == 1
        assert result.failed == 1
        assert len(result.missing_feedback_ids) == 1

        events = _events_with_key(log_output, "feedback_reconcile_emit_failed")
        assert events, log_output


class TestParseTimestampDefectFix:
    """L304 (DEFECT fix) — malformed timestamps must now log a warning.

    Empty input still returns None silently (first-class signal).
    """

    def test_empty_input_returns_none_silently(self, log_output: list[dict]) -> None:
        assert _parse_timestamp("") is None
        assert _events_with_key(log_output, "feedback_timestamp_parse_failed") == []

    def test_malformed_input_logs_warning(self, log_output: list[dict]) -> None:
        assert _parse_timestamp("not-a-timestamp") is None
        events = _events_with_key(log_output, "feedback_timestamp_parse_failed")
        assert events, log_output
        # logger.warning produces level "warning"; the rubric is "logged".
        assert events[0].get("log_level") == "warning"
        assert events[0].get("raw") == "not-a-timestamp"
