"""``GET /api/v1/graph/search`` and ``/facets``: the graph page's list and chips.

The facet counts every current node per stored ``node_type`` server-side,
under the same ``q`` the list applies, so its counts sum to the list's
``total`` however many pages the list spans.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.base.graph import GraphStore
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import retrieve

FACETS = "/api/v1/graph/search/facets"
SEARCH = "/api/v1/graph/search"

# More nodes of one type than a single /graph/search page holds (limit <= 500).
ACTIVITY_COUNT = 600


@pytest.fixture
def store(tmp_path: Path) -> Iterator[GraphStore]:
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    yield registry.knowledge.graph_store
    registry.close()
    app_module._registry = None


@pytest.fixture
def client(store: GraphStore) -> Iterator[TestClient]:
    @asynccontextmanager
    async def noop_lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    with TestClient(app) as c:
        yield c


def _list_total(client: TestClient, **params: str) -> int:
    resp = client.get(SEARCH, params={"limit": "1", **params})
    assert resp.status_code == 200
    return int(resp.json()["total"])


def _facets(client: TestClient, **params: str) -> dict[str, Any]:
    resp = client.get(FACETS, params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    return body


def test_search_lists_current_nodes_on_a_sqlite_store(
    client: TestClient, store: GraphStore
) -> None:
    store.upsert_node("n-1", "Activity", {"name": "alpha"})
    store.upsert_node("n-2", "concept", {"name": "beta"})
    store.upsert_node("n-3", "Concept", {"name": "gamma"})
    store.upsert_node("n-1", "Activity", {"name": "alpha, renamed"})

    resp = client.get(SEARCH, params={"sort": "name", "order": "asc"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert [(r["entity_id"], r["node_type"], r["name"]) for r in body["results"]] == [
        ("n-1", "Activity", "alpha, renamed"),
        ("n-2", "concept", "beta"),
        ("n-3", "Concept", "gamma"),
    ]


def test_search_pages_by_offset_and_normalises_sort_and_order(
    client: TestClient, store: GraphStore
) -> None:
    # Created in this order, which is not the order of their names.
    for node_id, name in [
        ("n-1", "delta"),
        ("n-2", "alpha"),
        ("n-3", "echo"),
        ("n-4", "charlie"),
        ("n-5", "bravo"),
    ]:
        store.upsert_node(node_id, "Activity", {"name": name})
        time.sleep(0.005)
    store.upsert_node("n-6", "concept", {"name": "aardvark"})

    def page(**params: str) -> dict[str, Any]:
        resp = client.get(SEARCH, params={"node_type": "Activity", **params})
        assert resp.status_code == 200, resp.text
        return resp.json()

    pages = [page(sort="name", order="asc", limit="2", offset=o) for o in "024"]

    assert [[r["name"] for r in p["results"]] for p in pages] == [
        ["alpha", "bravo"],
        ["charlie", "delta"],
        ["echo"],
    ]
    assert [(p["total"], p["count"], p["offset"]) for p in pages] == [
        (5, 2, 0),
        (5, 2, 2),
        (5, 1, 4),
    ]
    # order ignores case and anything but asc descends; an unknown sort is
    # created_at.
    by_name = ["n-2", "n-5", "n-4", "n-1", "n-3"]
    assert [r["entity_id"] for r in page(sort="name", order="ASC")["results"]] == (
        by_name
    )
    assert [
        r["entity_id"] for r in page(sort="name", order="sideways")["results"]
    ] == by_name[::-1]
    assert [r["entity_id"] for r in page(sort="bogus", order="asc")["results"]] == [
        "n-1",
        "n-2",
        "n-3",
        "n-4",
        "n-5",
    ]


def test_facets_count_every_type_beyond_one_list_page(
    client: TestClient, store: GraphStore
) -> None:
    store.upsert_nodes_bulk(
        [
            {
                "node_id": f"act-{i:04d}",
                "node_type": "Activity",
                "properties": {"name": f"run step {i}"},
            }
            for i in range(ACTIVITY_COUNT)
        ]
    )
    for node_id, node_type in [
        ("c-low-1", "concept"),
        ("c-low-2", "concept"),
        ("c-low-3", "concept"),
        ("c-up-1", "Concept"),
        ("c-up-2", "Concept"),
        ("p-1", "Person"),
        ("app-1", "SoftwareApplication"),
    ]:
        store.upsert_node(node_id, node_type, {"name": node_id})

    body = _facets(client)

    # Every type, in stored case, by count descending then type ascending.
    assert body["node_types"] == [
        {"node_type": "Activity", "count": ACTIVITY_COUNT},
        {"node_type": "concept", "count": 3},
        {"node_type": "Concept", "count": 2},
        {"node_type": "Person", "count": 1},
        {"node_type": "SoftwareApplication", "count": 1},
    ]
    assert body["total"] == ACTIVITY_COUNT + 7
    assert body["total"] == sum(row["count"] for row in body["node_types"])
    assert body["total"] == _list_total(client)


def test_facets_honour_q_as_the_list_does(
    client: TestClient, store: GraphStore
) -> None:
    store.upsert_node("n-1", "Activity", {"name": "Deploy the Widget"})
    store.upsert_node("n-2", "Activity", {"name": "widget smoke test"})
    store.upsert_node("widget-3", "concept", {"name": "unrelated"})
    store.upsert_node("n-4", "WidgetKind", {"name": "also unrelated"})
    store.upsert_node("n-5", "Activity", {"name": "gadget"})
    store.upsert_node("n-6", "concept", {"name": "sprocket"})
    store.upsert_node("n-7", "Person", {"name": "someone"})

    body = _facets(client, q="WIDGET")

    # Matched by name (twice), by node_id and by node_type, ignoring case.
    assert body["node_types"] == [
        {"node_type": "Activity", "count": 2},
        {"node_type": "WidgetKind", "count": 1},
        {"node_type": "concept", "count": 1},
    ]
    # The list agrees with the facet, per type and in total, for the same q.
    assert body["total"] == 4 == _list_total(client, q="WIDGET")
    for row in body["node_types"]:
        assert row["count"] == _list_total(
            client, q="WIDGET", node_type=row["node_type"]
        )

    nothing = _facets(client, q="no-node-has-this")
    assert nothing["node_types"] == []
    assert nothing["total"] == 0


@pytest.mark.parametrize("q", ["", "%", "_", "\\", " ", "É", "é", "ß"])
def test_facets_agree_with_the_list_on_wildcards_blanks_and_case(
    client: TestClient, store: GraphStore, q: str
) -> None:
    store.upsert_node("n-1", "Activity", {"name": "100% done"})
    store.upsert_node("n_2", "concept", {"name": "snake_case name"})
    store.upsert_node("n-3", "Concept", {"name": "Éclair widget"})
    store.upsert_node("n-4", "Person", {"name": "plain"})
    store.upsert_node("n-5", "Person", {"name": "back\\slash"})
    store.upsert_node("n-6", "Place", {"name": "Straße café"})

    body = _facets(client, q=q)

    # What %, _, \ and non-ASCII case match differs by backend; the facet
    # applies the list's own predicate, so the two agree.
    total = _list_total(client, q=q)
    assert total > 0
    assert body["total"] == total
    for row in body["node_types"]:
        assert row["count"] == _list_total(client, q=q, node_type=row["node_type"])


def test_facets_count_current_versions_only(
    client: TestClient, store: GraphStore
) -> None:
    store.upsert_node("svc", "SoftwareApplication", {"name": "api", "v": 1})
    store.upsert_node("moved", "legacy_kind", {"name": "renamed later", "v": 1})
    store.upsert_node("act-1", "Activity", {"name": "first"})
    store.upsert_node("act-2", "Activity", {"name": "second"})
    store.upsert_node("act-3", "Activity", {"name": "third"})
    # Each re-upsert closes the previous SCD-2 version.
    store.upsert_node("svc", "SoftwareApplication", {"name": "api", "v": 2})
    store.upsert_node("svc", "SoftwareApplication", {"name": "api", "v": 3})
    store.upsert_node("moved", "Activity", {"name": "renamed later", "v": 2})

    body = _facets(client)

    assert body["node_types"] == [
        {"node_type": "Activity", "count": 4},
        {"node_type": "SoftwareApplication", "count": 1},
    ]
    assert body["total"] == 5 == store.count_nodes() == _list_total(client)
    # A q that only a closed version's type matched finds nothing.
    assert _facets(client, q="legacy")["node_types"] == []
