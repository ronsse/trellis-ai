"""Tests for token tracking, usage analysis, and auto-trimming."""

from __future__ import annotations

from datetime import datetime

from trellis.retrieve.formatters import auto_trim_response
from trellis.retrieve.token_tracker import estimate_tokens, track_token_usage
from trellis.retrieve.token_usage import TokenUsageReport, analyze_token_usage
from trellis.stores.base.event_log import Event, EventLog, EventType

# ---------------------------------------------------------------------------
# Fake EventLog for testing
# ---------------------------------------------------------------------------


class _FakeEventLog(EventLog):
    """In-memory event log for unit tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def append(self, event: Event) -> None:
        self.events.append(event)

    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[Event]:
        result = self.events
        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        if since is not None:
            result = [e for e in result if e.occurred_at >= since]
        return result[:limit]

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        return len(self.get_events(event_type=event_type, since=since))

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 1


def test_estimate_tokens_short():
    # 12 chars -> 12//4 + 1 = 4
    assert estimate_tokens("hello world!") == 4


def test_estimate_tokens_longer():
    text = "a" * 100
    assert estimate_tokens(text) == 26  # 100//4 + 1


# ---------------------------------------------------------------------------
# track_token_usage
# ---------------------------------------------------------------------------


def test_track_token_usage_emits_event():
    log = _FakeEventLog()
    track_token_usage(
        log,
        layer="mcp",
        operation="get_context",
        response_tokens=500,
        budget_tokens=2000,
        trimmed=False,
        agent_id="test-agent",
    )

    assert len(log.events) == 1
    event = log.events[0]
    assert event.event_type == EventType.TOKEN_TRACKED
    assert event.source == "mcp:get_context"
    assert event.payload["layer"] == "mcp"
    assert event.payload["operation"] == "get_context"
    assert event.payload["response_tokens"] == 500
    assert event.payload["budget_tokens"] == 2000
    assert event.payload["trimmed"] is False
    assert event.payload["agent_id"] == "test-agent"


def test_track_token_usage_optional_fields():
    log = _FakeEventLog()
    track_token_usage(
        log,
        layer="cli",
        operation="search",
        response_tokens=100,
    )

    event = log.events[0]
    assert event.payload["budget_tokens"] is None
    assert event.payload["agent_id"] is None


# ---------------------------------------------------------------------------
# analyze_token_usage
# ---------------------------------------------------------------------------


def _make_token_event(
    layer: str,
    operation: str,
    response_tokens: int,
    budget_tokens: int | None = None,
) -> Event:
    return Event(
        event_type=EventType.TOKEN_TRACKED,
        source=f"{layer}:{operation}",
        payload={
            "layer": layer,
            "operation": operation,
            "response_tokens": response_tokens,
            "budget_tokens": budget_tokens,
            "trimmed": False,
            "agent_id": None,
        },
    )


def test_analyze_empty():
    log = _FakeEventLog()
    report = analyze_token_usage(log, days=7)
    assert report.total_responses == 0
    assert report.total_tokens == 0
    assert report.avg_tokens_per_response == 0.0
    assert report.by_layer == {}
    assert report.by_operation == []
    assert report.over_budget == []


def test_analyze_with_events():
    log = _FakeEventLog()
    log.events = [
        _make_token_event("mcp", "get_context", 500, 2000),
        _make_token_event("mcp", "get_context", 600, 2000),
        _make_token_event("mcp", "search", 300, 1000),
        _make_token_event("cli", "search", 200, None),
    ]

    report = analyze_token_usage(log, days=30)

    assert report.total_responses == 4
    assert report.total_tokens == 1600
    assert report.avg_tokens_per_response == 400.0

    # Layer breakdown
    assert "mcp" in report.by_layer
    assert report.by_layer["mcp"]["count"] == 3
    assert report.by_layer["mcp"]["total_tokens"] == 1400
    assert "cli" in report.by_layer
    assert report.by_layer["cli"]["count"] == 1

    # Operations sorted by total_tokens descending
    assert len(report.by_operation) >= 2
    assert report.by_operation[0]["operation"] == "get_context"
    assert report.by_operation[0]["total_tokens"] == 1100


def test_analyze_over_budget():
    log = _FakeEventLog()
    log.events = [
        _make_token_event("mcp", "get_context", 2500, 2000),  # over budget
        _make_token_event("mcp", "search", 500, 1000),  # within budget
    ]

    report = analyze_token_usage(log, days=30)
    assert len(report.over_budget) == 1
    assert report.over_budget[0]["operation"] == "get_context"
    assert report.over_budget[0]["response_tokens"] == 2500
    assert report.over_budget[0]["budget_tokens"] == 2000


def test_report_to_dict():
    report = TokenUsageReport(
        total_responses=10,
        total_tokens=5000,
        avg_tokens_per_response=500.0,
        by_layer={"mcp": {"count": 10, "total_tokens": 5000, "avg_tokens": 500.0}},
        by_operation=[],
        over_budget=[],
    )
    d = report.model_dump()
    assert d["total_responses"] == 10
    assert d["total_tokens"] == 5000
    assert d["avg_tokens_per_response"] == 500.0
    assert "mcp" in d["by_layer"]


# ---------------------------------------------------------------------------
# auto_trim_response
# ---------------------------------------------------------------------------


def test_auto_trim_no_trim_needed():
    text = "short text"
    result, trimmed = auto_trim_response(text, max_tokens=100)
    assert result == text
    assert trimmed is False


def test_auto_trim_tail_strategy():
    text = "a" * 1000  # ~251 tokens
    result, trimmed = auto_trim_response(text, max_tokens=50)
    assert trimmed is True
    assert len(result) <= 50 * 4 + 3  # max_chars + "..."
    assert result.endswith("...")


def test_auto_trim_low_relevance_strategy():
    sections = [
        "# Title\n\nIntro text",
        "## Section A\nImportant content here",
        "## Section B\nLess important content",
        "## Section C\nLeast important content",
    ]
    text = "\n".join(sections)

    # Budget enough for title + first section but not all
    result, trimmed = auto_trim_response(text, max_tokens=30, strategy="low_relevance")
    assert trimmed is True
    # Title should be preserved
    assert "Title" in result


def test_auto_trim_low_relevance_single_section():
    text = "## Only section\n" + "x" * 500
    result, trimmed = auto_trim_response(text, max_tokens=20, strategy="low_relevance")
    assert trimmed is True
    # Falls back to truncation
    assert result.endswith("...")
