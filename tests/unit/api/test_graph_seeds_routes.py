"""A seeded graph item reaches a REST caller without the seed list.

``POST /api/v1/packs`` and ``POST /api/v1/packs/sectioned`` dump each served
item's metadata whole, so any key left on an item is sent once per item. The
seeds the graph axis expanded from are recorded once, on ``PACK_ASSEMBLED``,
and must not ride on the items as well.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.retrieve.strategies import GRAPH_SEED_IDS_KEY, GRAPH_SELECTION_SEEDED
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import retrieve

_SEED = "domain:billing"
_NEIGHBOUR = "concept:invoice-retries"
#: Names ``domain:billing``, so the graph axis expands a seed set.
_INTENT = "billing runbook"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    graph = reg.knowledge.graph_store
    graph.upsert_node(_SEED, "domain", {"name": "billing"})
    graph.upsert_node(_NEIGHBOUR, "concept", {"name": "invoice retries"})
    graph.upsert_edge(_SEED, _NEIGHBOUR, "related_to")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> Iterator[TestClient]:
    @asynccontextmanager
    async def noop_lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    with TestClient(app) as c:
        yield c


def _graph_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["metadata"].get("source_strategy") == "graph"]


def _event_payloads(registry: StoreRegistry) -> list[dict[str, Any]]:
    events = registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=10
    )
    return [e.payload for e in events]


def test_flat_response_items_carry_no_seed_list(
    client: TestClient, registry: StoreRegistry
) -> None:
    response = client.post("/api/v1/packs", json={"intent": _INTENT})

    assert response.status_code == 200, response.text
    body = response.json()
    graph_items = _graph_items(body["items"])
    # Precondition: a seeded graph item really is in the response, so the
    # absence below is the strip and not an empty pack.
    assert graph_items
    assert all(
        i["metadata"]["graph_selection"] == GRAPH_SELECTION_SEEDED for i in graph_items
    )
    assert GRAPH_SEED_IDS_KEY not in json.dumps(body)
    (payload,) = _event_payloads(registry)
    assert payload["seed_entity_ids"] == [_SEED]


def test_sectioned_response_items_carry_no_seed_list(
    client: TestClient, registry: StoreRegistry
) -> None:
    response = client.post(
        "/api/v1/packs/sectioned",
        json={"intent": _INTENT, "sections": [{"name": "everything"}]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    served = [i for section in body["sections"] for i in section["items"]]
    graph_items = _graph_items(served)
    assert graph_items
    assert all(
        i["metadata"]["graph_selection"] == GRAPH_SELECTION_SEEDED for i in graph_items
    )
    assert GRAPH_SEED_IDS_KEY not in json.dumps(body)
    (payload,) = _event_payloads(registry)
    assert GRAPH_SEED_IDS_KEY not in json.dumps(payload)


def test_sectioned_entity_ids_seed_the_graph_axis(
    client: TestClient, registry: StoreRegistry
) -> None:
    """The REST route builds through the same ``build_sectioned``, so a
    section's ``entity_ids`` anchor the graph axis there too — the intent
    here names nothing resolvable."""
    response = client.post(
        "/api/v1/packs/sectioned",
        json={
            "intent": "do the next step",
            "sections": [{"name": "everything", "entity_ids": [_SEED]}],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    served = [i for section in body["sections"] for i in section["items"]]
    by_id = {i["item_id"]: i for i in _graph_items(served)}
    assert _NEIGHBOUR in by_id
    assert by_id[_NEIGHBOUR]["metadata"]["graph_selection"] == "caller_seeded"
    assert GRAPH_SEED_IDS_KEY not in json.dumps(body)
