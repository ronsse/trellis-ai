"""Tests for ``POST /api/v1/effectiveness/apply-noise-tags``.

Narrow coverage for the #833 fix: the route's
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
from trellis.stores.settings_store import SettingsStore, resolve_settings_path

PHANTOM = "ar:trace:phantom"
NOISY_DOC = "ar:doc:noisy"


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


def _seed_document_admission(registry: StoreRegistry) -> None:
    """Five packs citing a real document unhelpful — gate-admissible and
    resolvable, so ``apply_noise_tags`` writes it rather than refusing it.
    """
    event_log = registry.operational.event_log
    for i in range(5):
        pack_id = f"ar-doc-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test intent",
                "intent_family": "ar-test",
                "domain": "ar-test",
                "injected_item_ids": [NOISY_DOC],
                "injected_items": [
                    {
                        "item_id": NOISY_DOC,
                        "item_type": "document",
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
                "run_id": f"ar-doc-run-{i}",
                "intent_family": "ar-test",
                "outcome": "failure",
                "success": False,
                "helpful_item_ids": [],
                "unhelpful_item_ids": [NOISY_DOC],
            },
        )
    registry.knowledge.document_store.put(
        NOISY_DOC,
        "noisy test content",
        {"content_tags": {"signal_quality": "standard"}},
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

    def test_a_document_admission_is_tagged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """A gate-admitted id that resolves to a document is written.

        Mutant coverage: a REST route that reported a hardcoded 0 (or
        otherwise never read the write count) would still pass
        ``test_a_non_document_admission_is_refused_not_tagged`` and
        ``test_empty_store_tags_nothing``, both of which assert 0. This
        pins the positive case the unit suite was missing.
        """
        _seed_document_admission(registry)
        resp = client.post("/api/v1/effectiveness/apply-noise-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["demotion_screen"]["admitted"] == [NOISY_DOC]
        assert data["noise_candidates_tagged"] == 1
        assert data["noise_candidates_refused_not_document"] == 0
        assert data["noise_tags_written"] == 1
        assert data["noise_refused_not_document"] == []
        noisy = registry.knowledge.document_store.get(NOISY_DOC)
        assert noisy is not None
        assert noisy["metadata"]["content_tags"]["signal_quality"] == "noise"


THIN_DOC = "ar:doc:thin"


def _seed_thin_coverage_admission(registry: StoreRegistry) -> None:
    """Two packs citing a real document unhelpful -- below the module
    default coverage floor (5), so the gate refuses by default.
    """
    event_log = registry.operational.event_log
    for i in range(2):
        pack_id = f"ar-thin-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test intent",
                "intent_family": "ar-test",
                "domain": "ar-test",
                "injected_item_ids": [THIN_DOC],
                "injected_items": [
                    {
                        "item_id": THIN_DOC,
                        "item_type": "document",
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
                "run_id": f"ar-thin-run-{i}",
                "intent_family": "ar-test",
                "outcome": "failure",
                "success": False,
                "helpful_item_ids": [],
                "unhelpful_item_ids": [THIN_DOC],
            },
        )
    registry.knowledge.document_store.put(
        THIN_DOC,
        "thin coverage test content",
        {"content_tags": {"signal_quality": "standard"}},
    )


class TestEffectivenessRouteSettingsWiring:
    """Re-gate #867 M12: both the plain analysis route and the
    apply-noise-tags route must thread a *stored* settings override
    through to the demotion gate, not just accept it at the resolver
    level (``tests/unit/retrieve/test_effectiveness.py`` proves the
    resolver is read; this proves the REST route actually loads the
    store and passes it along).
    """

    def test_get_effectiveness_honours_a_stored_coverage_floor(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed_thin_coverage_admission(registry)

        default_resp = client.get("/api/v1/effectiveness")
        assert default_resp.status_code == 200
        assert default_resp.json()["demotion_screen"]["admitted"] == []

        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("noise_demotion_min_attributed_packs", 2)

        overridden_resp = client.get("/api/v1/effectiveness")
        assert overridden_resp.status_code == 200
        assert overridden_resp.json()["demotion_screen"]["admitted"] == [THIN_DOC]

    def test_apply_noise_tags_honours_a_stored_coverage_floor(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed_thin_coverage_admission(registry)

        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("noise_demotion_min_attributed_packs", 2)

        resp = client.post("/api/v1/effectiveness/apply-noise-tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["demotion_screen"]["admitted"] == [THIN_DOC]
        assert data["noise_tags_written"] == 1
        noisy = registry.knowledge.document_store.get(THIN_DOC)
        assert noisy is not None
        assert noisy["metadata"]["content_tags"]["signal_quality"] == "noise"
