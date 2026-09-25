"""Tests for enrichment service."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock

import pytest

from trellis.llm import LLMResponse, Message, TokenUsage
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_workers.enrichment.service import (
    EnrichmentResult,
    EnrichmentService,
    normalize_tag,
)


def _make_llm(content: str, *, usage: TokenUsage | None = None) -> AsyncMock:
    """Build an LLMClient-shaped mock whose ``generate`` returns ``content``."""
    mock = AsyncMock()
    mock.generate = AsyncMock(
        return_value=LLMResponse(content=content, model="test-model", usage=usage),
    )
    return mock


# ---------------------------------------------------------------------------
# normalize_tag
# ---------------------------------------------------------------------------


class TestNormalizeTag:
    def test_spaces_to_hyphens(self):
        assert normalize_tag("hello world") == "hello-world"

    def test_underscores_to_hyphens(self):
        assert normalize_tag("hello_world") == "hello-world"

    def test_special_chars_removed(self):
        assert normalize_tag("hello!@#world") == "helloworld"

    def test_consecutive_hyphens_collapsed(self):
        assert normalize_tag("hello---world") == "hello-world"

    def test_leading_trailing_hyphens_stripped(self):
        assert normalize_tag("-hello-") == "hello"

    def test_mixed_case_lowered(self):
        assert normalize_tag("Hello World") == "hello-world"

    def test_slash_preserved(self):
        assert normalize_tag("lang/python") == "lang/python"

    def test_whitespace_stripped(self):
        assert normalize_tag("  spaced  ") == "spaced"


# ---------------------------------------------------------------------------
# EnrichmentResult model
# ---------------------------------------------------------------------------


class TestEnrichmentResult:
    def test_defaults(self):
        result = EnrichmentResult()
        assert result.auto_tags == []
        assert result.auto_class is None
        assert result.auto_summary is None
        assert result.auto_importance == 0.0
        assert result.usage is None
        assert result.success is True
        assert result.error is None
        # B1: new structured failure_kind field defaults to None on success.
        assert result.failure_kind is None
        assert result.judging_model_id is None

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValueError):
            EnrichmentResult(unexpected_field="boom")

    def test_explicit_values(self):
        result = EnrichmentResult(
            auto_tags=["python", "ai"],
            auto_class="research",
            auto_summary="A summary.",
            auto_importance=0.75,
            tag_confidence=0.9,
            class_confidence=0.85,
        )
        assert result.auto_tags == ["python", "ai"]
        assert result.auto_importance == 0.75

    def test_failure_kind_accepts_extraction_failure_kind_slugs(self):
        """B1: failure_kind must accept the canonical
        ``ExtractionFailureKind`` slugs (closed set defined in
        ``trellis.extract.telemetry``)."""
        # Sample a few of the legal slugs — Literal validation is enforced
        # by pydantic.
        for slug in ("model_error", "parse_error", "batch_collector_error"):
            result = EnrichmentResult(success=False, failure_kind=slug)
            assert result.failure_kind == slug

    def test_failure_kind_rejects_unknown_slug(self):
        """The Literal closed set must reject made-up slugs so downstream
        consumers can rely on the value being one of the documented kinds."""
        with pytest.raises(ValueError):
            EnrichmentResult(success=False, failure_kind="enrichment_failure")


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

VALID_JSON = json.dumps(
    {
        "tags": ["python", "machine learning"],
        "class": "research",
        "summary": "A research paper on ML.",
        "importance": 0.7,
        "tag_confidence": 0.9,
        "class_confidence": 0.85,
    }
)


class TestParseResponse:
    @pytest.fixture
    def service(self):
        return EnrichmentService(llm=_make_llm(VALID_JSON))

    def test_valid_json(self, service):
        result = service._parse_response(VALID_JSON)
        assert result.success is True
        assert result.auto_tags == ["python", "machine-learning"]
        assert result.auto_class == "research"
        assert result.auto_summary == "A research paper on ML."
        assert result.auto_importance == 0.7

    def test_json_in_code_fence(self, service):
        fenced = f"```json\n{VALID_JSON}\n```"
        result = service._parse_response(fenced)
        assert result.success is True
        assert result.auto_class == "research"

    def test_json_in_surrounding_text(self, service):
        text = f"Here is the result:\n{VALID_JSON}\nDone."
        result = service._parse_response(text)
        assert result.success is True
        assert result.auto_class == "research"

    def test_invalid_json_error(self, service):
        result = service._parse_response("not json at all")
        assert result.success is False
        assert result.error is not None
        assert "No JSON found" in result.error

    def test_invalid_classification_set_to_none(self, service):
        data = {
            "tags": ["python"],
            "class": "nonexistent-class",
            "summary": "A summary.",
            "importance": 0.5,
        }
        result = service._parse_response(json.dumps(data))
        assert result.auto_class is None

    def test_importance_clamped(self, service):
        data = {
            "tags": [],
            "class": "notes",
            "summary": "test",
            "importance": 5.0,
        }
        result = service._parse_response(json.dumps(data))
        assert result.auto_importance == 1.0

    def test_importance_clamped_negative(self, service):
        data = {
            "tags": [],
            "class": "notes",
            "summary": "test",
            "importance": -1.0,
        }
        result = service._parse_response(json.dumps(data))
        assert result.auto_importance == 0.0

    def test_null_summary_normalised(self, service):
        data = {
            "tags": [],
            "class": "notes",
            "summary": "null",
            "importance": 0.5,
        }
        result = service._parse_response(json.dumps(data))
        assert result.auto_summary is None


# ---------------------------------------------------------------------------
# enrich (async)
# ---------------------------------------------------------------------------


class TestEnrich:
    async def test_enrich_success(self):
        llm = _make_llm(VALID_JSON)
        service = EnrichmentService(llm=llm)
        result = await service.enrich(
            content="Some content about Python ML.",
            title="ML Paper",
            existing_tags=["ai"],
        )
        assert result.success is True
        assert result.auto_tags == ["python", "machine-learning"]
        assert result.auto_class == "research"
        assert result.raw_response == VALID_JSON
        assert result.judging_model_id == "test-model"
        llm.generate.assert_awaited_once()

    async def test_resolves_judging_model_from_client_default(self):
        llm = _make_llm(VALID_JSON)
        llm.generate.return_value = LLMResponse(content=VALID_JSON, model=None)
        llm._default_model = "configured-default"
        service = EnrichmentService(llm=llm)

        result = await service.enrich(content="hello")

        assert result.success is True
        assert result.judging_model_id == "configured-default"

    async def test_missing_model_identity_degrades_loudly(
        self, event_log: SQLiteEventLog
    ) -> None:
        llm = _make_llm(VALID_JSON)
        llm.generate.return_value = LLMResponse(content=VALID_JSON, model=None)
        service = EnrichmentService(llm=llm, event_log=event_log)

        result = await service.enrich(content="hello")

        assert result.success is False
        assert result.failure_kind == "model_identity_missing"
        assert result.judging_model_id is None
        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(events) == 1
        assert events[0].payload["failure_kind"] == "model_identity_missing"

    async def test_enrich_surfaces_usage(self):
        usage = TokenUsage(prompt_tokens=120, completion_tokens=40, total_tokens=160)
        llm = _make_llm(VALID_JSON, usage=usage)
        service = EnrichmentService(llm=llm)
        result = await service.enrich(content="hello")
        assert result.usage == usage

    async def test_enrich_passes_messages(self):
        llm = _make_llm(VALID_JSON)
        service = EnrichmentService(llm=llm, max_content_length=20)
        await service.enrich(content="content body", title="T")
        call_kwargs = llm.generate.call_args.kwargs
        messages = call_kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        assert "content body" in messages[1].content

    async def test_enrich_llm_error(self):
        class BrokenLLM:
            async def generate(
                self,
                *,
                messages: list[Message],
                temperature: float = 0.3,
                max_tokens: int = 500,
                model: str | None = None,
            ) -> LLMResponse:
                msg = "LLM down"
                raise RuntimeError(msg)

        service = EnrichmentService(llm=BrokenLLM())
        result = await service.enrich(content="test content")
        assert result.success is False
        assert "LLM down" in result.error

    async def test_enrich_truncates_long_content(self):
        """An oversize cut is marked with size + reason, never silent (#310)."""
        llm = _make_llm(VALID_JSON)
        service = EnrichmentService(llm=llm, max_content_length=10)
        await service.enrich(content="A" * 100)
        user_content = llm.generate.call_args.kwargs["messages"][1].content
        assert (
            '<elided chars="90" original_size_chars="100" reason="oversize" />'
            in user_content
        )
        assert "A" * 10 in user_content
        assert "A" * 11 not in user_content

    async def test_enrich_stamps_importance_scored_at_when_score_set(self):
        """Greenfield writer contract (adr-importance-score-freshness §3.5):
        when ``auto_importance > 0`` is written, ``importance_scored_at``
        must be stamped at the same site so the read-path guardrail can
        age the score."""
        llm = _make_llm(VALID_JSON)  # importance: 0.7 in VALID_JSON
        service = EnrichmentService(llm=llm)
        result = await service.enrich(content="something")
        assert result.success is True
        assert result.auto_importance == 0.7
        assert result.importance_scored_at is not None
        # Should be tz-aware UTC datetime.
        assert result.importance_scored_at.tzinfo is not None

    async def test_enrich_no_stamp_when_importance_zero(self):
        """Zero importance => no score to age => no stamp required."""
        zero_json = json.dumps(
            {
                "tags": [],
                "class": "notes",
                "summary": "x",
                "importance": 0.0,
            }
        )
        llm = _make_llm(zero_json)
        service = EnrichmentService(llm=llm)
        result = await service.enrich(content="hello")
        assert result.auto_importance == 0.0
        assert result.importance_scored_at is None

    async def test_enrich_no_stamp_on_failure_path(self):
        """Failure paths return EnrichmentResult(success=False) and never
        touch ``auto_importance`` — stamp must remain None."""

        class BrokenLLM:
            async def generate(
                self,
                *,
                messages: list[Message],
                temperature: float = 0.3,
                max_tokens: int = 500,
                model: str | None = None,
            ) -> LLMResponse:
                msg = "LLM down"
                raise RuntimeError(msg)

        service = EnrichmentService(llm=BrokenLLM())
        result = await service.enrich(content="hello")
        assert result.success is False
        assert result.importance_scored_at is None


# ---------------------------------------------------------------------------
# batch_enrich
# ---------------------------------------------------------------------------


class TestBatchEnrich:
    async def test_batch_enrich_multiple(self):
        llm = _make_llm(VALID_JSON)
        service = EnrichmentService(llm=llm)
        items = [
            {"content": "Item 1", "title": "T1"},
            {"content": "Item 2", "title": "T2"},
            {"content": "Item 3", "title": "T3"},
        ]
        results = await service.batch_enrich(items, concurrency=2)
        assert len(results) == 3
        assert all(r.success for r in results)
        assert llm.generate.await_count == 3

    async def test_batch_enrich_with_error(self):
        call_count = 0

        class FlakyLLM:
            async def generate(
                self,
                *,
                messages: list[Message],
                temperature: float = 0.3,
                max_tokens: int = 500,
                model: str | None = None,
            ) -> LLMResponse:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    msg = "boom"
                    raise RuntimeError(msg)
                return LLMResponse(content=VALID_JSON, model="test-model")

        service = EnrichmentService(llm=FlakyLLM())
        items = [
            {"content": "ok1", "title": "T1"},
            {"content": "fail", "title": "T2"},
            {"content": "ok2", "title": "T3"},
        ]
        results = await service.batch_enrich(items)
        assert results[0].success is True
        assert results[1].success is False
        assert results[2].success is True


# ---------------------------------------------------------------------------
# Failure-injection: EXTRACTION_FAILED emission (post-C1.5 cleanup)
# ---------------------------------------------------------------------------


@pytest.fixture
def event_log(tmp_path: Any) -> SQLiteEventLog:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log  # type: ignore[misc]
    log.close()


@pytest.fixture(autouse=True)
def _bypass_extraction_failure_sampling():
    """Disable sampling so per-call assertions are deterministic.

    The telemetry helper samples emitted events by default (LRU-capped
    per ``(extractor_id, prompt_hash, failure_kind)`` cluster); tests
    need every call to fire a full event.
    """
    from trellis.extract.telemetry import reset_extraction_failure_state

    reset_extraction_failure_state()
    os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
    try:
        yield
    finally:
        os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)
        reset_extraction_failure_state()


