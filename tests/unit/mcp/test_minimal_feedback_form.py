"""``record_feedback`` accepts the minimal form; pin what it records (#1194).

A caller may send ``pack_id`` plus ``helpful_item_ids`` and/or
``unhelpful_item_ids`` and nothing else: no ``rating``, no ``success``. Such a
call used to be recorded as a success with ``rating`` 1.0 whatever it cited,
so a grade calling every item noise counted as a win. The rating is now
derived from the cited ids, the helpful share of them, and the
``FEEDBACK_RECORDED`` payload and the JSONL row carry ``rating_derived: true``
so a reader can tell a derivation from a grade.

This pins the effect on the MCP tool and on REST (through the SDK, which posts
to ``POST /packs/{pack_id}/feedback``): the three derived cases, that an
explicit ``rating`` or ``success`` is recorded as sent with no
``rating_derived``, and that a call naming no pack item keeps the behaviour
that predates the derivation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.sdk.test_record_feedback import _sync_client
from trellis.feedback.recording import (
    feedback_log_dir,
    load_feedback_log,
    reconcile_feedback_log_to_event_log,
)
from trellis.mcp.server import record_feedback as _record_feedback
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

record_feedback = unwrap_tool(_record_feedback)


def _payloads(registry: StoreRegistry) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
    ]


def _jsonl_rows(registry: StoreRegistry) -> list[dict[str, Any]]:
    assert registry.stores_dir is not None
    path = feedback_log_dir(registry.stores_dir) / "pack_feedback.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


#: ``(helpful, unhelpful, rating, success)`` for each derived case. A mixed call
#: lands on exactly ``SUCCESS_RATING_THRESHOLD`` and so counts as a success.
DERIVED_CASES = [
    pytest.param(["a", "b"], [], 1.0, True, id="helpful-only"),
    pytest.param([], ["x"], 0.0, False, id="unhelpful-only"),
    pytest.param(["a"], ["x"], 0.5, True, id="mixed"),
    pytest.param(["a"], ["x", "y", "z"], 0.25, False, id="mostly-unhelpful"),
]


class TestMcp:
    @pytest.mark.parametrize(
        ("helpful", "unhelpful", "rating", "success"), DERIVED_CASES
    )
    def test_the_rating_is_the_helpful_share(
        self,
        temp_registry: StoreRegistry,
        helpful: list[str],
        unhelpful: list[str],
        rating: float,
        success: bool,
    ) -> None:
        record_feedback(
            pack_id="pack_min",
            helpful_item_ids=helpful or None,
            unhelpful_item_ids=unhelpful or None,
        )
        (payload,) = _payloads(temp_registry)
        assert payload["pack_id"] == "pack_min"
        assert payload["rating"] == rating
        assert payload["success"] is success
        assert payload["outcome"] == ("success" if success else "failure")
        assert payload["rating_derived"] is True
        assert payload["helpful_item_ids"] == helpful
        if unhelpful:
            assert payload["unhelpful_item_ids"] == unhelpful

    def test_the_reply_reports_the_derived_value(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = record_feedback(pack_id="pack_min", unhelpful_item_ids=["x"])
        assert reply.startswith(
            "Feedback recorded (negative, rating 0.00, derived from the cited ids)"
        )
        assert "positive" not in reply

        reply = record_feedback(
            pack_id="pack_min", helpful_item_ids=["a"], unhelpful_item_ids=["x"]
        )
        assert reply.startswith(
            "Feedback recorded (positive, rating 0.50, derived from the cited ids)"
        )

    def test_the_jsonl_row_carries_the_marker_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_min", unhelpful_item_ids=["x"])
        (row,) = _jsonl_rows(temp_registry)
        assert row["rating"] == 0.0
        assert row["rating_derived"] is True
        assert row["outcome"] == "failure"

    def test_a_replayed_row_keeps_the_marker(
        self, temp_registry: StoreRegistry
    ) -> None:
        """``reconcile-feedback`` rebuilds a row from the file, not from memory."""
        record_feedback(pack_id="pack_min", unhelpful_item_ids=["x"])
        assert temp_registry.stores_dir is not None
        log_dir = feedback_log_dir(temp_registry.stores_dir)
        (loaded,) = load_feedback_log(log_dir)
        assert loaded.rating_derived is True
        assert loaded.effective_rating == 0.0

        # An empty event log stands in for a sink that was down at record time.
        replay_dir = temp_registry.stores_dir / "replay"
        replay_dir.mkdir()
        replayed = StoreRegistry(stores_dir=replay_dir)
        try:
            result = reconcile_feedback_log_to_event_log(
                log_dir, replayed.operational.event_log
            )
            assert result.emitted == 1
            (payload,) = _payloads(replayed)
        finally:
            replayed.close()
        assert payload["rating"] == 0.0
        assert payload["success"] is False
        assert payload["rating_derived"] is True

    def test_the_derived_payload_is_a_full_calls_keys_plus_the_marker(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_min", helpful_item_ids=["a"])
        record_feedback(
            pack_id="pack_full",
            rating=0.8,
            success=True,
            helpful_item_ids=["a"],
            unhelpful_item_ids=["b"],
            followed_advisory_ids=["adv-1"],
        )
        minimal, full = _payloads(temp_registry)  # oldest first
        assert minimal["pack_id"] == "pack_min"
        # Same keys, bar the empty lists a minimal call leaves out, and the
        # marker only a derivation carries.
        assert set(minimal) <= set(full) | {"rating_derived"}
        assert set(full) - set(minimal) == {
            "unhelpful_item_ids",
            "followed_advisory_ids",
        }
        assert "rating_derived" not in full

    def test_an_explicit_rating_is_used_as_sent(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_r", rating=0.2, helpful_item_ids=["a"])
        # A rating of 1.0 on an unhelpful-only call is the caller's claim.
        record_feedback(pack_id="pack_r", rating=1.0, unhelpful_item_ids=["x"])
        low, high = _payloads(temp_registry)
        assert low["rating"] == 0.2
        assert low["success"] is False
        assert high["rating"] == 1.0
        assert high["success"] is True
        assert "rating_derived" not in low
        assert "rating_derived" not in high
        assert all("rating_derived" not in row for row in _jsonl_rows(temp_registry))

    def test_an_explicit_success_is_used_as_sent(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_s", success=True, unhelpful_item_ids=["x"])
        record_feedback(pack_id="pack_s", success=False, helpful_item_ids=["a"])
        won, lost = _payloads(temp_registry)
        assert won["success"] is True
        assert won["rating"] == 1.0
        assert lost["success"] is False
        assert lost["rating"] == 0.0
        assert "rating_derived" not in won
        assert "rating_derived" not in lost

    def test_advisories_are_not_pack_item_verdicts(
        self, temp_registry: StoreRegistry
    ) -> None:
        """A followed advisory is guidance acted on, not a verdict on an item."""
        record_feedback(
            pack_id="pack_adv",
            unhelpful_item_ids=["x"],
            followed_advisory_ids=["adv-1", "adv-2"],
        )
        (payload,) = _payloads(temp_registry)
        assert payload["rating"] == 0.0
        assert payload["success"] is False
        assert payload["rating_derived"] is True
        assert payload["followed_advisory_ids"] == ["adv-1", "adv-2"]

    def test_a_call_naming_no_pack_item_keeps_the_earlier_behaviour(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Nothing to derive from: success, rating 1.0, no marker.

        Refusing it would turn a call that is valid today on every surface
        into an error, so it keeps what it always recorded.
        """
        record_feedback(pack_id="pack_adv", followed_advisory_ids=["adv-1"])
        record_feedback(trace_id="trace-1")
        advisory_only, trace_only = _payloads(temp_registry)
        for payload in (advisory_only, trace_only):
            assert payload["success"] is True
            assert payload["rating"] == 1.0
            assert "rating_derived" not in payload

    def test_duplicate_ids_do_not_weight_the_share(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(
            pack_id="pack_dup",
            helpful_item_ids=["a", "a", "a"],
            unhelpful_item_ids=["x"],
        )
        (payload,) = _payloads(temp_registry)
        assert payload["rating"] == 0.5


class TestRestAndSdk:
    @pytest.mark.parametrize(
        ("helpful", "unhelpful", "rating", "success"), DERIVED_CASES
    )
    def test_the_rating_is_the_helpful_share(
        self,
        tmp_path: Path,
        helpful: list[str],
        unhelpful: list[str],
        rating: float,
        success: bool,
    ) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            result = client.record_feedback(
                "pack_min", helpful_item_ids=helpful, unhelpful_item_ids=unhelpful
            )
            assert result.event_log_in_sync is True
            assert result.feedback == ("positive" if success else "negative")
            (payload,) = _payloads(registry)
            (row,) = _jsonl_rows(registry)
        assert payload["pack_id"] == "pack_min"
        assert payload["rating"] == rating
        assert payload["success"] is success
        assert payload["rating_derived"] is True
        assert row["rating"] == rating
        assert row["rating_derived"] is True

    def test_an_explicit_rating_is_used_as_sent(self, tmp_path: Path) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            client.record_feedback("pack_r", rating=0.9, unhelpful_item_ids=["x"])
            (payload,) = _payloads(registry)
            (row,) = _jsonl_rows(registry)
        assert payload["rating"] == 0.9
        assert payload["success"] is True
        assert "rating_derived" not in payload
        assert "rating_derived" not in row

    def test_an_explicit_success_is_used_as_sent(self, tmp_path: Path) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            client.record_feedback("pack_s", True, unhelpful_item_ids=["x"])
            (payload,) = _payloads(registry)
        assert payload["success"] is True
        assert payload["rating"] == 1.0
        assert "rating_derived" not in payload

    def test_a_call_naming_no_pack_item_keeps_the_earlier_behaviour(
        self, tmp_path: Path
    ) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            result = client.record_feedback("pack_min")
            (payload,) = _payloads(registry)
        assert result.feedback == "positive"
        assert payload["success"] is True
        assert payload["rating"] == 1.0
        assert "rating_derived" not in payload

    def test_rest_and_mcp_agree(
        self, tmp_path: Path, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(
            pack_id="pack_min", helpful_item_ids=["a"], unhelpful_item_ids=["x"]
        )
        (mcp_payload,) = _payloads(temp_registry)
        with _sync_client(tmp_path / "rest-stores") as (client, registry):
            client.record_feedback(
                "pack_min", helpful_item_ids=["a"], unhelpful_item_ids=["x"]
            )
            (rest_payload,) = _payloads(registry)
        volatile = {"feedback_id", "recorded_at", "timestamp", "timestamp_utc"}
        assert {k: v for k, v in mcp_payload.items() if k not in volatile} == {
            k: v for k, v in rest_payload.items() if k not in volatile
        }
        assert mcp_payload["rating"] == 0.5
        assert mcp_payload["rating_derived"] is True
