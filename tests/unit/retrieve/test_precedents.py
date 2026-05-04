"""Tests for ``trellis.retrieve.precedents.list_precedents``.

The regression of interest is the silent-truncation bug fixed in PR-8:
when a ``domain`` filter was supplied, the previous shape paged events
with ``limit`` first and applied the Python-side filter second, so a
large window of non-matching rows could starve the result list. The
new shape pushes ``domain`` into ``payload_filters`` so the limit
applies AFTER the filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.precedents import list_precedents
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def test_list_precedents_no_filter(event_log: SQLiteEventLog) -> None:
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing", "title": "Refund flow"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-2",
        payload={"domain": "shipping", "title": "Address validation"},
    )

    result = list_precedents(event_log)
    assert len(result) == 2


def test_list_precedents_domain_filter_pushed_to_sql(
    event_log: SQLiteEventLog,
) -> None:
    """Domain filter narrows results via SQL predicate."""
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing", "title": "Refund flow"},
    )
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-2",
        payload={"domain": "shipping", "title": "Address validation"},
    )

    result = list_precedents(event_log, domain="billing")
    assert len(result) == 1
    assert result[0]["entity_id"] == "prec-1"
    assert result[0]["domain"] == "billing"


def test_list_precedents_limit_applies_after_filter(
    event_log: SQLiteEventLog,
) -> None:
    """Regression: with the old post-fetch shape, a small ``limit`` and a
    domain filter could return fewer than ``limit`` matches. The new
    shape pushes the predicate into SQL so the cap applies *after* the
    filter and the user gets a full page.
    """
    # Emit 20 non-matching rows first (so they sit at the oldest end
    # under default ASC ordering and would fill the limit window first).
    for i in range(20):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="noise",
            entity_id=f"noise-{i}",
            payload={"domain": "shipping", "title": f"shipping-{i}"},
        )
    for i in range(5):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="match",
            entity_id=f"match-{i}",
            payload={"domain": "billing", "title": f"billing-{i}"},
        )

    result = list_precedents(event_log, domain="billing", limit=3)
    assert len(result) == 3
    assert all(row["domain"] == "billing" for row in result)


def test_list_precedents_no_match(event_log: SQLiteEventLog) -> None:
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="prec-1",
        payload={"domain": "billing"},
    )
    result = list_precedents(event_log, domain="nonexistent")
    assert result == []
