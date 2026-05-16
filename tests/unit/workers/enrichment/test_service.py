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
        llm.generate.assert_awaited_once()

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
        llm = _make_llm(VALID_JSON)
        service = EnrichmentService(llm=llm, max_content_length=10)
        await service.enrich(content="A" * 100)
        user_content = llm.generate.call_args.kwargs["messages"][1].content
        assert "[Content truncated...]" in user_content

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

            events = log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["failure_kind"] == "batch_collector_error"
            assert payload["extractor_id"] == "EnrichmentService.batch_enrich"
            assert payload["extractor_tier"] == "llm"
            assert payload["error_class"] == "RuntimeError"
            assert "collector escape" in payload["error_excerpt"]
            assert payload["model"] == "test-model"
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
