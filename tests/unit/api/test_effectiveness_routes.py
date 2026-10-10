"""Tests for ``POST /api/v1/effectiveness/apply-noise-tags``.

Narrow coverage for the noise-refused-non-document fix: the route's
``noise_candidates_tagged`` must count what ``apply_noise_tags`` actually
wrote, not what the demotion gate admitted, and
``noise_candidates_refused_not_document`` must name the remainder (a
gate admission — citation evidence alone, no notion of which store an
id belongs to — that resolves to no document).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

PHANTOM = "ar:trace:phantom"


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    import trellis_api.app as app_module

    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """App with just the admin router, auth OFF (the default with no env)."""

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    from trellis_api.routes import admin

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    return TestClient(app)


def _seed_phantom_admission(registry: StoreRegistry) -> None:
    """Five packs citing a non-document id unhelpful — gate-admissible.

    Same shape as ``tests/unit/cli/test_worker.py``'s
    ``test_live_run_separates_written_from_refused_not_document``: the
    demotion gate admits on citation evidence alone, so a trace id
    reaches ``apply_noise_tags`` here too, and writes nothing.
    """
    event_log = registry.operational.event_log
    for i in range(5):
        pack_id = f"ar-phantom-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test intent",
                "intent_family": "ar-test",
                "domain": "ar-test",
                "injected_item_ids": [PHANTOM],
                "injected_items": [
                    {
                        "item_id": PHANTOM,
                        "item_type": "trace",
                        "rank": 0,
                        "strategy_source": "document",
                    }
                ],
            },
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "run_id": f"ar-run-{i}",
                "intent_family": "ar-test",
                "outcome": "failure",
                "success": False,
                "helpful_item_ids": [],
                "unhelpful_item_ids": [PHANTOM],
            },
        )


class TestApplyNoiseTagsRoute:
    def test_empty_store_tags_nothing(self, client: TestClient) -> None:
        resp = client.post("/api/v1/effectiveness/apply-noise-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["noise_candidates_tagged"] == 0
        assert data["noise_candidates_proposed"] == 0
        assert data["noise_candidates_refused_not_document"] == 0

    def test_a_non_document_admission_is_refused_not_tagged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """A gate-admitted trace id is counted as refused, not tagged.

        Before this fix the route echoed the gate's admission count under
        ``noise_candidates_tagged`` (1) with no way to say it wrote
        nothing; it now reports 0 tagged, 1 refused.
        """
        _seed_phantom_admission(registry)
        resp = client.post("/api/v1/effectiveness/apply-noise-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["demotion_screen"]["admitted"] == [PHANTOM]
        assert data["noise_candidates_tagged"] == 0
        assert data["noise_candidates_refused_not_document"] == 1
        assert data["noise_tags_written"] == 0
        assert data["noise_refused_not_document"] == [PHANTOM]
        assert registry.knowledge.document_store.get(PHANTOM) is None
