"""``record_feedback`` already accepts the minimal form; pin what it does.

A caller may send ``pack_id`` plus ``helpful_item_ids`` and/or
``unhelpful_item_ids`` and nothing else: no ``rating``, no ``success``. The
reply trailer names ``rating`` because this call records a success without it;
this pins the effect the bare call has today on the MCP tool and on REST
(through the SDK, which posts to ``POST /packs/{pack_id}/feedback``):
``success`` is true, ``rating`` is 1.0, and the ``FEEDBACK_RECORDED`` payload
has the same keys as a full call.

The silent ``rating`` of 1.0 on an unhelpful-only call is current behaviour
and is pinned as such. Deriving a rating from the cited ids would change the
reply and the event for calls that are valid today, so it waits for the
readout (see ``docs/agent-guide/operations.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.sdk.test_record_feedback import _sync_client
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


class TestMcp:
    def test_helpful_ids_alone_are_enough(self, temp_registry: StoreRegistry) -> None:
        record_feedback(pack_id="pack_min", helpful_item_ids=["a", "b"])
        (payload,) = _payloads(temp_registry)
        assert payload["pack_id"] == "pack_min"
        assert payload["helpful_item_ids"] == ["a", "b"]
        assert payload["success"] is True
        assert payload["rating"] == 1.0

    def test_unhelpful_ids_alone_currently_record_success(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_min", unhelpful_item_ids=["x"])
        (payload,) = _payloads(temp_registry)
        assert payload["unhelpful_item_ids"] == ["x"]
        # Current behaviour, pinned: nothing was rated, so it reads as success.
        assert payload["success"] is True
        assert payload["rating"] == 1.0

    def test_the_payload_has_the_keys_of_a_full_call(
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
        # Same keys, bar the empty lists a minimal call leaves out.
        assert set(minimal) <= set(full)
        assert set(full) - set(minimal) == {
            "unhelpful_item_ids",
            "followed_advisory_ids",
        }

    def test_an_explicit_rating_still_wins(self, temp_registry: StoreRegistry) -> None:
        record_feedback(pack_id="pack_r", rating=0.2, helpful_item_ids=["a"])
        (payload,) = _payloads(temp_registry)
        assert payload["rating"] == 0.2
        assert payload["success"] is False


class TestRestAndSdk:
    def test_helpful_ids_alone_are_enough(self, tmp_path: Path) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            result = client.record_feedback("pack_min", helpful_item_ids=["a"])
            assert result.event_log_in_sync is True
            (payload,) = _payloads(registry)
        assert payload["pack_id"] == "pack_min"
        assert payload["helpful_item_ids"] == ["a"]
        assert payload["success"] is True
        assert payload["rating"] == 1.0

    def test_unhelpful_ids_alone_currently_record_success(self, tmp_path: Path) -> None:
        with _sync_client(tmp_path / "stores") as (client, registry):
            client.record_feedback("pack_min", unhelpful_item_ids=["x"])
            (payload,) = _payloads(registry)
        assert payload["unhelpful_item_ids"] == ["x"]
        assert payload["success"] is True
        assert payload["rating"] == 1.0

    def test_rest_and_mcp_agree(
        self, tmp_path: Path, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_min", helpful_item_ids=["a"])
        (mcp_payload,) = _payloads(temp_registry)
        with _sync_client(tmp_path / "rest-stores") as (client, registry):
            client.record_feedback("pack_min", helpful_item_ids=["a"])
            (rest_payload,) = _payloads(registry)
        volatile = {"feedback_id", "recorded_at", "timestamp", "timestamp_utc"}
        assert {k: v for k, v in mcp_payload.items() if k not in volatile} == {
            k: v for k, v in rest_payload.items() if k not in volatile
        }
