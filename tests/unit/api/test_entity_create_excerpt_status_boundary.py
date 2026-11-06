"""``POST /commands/batch`` cannot forge a precedent's ``source_excerpt_status``.

``entity.create`` replaces a node's whole bag, so it could set the stamp the
promotion writes and serving trusts (#1080). The handler refuses the stamp on a
precedent unless the command carries the reserved ``learning_promotion`` mark,
and this door must not be able to supply that mark: a caller's per-item
``metadata`` naming it is a 400 before any command runs.

Defective subject: drop the guard in ``EntityCreateHandler`` and the first test
stores the stamp; drop ``learning_promotion`` from
``RESERVED_COMMAND_METADATA_KEYS`` and the second stores it too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.core.precedent_document import (
    PROMOTION_COMMAND_MARK,
    SOURCE_EXCERPT_STATUS_KEY,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

CODE = "precedent_excerpt_status_via_entity_create"


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)


def _body(
    properties: dict[str, Any],
    *,
    entity_type: str = "precedent",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "operation": "entity.create",
        "args": {
            "entity_type": entity_type,
            "entity_id": "n1",
            "name": "Pooling",
            "properties": properties,
        },
    }
    if metadata is not None:
        command["metadata"] = metadata
    return {"commands": [command], "strategy": "sequential"}


def _seed_precedent(registry: StoreRegistry) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id="n1",
        node_type="precedent",
        properties={"name": "Pooling", SOURCE_EXCERPT_STATUS_KEY: "source_included"},
    )


class TestTheStampIsRefused:
    @pytest.mark.parametrize("existing", [True, False], ids=["existing", "new"])
    def test_a_caller_set_status_on_a_precedent_is_rejected_and_audited(
        self, client: TestClient, registry: StoreRegistry, existing: bool
    ) -> None:
        if existing:
            _seed_precedent(registry)

        resp = client.post(
            "/api/v1/commands/batch",
            json=_body({SOURCE_EXCERPT_STATUS_KEY: "source_labelled"}),
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "rejected", result
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
        assert [e.payload["reason"] for e in rejected] == [CODE]
        node = registry.knowledge.graph_store.get_node("n1")
        if existing:
            assert node is not None
            assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"
        else:
            assert node is None

    def test_a_planted_promotion_mark_is_a_400_and_writes_nothing(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json=_body(
                {SOURCE_EXCERPT_STATUS_KEY: "source_labelled"},
                metadata={PROMOTION_COMMAND_MARK: True},
            ),
        )

        assert resp.status_code == 400, resp.text
        assert PROMOTION_COMMAND_MARK in resp.json()["detail"]
        assert registry.knowledge.graph_store.get_node("n1") is None
        assert (
            registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            == []
        )


class TestOrdinaryWritesAreUnaffected:
    def test_the_property_on_a_non_precedent_is_accepted(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json=_body({SOURCE_EXCERPT_STATUS_KEY: "x"}, entity_type="concept"),
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "success", result
        node = registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "x"

    def test_a_precedent_create_without_the_status_is_accepted(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/v1/commands/batch", json=_body({"summary": "pool connections"})
        )

        assert resp.status_code == 200, resp.text
        [result] = resp.json()["results"]
        assert result["status"] == "success", result
