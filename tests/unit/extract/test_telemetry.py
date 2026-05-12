"""Tests for extractor-fallback telemetry aggregation (Gap 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract.telemetry import (
    ExtractionValidationReport,
    ExtractorFallbackReport,
    analyze_extraction_validation,
    analyze_extractor_fallbacks,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_dispatch(
    log: SQLiteEventLog,
    *,
    source_hint: str | None,
    extractor: str = "stub",
    tier: str = "deterministic",
) -> None:
    log.emit(
        EventType.EXTRACTION_DISPATCHED,
        source="extraction_dispatcher",
        payload={
            "extractor_used": extractor,
            "tier": tier,
            "source_hint": source_hint,
            "entities": 1,
            "edges": 0,
            "llm_calls": 0,
            "tokens_used": 0,
            "overall_confidence": 1.0,
        },
    )


def _emit_fallback(
    log: SQLiteEventLog,
    *,
    source_hint: str | None,
    reason: str,
    chosen_tier: str = "llm",
    skipped_tier: str | None = "deterministic",
    chosen_extractor: str = "llm",
) -> None:
    log.emit(
        EventType.EXTRACTOR_FALLBACK,
        source="extraction_dispatcher",
        payload={
            "source_hint": source_hint,
            "chosen_extractor": chosen_extractor,
            "chosen_tier": chosen_tier,
            "skipped_tier": skipped_tier,
            "reason": reason,
        },
    )


class TestAnalyzeExtractorFallbacks:
    def test_empty_window_returns_note(self, event_log) -> None:
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert isinstance(report, ExtractorFallbackReport)
        assert report.total_dispatches == 0
        assert report.total_fallbacks == 0
        assert report.notes
        assert "No EXTRACTION_DISPATCHED" in report.notes[0]

    def test_counts_dispatches_and_fallbacks(self, event_log) -> None:
        for _ in range(5):
            _emit_dispatch(event_log, source_hint="dbt")
        _emit_fallback(event_log, source_hint="dbt", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert report.total_dispatches == 5
        assert report.total_fallbacks == 1
        assert report.overall_fallback_rate == pytest.approx(0.2)

    def test_reason_counts_and_per_source(self, event_log) -> None:
        for _ in range(4):
            _emit_dispatch(event_log, source_hint="dbt")
            _emit_fallback(event_log, source_hint="dbt", reason="empty_result")
        _emit_dispatch(event_log, source_hint="lineage")
        _emit_fallback(event_log, source_hint="lineage", reason="prefer_tier_override")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert report.reason_counts == {
            "empty_result": 4,
            "prefer_tier_override": 1,
        }
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["dbt"].fallback_rate == pytest.approx(1.0)
        assert by_source["dbt"].reasons == {"empty_result": 4}
        assert by_source["lineage"].reasons == {"prefer_tier_override": 1}

    def test_source_without_dispatch_still_counted(self, event_log) -> None:
        # Fallback for a source that has no matching dispatch event
        _emit_fallback(event_log, source_hint="orphan", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["orphan"].total_dispatches == 0
        assert by_source["orphan"].fallback_events == 1
        assert by_source["orphan"].fallback_rate == 0.0  # divide-by-zero fallback

    def test_none_source_hint_bucketed(self, event_log) -> None:
        _emit_dispatch(event_log, source_hint=None)
        _emit_fallback(event_log, source_hint=None, reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert "<none>" in by_source

    def test_finding_fires_on_high_empty_result_rate(self, event_log) -> None:
        # 12 dispatches with 10 empty_result fallbacks → 83% rate
        for _ in range(12):
            _emit_dispatch(event_log, source_hint="rules-heavy")
        for _ in range(10):
            _emit_fallback(event_log, source_hint="rules-heavy", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert any("rules-heavy" in f and "empty_result" in f for f in report.findings)

    def test_finding_fires_on_high_prefer_tier_override(self, event_log) -> None:
        for _ in range(15):
            _emit_dispatch(event_log, source_hint="manual-override")
        for _ in range(12):
            _emit_fallback(
                event_log,
                source_hint="manual-override",
                reason="prefer_tier_override",
            )
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert any(
            "manual-override" in f and "lower tier" in f for f in report.findings
        )

    def test_finding_suppressed_below_sample_floor(self, event_log) -> None:
        # 5 dispatches, 5 fallbacks → 100% rate but below 10-sample floor
        for _ in range(5):
            _emit_dispatch(event_log, source_hint="sparse")
            _emit_fallback(event_log, source_hint="sparse", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert not any("sparse" in f for f in report.findings)


def _emit_rejected(
    log: SQLiteEventLog,
    *,
    source_hint: str | None,
    extractor_used: str = "rules",
    codes: list[str] | None = None,
) -> None:
    findings = [
        {"validator_name": code, "code": code, "message": "x", "affected": {}}
        for code in (codes or ["empty_result"])
    ]
    log.emit(
        EventType.EXTRACTION_REJECTED,
        source="extraction_dispatcher",
        payload={
            "source_hint": source_hint,
            "extractor_used": extractor_used,
            "findings": findings,
        },
    )


class TestAnalyzeExtractionValidation:
    """ADR §5.6 — analyzer mirrors analyze_extractor_fallbacks shape."""

    def test_empty_window_returns_note(self, event_log) -> None:
        report = analyze_extraction_validation(event_log, days=30)
        assert isinstance(report, ExtractionValidationReport)
        assert report.total_dispatches == 0
        assert report.total_rejected == 0
        assert report.notes
        assert "EXTRACTION_DISPATCHED" in report.notes[0]

    def test_aggregates_per_source_and_per_code(self, event_log) -> None:
        for _ in range(4):
            _emit_dispatch(event_log, source_hint="dbt")
            _emit_rejected(event_log, source_hint="dbt", codes=["empty_result"])
        _emit_dispatch(event_log, source_hint="lineage")
        _emit_rejected(
            event_log,
            source_hint="lineage",
            codes=["missing_generation_spec", "orphan_edge"],
        )
        report = analyze_extraction_validation(event_log, days=30)
        assert report.total_dispatches == 5
        assert report.total_rejected == 5
        assert report.code_counts == {
            "empty_result": 4,
            "missing_generation_spec": 1,
            "orphan_edge": 1,
        }
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["dbt"].rejection_rate == pytest.approx(1.0)
        assert by_source["dbt"].codes == {"empty_result": 4}
        assert by_source["lineage"].codes == {
            "missing_generation_spec": 1,
            "orphan_edge": 1,
        }

    def test_finding_fires_above_threshold(self, event_log) -> None:
        for _ in range(12):
            _emit_dispatch(event_log, source_hint="rules-heavy")
        for _ in range(10):
            _emit_rejected(
                event_log,
                source_hint="rules-heavy",
                codes=["empty_result"],
            )
        report = analyze_extraction_validation(event_log, days=30)
        assert any("rules-heavy" in f and "empty_result" in f for f in report.findings)

    def test_finding_suppressed_below_sample_floor(self, event_log) -> None:
        for _ in range(5):
            _emit_dispatch(event_log, source_hint="sparse")
            _emit_rejected(event_log, source_hint="sparse", codes=["empty_result"])
        report = analyze_extraction_validation(event_log, days=30)
        assert not any("sparse" in f for f in report.findings)

    def test_extractor_breakdown_per_source(self, event_log) -> None:
        for _ in range(2):
            _emit_dispatch(event_log, source_hint="dbt")
        _emit_rejected(event_log, source_hint="dbt", extractor_used="rules")
        _emit_rejected(event_log, source_hint="dbt", extractor_used="llm")
        report = analyze_extraction_validation(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["dbt"].extractors == {"rules": 1, "llm": 1}

    def test_none_source_hint_bucketed(self, event_log) -> None:
        _emit_dispatch(event_log, source_hint=None)
        _emit_rejected(event_log, source_hint=None)
        report = analyze_extraction_validation(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert "<none>" in by_source


# ---------------------------------------------------------------------------
# Phase 0 — emit_extraction_failure helper.
# See docs/design/adr-extraction-failure-telemetry.md
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_extraction_failure_state():
    """Per-test isolation for the process-local sampling LRU.

    Without this, sampling-cap state leaks between tests in the same
    process and breaks deterministic assertions.
    """
    from trellis.extract.telemetry import reset_extraction_failure_state

    reset_extraction_failure_state()
    yield
    reset_extraction_failure_state()


class TestEmitExtractionFailure:
    """Tests for :func:`emit_extraction_failure` — Phase 0 helper."""

    def test_emit_shape_records_full_payload(self, event_log) -> None:
        """The default emit records every schema field on the event."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        try:
            emit_extraction_failure(
                event_log=event_log,
                extractor_id="LLMExtractor",
                extractor_tier="llm",
                failure_kind="parse_error",
                source_hint="freetext",
                prompt_hash="ph-1",
                source_excerpt_hash="seh-1",
                model="m-1",
                error_class="JSONDecodeError",
                error_excerpt="Expecting value: line 1 column 1 (char 0)",
                correlation_id="corr-1",
            )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 1
            payload = events[0].payload
            assert payload["extractor_id"] == "LLMExtractor"
            assert payload["extractor_tier"] == "llm"
            assert payload["failure_kind"] == "parse_error"
            assert payload["source_hint"] == "freetext"
            assert payload["prompt_hash"] == "ph-1"
            assert payload["source_excerpt_hash"] == "seh-1"
            assert payload["model"] == "m-1"
            assert payload["error_class"] == "JSONDecodeError"
            assert payload["error_excerpt"] == (
                "Expecting value: line 1 column 1 (char 0)"
            )
            assert payload["correlation_id"] == "corr-1"
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)

    def test_emit_is_noop_when_event_log_is_none(self) -> None:
        """``event_log=None`` is a documented no-op — extractors don't
        have to special-case "wired vs. unwired"."""
        from trellis.extract.telemetry import emit_extraction_failure

        # Should not raise.
        emit_extraction_failure(
            event_log=None,
            extractor_id="X",
            extractor_tier="llm",
            failure_kind="parse_error",
            error_class="JSONDecodeError",
            error_excerpt="boom",
        )

    def test_sampling_cap_applies_per_cluster(self, event_log) -> None:
        """First ``cap`` events emit in full; subsequent emit aggregate-only."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_SAMPLE_CAP"] = "3"
        try:
            for _ in range(5):
                emit_extraction_failure(
                    event_log=event_log,
                    extractor_id="LLMExtractor",
                    extractor_tier="llm",
                    failure_kind="parse_error",
                    prompt_hash="same-prompt",
                    error_class="JSONDecodeError",
                    error_excerpt="boom",
                )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            # All 5 are recorded (POC: aggregate-only updates still
            # append a small event so the count is queryable) but only
            # the first 3 have sampled=False.
            assert len(events) == 5
            sampled_flags = sorted(e.payload["sampled"] for e in events)
            assert sampled_flags == [False, False, False, True, True]
        finally:
            os.environ.pop("EXTRACTION_FAILURE_SAMPLE_CAP", None)

    def test_sampling_keys_are_per_cluster(self, event_log) -> None:
        """Different ``(extractor_id, prompt_hash, failure_kind)``
        triples are independent clusters — one hitting cap does not
        suppress another."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_SAMPLE_CAP"] = "1"
        try:
            # First cluster — emit twice (1 full + 1 sampled).
            for _ in range(2):
                emit_extraction_failure(
                    event_log=event_log,
                    extractor_id="A",
                    extractor_tier="llm",
                    failure_kind="parse_error",
                    prompt_hash="p1",
                    error_class="X",
                    error_excerpt="e",
                )
            # Different cluster — first emit should be full.
            emit_extraction_failure(
                event_log=event_log,
                extractor_id="B",
                extractor_tier="llm",
                failure_kind="parse_error",
                prompt_hash="p1",
                error_class="X",
                error_excerpt="e",
            )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            by_extractor = {
                (e.payload["extractor_id"], e.payload["sampled"]) for e in events
            }
            assert ("A", False) in by_extractor
            assert ("A", True) in by_extractor
            assert ("B", False) in by_extractor
        finally:
            os.environ.pop("EXTRACTION_FAILURE_SAMPLE_CAP", None)

    def test_no_sample_env_bypasses_sampling(self, event_log) -> None:
        """``EXTRACTION_FAILURE_NO_SAMPLE=1`` keeps every event in full."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        os.environ["EXTRACTION_FAILURE_SAMPLE_CAP"] = "1"
        try:
            for _ in range(5):
                emit_extraction_failure(
                    event_log=event_log,
                    extractor_id="LLMExtractor",
                    extractor_tier="llm",
                    failure_kind="parse_error",
                    prompt_hash="same",
                    error_class="X",
                    error_excerpt="e",
                )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events) == 5
            assert all(e.payload["sampled"] is False for e in events)
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)
            os.environ.pop("EXTRACTION_FAILURE_SAMPLE_CAP", None)

    def test_redaction_email_uuid_ssn(self, event_log) -> None:
        """Conservative redactor covers POC seed patterns."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        try:
            excerpt = (
                "user alice@example.com triggered failure "
                "session=550e8400-e29b-41d4-a716-446655440000 "
                "ssn=123-45-6789 boom"
            )
            emit_extraction_failure(
                event_log=event_log,
                extractor_id="X",
                extractor_tier="llm",
                failure_kind="parse_error",
                error_class="X",
                error_excerpt=excerpt,
            )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            redacted = events[0].payload["error_excerpt"]
            assert "alice@example.com" not in redacted
            assert "550e8400-e29b-41d4-a716-446655440000" not in redacted
            assert "123-45-6789" not in redacted
            assert "[REDACTED_EMAIL]" in redacted
            assert "[REDACTED_UUID]" in redacted
            assert "[REDACTED_SSN]" in redacted
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)

    def test_error_excerpt_bounded_at_200_chars(self, event_log) -> None:
        """The 200-char cap holds even after redaction expands text."""
        import os

        from trellis.extract.telemetry import emit_extraction_failure

        os.environ["EXTRACTION_FAILURE_NO_SAMPLE"] = "1"
        try:
            # A long excerpt with several emails — redaction replacement
            # is bounded-length but the raw input could grow; re-cap is
            # what makes the bound hold.
            long_excerpt = "x@y.z " * 100  # 600 chars of mostly emails
            emit_extraction_failure(
                event_log=event_log,
                extractor_id="X",
                extractor_tier="llm",
                failure_kind="parse_error",
                error_class="X",
                error_excerpt=long_excerpt,
            )
            events = event_log.get_events(event_type=EventType.EXTRACTION_FAILED)
            assert len(events[0].payload["error_excerpt"]) <= 200
        finally:
            os.environ.pop("EXTRACTION_FAILURE_NO_SAMPLE", None)

    def test_invalid_sample_cap_raises_on_load(self) -> None:
        """POC directive: misconfigured cap fails loud."""
        import os

        from trellis.extract.telemetry import _load_sample_cap

        os.environ["EXTRACTION_FAILURE_SAMPLE_CAP"] = "not-a-number"
        try:
            with pytest.raises(ValueError, match="non-negative integer"):
                _load_sample_cap()
        finally:
            os.environ.pop("EXTRACTION_FAILURE_SAMPLE_CAP", None)

        os.environ["EXTRACTION_FAILURE_SAMPLE_CAP"] = "-1"
        try:
            with pytest.raises(ValueError, match="non-negative integer"):
                _load_sample_cap()
        finally:
            os.environ.pop("EXTRACTION_FAILURE_SAMPLE_CAP", None)
