"""Tests for the PrecedentMiner learning worker."""

from __future__ import annotations

import json
from typing import Any

import pytest

from trellis.llm import LLMResponse, Message
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.precedent import Precedent
from trellis.schemas.trace import (
    EvidenceRef,
    Outcome,
    Trace,
    TraceContext,
)
from trellis.stores.event_log import EventType, SQLiteEventLog
from trellis.stores.trace import SQLiteTraceStore
from trellis_workers.learning.miner import PrecedentMiner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace(
    *,
    intent: str = "do something",
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
    summary: str | None = "completed",
    domain: str | None = "testing",
    evidence_ids: list[str] | None = None,
    no_outcome: bool = False,
) -> Trace:
    """Create a minimal Trace for testing."""
    outcome = None if no_outcome else Outcome(status=status, summary=summary)
    evidence = [EvidenceRef(evidence_id=eid) for eid in (evidence_ids or [])]
    return Trace(
        source=TraceSource.AGENT,
        intent=intent,
        outcome=outcome,
        context=TraceContext(domain=domain),
        evidence_used=evidence,
    )


_MOCK_JSON = json.dumps(
    [
        {
            "title": "Timeout pattern",
            "description": "Multiple traces failed due to timeouts",
            "pattern": "timeout on external calls",
            "confidence": 0.85,
        },
    ]
)


