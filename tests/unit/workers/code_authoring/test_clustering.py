"""Tests for :mod:`trellis_workers.code_authoring.clustering`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trellis.stores.base.event_log import Event, EventType
from trellis_workers.code_authoring.clustering import (
    Cluster,
    cluster_failures,
    compute_cluster_signature,
)


def _make_failure_event(
    *,
    source_file: str = "src/trellis/extract/llm.py",
    failure_class: str = "parse_error",
    occurred_at: datetime | None = None,
    extractor_id: str = "LLMExtractor",
    payload_override: dict | None = None,
) -> Event:
    """Build a synthetic ``EXTRACTION_FAILED`` event for clustering tests.

    Mirrors the payload shape that
    :func:`trellis.extract.telemetry.emit_extraction_failure` writes — we
    don't go through the helper because that would couple every test to
    the helper's sampling LRU.
    """
    occurred = occurred_at or datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    payload: dict = {
        "extractor_id": extractor_id,
        "extractor_tier": "llm",
        "failure_kind": failure_class,
        "source_hint": source_file,
        "prompt_hash": "h" * 16,
        "error_class": "ValueError",
        "error_excerpt": "bad json",
    }
    if payload_override:
        payload.update(payload_override)
    return Event(
        event_type=EventType.EXTRACTION_FAILED,
        source="extraction_failure_helper",
        occurred_at=occurred,
        recorded_at=occurred,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# compute_cluster_signature
# ---------------------------------------------------------------------------


def test_signature_is_stable_for_same_key() -> None:
    """Same ``(file, class)`` → same hex digest across calls / runs."""
    sig1 = compute_cluster_signature("src/foo.py", "parse_error")
    sig2 = compute_cluster_signature("src/foo.py", "parse_error")
    assert sig1 == sig2
    # Sanity: hex digest of SHA-256 → 64 chars.
    assert len(sig1) == 64
    assert all(c in "0123456789abcdef" for c in sig1)


def test_signature_differs_for_different_keys() -> None:
    """Different files or classes → different signatures."""
    base = compute_cluster_signature("src/foo.py", "parse_error")
    assert compute_cluster_signature("src/bar.py", "parse_error") != base
    assert compute_cluster_signature("src/foo.py", "validation_error") != base


# ---------------------------------------------------------------------------
# cluster_failures — happy paths
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list() -> None:
    """No events → no clusters."""
    result = cluster_failures([], window=timedelta(hours=24))
    assert result == []


def test_single_event_produces_one_cluster() -> None:
    """One event → one cluster of size 1 with matching signature."""
    event = _make_failure_event()
    clusters = cluster_failures([event], window=timedelta(hours=24))
    assert len(clusters) == 1
    c = clusters[0]
    assert isinstance(c, Cluster)
    assert c.count == 1
    assert c.source_file == "src/trellis/extract/llm.py"
    assert c.failure_class == "parse_error"
    assert c.events == (event.event_id,)
    assert c.signature == compute_cluster_signature(
        "src/trellis/extract/llm.py", "parse_error"
    )
    assert c.earliest_at == event.occurred_at
    assert c.latest_at == event.occurred_at


def test_multiple_events_same_key_collapse_into_one_cluster() -> None:
    """Five events with the same ``(file, class)`` → one cluster of size 5."""
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    events = [
        _make_failure_event(occurred_at=base + timedelta(minutes=i))
        for i in range(5)
    ]
    clusters = cluster_failures(events, window=timedelta(hours=24))
    assert len(clusters) == 1
    c = clusters[0]
    assert c.count == 5
    assert set(c.events) == {e.event_id for e in events}
    assert c.earliest_at == base
    assert c.latest_at == base + timedelta(minutes=4)


def test_different_files_split_into_separate_clusters() -> None:
    """Failures from two source files → two distinct clusters."""
    events = [
        _make_failure_event(source_file="src/a.py"),
        _make_failure_event(source_file="src/b.py"),
        _make_failure_event(source_file="src/a.py"),
    ]
    clusters = cluster_failures(events, window=timedelta(hours=24))
    assert len(clusters) == 2
    by_file = {c.source_file: c for c in clusters}
    assert by_file["src/a.py"].count == 2
    assert by_file["src/b.py"].count == 1


def test_different_failure_classes_split_into_separate_clusters() -> None:
    """Same file but different ``failure_kind`` → distinct clusters."""
    events = [
        _make_failure_event(failure_class="parse_error"),
        _make_failure_event(failure_class="validation_error"),
        _make_failure_event(failure_class="parse_error"),
    ]
    clusters = cluster_failures(events, window=timedelta(hours=24))
    assert len(clusters) == 2
    by_kind = {c.failure_class: c for c in clusters}
    assert by_kind["parse_error"].count == 2
    assert by_kind["validation_error"].count == 1


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_events_outside_window_are_excluded() -> None:
    """Events older than ``now - window`` are dropped from clustering."""
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    events = [
        _make_failure_event(occurred_at=now),  # in window
        _make_failure_event(occurred_at=now - timedelta(hours=2)),  # in window
        _make_failure_event(occurred_at=now - timedelta(days=5)),  # outside
    ]
    clusters = cluster_failures(
        events,
        window=timedelta(hours=24),
        now=now,
    )
    assert len(clusters) == 1
    assert clusters[0].count == 2


def test_window_boundary_is_inclusive() -> None:
    """Event at exactly ``now - window`` is kept (not dropped)."""
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    window = timedelta(hours=24)
    boundary_event = _make_failure_event(occurred_at=now - window)
    clusters = cluster_failures([boundary_event], window=window, now=now)
    assert len(clusters) == 1
    assert clusters[0].count == 1


def test_now_defaults_to_latest_event_timestamp() -> None:
    """With no explicit ``now``, the most-recent event anchors the window."""
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    # Two events one hour apart; window=30min from latest → only newest survives.
    events = [
        _make_failure_event(occurred_at=base),
        _make_failure_event(occurred_at=base + timedelta(hours=1)),
    ]
    clusters = cluster_failures(events, window=timedelta(minutes=30))
    assert len(clusters) == 1
    assert clusters[0].count == 1


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_events_with_missing_payload_keys_are_skipped() -> None:
    """An event with no source_hint / no failure_kind is silently skipped.

    Synthetic-bucketing those into an "unknown" group would generate
    proposals recommending "fix unknown problem in unknown file", which
    is worse than emitting nothing.
    """
    good = _make_failure_event()
    bad_no_file = Event(
        event_type=EventType.EXTRACTION_FAILED,
        source="extraction_failure_helper",
        payload={"failure_kind": "parse_error"},  # no source_hint
    )
    bad_no_class = Event(
        event_type=EventType.EXTRACTION_FAILED,
        source="extraction_failure_helper",
        payload={"source_hint": "src/foo.py"},  # no failure_kind
    )
    bad_null_file = Event(
        event_type=EventType.EXTRACTION_FAILED,
        source="extraction_failure_helper",
        payload={"source_hint": None, "failure_kind": "parse_error"},
    )
    clusters = cluster_failures(
        [good, bad_no_file, bad_no_class, bad_null_file],
        window=timedelta(hours=24),
        now=good.occurred_at,
    )
    assert len(clusters) == 1
    assert clusters[0].count == 1
    assert clusters[0].events == (good.event_id,)


def test_clusters_returned_in_signature_order() -> None:
    """Cluster output is sorted by signature for deterministic test fixtures."""
    events = [
        _make_failure_event(source_file="src/z.py"),
        _make_failure_event(source_file="src/a.py"),
        _make_failure_event(source_file="src/m.py"),
    ]
    clusters = cluster_failures(events, window=timedelta(hours=24))
    sigs = [c.signature for c in clusters]
    assert sigs == sorted(sigs)
