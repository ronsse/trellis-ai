"""What serving a pack to an ``external`` key writes, and what it leaves alone.

An ``external`` key's pack is outside the holdout, session dedup, attribution
and promotion, all of which read ``PACK_ASSEMBLED``. It records what the key
read in a separate event, ``PUBLIC_PACK_SERVED``, and nothing that an analysis
reader consumes changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.public_corpus import (
    EMBED_FN_PATH,
    PUBLIC_IDS,
    QUERY,
    build_world,
    differences,
    mask,
)
from trellis.analyze.holdout import analyze_holdout
from trellis.auth import generate_api_key
from trellis.retrieve.effectiveness import analyze_effectiveness
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

PAYLOAD_KEYS = {"pack_id", "served_item_ids", "count", "key_id"}


class World:
    """A ``full`` store, an app over it, and one key of each kind."""

    def __init__(self, registry: StoreRegistry) -> None:
        self.registry = registry
        self.client = TestClient(create_app())
        self.headers: dict[str, dict[str, str]] = {}
        self.key_ids: dict[str, str] = {}
        for name, scopes in (("external", ["read", "external"]), ("read", ["read"])):
            token, record = generate_api_key(name, scopes)
            registry.operational.api_key_store.create(record)
            self.headers[name] = {"Authorization": f"Bearer {token}"}
            self.key_ids[name] = record.key_id

    def post(self, who: str, url: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.post(url, json=body, headers=self.headers[who])
        assert resp.status_code == 200, resp.text
        return resp.json()

    def events(self, event_type: EventType) -> list[Any]:
        return self.registry.operational.event_log.get_events(
            event_type=event_type, limit=1000
        )


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    monkeypatch.delenv("TRELLIS_PACK_HOLDOUT_RATE", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    build_world(reg, "full")
    try:
        yield World(reg)
    finally:
        reg.close()
        app_module._registry = None


def _served_ids(pack: dict[str, Any]) -> list[str]:
    if "sections" in pack:
        return [i["item_id"] for s in pack["sections"] for i in s["items"]]
    return [i["item_id"] for i in pack["items"]]


class TestEvent:
    def test_a_flat_pack_emits_one_public_served_event(self, world: World) -> None:
        pack = world.post("external", "/api/v1/packs", {"intent": QUERY})
        served = _served_ids(pack)
        assert served, "the pack must serve something for this to prove anything"
        (event,) = world.events(EventType.PUBLIC_PACK_SERVED)
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.payload["pack_id"] == pack["pack_id"]
        assert event.payload["served_item_ids"] == served
        assert event.payload["count"] == len(served)
        assert event.payload["key_id"] == world.key_ids["external"]

    def test_a_sectioned_pack_emits_one_public_served_event(self, world: World) -> None:
        pack = world.post(
            "external",
            "/api/v1/packs/sectioned",
            {
                "intent": QUERY,
                "sections": [{"name": "A", "max_items": 2}, {"name": "B"}],
            },
        )
        served = _served_ids(pack)
        assert served
        (event,) = world.events(EventType.PUBLIC_PACK_SERVED)
        assert set(event.payload) == PAYLOAD_KEYS
        assert event.payload["pack_id"] == pack["pack_id"]
        assert event.payload["served_item_ids"] == served
        assert event.payload["count"] == len(served)
        assert event.payload["key_id"] == world.key_ids["external"]

    def test_only_public_documents_are_ever_named(self, world: World) -> None:
        world.post("external", "/api/v1/packs", {"intent": QUERY})
        world.post("external", "/api/v1/packs", {"intent": "bulkword failover"})
        for event in world.events(EventType.PUBLIC_PACK_SERVED):
            assert set(event.payload["served_item_ids"]) <= set(PUBLIC_IDS)

    def test_no_pack_assembled_is_emitted(self, world: World) -> None:
        world.post("external", "/api/v1/packs", {"intent": QUERY})
        world.post(
            "external",
            "/api/v1/packs/sectioned",
            {"intent": QUERY, "sections": [{"name": "A"}]},
        )
        assert world.events(EventType.PACK_ASSEMBLED) == []
        assert len(world.events(EventType.PUBLIC_PACK_SERVED)) == 2

    def test_an_ordinary_key_still_emits_pack_assembled(self, world: World) -> None:
        """The control: the event the external key must not emit is real."""
        world.post("read", "/api/v1/packs", {"intent": QUERY})
        assert len(world.events(EventType.PACK_ASSEMBLED)) == 1
        assert world.events(EventType.PUBLIC_PACK_SERVED) == []


class TestNoFeedbackRow:
    """No reader needs a feedback row for an external pack: the key cannot write one."""

    def test_an_external_key_cannot_grade_its_own_pack(self, world: World) -> None:
        pack = world.post("external", "/api/v1/packs", {"intent": QUERY})
        resp = world.client.post(
            f"/api/v1/packs/{pack['pack_id']}/feedback",
            json={"success": True, "helpful_item_ids": _served_ids(pack)},
            headers=world.headers["external"],
        )
        assert resp.status_code == 403, resp.text
        assert world.events(EventType.FEEDBACK_RECORDED) == []


class TestHoldout:
    def test_an_external_pack_is_never_held_out(
        self, world: World, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        pack = world.post("external", "/api/v1/packs", {"intent": QUERY})
        sectioned = world.post(
            "external",
            "/api/v1/packs/sectioned",
            {"intent": QUERY, "sections": [{"name": "A"}]},
        )
        assert _served_ids(pack)
        assert _served_ids(sectioned)

    def test_an_ordinary_pack_is_held_out_at_the_same_rate(
        self, world: World, monkeypatch
    ) -> None:
        """The control: at rate 1.0 the holdout does withhold a pack."""
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        pack = world.post("read", "/api/v1/packs", {"intent": QUERY})
        assert _served_ids(pack) == []

    def test_the_rate_does_not_change_what_an_external_key_receives(
        self, world: World, monkeypatch
    ) -> None:
        off = world.post("external", "/api/v1/packs", {"intent": QUERY})
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        on = world.post("external", "/api/v1/packs", {"intent": QUERY})
        assert not differences(mask(off), mask(on))


class TestReaders:
    """An external key's packs reach no reader of ``PACK_ASSEMBLED``."""

    #: A fixed window end, so two reports differ only if the log did.
    UNTIL = datetime.now(UTC) + timedelta(hours=1)

    def _reports(self, world: World) -> tuple[Any, Any]:
        log = world.registry.operational.event_log
        holdout = analyze_holdout(
            log, days=1, until=self.UNTIL, permutations=50, bootstraps=50
        )
        effectiveness = analyze_effectiveness(log, days=1)
        return holdout, effectiveness

    def test_external_packs_change_nothing_a_reader_consumes(
        self, world: World, monkeypatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_PACK_HOLDOUT_RATE", "1.0")
        before_h, before_e = self._reports(world)
        for _ in range(3):
            world.post("external", "/api/v1/packs", {"intent": QUERY})
            world.post(
                "external",
                "/api/v1/packs/sectioned",
                {"intent": QUERY, "sections": [{"name": "A"}]},
            )
        after_h, after_e = self._reports(world)
        assert after_e.total_packs == before_e.total_packs == 0
        assert not differences(
            mask(before_h.model_dump(mode="json")),
            mask(after_h.model_dump(mode="json")),
        )
        assert not differences(
            mask(before_e.model_dump(mode="json")),
            mask(after_e.model_dump(mode="json")),
        )

    def test_the_readers_do_see_an_ordinary_pack(self, world: World) -> None:
        """The control: without it the test above could be reading nothing."""
        _, before_e = self._reports(world)
        world.post("read", "/api/v1/packs", {"intent": QUERY})
        _, after_e = self._reports(world)
        assert after_e.total_packs == before_e.total_packs + 1
