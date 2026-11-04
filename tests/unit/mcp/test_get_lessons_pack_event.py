"""``get_lessons`` writes a ``PACK_ASSEMBLED`` only behind its default-off knob."""

from __future__ import annotations

import pytest

from tests.unit.mcp.conftest import unwrap_tool
from trellis.core.pack_holdout import drop_holdout, is_holdout
from trellis.feedback.attribution import lookup_pack_item_ids
from trellis.mcp.server import get_lessons as _get_lessons
from trellis.retrieve.builder_factory import (
    GET_LESSONS_PACK_EVENT_ENV,
    GET_LESSONS_PACK_EVENT_SETTING,
    _get_lessons_pack_event_enabled,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

get_lessons = unwrap_tool(_get_lessons)


def _seed(registry: StoreRegistry, *names: str) -> None:
    event_log = registry.operational.event_log
    for index, name in enumerate(names):
        event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="test",
            entity_id=f"prec-{index}",
            payload={"title": name, "description": f"{name} body", "domain": "ops"},
        )


def _packs(registry: StoreRegistry) -> list:
    return registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=50
    )


@pytest.fixture(autouse=True)
def _knob_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GET_LESSONS_PACK_EVENT_ENV, raising=False)


class TestKnobOff:
    def test_default_reply_has_no_pack_and_no_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, "First lesson", "Second lesson")

        reply = get_lessons()

        assert "First lesson" in reply
        assert "pack_id" not in reply
        assert _packs(temp_registry) == []

    @pytest.mark.parametrize("raw", ["0", "false", "off", "maybe"])
    def test_falsey_and_unrecognised_values_stay_off(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        _seed(temp_registry, "First lesson")
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, raw)

        reply = get_lessons()

        assert "pack_id" not in reply
        assert _packs(temp_registry) == []


class TestKnobOn:
    def test_reply_is_the_off_reply_plus_a_footer(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry, "First lesson", "Second lesson")
        off_reply = get_lessons()
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")

        on_reply = get_lessons()

        assert on_reply.startswith(off_reply)
        (event,) = _packs(temp_registry)
        footer = on_reply[len(off_reply) :]
        assert f"**pack_id:** `{event.entity_id}`" in footer
        assert "`prec-0`" in footer or "`prec-1`" in footer

    def test_the_event_rows_and_variants(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry, "First lesson", "Second lesson")
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "true")

        get_lessons(domain="ops")

        (event,) = _packs(temp_registry)
        payload = event.payload
        assert payload["surface"] == "get_lessons"
        assert payload["pack_id"] == event.entity_id
        assert payload["items_count"] == 2
        assert sorted(payload["injected_item_ids"]) == ["prec-0", "prec-1"]
        rows = payload["injected_items"]
        assert [row["rank"] for row in rows] == [1, 2]
        assert all(row["item_type"] == "precedent" for row in rows)
        assert all(row["domain"] == ["ops"] for row in rows)
        assert payload["variants"] == {}

    def test_the_pack_can_be_named_by_feedback(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry, "First lesson")
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")

        get_lessons()

        (event,) = _packs(temp_registry)
        assert lookup_pack_item_ids(
            temp_registry.operational.event_log, event.entity_id
        ) == ["prec-0"]

    def test_the_event_is_exempt_from_the_holdout_draw(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry, "First lesson")
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        # A rate of 1.0 would hold out every built pack. It must not touch this.
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")

        reply = get_lessons()

        (event,) = _packs(temp_registry)
        assert "First lesson" in reply
        assert event.payload["holdout"] is False
        assert event.payload["holdout_rate"] == 0.0
        assert is_holdout(event.payload) is False
        kept, _ = drop_holdout([event], [])
        assert kept == [event]

    def test_no_lessons_writes_no_event(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")

        assert get_lessons() == "No lessons found."
        assert _packs(temp_registry) == []

    def test_a_failed_emit_leaves_the_reply_unchanged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(temp_registry, "First lesson")
        off_reply = get_lessons()
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "1")
        event_log = temp_registry.operational.event_log
        real_emit = event_log.emit

        def _emit(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            if event_type == EventType.PACK_ASSEMBLED:
                msg = "boom"
                raise RuntimeError(msg)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(event_log, "emit", _emit)

        assert get_lessons() == off_reply


class TestResolver:
    def test_unset_is_off(self) -> None:
        assert _get_lessons_pack_event_enabled() is False
        assert _get_lessons_pack_event_enabled({}) is False

    def test_a_stored_bool_is_read_and_a_non_bool_is_ignored(self) -> None:
        assert _get_lessons_pack_event_enabled({GET_LESSONS_PACK_EVENT_SETTING: True})
        assert not _get_lessons_pack_event_enabled(
            {GET_LESSONS_PACK_EVENT_SETTING: "yes"}
        )

    def test_env_wins_over_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "off")
        assert not _get_lessons_pack_event_enabled(
            {GET_LESSONS_PACK_EVENT_SETTING: True}
        )
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "on")
        assert _get_lessons_pack_event_enabled({GET_LESSONS_PACK_EVENT_SETTING: False})

    def test_an_unrecognised_env_value_ignores_a_stored_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GET_LESSONS_PACK_EVENT_ENV, "maybe")
        assert not _get_lessons_pack_event_enabled(
            {GET_LESSONS_PACK_EVENT_SETTING: True}
        )
