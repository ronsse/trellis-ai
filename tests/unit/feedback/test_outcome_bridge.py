"""Tests for the PackFeedback -> OutcomeEvent bridge in record_feedback."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.feedback import PackFeedback, record_feedback
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore


@pytest.fixture
def outcome_store(tmp_path: Path):
    s = SQLiteOutcomeStore(tmp_path / "outcomes.db")
    yield s
    s.close()


def _feedback(**overrides) -> PackFeedback:
    defaults: dict = {
        "run_id": "run-xyz",
        "phase": "retrieve",
        "intent": "fetch objective context",
        "outcome": "success",
        "items_served": ["i1", "i2", "i3"],
        "items_referenced": ["i1", "i3"],
        "intent_family": "plan",
        "relevance_scores": {"i1": 0.8, "i2": 0.3, "i3": 0.9},
        "agent_id": "agent-42",
    }
    defaults.update(overrides)
    return PackFeedback(**defaults)


def test_no_outcome_when_store_missing(
    tmp_path: Path, outcome_store: SQLiteOutcomeStore
):
    record_feedback(_feedback(), log_dir=tmp_path)  # no outcome_store
    assert outcome_store.count() == 0


def test_outcome_appended_when_store_provided(
    tmp_path: Path, outcome_store: SQLiteOutcomeStore
):
    record_feedback(
        _feedback(),
        log_dir=tmp_path,
        outcome_store=outcome_store,
        pack_id="pack-123",
    )
    events = outcome_store.query()
    assert len(events) == 1
    ev = events[0]
    assert ev.run_id == "run-xyz"
    assert ev.intent_family == "plan"
    assert ev.phase == "retrieve"
    assert ev.pack_id == "pack-123"
    assert ev.agent_id == "agent-42"
    assert ev.component_id == "retrieve.pack_builder.PackBuilder"
    assert ev.outcome.success is True
    assert ev.outcome.items_served == 3
    assert ev.outcome.items_referenced == 2


def test_failure_outcome_maps_success_false(
    tmp_path: Path, outcome_store: SQLiteOutcomeStore
):
    record_feedback(
        _feedback(outcome="failure"),
        log_dir=tmp_path,
        outcome_store=outcome_store,
    )
    events = outcome_store.query()
    assert events[0].outcome.success is False


def test_custom_component_id(tmp_path: Path, outcome_store: SQLiteOutcomeStore):
    record_feedback(
        _feedback(),
        log_dir=tmp_path,
        outcome_store=outcome_store,
        component_id="my.custom.component",
    )
    events = outcome_store.query()
    assert events[0].component_id == "my.custom.component"


def test_metadata_preserves_relevance_scores_and_intent(
    tmp_path: Path, outcome_store: SQLiteOutcomeStore
):
    record_feedback(
        _feedback(metadata={"note": "canary run"}),
        log_dir=tmp_path,
        outcome_store=outcome_store,
    )
    md = outcome_store.query()[0].metadata
    assert md["intent"] == "fetch objective context"
    assert md["pack_outcome"] == "success"
    assert md["relevance_scores"]["i1"] == 0.8
    assert md["feedback_metadata"] == {"note": "canary run"}


def test_outcome_failure_is_non_fatal(tmp_path: Path):
    """File write and event emit must succeed even if outcome_store explodes."""

    class BrokenOutcomeStore:
        def append(self, *_args, **_kwargs):
            msg = "ops store down"
            raise RuntimeError(msg)

    result = record_feedback(
        _feedback(),
        log_dir=tmp_path,
        outcome_store=BrokenOutcomeStore(),  # type: ignore[arg-type]
    )
    assert result.log_path.exists()
    assert result.log_path.read_text(encoding="utf-8").strip() != ""
    # Note: ``record_outcome`` (in trellis.ops) swallows store errors
    # internally and still returns an event, so outcome_emitted stays
    # True here. File durability is what this test protects — divergence
    # between outcome_store and JSONL is covered by ops-side tests.


def test_dual_emit_event_and_outcome(tmp_path: Path, outcome_store: SQLiteOutcomeStore):
    """When both sinks are passed, each receives its own record."""
    captured_events: list[object] = []

    class CapturingEventLog:
        def emit(self, *args, **kwargs):
            captured_events.append((args, kwargs))

        def get_events(self, **_ignored):
            # No prior events; idempotency check is a no-op.
            return []

    record_feedback(
        _feedback(),
        log_dir=tmp_path,
        event_log=CapturingEventLog(),  # type: ignore[arg-type]
        outcome_store=outcome_store,
        pack_id="pack-xyz",
    )
    assert len(captured_events) == 1
    assert outcome_store.count() == 1
