"""Contract for the capped-read helper — ``scan_events`` (#374).

Three analyzers read the EventLog with ``limit=5000`` and, before this,
the ``order="asc"`` default. A window with more matches than the cap
therefore returned the *oldest* rows: a write outage that started this
morning fell outside the answer and nothing said so, which is a health
report that reads clean through exactly the incident it exists to catch.

Every test here is written so it fails if that behaviour comes back —
the direction of the drop, and the disclosure that a drop happened.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    Event,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)
from trellis.stores.sqlite.event_log import SQLiteEventLog

_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _seed(log: SQLiteEventLog, n: int, *, event_type: EventType) -> list[str]:
    """Append ``n`` events one minute apart, oldest first. Returns ids."""
    ids = []
    for index in range(n):
        event = Event(
            event_type=event_type,
            source="test",
            entity_id=f"e{index:03d}",
            occurred_at=_T0 + timedelta(minutes=index),
            payload={"index": index},
        )
        log.append(event)
        ids.append(event.entity_id or "")
    return ids


@pytest.fixture
def log(tmp_path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


# ---------------------------------------------------------------------------
# The cap drops the OLDEST rows — the whole point of #374
# ---------------------------------------------------------------------------


def test_truncation_keeps_the_newest_events(log: SQLiteEventLog) -> None:
    ids = _seed(log, 6, event_type=EventType.MUTATION_EXECUTED)
    scan = scan_events(log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=3)
    assert [e.entity_id for e in scan.events] == ids[-3:]


def test_kept_events_are_still_delivered_oldest_first(log: SQLiteEventLog) -> None:
    """The read is descending; what the caller consumes is not.

    Reversing is what confines the change to *which* events are dropped.
    Every aggregation written against chronological arrival — most visibly
    ``pack_value._response_token_join``'s last-write-wins — keeps working
    without being revisited.
    """
    _seed(log, 6, event_type=EventType.MUTATION_EXECUTED)
    scan = scan_events(log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=3)
    stamps = [e.occurred_at for e in scan.events]
    assert stamps == sorted(stamps)


# ---------------------------------------------------------------------------
# Truncation is disclosed, and the disclosure can read both ways
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seeded", "limit", "expected"),
    [(3, 10, False), (9, 10, False), (10, 10, True), (11, 10, True)],
)
def test_truncated_flag_is_not_a_constant(
    log: SQLiteEventLog, seeded: int, limit: int, expected: bool
) -> None:
    """Both readings are reachable, and the boundary is at the cap.

    A window holding exactly ``limit`` events reports truncated: the read
    came back full and nothing in the result can distinguish "exactly the
    cap" from "the cap bit". Over-disclosing is the safe direction.
    """
    _seed(log, seeded, event_type=EventType.MUTATION_EXECUTED)
    scan = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=limit
    )
    assert scan.coverage.truncated is expected


def test_untruncated_scan_reports_full_coverage(log: SQLiteEventLog) -> None:
    _seed(log, 4, event_type=EventType.MUTATION_EXECUTED)
    coverage = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=10
    ).coverage
    assert coverage.scanned == 4
    assert coverage.matched == 4
    assert coverage.dropped == 0
    assert coverage.truncated_event_types == []
    assert coverage.covered_since == ""
    assert coverage.note == ""


def test_truncated_scan_reports_what_it_dropped(log: SQLiteEventLog) -> None:
    _seed(log, 9, event_type=EventType.MUTATION_EXECUTED)
    coverage = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=4
    ).coverage
    assert coverage.truncated is True
    assert coverage.scanned == 4
    assert coverage.matched == 9
    assert coverage.dropped == 5
    assert coverage.truncated_event_types == [EventType.MUTATION_EXECUTED.value]
    assert "TRUNCATED" in coverage.note
    assert "mutation.executed" in coverage.note


def test_covered_since_names_the_real_start_of_the_evidence(
    log: SQLiteEventLog,
) -> None:
    """The disclosure a reader needs most: the window is not the window."""
    _seed(log, 9, event_type=EventType.MUTATION_EXECUTED)
    coverage = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=4
    ).coverage
    # Nine events one minute apart; keeping four means the oldest kept is
    # the sixth (index 5), not the first.
    assert coverage.covered_since == (_T0 + timedelta(minutes=5)).isoformat()
    assert coverage.covered_since in coverage.note


def test_json_round_trip_carries_every_field(log: SQLiteEventLog) -> None:
    """Reports embed this and serialize with a plain ``model_dump()``."""
    _seed(log, 6, event_type=EventType.MUTATION_EXECUTED)
    dumped = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=3
    ).coverage.model_dump()
    import json

    assert json.loads(json.dumps(dumped))["truncated"] is True


# ---------------------------------------------------------------------------
# Unknown is reported as unknown, never as zero
# ---------------------------------------------------------------------------


def test_unknown_total_degrades_to_none_not_zero(log: SQLiteEventLog) -> None:
    """A ``count`` that raises must not turn into a confident number."""
    _seed(log, 6, event_type=EventType.MUTATION_EXECUTED)

    down = RuntimeError("count is down")

    class _CountBroken(SQLiteEventLog):
        def count(self, **_kwargs: object) -> int:
            raise down

    broken = _CountBroken(log._db_path)
    coverage = scan_events(
        broken, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=3
    ).coverage
    assert coverage.truncated is True
    assert coverage.scanned == 3
    assert coverage.matched is None
    assert coverage.dropped is None
    assert "unknown number" in coverage.note


def test_until_bound_refuses_to_guess_the_total(log: SQLiteEventLog) -> None:
    """``EventLog.count`` takes no ``until``; counting anyway would report a
    total over a wider window than the scan covered."""
    _seed(log, 9, event_type=EventType.MUTATION_EXECUTED)
    coverage = scan_events(
        log,
        event_type=EventType.MUTATION_EXECUTED,
        since=_T0,
        until=_T0 + timedelta(hours=1),
        limit=4,
    ).coverage
    assert coverage.truncated is True
    assert coverage.matched is None


def test_non_positive_limit_is_never_reported_as_truncated(
    log: SQLiteEventLog,
) -> None:
    _seed(log, 3, event_type=EventType.MUTATION_EXECUTED)
    coverage = scan_events(
        log, event_type=EventType.MUTATION_EXECUTED, since=_T0, limit=0
    ).coverage
    assert coverage.truncated is False


def test_default_limit_is_the_shared_one() -> None:
    assert DEFAULT_SCAN_LIMIT == 5000


# ---------------------------------------------------------------------------
# merge_coverage — one verdict for a report made of several reads
# ---------------------------------------------------------------------------


def test_merge_takes_the_narrowest_covered_since() -> None:
    """The latest start bounds what the whole report can claim.

    Reporting the earliest would overstate coverage — the exact move this
    model exists to prevent.
    """
    early = ScanCoverage(
        limit=10,
        scanned=10,
        matched=20,
        dropped=10,
        truncated=True,
        truncated_event_types=["a"],
        covered_since="2026-08-01T00:00:00+00:00",
        note="A",
    )
    late = ScanCoverage(
        limit=10,
        scanned=10,
        matched=30,
        dropped=20,
        truncated=True,
        truncated_event_types=["b"],
        covered_since="2026-08-20T00:00:00+00:00",
        note="B",
    )
    merged = merge_coverage(early, late)
    assert merged.covered_since == "2026-08-20T00:00:00+00:00"
    assert merged.truncated_event_types == ["a", "b"]
    assert merged.scanned == 20
    assert merged.matched == 50
    assert merged.dropped == 30
    assert merged.note == "A B"


def test_merge_is_truncated_if_any_read_was() -> None:
    clean = ScanCoverage(limit=10, scanned=2, matched=2, dropped=0)
    capped = ScanCoverage(
        limit=10,
        scanned=10,
        matched=99,
        dropped=89,
        truncated=True,
        truncated_event_types=["x"],
        note="capped",
    )
    assert merge_coverage(clean, capped).truncated is True
    assert merge_coverage(clean, clean).truncated is False


def test_merge_propagates_an_unknown_total() -> None:
    known = ScanCoverage(limit=10, scanned=2, matched=2, dropped=0)
    unknown = ScanCoverage(limit=10, scanned=10, truncated=True, matched=None)
    assert merge_coverage(known, unknown).matched is None
    assert merge_coverage(known, unknown).dropped is None


def test_merge_of_nothing_is_the_empty_coverage() -> None:
    assert merge_coverage() == ScanCoverage()