class _StubLLM:
    """LLMClient stub returning a canned ``LLMResponse``."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content=self._content, model=model or "test-model")


class _BrokenLLM:
    """LLMClient stub that always raises."""

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        msg = "boom"
        raise RuntimeError(msg)


def _mock_llm() -> _StubLLM:
    return _StubLLM(_MOCK_JSON)


def _bad_json_llm() -> _StubLLM:
    return _StubLLM("this is not json at all")


def _error_llm() -> _BrokenLLM:
    return _BrokenLLM()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_store(tmp_path: Any) -> SQLiteTraceStore:
    store = SQLiteTraceStore(tmp_path / "traces.db")
    yield store  # type: ignore[misc]
    store.close()


@pytest.fixture
def event_log(tmp_path: Any) -> SQLiteEventLog:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log  # type: ignore[misc]
    log.close()


# ---------------------------------------------------------------------------
# extract_precedent_from_trace — happy paths
# ---------------------------------------------------------------------------


class TestExtractPrecedentHappy:
    """Happy-path tests for deterministic precedent extraction."""

    def test_success_outcome(self, trace_store: SQLiteTraceStore) -> None:
        trace = _make_trace(
            intent="deploy service",
            status=OutcomeStatus.SUCCESS,
            summary="deployed ok",
            domain="infra",
            evidence_ids=["ev-1", "ev-2"],
        )
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)

        assert precedent is not None
        assert isinstance(precedent, Precedent)
        assert precedent.title == "deploy service"
        assert precedent.description == "deployed ok"
        assert precedent.source_trace_ids == [trace.trace_id]
        assert precedent.confidence == 0.7
        assert precedent.promoted_by == "precedent_miner"
        assert precedent.applicability == ["infra"]
        assert precedent.evidence_refs == ["ev-1", "ev-2"]

    def test_trace_not_found_returns_none(self, trace_store: SQLiteTraceStore) -> None:
        miner = PrecedentMiner(trace_store)
        assert miner.extract_precedent_from_trace("nonexistent") is None

    def test_no_outcome_returns_none(self, trace_store: SQLiteTraceStore) -> None:
        trace = _make_trace(no_outcome=True)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store)
        assert miner.extract_precedent_from_trace(trace.trace_id) is None


# ---------------------------------------------------------------------------
# extract_precedent_from_trace — confidence mapping
# ---------------------------------------------------------------------------


class TestConfidenceMapping:
    """Verify confidence heuristic per outcome status."""

    @pytest.mark.parametrize(
        ("status", "expected_confidence"),
        [
            (OutcomeStatus.SUCCESS, 0.7),
            (OutcomeStatus.PARTIAL, 0.4),
            (OutcomeStatus.FAILURE, 0.3),
            (OutcomeStatus.UNKNOWN, 0.2),
        ],
    )
    def test_confidence_per_status(
        self,
        trace_store: SQLiteTraceStore,
        status: OutcomeStatus,
        expected_confidence: float,
    ) -> None:
        trace = _make_trace(status=status)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)

        assert precedent is not None
        assert precedent.confidence == expected_confidence


# ---------------------------------------------------------------------------
# extract_precedent_from_trace — event emission
# ---------------------------------------------------------------------------


class TestExtractEventEmission:
    """Verify event log receives PRECEDENT_PROMOTED on extraction."""

    def test_emits_event(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        trace = _make_trace(status=OutcomeStatus.FAILURE)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store, event_log=event_log)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)

        assert precedent is not None

        events = event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.entity_id == precedent.precedent_id
        assert evt.entity_type == "precedent"
        assert evt.payload["trace_id"] == trace.trace_id
        assert evt.payload["outcome_status"] == "failure"
        assert evt.payload["action"] == "precedent_extracted"

    def test_no_event_without_event_log(self, trace_store: SQLiteTraceStore) -> None:
        """No error when event_log is None."""
        trace = _make_trace(status=OutcomeStatus.SUCCESS)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store, event_log=None)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)
        assert precedent is not None


# ---------------------------------------------------------------------------
# generate_precedent_candidates — early returns
# ---------------------------------------------------------------------------


class TestGenerateCandidatesEarlyReturn:
    """Tests for cases that short-circuit before LLM call."""

    @pytest.mark.asyncio
    async def test_no_llm_returns_empty(self, trace_store: SQLiteTraceStore) -> None:
        miner = PrecedentMiner(trace_store, llm=None)
        result = await miner.generate_precedent_candidates()
        assert result == []

    @pytest.mark.asyncio
    async def test_not_enough_failures_returns_empty(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # Only 2 failures, min_traces defaults to 3
        for i in range(2):
            trace = _make_trace(
                intent=f"fail {i}",
                status=OutcomeStatus.FAILURE,
            )
            trace_store.append(trace)

        miner = PrecedentMiner(trace_store, llm=_mock_llm())
        result = await miner.generate_precedent_candidates()
        assert result == []


# ---------------------------------------------------------------------------
# generate_precedent_candidates — happy path
# ---------------------------------------------------------------------------


class TestGenerateCandidatesHappy:
    """Happy path: LLM returns well-formed JSON."""

    def _seed_failures(
        self,
        store: SQLiteTraceStore,
        count: int = 4,
        domain: str | None = "backend",
    ) -> list[Trace]:
        traces = []
        for i in range(count):
            t = _make_trace(
                intent=f"task-{i}",
                status=OutcomeStatus.FAILURE,
                summary=f"failed #{i}",
                domain=domain,
            )
            store.append(t)
            traces.append(t)
        return traces

    @pytest.mark.asyncio
    async def test_returns_precedent_candidates(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, llm=_mock_llm())
        result = await miner.generate_precedent_candidates()

        assert len(result) == 1
        p = result[0]
        assert isinstance(p, Precedent)
        assert p.title == "Timeout pattern"
        assert p.description == "Multiple traces failed due to timeouts"
        assert p.pattern == "timeout on external calls"
        assert p.confidence == 0.85
        assert p.promoted_by == "precedent_miner"
        assert len(p.source_trace_ids) == 4

    @pytest.mark.asyncio
    async def test_domain_filtering(self, trace_store: SQLiteTraceStore) -> None:
        self._seed_failures(trace_store, domain="backend")
        # Also seed some in a different domain
        for i in range(4):
            t = _make_trace(
                intent=f"other-{i}",
                status=OutcomeStatus.FAILURE,
                domain="frontend",
            )
            trace_store.append(t)

        miner = PrecedentMiner(trace_store, llm=_mock_llm())
        result = await miner.generate_precedent_candidates(domain="backend")

        assert len(result) == 1
        assert result[0].applicability == ["backend"]

    @pytest.mark.asyncio
    async def test_emits_events_for_candidates(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, event_log=event_log, llm=_mock_llm())
        result = await miner.generate_precedent_candidates()

        assert len(result) == 1
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
        )
        assert len(events) == 1
        assert events[0].payload["action"] == "precedent_candidate_generated"
        assert events[0].payload["title"] == "Timeout pattern"


# ---------------------------------------------------------------------------
# generate_precedent_candidates — error handling
# ---------------------------------------------------------------------------


class TestGenerateCandidatesErrors:
    """Error-handling paths for candidate generation."""

    def _seed_failures(self, store: SQLiteTraceStore, count: int = 4) -> None:
        for i in range(count):
            t = _make_trace(
                intent=f"fail-{i}",
                status=OutcomeStatus.FAILURE,
                summary=f"failed #{i}",
            )
            store.append(t)

    @pytest.mark.asyncio
    async def test_invalid_json_returns_empty(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, llm=_bad_json_llm())
        result = await miner.generate_precedent_candidates()
        assert result == []

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, llm=_error_llm())
        result = await miner.generate_precedent_candidates()
        assert result == []
