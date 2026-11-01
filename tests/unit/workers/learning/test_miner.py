"""Tests for the PrecedentMiner learning worker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import trellis_workers.learning.miner as miner_module
from trellis.llm import LLMResponse, Message
from trellis.llm.egress import EgressGate
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.precedent import Precedent
from trellis.schemas.trace import (
    EvidenceRef,
    Outcome,
    Trace,
    TraceContext,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.trace import SQLiteTraceStore
from trellis_workers.learning.miner import (
    PrecedentMiner,
    compute_source_digest,
    scan_failure_traces,
)

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
    created_at: datetime | None = None,
) -> Trace:
    """Create a minimal Trace for testing."""
    outcome = None if no_outcome else Outcome(status=status, summary=summary)
    evidence = [EvidenceRef(evidence_id=eid) for eid in (evidence_ids or [])]
    extra: dict[str, Any] = {} if created_at is None else {"created_at": created_at}
    return Trace(
        source=TraceSource.AGENT,
        intent=intent,
        outcome=outcome,
        context=TraceContext(domain=domain),
        evidence_used=evidence,
        **extra,
    )


#: A route that sends nowhere off the host: these tests are not about egress.
_EGRESS = EgressGate(None, local=True)

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


class _CountingLLM(_StubLLM):
    """A stub that records how many times it was asked, and what."""

    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.prompts: list[str] = []

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.prompts.append(messages[-1].content)
        return await super().generate(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )


_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _seed_timeline(
    store: SQLiteTraceStore,
    *,
    statuses: list[OutcomeStatus],
    domain: str | None = "testing",
) -> list[Trace]:
    """Append one trace per status, oldest first, a minute apart.

    ``statuses[0]`` is the oldest trace, so a store read newest-first starts
    at ``statuses[-1]``.
    """
    traces = []
    for i, status in enumerate(statuses):
        t = _make_trace(
            intent=f"timeline-{i}",
            status=status,
            summary=f"outcome #{i}",
            domain=domain,
            created_at=_EPOCH + timedelta(minutes=i),
        )
        store.append(t)
        traces.append(t)
    return traces


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

        miner = PrecedentMiner(trace_store, egress=_EGRESS)
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
        miner = PrecedentMiner(trace_store, egress=_EGRESS)
        assert miner.extract_precedent_from_trace("nonexistent") is None

    def test_no_outcome_returns_none(self, trace_store: SQLiteTraceStore) -> None:
        trace = _make_trace(no_outcome=True)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store, egress=_EGRESS)
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

        miner = PrecedentMiner(trace_store, egress=_EGRESS)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)

        assert precedent is not None
        assert precedent.confidence == expected_confidence


# ---------------------------------------------------------------------------
# extract_precedent_from_trace — event emission
# ---------------------------------------------------------------------------


class TestExtractEventEmission:
    """Verify event log receives PRECEDENT_CANDIDATE_MINED on extraction.

    A mined candidate is ungoverned and unapproved, so the miner never
    emits ``PRECEDENT_PROMOTED``: the readers behind ``get_lessons`` and
    ``GET /precedents`` would serve it as a promoted lesson.
    """

    def test_emits_event(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        trace = _make_trace(status=OutcomeStatus.FAILURE)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store, event_log=event_log, egress=_EGRESS)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)

        assert precedent is not None

        assert event_log.get_events(event_type=EventType.PRECEDENT_PROMOTED) == []
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED,
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.source == "precedent_miner"
        assert evt.entity_id == precedent.precedent_id
        assert evt.entity_type == "precedent"
        assert evt.payload["trace_id"] == trace.trace_id
        assert evt.payload["outcome_status"] == "failure"
        assert evt.payload["action"] == "precedent_extracted"

    def test_no_event_without_event_log(self, trace_store: SQLiteTraceStore) -> None:
        """No error when event_log is None."""
        trace = _make_trace(status=OutcomeStatus.SUCCESS)
        trace_store.append(trace)

        miner = PrecedentMiner(trace_store, event_log=None, egress=_EGRESS)
        precedent = miner.extract_precedent_from_trace(trace.trace_id)
        assert precedent is not None


# ---------------------------------------------------------------------------
# generate_precedent_candidates — early returns
# ---------------------------------------------------------------------------


class TestGenerateCandidatesEarlyReturn:
    """Tests for cases that short-circuit before LLM call."""

    @pytest.mark.asyncio
    async def test_no_llm_returns_empty(self, trace_store: SQLiteTraceStore) -> None:
        miner = PrecedentMiner(trace_store, llm=None, egress=_EGRESS)
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

        miner = PrecedentMiner(trace_store, llm=_mock_llm(), egress=_EGRESS)
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

        miner = PrecedentMiner(trace_store, llm=_mock_llm(), egress=_EGRESS)
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
    async def test_fenced_response_without_closing_fence_keeps_last_item(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        self._seed_failures(trace_store)
        response = "```json\n" + json.dumps(
            [
                {
                    "title": "First pattern",
                    "description": "First distinct description",
                    "pattern": "first",
                    "confidence": 0.4,
                },
                {
                    "title": "Second pattern",
                    "description": "Second distinct description",
                    "pattern": "second",
                    "confidence": 0.9,
                },
            ]
        )

        result = await PrecedentMiner(
            trace_store, llm=_StubLLM(response), egress=_EGRESS
        ).generate_precedent_candidates()

        assert [(item.title, item.confidence) for item in result] == [
            ("First pattern", 0.4),
            ("Second pattern", 0.9),
        ]

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

        miner = PrecedentMiner(trace_store, llm=_mock_llm(), egress=_EGRESS)
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

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )
        result = await miner.generate_precedent_candidates()

        assert len(result) == 1
        assert event_log.get_events(event_type=EventType.PRECEDENT_PROMOTED) == []
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED,
        )
        assert len(events) == 1
        assert events[0].source == "precedent_miner"
        assert events[0].payload["action"] == "precedent_candidate_generated"
        assert events[0].payload["title"] == "Timeout pattern"


# ---------------------------------------------------------------------------
# generate_precedent_candidates — non-finite confidence from the LLM
# ---------------------------------------------------------------------------


class TestGenerateCandidatesNonFiniteConfidence:
    """A non-finite ``confidence`` from the model falls back to 0.5.

    ``json.loads`` accepts the non-standard ``NaN`` token, and ``float()``
    accepts the string ``"nan"`` too — both reached ``Precedent.confidence``
    unclamped before this test, then failed the candidate event emit
    (``allow_nan=False``) after an earlier candidate in the same reply had
    already been emitted. ``coerce_finite_float`` catches both at the
    source instead.
    """

    def _seed_failures(self, store: SQLiteTraceStore, count: int = 4) -> None:
        for i in range(count):
            t = _make_trace(
                intent=f"fail-{i}",
                status=OutcomeStatus.FAILURE,
                summary=f"failed #{i}",
            )
            store.append(t)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_confidence", [float("nan"), "nan"])
    async def test_non_finite_confidence_falls_back_to_default(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        raw_confidence: object,
    ) -> None:
        self._seed_failures(trace_store)
        response = json.dumps(
            [
                {
                    "title": "Timeout pattern",
                    "description": "Multiple traces failed due to timeouts",
                    "pattern": "timeout on external calls",
                    "confidence": raw_confidence,
                },
            ],
            allow_nan=True,
        )

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_StubLLM(response), egress=_EGRESS
        )
        result = await miner.generate_precedent_candidates()

        assert len(result) == 1
        assert result[0].confidence == 0.5

        events = event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED)
        assert len(events) == 1
        assert events[0].payload["confidence"] == 0.5


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
    async def test_invalid_json_raises_extraction_failure(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        """ADR-extraction-failure-telemetry: parse errors emit + raise.

        The old silent ``return []`` masked broken-model symptoms; the
        miner now raises so the caller (or operator-level loop) can see
        the failure. The dispatcher is the *one* legitimate degrader and
        the miner doesn't go through the dispatcher.
        """
        from trellis.extract.telemetry import ExtractionFailureError

        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, llm=_bad_json_llm(), egress=_EGRESS)
        with pytest.raises(ExtractionFailureError) as excinfo:
            await miner.generate_precedent_candidates()
        assert excinfo.value.failure_kind == "parse_error"
        assert excinfo.value.extractor_id == "PrecedentMiner"

    @pytest.mark.asyncio
    async def test_llm_exception_returns_empty(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        self._seed_failures(trace_store)

        miner = PrecedentMiner(trace_store, llm=_error_llm(), egress=_EGRESS)
        result = await miner.generate_precedent_candidates()
        assert result == []

    @pytest.mark.asyncio
    async def test_invalid_json_emits_extraction_failed_event(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        """When wired with an EventLog the miner emits EXTRACTION_FAILED."""
        import os

        from trellis.extract.telemetry import (
            ExtractionFailureError,
            reset_extraction_failure_state,
        )

        reset_extraction_failure_state()
        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        try:
            self._seed_failures(trace_store)
            miner = PrecedentMiner(
                trace_store, event_log=event_log, llm=_bad_json_llm(), egress=_EGRESS
            )
            with pytest.raises(ExtractionFailureError):
                await miner.generate_precedent_candidates()

            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["extractor_id"] == "PrecedentMiner"
            assert payload["failure_kind"] == "parse_error"
            assert payload["extractor_tier"] == "llm"
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)
            reset_extraction_failure_state()


# ---------------------------------------------------------------------------
# The limit counts failures
# ---------------------------------------------------------------------------

F = OutcomeStatus.FAILURE
S = OutcomeStatus.SUCCESS


class _SpyTraceStore:
    """Wraps a trace store and records the ``limit`` of every ``query``."""

    def __init__(self, inner: SQLiteTraceStore) -> None:
        self._inner = inner
        self.limits: list[int] = []

    def query(self, **kwargs: Any) -> list[Trace]:
        self.limits.append(kwargs["limit"])
        return self._inner.query(**kwargs)


class TestLimitCountsFailures:
    """``limit`` is a number of failure/partial traces, not of traces read."""

    @pytest.mark.asyncio
    async def test_failures_older_than_the_newest_window_reach_the_llm(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # Five failures, then ten newer successes: a ``limit=10`` read of the
        # newest traces holds no failure at all.
        _seed_timeline(trace_store, statuses=[F] * 5 + [S] * 10)
        llm = _CountingLLM(_MOCK_JSON)

        miner = PrecedentMiner(trace_store, llm=llm, egress=_EGRESS)
        result = await miner.generate_precedent_candidates(min_traces=3, limit=10)

        assert len(llm.prompts) == 1
        assert len(result) == 1
        assert len(result[0].source_trace_ids) == 5

    @pytest.mark.asyncio
    async def test_limit_keeps_the_newest_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        traces = _seed_timeline(trace_store, statuses=[F, S] * 10)
        newest_failures = [t.trace_id for t in traces if _is_f(t)][::-1][:4]

        miner = PrecedentMiner(trace_store, llm=_mock_llm(), egress=_EGRESS)
        result = await miner.generate_precedent_candidates(min_traces=3, limit=4)

        assert result[0].source_trace_ids == newest_failures

    def test_failures_beyond_the_first_read_are_found_by_widening(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # The first read is 100 traces wide; the failures sit 150 successes back.
        _seed_timeline(trace_store, statuses=[F] * 3 + [S] * 150)

        scan = scan_failure_traces(trace_store, domain=None, limit=10)

        assert len(scan.traces) == 3
        assert scan.examined == 153
        assert scan.exhausted is True
        assert scan.bound_hit is False

    def test_partial_counts_and_a_trace_without_an_outcome_does_not(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        trace_store.append(_make_trace(status=OutcomeStatus.PARTIAL))
        trace_store.append(_make_trace(no_outcome=True))
        trace_store.append(_make_trace(status=S))

        scan = scan_failure_traces(trace_store, domain=None, limit=10)

        assert [t.outcome.status for t in scan.traces if t.outcome] == [
            OutcomeStatus.PARTIAL
        ]
        assert scan.exhausted is True

    def test_traces_sharing_one_instant_are_all_read(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # ``until`` is inclusive, so a cursor on ``created_at`` cannot step
        # past a page that is one instant wide. The widening read can.
        for i in range(250):
            trace_store.append(
                _make_trace(
                    intent=f"same-instant-{i}",
                    status=F if i % 50 == 0 else S,
                    created_at=_EPOCH,
                )
            )

        scan = scan_failure_traces(trace_store, domain=None, limit=5)

        assert len(scan.traces) == 5

    def test_a_newest_window_with_enough_failures_costs_one_read(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 10 + [F] * 5)
        spy = _SpyTraceStore(trace_store)

        scan = scan_failure_traces(spy, domain=None, limit=3)  # type: ignore[arg-type]

        assert len(scan.traces) == 3
        # One read, 100 wide though ``limit`` is 3, plus the row that says
        # whether the store goes on.
        assert spy.limits == [101]


def _is_f(trace: Trace) -> bool:
    return trace.outcome is not None and trace.outcome.status is F


# ---------------------------------------------------------------------------
# An empty result says why it is empty
# ---------------------------------------------------------------------------


class TestEmptyResultSaysWhy:
    @pytest.mark.asyncio
    async def test_a_store_with_no_failures_reports_no_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 8)
        llm = _CountingLLM(_MOCK_JSON)

        report = await PrecedentMiner(trace_store, llm=llm, egress=_EGRESS).mine()

        assert report.outcome == "no_failures"
        assert report.read_bound_hit is False
        assert report.traces_examined == 8
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_the_read_bound_stopping_before_any_failure_is_not_no_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5 + [S] * 30)
        llm = _CountingLLM(_MOCK_JSON)

        report = await PrecedentMiner(trace_store, llm=llm, egress=_EGRESS).mine(
            scan_cap=20
        )

        assert report.outcome == "read_bound_hit"
        assert report.read_bound_hit is True
        assert report.failures_in_scope == 0
        assert report.traces_examined == 20
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_a_store_of_exactly_the_bound_is_not_reported_cut_off(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 20)

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(scan_cap=20)

        assert report.outcome == "no_failures"
        assert report.read_bound_hit is False

    @pytest.mark.asyncio
    async def test_the_bound_stopping_short_of_min_traces_names_the_bound(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # One failure inside the bound, more behind it: not "below min".
        _seed_timeline(trace_store, statuses=[F] * 4 + [S] * 14 + [F])

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(scan_cap=15)

        assert report.outcome == "read_bound_hit"
        assert report.failures_in_scope == 1

    @pytest.mark.asyncio
    async def test_a_read_to_the_end_below_min_traces_is_below_min_traces(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F, F, S, S])

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine()

        assert report.outcome == "below_min_traces"
        assert report.read_bound_hit is False

    @pytest.mark.asyncio
    async def test_enough_failures_inside_the_bound_still_flag_the_cut(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # Three failures are inside the bound, so it mines, but older traces
        # were not read, and the report says so.
        _seed_timeline(trace_store, statuses=[F] * 2 + [S] * 20 + [F] * 3)

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(min_traces=3, limit=10, scan_cap=22)

        assert report.outcome == "mined"
        assert report.read_bound_hit is True

    @pytest.mark.asyncio
    async def test_a_model_that_returns_nothing_is_named_apart_from_an_outage(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)

        empty = await PrecedentMiner(
            trace_store, llm=_StubLLM("[]"), egress=_EGRESS
        ).mine()
        broken = await PrecedentMiner(
            trace_store, llm=_error_llm(), egress=_EGRESS
        ).mine()

        assert empty.outcome == "no_candidates"
        assert broken.outcome == "llm_error"


# ---------------------------------------------------------------------------
# A rerun over unchanged failures
# ---------------------------------------------------------------------------


class TestRerunMintsNoDuplicates:
    """The same failures, mined twice, leave one set of candidates."""

    @pytest.mark.asyncio
    async def test_rerun_over_unchanged_failures_mints_no_second_candidate(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        llm = _CountingLLM(_MOCK_JSON)

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        )
        first = await miner.mine()
        second = await miner.mine()

        events = event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED)
        assert len(events) == 1
        assert len(llm.prompts) == 1
        assert first.outcome == "mined"
        assert second.outcome == "skipped_unchanged"
        assert second.candidates == ()
        assert second.already_mined == (first.candidates[0].precedent_id,)
        assert second.source_digest == first.source_digest

    @pytest.mark.asyncio
    async def test_the_candidate_event_carries_the_digest_a_rerun_matches(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        traces = _seed_timeline(trace_store, statuses=[F] * 4)

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )
        await miner.generate_precedent_candidates(domain="testing")

        [event] = event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED)
        assert event.payload["source_digest"] == compute_source_digest(
            traces, "testing"
        )

    @pytest.mark.asyncio
    async def test_a_new_failure_mines_again(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )
        await miner.mine()

        trace_store.append(_make_trace(status=F, created_at=_EPOCH + timedelta(days=1)))
        again = await miner.mine()

        assert again.outcome == "mined"
        assert (
            len(event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED))
            == 2
        )

    @pytest.mark.asyncio
    async def test_a_new_success_does_not_change_the_input(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )
        await miner.mine()

        trace_store.append(_make_trace(status=S, created_at=_EPOCH + timedelta(days=1)))

        assert (await miner.mine()).outcome == "skipped_unchanged"

    @pytest.mark.asyncio
    async def test_the_same_traces_in_another_domain_scope_mine_again(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # The candidate's ``applicability`` is the domain, so a run scoped to
        # it is a different record, not a duplicate.
        _seed_timeline(trace_store, statuses=[F] * 4, domain="backend")
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )

        unscoped = await miner.mine()
        scoped = await miner.mine(domain="backend")

        assert unscoped.outcome == "mined"
        assert scoped.outcome == "mined"
        assert scoped.source_digest != unscoped.source_digest

    @pytest.mark.asyncio
    async def test_a_run_that_minted_nothing_does_not_block_the_next(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        from trellis.extract.telemetry import ExtractionFailureError

        _seed_timeline(trace_store, statuses=[F] * 4)

        bad = PrecedentMiner(
            trace_store, event_log=event_log, llm=_bad_json_llm(), egress=_EGRESS
        )
        with pytest.raises(ExtractionFailureError):
            await bad.mine()
        good = await PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        ).mine()

        assert good.outcome == "mined"

    @pytest.mark.asyncio
    async def test_a_candidate_minted_before_the_digest_existed_does_not_block(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        event_log.emit(
            EventType.PRECEDENT_CANDIDATE_MINED,
            source="precedent_miner",
            entity_id="legacy-candidate",
            entity_type="precedent",
            payload={
                "action": "precedent_candidate_generated",
                "title": "Timeout pattern",
                "confidence": 0.85,
            },
        )

        report = await PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        ).mine()

        assert report.outcome == "mined"

    @pytest.mark.asyncio
    async def test_without_an_event_log_there_is_nothing_to_skip_on(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        llm = _CountingLLM(_MOCK_JSON)

        miner = PrecedentMiner(trace_store, llm=llm, egress=_EGRESS)
        await miner.mine()
        await miner.mine()

        assert len(llm.prompts) == 2

    def test_a_plan_asks_no_model_and_writes_nothing(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 4)
        llm = _CountingLLM(_MOCK_JSON)

        plan = PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        ).plan()

        assert plan.outcome == "would_mine"
        assert plan.failures_in_scope == 4
        assert llm.prompts == []
        assert (
            event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED) == []
        )


# ---------------------------------------------------------------------------
# A run is judged by its events as a whole
# ---------------------------------------------------------------------------

_THREE_ITEMS_SECOND_MALFORMED = json.dumps(
    [
        {"title": "A", "description": "d", "pattern": "p"},
        {"title": "B", "description": "d", "pattern": ["x", "y"]},
        {"title": "C", "description": "d"},
    ]
)

_TWO_ITEMS = json.dumps(
    [
        {"title": "A", "description": "d", "pattern": "p", "confidence": 0.5},
        {"title": "B", "description": "d", "pattern": "q", "confidence": 0.6},
    ]
)


def _mined_events(event_log: SQLiteEventLog) -> list[Any]:
    return event_log.get_events(event_type=EventType.PRECEDENT_CANDIDATE_MINED)


class TestRunCompleteness:
    """A run cut off part-way is not a record a later run may skip against."""

    @pytest.mark.asyncio
    async def test_a_malformed_item_mints_nothing_and_the_next_run_asks_again(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        from trellis.extract.telemetry import ExtractionFailureError

        _seed_timeline(trace_store, statuses=[F] * 5)

        bad = PrecedentMiner(
            trace_store,
            event_log=event_log,
            llm=_StubLLM(_THREE_ITEMS_SECOND_MALFORMED),
            egress=_EGRESS,
        )
        with pytest.raises(ExtractionFailureError):
            await bad.mine()
        assert _mined_events(event_log) == []

        llm = _CountingLLM(_MOCK_JSON)
        good = await PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        ).mine()

        assert good.outcome == "mined"
        assert len(llm.prompts) == 1

    @pytest.mark.asyncio
    async def test_a_failed_emit_part_way_is_retried_not_skipped(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5)
        real_emit = event_log.emit
        calls = {"n": 0}

        def flaky_emit(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                msg = "disk full"
                raise RuntimeError(msg)
            return real_emit(*args, **kwargs)

        monkeypatch.setattr(event_log, "emit", flaky_emit)
        cut_off = PrecedentMiner(
            trace_store, event_log=event_log, llm=_StubLLM(_TWO_ITEMS), egress=_EGRESS
        )
        with pytest.raises(RuntimeError, match="disk full"):
            await cut_off.mine()
        assert len(_mined_events(event_log)) == 1

        llm = _CountingLLM(_TWO_ITEMS)
        retry = await PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        ).mine()

        assert retry.outcome == "mined"
        assert len(llm.prompts) == 1
        assert len(_mined_events(event_log)) == 3

    @pytest.mark.asyncio
    async def test_a_complete_run_skips_and_names_only_its_own_candidates(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5)
        real_emit = event_log.emit
        calls = {"n": 0}

        def flaky_emit(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                msg = "disk full"
                raise RuntimeError(msg)
            return real_emit(*args, **kwargs)

        monkeypatch.setattr(event_log, "emit", flaky_emit)
        with pytest.raises(RuntimeError):
            await PrecedentMiner(
                trace_store,
                event_log=event_log,
                llm=_StubLLM(_TWO_ITEMS),
                egress=_EGRESS,
            ).mine()
        monkeypatch.setattr(event_log, "emit", real_emit)

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_StubLLM(_TWO_ITEMS), egress=_EGRESS
        )
        complete = await miner.mine()
        skipped = await miner.mine()

        assert skipped.outcome == "skipped_unchanged"
        # Three events exist; the orphan from the cut-off run is not named.
        assert len(_mined_events(event_log)) == 3
        assert set(skipped.already_mined) == {
            c.precedent_id for c in complete.candidates
        }
        assert len(skipped.already_mined) == 2

    @pytest.mark.asyncio
    async def test_a_runs_events_share_an_id_and_declare_its_size(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5)
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_StubLLM(_TWO_ITEMS), egress=_EGRESS
        )
        await miner.mine()
        trace_store.append(_make_trace(status=F, created_at=_EPOCH + timedelta(days=1)))
        await miner.mine()

        events = _mined_events(event_log)

        assert len(events) == 4
        by_run: dict[str, list[Any]] = {}
        for event in events:
            by_run.setdefault(event.payload["mining_run_id"], []).append(event)
        assert len(by_run) == 2
        assert all(len(group) == 2 for group in by_run.values())
        assert all(e.payload["run_candidate_count"] == 2 for e in events)

    @pytest.mark.asyncio
    async def test_a_run_larger_than_the_lookup_cannot_be_shown_complete(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(miner_module, "_RUN_LOOKUP_LIMIT", 1)
        _seed_timeline(trace_store, statuses=[F] * 5)
        llm = _CountingLLM(_TWO_ITEMS)

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        )
        await miner.mine()
        second = await miner.mine()

        # Two events, one readable: the run is not provably whole.
        assert second.outcome == "mined"
        assert len(llm.prompts) == 2

    @pytest.mark.asyncio
    async def test_a_run_as_large_as_the_lookup_is_complete(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(miner_module, "_RUN_LOOKUP_LIMIT", 2)
        _seed_timeline(trace_store, statuses=[F] * 5)

        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_StubLLM(_TWO_ITEMS), egress=_EGRESS
        )
        await miner.mine()

        assert (await miner.mine()).outcome == "skipped_unchanged"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source", "event_type"),
        [
            ("someone_else", EventType.PRECEDENT_CANDIDATE_MINED),
            ("precedent_miner", EventType.PRECEDENT_PROMOTED),
        ],
    )
    async def test_only_the_miners_candidate_events_count(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        source: str,
        event_type: EventType,
    ) -> None:
        traces = _seed_timeline(trace_store, statuses=[F] * 4)
        event_log.emit(
            event_type,
            source=source,
            entity_id="not-a-mined-candidate",
            entity_type="precedent",
            payload={
                "source_digest": compute_source_digest(traces, None),
                "mining_run_id": "r1",
                "run_candidate_count": 1,
            },
        )

        report = await PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        ).mine()

        assert report.outcome == "mined"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("extra", "entity_id"),
        [
            ({}, "earlier-build"),
            ({"run_candidate_count": 1}, "no-run-id"),
            ({"mining_run_id": "r1"}, "no-count"),
            ({"mining_run_id": "r1", "run_candidate_count": True}, "bool-count"),
            ({"mining_run_id": 7, "run_candidate_count": 1}, "int-run-id"),
            ({"mining_run_id": "r1", "run_candidate_count": 1}, None),
        ],
        ids=[
            "digest_only",
            "no_run_id",
            "no_count",
            "bool_count",
            "non_str_run_id",
            "no_candidate_id",
        ],
    )
    async def test_an_event_that_cannot_be_judged_is_not_a_record_to_skip_against(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        extra: dict[str, Any],
        entity_id: str | None,
    ) -> None:
        traces = _seed_timeline(trace_store, statuses=[F] * 4)
        event_log.emit(
            EventType.PRECEDENT_CANDIDATE_MINED,
            source="precedent_miner",
            entity_id=entity_id,
            entity_type="precedent",
            payload={"source_digest": compute_source_digest(traces, None), **extra},
        )

        report = await PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        ).mine()

        assert report.outcome == "mined"

    def test_the_newest_complete_run_is_the_one_named(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two whole runs over the same input (one written by a build that did
        # not skip) and a lookup that holds only one run's worth: it must be
        # the newest events it reads, not the oldest.
        monkeypatch.setattr(miner_module, "_RUN_LOOKUP_LIMIT", 2)
        traces = _seed_timeline(trace_store, statuses=[F] * 4)
        digest = compute_source_digest(traces, None)
        for run_id, ids in (("old", ("o1", "o2")), ("new", ("n1", "n2"))):
            for candidate_id in ids:
                event_log.emit(
                    EventType.PRECEDENT_CANDIDATE_MINED,
                    source="precedent_miner",
                    entity_id=candidate_id,
                    entity_type="precedent",
                    payload={
                        "source_digest": digest,
                        "mining_run_id": run_id,
                        "run_candidate_count": 2,
                    },
                )

        report = PrecedentMiner(trace_store, event_log=event_log, egress=_EGRESS).plan()

        assert report.outcome == "skipped_unchanged"
        assert report.already_mined == ("n1", "n2")


# ---------------------------------------------------------------------------
# How much of the store a run reads, and what it shows the model
# ---------------------------------------------------------------------------


class TestScanStopsWhenItHasEnough:
    def test_a_large_default_limit_does_not_walk_the_ladder_for_failures_it_cannot_use(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # 21 failures newest, then 6,000 older successes: 20 is all the model
        # is ever shown, so one 100-wide read is enough and no bound is hit.
        _seed_timeline(trace_store, statuses=[S] * 6_000 + [F] * 21)
        spy = _SpyTraceStore(trace_store)

        scan = scan_failure_traces(spy, domain=None, limit=100)  # type: ignore[arg-type]

        assert spy.limits == [101]
        assert len(scan.traces) == 21
        assert scan.examined == 100
        assert scan.bound_hit is False
        assert scan.exhausted is False

    def test_min_traces_above_twenty_raises_the_target(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # 30 failures sit 150 successes back; a minimum of 25 needs them all.
        _seed_timeline(trace_store, statuses=[F] * 30 + [S] * 150)
        spy = _SpyTraceStore(trace_store)

        scan = scan_failure_traces(spy, domain=None, limit=100, min_traces=25)  # type: ignore[arg-type]

        assert spy.limits == [101, 201]
        assert len(scan.traces) == 30

    def test_the_read_widens_by_doubling_to_the_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 3 + [S] * 450)
        spy = _SpyTraceStore(trace_store)

        scan = scan_failure_traces(spy, domain=None, limit=3)  # type: ignore[arg-type]

        assert spy.limits == [101, 201, 401, 801]
        assert scan.examined == 453
        assert len(scan.traces) == 3

    def test_the_cap_ends_the_widening_and_is_not_exhaustion(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 400)
        spy = _SpyTraceStore(trace_store)

        scan = scan_failure_traces(spy, domain=None, limit=5, scan_cap=150)  # type: ignore[arg-type]

        assert spy.limits == [101, 151]
        assert scan.examined == 150
        assert scan.bound_hit is True
        assert scan.exhausted is False

    @pytest.mark.parametrize(("limit", "scan_cap"), [(0, 100), (-1, 100), (5, 0)])
    def test_a_scan_that_cannot_read_anything_is_refused(
        self, trace_store: SQLiteTraceStore, limit: int, scan_cap: int
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 3)

        with pytest.raises(ValueError, match="at least 1"):
            scan_failure_traces(
                trace_store, domain=None, limit=limit, scan_cap=scan_cap
            )

    @pytest.mark.asyncio
    async def test_the_api_refuses_what_it_cannot_read_instead_of_reporting_it_read(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        miner = PrecedentMiner(trace_store, llm=_mock_llm(), egress=_EGRESS)

        with pytest.raises(ValueError, match="at least 1"):
            await miner.mine(limit=0)
        with pytest.raises(ValueError, match="at least 1"):
            await miner.mine(scan_cap=0)
        with pytest.raises(ValueError, match="at least 1"):
            miner.plan(limit=0)

    @pytest.mark.asyncio
    async def test_generate_honours_the_scan_cap(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5 + [S] * 30)
        llm = _CountingLLM(_MOCK_JSON)

        result = await PrecedentMiner(
            trace_store, llm=llm, egress=_EGRESS
        ).generate_precedent_candidates(scan_cap=20)

        assert result == []
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_a_model_is_shown_at_most_twenty_of_the_newest_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        traces = _seed_timeline(trace_store, statuses=[F] * 25)
        llm = _CountingLLM(_MOCK_JSON)

        report = await PrecedentMiner(trace_store, llm=llm, egress=_EGRESS).mine()

        newest_twenty = [t.trace_id for t in traces][::-1][:20]
        assert report.failures_in_scope == 25
        assert [t.trace_id for t in report.analyzed] == newest_twenty
        assert "Analyze these 20 failed/partial traces" in llm.prompts[0]
        assert len(report.candidates[0].source_trace_ids) == 20

    @pytest.mark.asyncio
    async def test_an_older_failure_beyond_the_newest_twenty_is_not_a_new_input(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # Disclosed, not paged: failures past the newest 20 are never shown,
        # so adding one changes nothing the model would see.
        _seed_timeline(trace_store, statuses=[F] * 25)
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=_mock_llm(), egress=_EGRESS
        )
        await miner.mine()

        trace_store.append(_make_trace(status=F, created_at=_EPOCH - timedelta(days=1)))
        again = await miner.mine()

        assert again.failures_in_scope == 26
        assert again.outcome == "skipped_unchanged"

    @pytest.mark.asyncio
    async def test_a_limit_below_twenty_caps_what_is_shown(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 25)

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(limit=5)

        assert report.failures_in_scope == 5
        assert len(report.analyzed) == 5

    @pytest.mark.asyncio
    async def test_without_a_model_the_outcome_is_no_llm(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 5)

        report = await PrecedentMiner(trace_store, egress=_EGRESS).mine(scan_cap=77)

        assert report.outcome == "no_llm"
        assert report.scan_cap == 77
        assert report.candidates == ()


class TestMinTracesAndLimit:
    @pytest.mark.asyncio
    async def test_a_minimum_of_zero_over_no_failures_asks_nothing(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[S] * 3)
        llm = _CountingLLM(_MOCK_JSON)

        miner = PrecedentMiner(trace_store, llm=llm, egress=_EGRESS)
        mined = await miner.mine(min_traces=0)
        planned = miner.plan(min_traces=0)

        assert mined.outcome == "no_failures"
        assert planned.outcome == "no_failures"
        assert llm.prompts == []

    @pytest.mark.asyncio
    async def test_a_minimum_of_zero_over_a_cut_off_read_is_not_no_failures(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 3 + [S] * 30)

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(min_traces=0, scan_cap=10)

        assert report.outcome == "read_bound_hit"

    @pytest.mark.asyncio
    async def test_a_limit_below_the_minimum_reads_no_further_than_it_needs(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        # The API does not refuse it (the CLI does): it reports below the
        # minimum, and does not claim the store was read to its end.
        _seed_timeline(trace_store, statuses=[F] * 10)
        llm = _CountingLLM(_MOCK_JSON)

        report = await PrecedentMiner(trace_store, llm=llm, egress=_EGRESS).mine(
            min_traces=5, limit=2
        )

        assert report.outcome == "below_min_traces"
        assert report.failures_in_scope == 2
        assert report.read_to_end is False
        assert llm.prompts == []

    @pytest.mark.parametrize(
        ("count", "scan_cap", "exhausted", "bound_hit"),
        [(5, 100, True, False), (10, 6, False, True), (10, 100, False, False)],
        ids=["store_ended", "cap_stopped_it", "enough_found"],
    )
    def test_a_scan_keeps_no_more_than_the_limit_however_it_stopped(
        self,
        trace_store: SQLiteTraceStore,
        count: int,
        scan_cap: int,
        exhausted: bool,
        bound_hit: bool,
    ) -> None:
        # Only the API can ask for a limit below the minimum, and the three
        # ways a scan ends must all honour the limit it was given.
        _seed_timeline(trace_store, statuses=[F] * count)

        scan = scan_failure_traces(
            trace_store, domain=None, limit=2, scan_cap=scan_cap, min_traces=8
        )

        assert len(scan.traces) == 2
        assert (scan.exhausted, scan.bound_hit) == (exhausted, bound_hit)

    @pytest.mark.asyncio
    async def test_a_read_that_did_reach_the_end_says_so(
        self, trace_store: SQLiteTraceStore
    ) -> None:
        _seed_timeline(trace_store, statuses=[F] * 3)

        report = await PrecedentMiner(
            trace_store, llm=_mock_llm(), egress=_EGRESS
        ).mine(min_traces=5, limit=2)

        assert report.outcome == "below_min_traces"
        assert report.read_to_end is True


class TestANewSuccessAndTheReadWindow:
    @pytest.mark.asyncio
    async def test_a_new_success_mines_again_only_by_aging_a_failure_out(
        self,
        trace_store: SQLiteTraceStore,
        event_log: SQLiteEventLog,
    ) -> None:
        # Behaviour pinned, not endorsed: the input is the failures the read
        # window holds, so a failure ageing out of it changes the digest. No
        # subset check, because the next run is still a different question.
        _seed_timeline(trace_store, statuses=[S, F, S, F, S, F])
        llm = _CountingLLM(_MOCK_JSON)
        miner = PrecedentMiner(
            trace_store, event_log=event_log, llm=llm, egress=_EGRESS
        )
        kwargs = {"min_traces": 2, "scan_cap": 6}
        assert (await miner.mine(**kwargs)).outcome == "mined"

        # Pushes the oldest *success* out of the window: same failures.
        trace_store.append(_make_trace(status=S, created_at=_EPOCH + timedelta(days=1)))
        assert (await miner.mine(**kwargs)).outcome == "skipped_unchanged"

        # Pushes the oldest *failure* out: the shrunken set is a new input.
        trace_store.append(_make_trace(status=S, created_at=_EPOCH + timedelta(days=2)))
        again = await miner.mine(**kwargs)

        assert again.outcome == "mined"
        assert again.failures_in_scope == 2
        assert len(llm.prompts) == 2