class TestEnrichmentFailureTelemetry:
    """ADR-extraction-failure-telemetry / post-C1.5 cleanup:

    The three previously-silent failure sites in ``EnrichmentService``
    now emit ``EXTRACTION_FAILED`` events before returning a degraded
    ``EnrichmentResult(success=False, ...)``. This keeps the documented
    graceful-degradation contract that ``LLMFacetClassifier`` relies on
    while making the failure visible to the failure-telemetry analyzer.
    """

    async def test_broad_except_emits_extraction_failed(
        self, event_log: SQLiteEventLog
    ) -> None:
        """``enrich``'s top-level ``except Exception`` path emits."""

        class BrokenLLM:
            async def generate(
                self,
                *,
                messages: list[Message],
                temperature: float = 0.3,
                max_tokens: int = 500,
                model: str | None = None,
            ) -> LLMResponse:
                msg = "LLM unreachable"
                raise RuntimeError(msg)

        service = EnrichmentService(llm=BrokenLLM(), event_log=event_log)
        result = await service.enrich(content="some content", title="t")

        # Graceful-degradation contract preserved.
        assert result.success is False
        assert "LLM unreachable" in result.error
        # B1: structured failure_kind mirrors the emitted event slug.
        assert result.failure_kind == "model_error"

        # And the failure is no longer silent.
        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["extractor_id"] == "EnrichmentService"
        assert payload["extractor_tier"] == "llm"
        assert payload["failure_kind"] == "model_error"
        assert payload["source_hint"] == "enrichment"
        assert payload["error_class"] == "RuntimeError"
        assert "LLM unreachable" in payload["error_excerpt"]
        # Source-excerpt hash present so the analyzer can cluster.
        assert payload["source_excerpt_hash"] is not None

    async def test_parse_no_json_emits_extraction_failed(
        self, event_log: SQLiteEventLog
    ) -> None:
        """``_parse_response`` "no JSON found" branch emits."""
        # LLM returns plain text — no ``{`` anywhere => "No JSON found" branch.
        llm = _make_llm("not json at all just prose")
        service = EnrichmentService(llm=llm, event_log=event_log)
        result = await service.enrich(content="x")

        assert result.success is False
        assert "No JSON found" in result.error
        # B1: structured failure_kind mirrors the emitted event slug.
        assert result.failure_kind == "parse_error"

        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["extractor_id"] == "EnrichmentService"
        assert payload["failure_kind"] == "parse_error"
        assert payload["error_class"] == "JSONDecodeError"

    async def test_parse_brace_substring_invalid_emits_extraction_failed(
        self, event_log: SQLiteEventLog
    ) -> None:
        """``_parse_response`` inner ``json.loads`` failure path emits.

        A response containing ``{ ... }`` matches the regex but is still
        malformed JSON — this hits the inner ``except json.JSONDecodeError``
        on line ~259 (the sibling case the C1.5 audit called out).
        """
        # Outer json.loads fails; the {...} regex matches the broken brace
        # block; inner json.loads also fails => second emit site.
        llm = _make_llm("preface {tags: bad, no_quotes: true} suffix")
        service = EnrichmentService(llm=llm, event_log=event_log)
        result = await service.enrich(content="x")

        assert result.success is False
        assert "Invalid JSON" in result.error
        # B1: structured failure_kind mirrors the emitted event slug.
        assert result.failure_kind == "parse_error"
        # A salvage attempt that failed is a failure, never a salvage (#514).
        assert result.parse_salvaged is False
        assert (
            event_log.get_events(event_type=EventType.EXTRACTION_PARSE_SALVAGED) == []
        )

        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["extractor_id"] == "EnrichmentService"
        assert payload["failure_kind"] == "parse_error"
        assert payload["error_class"] == "JSONDecodeError"

    async def test_no_event_log_is_a_noop(self) -> None:
        """Without ``event_log`` wired, behavior is unchanged.

        Existing callers that don't pass an event_log must still get the
        same ``EnrichmentResult(success=False, ...)`` contract — the
        emit helper is a no-op in that case.
        """

        class BrokenLLM:
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

        service = EnrichmentService(llm=BrokenLLM())  # no event_log
        result = await service.enrich(content="x")
        assert result.success is False
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# batch_enrich — gather-collector telemetry
# ---------------------------------------------------------------------------


