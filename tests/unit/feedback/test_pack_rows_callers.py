"""Callers of ``feedback/pack_rows.py`` that had no sectioned or held-out test.

Four readers moved onto the typed reader without any test that fed them a
sectioned pack carrying ``injected_items[]`` (#958) or a withheld pack:

* the precedent evidence gate, whose only sectioned case predates #958 and
  omits the rows entirely,
* ``summarize_serve_attribution``, whose "has per-item rows" test is
  :func:`~trellis.feedback.pack_rows.has_served_rows`,
* the judged-outcome serving index, which keys a serving on ``(pack, item)``,
* the pack-value token tally, which prices each served row.

Both are driven through the real event log and the real joins, so the held-out
case also pins that the drop happens upstream of the migrated read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trellis.learning.judged_outcomes import _ServingIndex
from trellis.learning.precedent_evidence_gate import gather_precedent_citation_evidence
from trellis.ops.write_health import summarize_serve_attribution
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

_T0 = datetime.now(tz=UTC) - timedelta(hours=6)


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _row(item_id: str, rank: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": "precedent",
        "rank": rank,
        "estimated_tokens": 10 * rank,
        **extra,
    }


def _sectioned_payload(item_ids: list[str]) -> dict[str, Any]:
    """The #958 shape: a ``sections`` list and per-item rows with ``section``."""
    return {
        "intent": "test",
        "sections": [
            {
                "name": "main",
                "items_count": len(item_ids),
                "item_ids": item_ids,
                "injected_advisory_ids": [],
            }
        ],
        "injected_items": [
            _row(item_id, rank + 1, section="main")
            for rank, item_id in enumerate(item_ids)
        ],
    }


def _held_out_payload(item_ids: list[str], *, rows_leak: bool) -> dict[str, Any]:
    """A withheld pack: the builder leaves ``injected_items`` empty.

    ``rows_leak=True`` is the hand-built drift shape that keeps them, to pin
    that the join drops the pack whatever its rows say.
    """
    would_be = [_row(item_id, rank + 1) for rank, item_id in enumerate(item_ids)]
    return {
        "intent": "test",
        "holdout": True,
        "holdout_rate": 1.0,
        "injected_items": would_be if rows_leak else [],
        "holdout_items": would_be,
    }


def _pack(
    event_log: EventLog,
    pack_id: str,
    payload: dict[str, Any],
    *,
    at: datetime | None = None,
) -> None:
    event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
            occurred_at=at or _T0 + timedelta(hours=1),
        )
    )


def _feedback(
    event_log: EventLog,
    pack_id: str,
    *,
    unhelpful: Sequence[str] = (),
    helpful: Sequence[str] = (),
) -> None:
    event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=f"fb-{pack_id}",
            payload={
                "pack_id": pack_id,
                "rating": 0.2,
                "success": False,
                "unhelpful_item_ids": list(unhelpful),
                "helpful_item_ids": list(helpful),
            },
            occurred_at=_T0 + timedelta(hours=2),
        )
    )


class TestPrecedentGateReadsServedRows:
    def test_a_sectioned_pack_with_rows_is_an_appearance(
        self, event_log: EventLog
    ) -> None:
        _pack(event_log, "pack-s", _sectioned_payload(["p1"]))
        _feedback(event_log, "pack-s", unhelpful=["p1"])

        evidence = gather_precedent_citation_evidence(event_log, "p1", since=_T0)

        assert evidence is not None
        assert evidence.appearances == 1
        assert evidence.unhelpful_count == 1

    @pytest.mark.parametrize("rows_leak", [False, True])
    def test_a_held_out_pack_is_no_appearance(
        self, event_log: EventLog, rows_leak: bool
    ) -> None:
        _pack(event_log, "pack-h", _held_out_payload(["p1"], rows_leak=rows_leak))
        _feedback(event_log, "pack-h", unhelpful=["p1"])

        assert gather_precedent_citation_evidence(event_log, "p1", since=_T0) is None


class TestServeAttributionCountsRows:
    def test_a_sectioned_pack_with_rows_is_attributable(
        self, event_log: EventLog
    ) -> None:
        _pack(event_log, "pack-s", _sectioned_payload(["a"]))

        report = summarize_serve_attribution(event_log, days=1)

        assert report.packs == 1
        assert report.packs_with_injected_items == 1

    def test_a_sectioned_pack_without_rows_is_not(self, event_log: EventLog) -> None:
        payload = _sectioned_payload(["a"])
        payload["injected_items"] = []
        _pack(event_log, "pack-s", payload)

        report = summarize_serve_attribution(event_log, days=1)

        assert report.packs == 1
        assert report.packs_with_injected_items == 0

    def test_a_held_out_pack_is_not_a_pack_at_all(self, event_log: EventLog) -> None:
        _pack(event_log, "pack-h", _held_out_payload(["a"], rows_leak=True))
        _pack(event_log, "pack-f", {"injected_items": [_row("b")]})

        report = summarize_serve_attribution(event_log, days=1)

        assert report.packs == 1
        assert report.packs_with_injected_items == 1


class TestJudgedServingIndex:
    def test_a_sectioned_pack_is_indexed_by_pack_and_item(self) -> None:
        event = Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-s",
            entity_type="pack",
            payload=_sectioned_payload(["a", "b"]),
            occurred_at=_T0,
        )

        index = _ServingIndex({"pack-s": event})

        assert index.served_ids == {"pack-s": {"a", "b"}}
        assert [serving.pack_id for serving in index.matching("a")] == ["pack-s"]


def _dir(base: Path, name: str) -> Path:
    path = base / name
    path.mkdir()
    return path


class TestPackValueTallyReadsRows:
    def _tally(self, tmp_path: Path, payload: dict[str, Any]) -> tuple[int, int, int]:
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            _pack(log, "pack-1", payload)
            _feedback(log, "pack-1", helpful=["a"], unhelpful=["b"])
            report = summarize_pack_value(log, days=30)
        finally:
            log.close()
        return report.injected_tokens, report.helpful_tokens, report.unhelpful_tokens

    def test_a_sectioned_pack_is_priced_like_a_flat_one(self, tmp_path: Path) -> None:
        flat = {"injected_items": _sectioned_payload(["a", "b"])["injected_items"]}
        sectioned = _sectioned_payload(["a", "b"])

        flat_tally = self._tally(_dir(tmp_path, "flat"), flat)
        sectioned_tally = self._tally(_dir(tmp_path, "sectioned"), sectioned)

        assert flat_tally[0] > 0, "the flat control must price something"
        assert sectioned_tally == flat_tally
