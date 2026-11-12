"""A feedback ``pack_id`` has one stored form.

Two families write ``FEEDBACK_RECORDED`` (CLAUDE.md, "Feedback path"). The
governed one (``trellis curate feedback``, ``POST /feedback``) has always
stripped ``pack_id``. The ``PackFeedback`` one (MCP ``record_feedback``,
``POST /packs/{pack_id}/feedback``, the SDK) recorded it as given, so a padded
id reached the JSONL row and the event. The readers' half, a row already
recorded padded, is ``test_padded_pack_id_readers.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import load_feedback_log, record_feedback
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis_api.app import create_app

PACK = "pack-synthetic-1"
PADDED = f"  {PACK}\t"


def _signal(pack_id: str | None) -> PackFeedback:
    return PackFeedback.from_agent_signal(
        run_id=pack_id or "run-1",
        success=None,
        rating=0.9,
        helpful_item_ids=["item-a"],
        unhelpful_item_ids=[],
        pack_id=pack_id,
    )


# ---------------------------------------------------------------------------
# Writer: one stored form
# ---------------------------------------------------------------------------


class TestWriterRecordsOneForm:
    def test_row_and_event_carry_the_stripped_id(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            feedback = _signal(PADDED)
            record_feedback(
                feedback, log_dir=tmp_path / "fb", event_log=log, pack_id=PADDED
            )

            (loaded,) = load_feedback_log(tmp_path / "fb")
            assert loaded.metadata["pack_id"] == PACK

            (event,) = log.get_events(event_type=EventType.FEEDBACK_RECORDED)
            assert event.payload["pack_id"] == PACK
            assert event.entity_id == PACK
            assert event.payload["metadata"]["pack_id"] == PACK
        finally:
            log.close()

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_id_is_treated_as_an_absent_one(
        self, tmp_path: Path, blank: str
    ) -> None:
        """Absent is ``None``: no ``pack_id`` key, no ``entity_id``, no metadata."""
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            feedback = _signal(blank)
            assert "pack_id" not in feedback.metadata
            record_feedback(
                feedback, log_dir=tmp_path / "fb", event_log=log, pack_id=blank
            )

            (event,) = log.get_events(event_type=EventType.FEEDBACK_RECORDED)
            assert "pack_id" not in event.payload
            assert event.entity_id is None
        finally:
            log.close()

    def test_a_clean_id_is_recorded_unchanged(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            record_feedback(
                _signal(PACK), log_dir=tmp_path / "fb", event_log=log, pack_id=PACK
            )
            (event,) = log.get_events(event_type=EventType.FEEDBACK_RECORDED)
            assert event.payload["pack_id"] == PACK
            assert event.entity_id == PACK
        finally:
            log.close()


class TestRestPackFeedbackRecordsOneForm:
    """``POST /packs/{pack_id}/feedback`` passes through the same two calls."""

    @pytest.fixture
    def registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[StoreRegistry]:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        app_module._registry = reg
        yield reg
        reg.close()
        app_module._registry = None

    def test_a_padded_path_id_is_recorded_stripped(
        self, registry: StoreRegistry
    ) -> None:
        client = TestClient(create_app())
        resp = client.post(
            f"/api/v1/packs/%20{PACK}%20/feedback",
            json={"rating": 0.9, "helpful_item_ids": ["item-a"]},
        )
        assert resp.status_code == 200, resp.text

        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED
        )
        assert event.payload["pack_id"] == PACK
        assert event.entity_id == PACK
