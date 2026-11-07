"""The REST doors keep ``precedent://learning/`` for the promotion (#1080).

Entity doors: ``POST /entities``, ``POST /ingest/bulk`` and ``POST
/commands/batch`` all run ``entity.create`` without the reserved
``learning_promotion`` mark, so a create at a namespace id is refused
(``precedent_namespace_via_entity_create``) and a caller cannot supply the mark
(``/commands/batch`` answers 400 for it, before any command runs).

Vector door: ``POST /vectors`` refuses a row at a namespace id with a 422 for
every caller, the whole batch unwritten. The promotion's own vector row is
written in process with its document, never through this route, and a real
promotion still writes it.

Defective subject: drop the new refusal in ``EntityCreateHandler`` and the
entity tests store the node; drop the prefix check in ``_vector_item_problem``
and the vector tests store a row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.core.precedent_document import (
    PRECEDENT_ENTITY_ID_PREFIX,
    PROMOTION_COMMAND_MARK,
    SOURCE_EXCERPT_STATUS_KEY,
)
from trellis.learning.scoring import submit_learning_promotion
from trellis.mutate import build_curate_executor
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

CODE = "precedent_namespace_via_entity_create"
NODE_ID = f"{PRECEDENT_ENTITY_ID_PREFIX}p1"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


def _rejection_reasons(registry: StoreRegistry) -> list[str]:
    return [
        e.payload["reason"]
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
    ]


def _entity(entity_type: str = "precedent", **extra: Any) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": NODE_ID,
        "name": "Pooling",
        **extra,
    }


def _promote(registry: StoreRegistry) -> dict[str, Any]:
    return submit_learning_promotion(
        build_curate_executor(registry, clearance="restricted"),
        entity_payload={
            "entity_type": "precedent",
            "entity_id": NODE_ID,
            "name": "Pooling",
            "properties": {
                "summary": "pool connections",
                SOURCE_EXCERPT_STATUS_KEY: "source_included",
            },
        },
        edge_payloads=[],
        graph_store=registry.knowledge.graph_store,
        event_log=registry.operational.event_log,
        document_store=registry.knowledge.document_store,
        requested_by="test",
    )


class TestEntityDoors:
    def test_post_entities_refuses_a_precedent_at_a_namespace_id(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post("/api/v1/entities", json=_entity())

        assert resp.status_code == 400, resp.text
        assert _rejection_reasons(registry) == [CODE]
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_post_entities_refuses_an_upsert_over_a_promoted_precedent(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        assert _promote(registry)["status"] != "entity_failed"
        before = registry.knowledge.graph_store.get_node(NODE_ID)

        resp = client.post(
            "/api/v1/entities", json=_entity(properties={"summary": "forged"})
        )

        assert resp.status_code == 400, resp.text
        assert _rejection_reasons(registry) == [CODE]
        assert registry.knowledge.graph_store.get_node(NODE_ID) == before

    def test_bulk_ingest_refuses_it_per_item_and_writes_the_rest(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/ingest/bulk",
            json={
                "entities": [
                    _entity(),
                    {"entity_type": "concept", "entity_id": "n1", "name": "Ok"},
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        statuses = [r["status"] for r in resp.json()["entities"]["results"]]
        assert statuses == ["rejected", "success"]
        assert _rejection_reasons(registry) == [CODE]
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None
        assert registry.knowledge.graph_store.get_node("n1") is not None

    def test_commands_batch_rejects_it_and_audits_the_code(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [{"operation": "entity.create", "args": _entity()}],
                "strategy": "sequential",
            },
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "rejected", result
        assert _rejection_reasons(registry) == [CODE]
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_a_planted_promotion_mark_is_a_400_and_writes_nothing(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": _entity(),
                        "metadata": {PROMOTION_COMMAND_MARK: True},
                    }
                ],
                "strategy": "sequential",
            },
        )

        assert resp.status_code == 400, resp.text
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_a_mark_in_the_args_is_only_another_argument(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.create",
                        "args": _entity(
                            **{
                                PROMOTION_COMMAND_MARK: True,
                                "metadata": {PROMOTION_COMMAND_MARK: True},
                            }
                        ),
                    }
                ],
                "strategy": "sequential",
            },
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "rejected", result
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_an_ordinary_entity_is_unaffected(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/entities",
            json={"entity_type": "precedent", "entity_id": "n1", "name": "Plain"},
        )

        assert resp.status_code == 200, resp.text

    def test_a_promoted_precedent_is_still_edited_through_entity_update(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        assert _promote(registry)["status"] != "entity_failed"

        resp = client.post(
            "/api/v1/commands/batch",
            json={
                "commands": [
                    {
                        "operation": "entity.update",
                        "args": {
                            "entity_id": NODE_ID,
                            "properties": {"note": "edited"},
                        },
                    }
                ],
                "strategy": "sequential",
            },
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "success", result
        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node is not None
        assert node["properties"]["note"] == "edited"


def _vectors(registry: StoreRegistry, *item_ids: str, **row: Any) -> dict[str, Any]:
    width = registry.knowledge.vector_store.dimensions or 8
    return {
        "vectors": [
            {"item_id": item_id, "vector": [0.5] * width, **row} for item_id in item_ids
        ]
    }


class TestVectorDoor:
    @pytest.fixture
    def admin_headers(self, registry: StoreRegistry) -> dict[str, str]:
        token, record = generate_api_key("k", ["admin"])
        registry.operational.api_key_store.create(record)
        return {"Authorization": f"Bearer {token}"}

    @pytest.mark.parametrize(
        "item_id",
        [
            NODE_ID,
            f"{PRECEDENT_ENTITY_ID_PREFIX}ghost",
            PRECEDENT_ENTITY_ID_PREFIX,
            f"{NODE_ID}#chunk-1",
        ],
    )
    def test_a_row_at_a_namespace_id_is_a_422_and_nothing_is_written(
        self, client: TestClient, registry: StoreRegistry, item_id: str
    ) -> None:
        resp = client.post(
            "/api/v1/vectors", json=_vectors(registry, "doc-ok", item_id)
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith("vectors[1].item_id")
        assert PRECEDENT_ENTITY_ID_PREFIX in resp.json()["detail"]
        assert registry.knowledge.vector_store.count() == 0

    def test_an_admin_key_is_refused_too(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

        resp = client.post(
            "/api/v1/vectors", json=_vectors(registry, NODE_ID), headers=admin_headers
        )

        assert resp.status_code == 422, resp.text
        assert registry.knowledge.vector_store.count() == 0

    def test_a_row_that_carries_a_promotion_looking_metadata_lifts_nothing(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/vectors",
            json=_vectors(
                registry,
                NODE_ID,
                metadata={
                    PROMOTION_COMMAND_MARK: True,
                    "node_type": "precedent",
                    "content": "forged lesson",
                },
            ),
        )

        assert resp.status_code == 422, resp.text
        assert registry.knowledge.vector_store.get(NODE_ID) is None

    @pytest.mark.parametrize(
        "item_id",
        [
            "doc-a",
            "precedent://other/x",
            "Precedent://learning/x",
            "x/precedent://learning/x",
            "precedent:/learning/x",
        ],
    )
    def test_an_id_that_merely_resembles_the_namespace_is_written(
        self, client: TestClient, registry: StoreRegistry, item_id: str
    ) -> None:
        resp = client.post("/api/v1/vectors", json=_vectors(registry, item_id))

        assert resp.status_code == 200, resp.text
        assert resp.json()["upserted"] == 1
        assert registry.knowledge.vector_store.get(item_id) is not None

    def test_the_promotions_own_vector_row_is_still_written(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        width = registry.knowledge.vector_store.dimensions or 8
        monkeypatch.setattr(
            "trellis.retrieve.embed_ingest_hook.embed_on_ingest_enabled",
            lambda registry=None: True,
        )
        monkeypatch.setattr(
            type(registry),
            "embedding_fn",
            property(lambda _self: lambda _text: [1.0] + [0.0] * (width - 1)),
        )

        outcome = _promote(registry)

        assert outcome["status"] != "entity_failed", outcome
        assert registry.knowledge.vector_store.get(NODE_ID) is not None