class TestBatchEnrichCollectorTelemetry:
    """When ``asyncio.gather`` returns a raw Exception for a task (i.e. a
    failure escaped ``enrich``'s broad-except), ``batch_enrich`` must emit
    an ``EXTRACTION_FAILED`` event with ``failure_kind="batch_collector_error"``
    and the exception type + message in the payload.

    These failures used to vanish into the list-comprehension that wrapped
    raw exceptions into ``EnrichmentResult(success=False, error=str(r))``
    without any telemetry — invisible to downstream analyzers.
    """

    async def test_collector_exception_emits_batch_collector_error(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from trellis.extract.telemetry import reset_extraction_failure_state
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        reset_extraction_failure_state()
        # Disable sampling so we get the full event without LRU state
        # leaking across test ordering.
        monkeypatch.setenv("EXTRACTION_FAILURE_NO_SAMPLE", "1")
        try:
            log = SQLiteEventLog(Path(tmp_path) / "events.db")
            service = EnrichmentService(
                llm=_make_llm(VALID_JSON),
                event_log=log,
                model="test-model",
            )

            # Patch ``enrich`` to raise — simulating an exception that
            # bubbles past ``_with_sem``'s try/except (the broad-except in
            # ``enrich`` is gone; e.g. CancelledError, semaphore poisoning,
            # asyncio internals). ``return_exceptions=True`` on the
            # surrounding ``gather`` turns this into a raw Exception value
            # in the results list.
            async def _boom(*_args, **_kwargs):
                msg = "collector escape"
                raise RuntimeError(msg)

            service.enrich = _boom  # type: ignore[method-assign]

            results = await service.batch_enrich(
                [{"content": "anything", "title": "T"}]
            )
            assert len(results) == 1
            assert results[0].success is False
            assert "collector escape" in (results[0].error or "")
            # B1: structured failure_kind also surfaces on the per-item
            # result, not just on the emitted event.
            assert results[0].failure_kind == "batch_collector_error"

            events = log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["failure_kind"] == "batch_collector_error"
            assert payload["extractor_id"] == "EnrichmentService.batch_enrich"
            assert payload["extractor_tier"] == "llm"
            assert payload["error_class"] == "RuntimeError"
            assert "collector escape" in payload["error_excerpt"]
            assert payload["model"] == "test-model"
            # No item_id/correlation_id on the input => source_hint=None
            # (pre-B2 behavior preserved when caller doesn't opt in).
            assert payload["source_hint"] is None
            assert payload["correlation_id"] is None
            log.close()
        finally:
            reset_extraction_failure_state()

    async def test_collector_exception_without_event_log_is_silent(self):
        """No event_log => no event. Matches the optional-event-log pattern
        used across the codebase: emit is a no-op rather than a crash."""

        service = EnrichmentService(llm=_make_llm(VALID_JSON))
        # No event_log wired — emit_extraction_failure should be a no-op.

        async def _boom(*_args, **_kwargs):
            msg = "unwired"
            raise RuntimeError(msg)

        service.enrich = _boom  # type: ignore[method-assign]
        results = await service.batch_enrich([{"content": "x"}])
        assert results[0].success is False
        assert "unwired" in (results[0].error or "")
        # B1: failure_kind populated on the result even when the emit is
        # a no-op — downstream consumers can still branch on it.
        assert results[0].failure_kind == "batch_collector_error"

    async def test_collector_exception_forwards_item_id_as_source_hint(
        self, tmp_path, monkeypatch
    ):
        """B2: when the caller supplies ``"item_id"`` on a batch item, the
        gather-collector emit must flow it through as ``source_hint`` so
        downstream clustering can bucket the failure instead of skipping
        it (see ``trellis_workers.code_authoring.clustering`` ~158-164).
        """
        from pathlib import Path

        from trellis.extract.telemetry import reset_extraction_failure_state
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        reset_extraction_failure_state()
        monkeypatch.setenv("EXTRACTION_FAILURE_NO_SAMPLE", "1")
        try:
            log = SQLiteEventLog(Path(tmp_path) / "events.db")
            service = EnrichmentService(
                llm=_make_llm(VALID_JSON),
                event_log=log,
                model="test-model",
            )

            async def _boom(*_args, **_kwargs):
                msg = "escape"
                raise RuntimeError(msg)

            service.enrich = _boom  # type: ignore[method-assign]

            results = await service.batch_enrich(
                [
                    {"content": "a", "item_id": "doc:42"},
                    {"content": "b", "item_id": "doc:43"},
                ],
            )
            assert len(results) == 2
            assert all(r.success is False for r in results)

            events = log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 2
            source_hints = {e.payload["source_hint"] for e in events}
            assert source_hints == {"doc:42", "doc:43"}
            log.close()
        finally:
            reset_extraction_failure_state()

    async def test_collector_exception_forwards_correlation_id_when_no_item_id(
        self, tmp_path, monkeypatch
    ):
        """B2: ``"correlation_id"`` is used as the fallback for ``source_hint``
        when ``"item_id"`` is absent, and is also forwarded as the event's
        own ``correlation_id`` payload field."""
        from pathlib import Path

        from trellis.extract.telemetry import reset_extraction_failure_state
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        reset_extraction_failure_state()
        monkeypatch.setenv("EXTRACTION_FAILURE_NO_SAMPLE", "1")
        try:
            log = SQLiteEventLog(Path(tmp_path) / "events.db")
            service = EnrichmentService(
                llm=_make_llm(VALID_JSON),
                event_log=log,
                model="test-model",
            )

            async def _boom(*_args, **_kwargs):
                msg = "escape"
                raise RuntimeError(msg)

            service.enrich = _boom  # type: ignore[method-assign]

            results = await service.batch_enrich(
                [{"content": "x", "correlation_id": "trace-7"}],
            )
            assert results[0].success is False

            events = log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["source_hint"] == "trace-7"
            assert payload["correlation_id"] == "trace-7"
            log.close()
        finally:
            reset_extraction_failure_state()

    async def test_collector_exception_item_id_wins_over_correlation_id(
        self, tmp_path, monkeypatch
    ):
        """B2: when both are present, ``item_id`` takes precedence for
        ``source_hint`` and ``correlation_id`` still flows to its own
        payload field (the analyzer can use either)."""
        from pathlib import Path

        from trellis.extract.telemetry import reset_extraction_failure_state
        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        reset_extraction_failure_state()
        monkeypatch.setenv("EXTRACTION_FAILURE_NO_SAMPLE", "1")
        try:
            log = SQLiteEventLog(Path(tmp_path) / "events.db")
            service = EnrichmentService(
                llm=_make_llm(VALID_JSON),
                event_log=log,
                model="test-model",
            )

            async def _boom(*_args, **_kwargs):
                msg = "escape"
                raise RuntimeError(msg)

            service.enrich = _boom  # type: ignore[method-assign]

            results = await service.batch_enrich(
                [
                    {
                        "content": "x",
                        "item_id": "doc:99",
                        "correlation_id": "trace-99",
                    }
                ],
            )
            assert results[0].success is False

            events = log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["source_hint"] == "doc:99"
            assert payload["correlation_id"] == "trace-99"
            log.close()
        finally:
            reset_extraction_failure_state()


# ---------------------------------------------------------------------------
# Parse-salvage count (#514): a response rescued by the brace regex is
# counted, so ``extraction.failed`` stops being a floor with no ceiling.
# ---------------------------------------------------------------------------

#: Synthetic payload carrying sentinels, so a payload that leaked any part of
#: the response (prose wrapper *or* the parsed memory content) is detectable.
_SALVAGE_BODY = json.dumps(
    {
        "tags": ["sentinel-tag-alpha", "beta"],
        "class": "research",
        "summary": "SENTINEL_SUMMARY_TEXT",
        "importance": 0.6,
        "tag_confidence": 0.4,
        "class_confidence": 0.3,
    }
)
_SALVAGE_SENTINELS = ("SENTINEL_PREFACE", "sentinel-tag-alpha", "SENTINEL_SUMMARY")

#: Mixed shapes, deliberately not a population of one. ``salvaged`` is the
#: expected ``salvage_kind`` (``None`` = must not be counted); ``failed`` is
#: whether the response is unrecoverable and must emit ``extraction.failed``.
_ENRICHMENT_SHAPES = [
    pytest.param(_SALVAGE_BODY, None, False, id="clean"),
    pytest.param(f"```json\n{_SALVAGE_BODY}\n```", None, False, id="fenced"),
    pytest.param(
        f"SENTINEL_PREFACE here you go:\n{_SALVAGE_BODY}\nHope that helps.",
        "brace_regex",
        False,
        id="prose-wrapped",
    ),
    pytest.param(
        f"```json\n{_SALVAGE_BODY}\n```\nSENTINEL_PREFACE trailing note",
        "brace_regex",
        False,
        id="fence-then-prose",
    ),
    pytest.param("SENTINEL_PREFACE no json here", None, True, id="no-braces"),
    pytest.param(
        "SENTINEL_PREFACE {tags: bad, no_quotes: true} tail",
        None,
        True,
        id="broken-braces",
    ),
]


def _comparable(result: EnrichmentResult) -> dict[str, Any]:
    """Result fields that are a function of the response, not the clock."""
    return result.model_dump(exclude={"importance_scored_at"})


class TestParseSalvageCount:
    @pytest.mark.parametrize(("response", "salvaged", "failed"), _ENRICHMENT_SHAPES)
    async def test_only_a_successful_salvage_is_counted(
        self,
        event_log: SQLiteEventLog,
        response: str,
        salvaged: str | None,
        failed: bool,
    ) -> None:
        from trellis.core.hashing import content_hash

        content = f"source document for {len(response)}"
        service = EnrichmentService(llm=_make_llm(response), event_log=event_log)
        result = await service.enrich(content=content)

        assert result.success is (not failed)
        assert result.parse_salvaged is (salvaged is not None)

        salvage_events = event_log.get_events(
            event_type=EventType.EXTRACTION_PARSE_SALVAGED
        )
        failed_events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(failed_events) == (1 if failed else 0)
        if salvaged is None:
            assert salvage_events == []
            return

        assert len(salvage_events) == 1
        payload = salvage_events[0].payload
        assert payload == {
            "extractor_id": "EnrichmentService",
            "extractor_tier": "llm",
            "salvage_kind": salvaged,
            "source_hint": "enrichment",
            "prompt_hash": None,
            "source_excerpt_hash": content_hash(content),
            "model": "test-model",
            "raw_length": len(response),
        }
        # Digests and lengths only: nothing of the response reaches the log.
        serialized = json.dumps(payload)
        for sentinel in _SALVAGE_SENTINELS:
            assert sentinel not in serialized

    async def test_salvage_changes_nothing_about_the_parse(
        self, event_log: SQLiteEventLog
    ) -> None:
        """Counting is observation only: the rescued result is the clean one.

        The same body served clean, and served wrapped in prose, must yield the
        same enrichment apart from the two fields that describe the transport.
        """
        wrapped = f"SENTINEL_PREFACE here you go:\n{_SALVAGE_BODY}\nDone."
        clean = await EnrichmentService(llm=_make_llm(_SALVAGE_BODY)).enrich(
            content="c"
        )
        rescued = await EnrichmentService(
            llm=_make_llm(wrapped), event_log=event_log
        ).enrich(content="c")

        transport = {"raw_response", "parse_salvaged"}
        assert clean.parse_salvaged is False
        assert rescued.parse_salvaged is True
        assert rescued.auto_tags == ["sentinel-tag-alpha", "beta"]
        assert {
            k: v for k, v in _comparable(rescued).items() if k not in transport
        } == {k: v for k, v in _comparable(clean).items() if k not in transport}

    @pytest.mark.parametrize(
        "response",
        [
            f"SENTINEL_PREFACE here you go:\n{_SALVAGE_BODY}\nDone.",
            f"```json\n{_SALVAGE_BODY}\n```\ntrailing",
        ],
        ids=["prose-wrapped", "fence-then-prose"],
    )
    async def test_event_log_wiring_does_not_change_the_result(
        self, event_log: SQLiteEventLog, response: str
    ) -> None:
        with_log = await EnrichmentService(
            llm=_make_llm(response), event_log=event_log
        ).enrich(content="c")
        without_log = await EnrichmentService(llm=_make_llm(response)).enrich(
            content="c"
        )
        assert with_log.parse_salvaged is True
        assert _comparable(with_log) == _comparable(without_log)
        assert (
            len(event_log.get_events(event_type=EventType.EXTRACTION_PARSE_SALVAGED))
            == 1
        )

    async def test_broken_event_log_does_not_fail_a_salvaged_parse(self) -> None:
        """Fail-soft: the count must never turn a rescued parse into a failure."""

        class ExplodingLog:
            def emit(self, *_args: Any, **_kwargs: Any) -> Any:
                msg = "event log down"
                raise RuntimeError(msg)

        response = f"SENTINEL_PREFACE:\n{_SALVAGE_BODY}\nDone."
        service = EnrichmentService(
            llm=_make_llm(response),
            event_log=ExplodingLog(),  # type: ignore[arg-type]
        )
        result = await service.enrich(content="c")
        assert result.success is True
        assert result.parse_salvaged is True
        assert result.auto_class == "research"


# ---------------------------------------------------------------------------
# A response that parses as JSON but is not the object the prompt asks for.
# Every such shape must come back as a failed result: an exception here
# escapes ``enrich`` (its broad catch covers only the model call), so the
# classifier raised instead of degrading and ``batch_enrich`` filed the item
# under ``batch_collector_error``.
# ---------------------------------------------------------------------------

#: Every JSON type that is not an object, plus the fenced form the clean path
#: unwraps. The second element is the type the failure must name.
_NON_OBJECT_RESPONSES = [
    pytest.param("[]", "list", id="empty-array"),
    pytest.param(
        '[{"tags": ["alpha"], "class": "research"}]', "list", id="array-of-object"
    ),
    pytest.param("null", "NoneType", id="null"),
    pytest.param('"a bare string"', "str", id="string"),
    pytest.param("42", "int", id="integer"),
    pytest.param("0.25", "float", id="float"),
    pytest.param("true", "bool", id="boolean"),
    pytest.param('```json\n["alpha", "beta"]\n```', "list", id="fenced-array"),
]


class _ByContentLLM:
    """Answer each prompt with the response keyed by a marker in its content.

    ``batch_enrich`` runs items concurrently, so call order is not item order.
    """

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        prompt = messages[-1].content
        matches = [text for marker, text in self._responses.items() if marker in prompt]
        assert len(matches) == 1, prompt
        return LLMResponse(content=matches[0], model="test-model")


class TestNonObjectResponse:
    @pytest.mark.parametrize(("response", "type_name"), _NON_OBJECT_RESPONSES)
    async def test_enrich_returns_a_parse_error_instead_of_raising(
        self, event_log: SQLiteEventLog, response: str, type_name: str
    ) -> None:
        from trellis.core.hashing import content_hash

        service = EnrichmentService(
            llm=_make_llm(response), event_log=event_log, model="model-7"
        )
        result = await service.enrich(content="source document")

        assert result.success is False
        assert result.failure_kind == "parse_error"
        assert result.error == f"Not a JSON object: {type_name}"
        assert result.raw_response == response
        assert result.parse_salvaged is False
        # A failed parse proves no model identity and stamps no score.
        assert result.judging_model_id is None
        assert result.importance_scored_at is None

        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert len(events) == 1
        assert events[0].payload == {
            "extractor_id": "EnrichmentService",
            "extractor_tier": "llm",
            "failure_kind": "parse_error",
            "source_hint": "enrichment",
            "prompt_hash": None,
            # The response digest, as the two malformed-JSON branches record.
            "source_excerpt_hash": content_hash(response),
            "model": "model-7",
            "error_class": "NonObjectResponse",
            "error_excerpt": f"expected a JSON object, got {type_name}",
            "correlation_id": None,
            "cluster_count": 1,
            "sampled": False,
        }
        assert (
            event_log.get_events(event_type=EventType.EXTRACTION_PARSE_SALVAGED) == []
        )

    async def test_batch_enrich_records_the_pipeline_kind_per_item(
        self, event_log: SQLiteEventLog
    ) -> None:
        """A non-object response is a parse failure, not a collector escape."""
        llm = _ByContentLLM(
            {
                "ITEM-ALPHA": VALID_JSON,
                "ITEM-BRAVO": "[]",
                "ITEM-CHARLIE": "null",
                "ITEM-DELTA": _SALVAGE_BODY,
            }
        )
        service = EnrichmentService(llm=llm, event_log=event_log)
        items = [
            {"content": f"body of {marker}", "item_id": f"id-{marker}"}
            for marker in ("ITEM-ALPHA", "ITEM-BRAVO", "ITEM-CHARLIE", "ITEM-DELTA")
        ]
        results = await service.batch_enrich(items, concurrency=2)

        assert [r.success for r in results] == [True, False, False, True]
        assert [r.failure_kind for r in results] == [
            None,
            "parse_error",
            "parse_error",
            None,
        ]
        # Each result is its own item's: the two successes parsed different bodies.
        assert [r.auto_tags for r in results] == [
            ["python", "machine-learning"],
            [],
            [],
            ["sentinel-tag-alpha", "beta"],
        ]

        events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
        assert sorted(
            (e.payload["failure_kind"], e.payload["error_excerpt"]) for e in events
        ) == [
            ("parse_error", "expected a JSON object, got NoneType"),
            ("parse_error", "expected a JSON object, got list"),
        ]
        assert {e.payload["extractor_id"] for e in events} == {"EnrichmentService"}


# ---------------------------------------------------------------------------
# One wrong-typed field degrades that field alone: the same conventions the
# parser already applied to ``tags`` (non-list -> []) and to the confidences
# (unparseable -> None), extended to the fields that raised instead.
# ---------------------------------------------------------------------------

#: Distinct values per field, so no constant satisfies the survival check.
_GOOD_FIELDS: dict[str, Any] = {
    "tags": ["Kappa Topic", "lambda_tag"],
    "class": "architecture",
    "summary": "Summary sentence four.",
    "importance": 0.35,
    "tag_confidence": 0.62,
    "class_confidence": 0.47,
}
_GOOD_PARSED: dict[str, Any] = {
    "auto_tags": ["kappa-topic", "lambda-tag"],
    "auto_class": "architecture",
    "auto_summary": "Summary sentence four.",
    "auto_importance": 0.35,
    "tag_confidence": 0.62,
    "class_confidence": 0.47,
}

#: ``(response key, bad value, result field, degraded value)``. The degraded
#: value is what an absent key already produces: ``0.0`` importance, ``None``
#: class / summary / confidence, ``[]`` tags.
_BAD_FIELDS = [
    pytest.param("importance", "high", "auto_importance", 0.0, id="importance-word"),
    pytest.param("importance", None, "auto_importance", 0.0, id="importance-null"),
    pytest.param("importance", [0.9], "auto_importance", 0.0, id="importance-list"),
    pytest.param(
        "importance", {"score": 0.9}, "auto_importance", 0.0, id="importance-dict"
    ),
    pytest.param(
        "importance", 10**400, "auto_importance", 0.0, id="importance-huge-int"
    ),
    pytest.param("summary", ["one", "two"], "auto_summary", None, id="summary-list"),
    pytest.param("summary", {"text": "x"}, "auto_summary", None, id="summary-dict"),
    pytest.param("summary", 12, "auto_summary", None, id="summary-int"),
    pytest.param("summary", False, "auto_summary", None, id="summary-bool"),
    pytest.param("class", [], "auto_class", None, id="class-empty-list"),
    pytest.param("class", {}, "auto_class", None, id="class-empty-dict"),
    pytest.param("class", 0, "auto_class", None, id="class-zero"),
    pytest.param("class", False, "auto_class", None, id="class-false"),
    pytest.param(
        "tag_confidence", 10**400, "tag_confidence", None, id="tag-conf-huge-int"
    ),
    pytest.param(
        "class_confidence", 10**400, "class_confidence", None, id="class-conf-huge"
    ),
    pytest.param("tags", "not-a-list", "auto_tags", [], id="tags-string"),
]


class TestWrongTypedField:
    @pytest.mark.parametrize(("key", "bad", "field", "degraded"), _BAD_FIELDS)
    async def test_one_bad_field_degrades_alone(
        self,
        event_log: SQLiteEventLog,
        key: str,
        bad: Any,
        field: str,
        degraded: Any,
    ) -> None:
        response = json.dumps({**_GOOD_FIELDS, key: bad})
        service = EnrichmentService(llm=_make_llm(response), event_log=event_log)
        result = await service.enrich(content="c")

        expected = {**_GOOD_PARSED, field: degraded}
        assert result.success is True
        assert result.failure_kind is None
        assert {name: getattr(result, name) for name in _GOOD_PARSED} == expected
        # A degraded importance is no score, so it carries no scored-at stamp.
        assert (result.importance_scored_at is not None) is (
            expected["auto_importance"] > 0
        )
        assert event_log.get_events(event_type=EventType.EXTRACTION_FAILED) == []
