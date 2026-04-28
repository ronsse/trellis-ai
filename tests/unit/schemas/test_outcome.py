"""Tests for OutcomeEvent and ComponentOutcome schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.outcome import (
    INTENT_FAMILIES,
    PHASES,
    ComponentOutcome,
    OutcomeEvent,
)


def test_component_outcome_minimal():
    outcome = ComponentOutcome(success=True, latency_ms=12.5)
    assert outcome.success is True
    assert outcome.latency_ms == 12.5
    assert outcome.metrics == {}
    assert outcome.error is None


def test_component_outcome_forbids_extra():
    with pytest.raises(ValidationError):
        ComponentOutcome(success=True, latency_ms=1.0, unknown_field="x")


def test_component_outcome_rejects_negative_latency():
    with pytest.raises(ValidationError):
        ComponentOutcome(success=True, latency_ms=-1.0)


def test_component_outcome_with_metrics():
    outcome = ComponentOutcome(
        success=False,
        latency_ms=250.0,
        items_served=10,
        items_referenced=3,
        metrics={"precision": 0.3, "tokens_used": 1200.0},
        error="retry needed",
    )
    assert outcome.metrics["precision"] == 0.3
    assert outcome.error == "retry needed"


def test_outcome_event_minimal():
    event = OutcomeEvent(
        component_id="retrieve.strategies.KeywordSearch",
        outcome=ComponentOutcome(success=True, latency_ms=5.0),
    )
    assert event.event_id  # auto-generated
    assert event.component_id == "retrieve.strategies.KeywordSearch"
    assert event.domain is None
    assert event.intent_family is None


def test_outcome_event_forbids_extra():
    with pytest.raises(ValidationError):
        OutcomeEvent(
            component_id="x",
            outcome=ComponentOutcome(success=True, latency_ms=1.0),
            not_a_real_field="boom",
        )


def test_outcome_event_full_axes():
    event = OutcomeEvent(
        component_id="retrieve.pack_builder.PackBuilder",
        params_version="01HF...",
        domain="sportsbook",
        intent_family="diagnose",
        tool_name="get_task_context",
        phase="assemble",
        agent_role="claude-code",
        agent_id="agent-42",
        run_id="run-001",
        session_id="sess-001",
        pack_id="pack-001",
        trace_id="trace-001",
        outcome=ComponentOutcome(success=True, latency_ms=120.0, items_served=15),
        cohort="A",
        segment="us",
        metadata={"note": "canary cohort"},
    )
    assert event.domain == "sportsbook"
    assert event.intent_family == "diagnose"
    assert event.cohort == "A"


def test_intent_family_catalog_has_10_entries():
    assert len(INTENT_FAMILIES) == 10
    assert "discover" in INTENT_FAMILIES
    assert "classify" in INTENT_FAMILIES


def test_phase_catalog_has_7_entries():
    assert len(PHASES) == 7
    assert "enrich" in PHASES
    assert "classify" not in PHASES


def test_intent_family_is_freeform_string():
    event = OutcomeEvent(
        component_id="x",
        intent_family="custom_family",  # not in catalog — still accepted
        outcome=ComponentOutcome(success=True, latency_ms=1.0),
    )
    assert event.intent_family == "custom_family"


def test_outcome_event_roundtrip():
    original = OutcomeEvent(
        component_id="x",
        domain="d",
        intent_family="plan",
        phase="retrieve",
        outcome=ComponentOutcome(success=True, latency_ms=10.0, metrics={"p": 0.9}),
    )
    dumped = original.model_dump(mode="json")
    restored = OutcomeEvent.model_validate(dumped)
    assert restored.event_id == original.event_id
    assert restored.outcome.metrics["p"] == 0.9
