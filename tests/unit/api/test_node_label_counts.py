"""Counts and shape on the REST graph doors agree with what they withhold (#1264).

A node labelled above the key is left out of ``GET /graph/search``'s page
(#1029). It is now left out of ``total`` and of every
``/graph/search/facets`` bucket as well, exactly as a withheld promoted
precedent already was (#1080): nothing reports the subtraction. Paging stays
in store order, so a page holding a withheld row is short and walking the
offsets serves each readable match once.

``GET /entities/{id}`` with ``depth`` above 1 no longer returns a node whose
every path from the root runs through a withheld node; a node also reached by
a path of readable nodes is still returned.

A cleared key, auth off, and a graph with no labelled node answer exactly as
before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

#: A key below the label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "restricted"]
_LABEL = {DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"}}
QUERY = "Orchard"

ROOT = "entity://root"
GATE = "entity://gate"
FAR = "entity://far"
SIDE = "entity://side"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _props(name: str, *, label: bool) -> dict[str, Any]:
    props: dict[str, Any] = {"name": name}
    if label:
        props.update(_LABEL)
    return props


def _seed_search(registry: StoreRegistry, *, label: bool) -> None:
    """Five matches over two types; ``entity://m2`` (a ``table``) may be labelled."""
    graph = registry.knowledge.graph_store
    for i in range(5):
        node_type = "table" if i % 2 == 0 else "service"
        graph.upsert_node(
            f"entity://m{i}",
            node_type,
            _props(f"Orchard {i}", label=label and i == 2),
        )
    graph.upsert_node("entity://other", "table", {"name": "Unrelated"})


def _get(client, path, params=None, headers=None) -> dict[str, Any]:
    resp = client.get(path, params=params or {}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _search(client, registry, scopes, **params) -> dict[str, Any]:
    return _get(
        client,
        "/api/v1/graph/search",
        {"q": QUERY, "sort": "name", "order": "asc", **params},
        _headers(registry, scopes),
    )


def _facets(client, registry, scopes) -> dict[str, Any]:
    return _get(
        client,
        "/api/v1/graph/search/facets",
        {"q": QUERY},
        _headers(registry, scopes),
    )


# ---------------------------------------------------------------------------
# total and facets
# ---------------------------------------------------------------------------


def test_search_total_leaves_out_a_labelled_node(client, registry) -> None:
    _seed_search(registry, label=True)
    below = _search(client, registry, BELOW)
    cleared = _search(client, registry, CLEARED)

    assert cleared["total"] == 5
    assert below["total"] == 4
    assert below["total"] == len(below["results"])
    assert "entity://m2" not in {r["entity_id"] for r in below["results"]}


def test_search_total_with_a_node_type_filter(client, registry) -> None:
    _seed_search(registry, label=True)
    tables = _search(client, registry, BELOW, node_type="table")
    services = _search(client, registry, BELOW, node_type="service")

    assert tables["total"] == 2  # m0, m4; m2 withheld
    assert services["total"] == 2  # m1, m3; nothing withheld


def test_facets_leave_out_a_labelled_node(client, registry) -> None:
    _seed_search(registry, label=True)
    below = _facets(client, registry, BELOW)
    cleared = _facets(client, registry, CLEARED)

    assert cleared["node_types"] == [
        {"node_type": "table", "count": 3},
        {"node_type": "service", "count": 2},
    ]
    assert below["node_types"] == [
        {"node_type": "service", "count": 2},
        {"node_type": "table", "count": 2},
    ]
    assert below["total"] == 4
    assert below["total"] == _search(client, registry, BELOW)["total"]


def test_a_type_whose_only_match_is_labelled_has_no_bucket(client, registry) -> None:
    graph = registry.knowledge.graph_store
    graph.upsert_node("entity://a", "table", {"name": "Orchard a"})
    graph.upsert_node("entity://b", "vault", _props("Orchard b", label=True))

    below = _facets(client, registry, BELOW)
    assert below["node_types"] == [{"node_type": "table", "count": 1}]
    assert "vault" not in json.dumps(below)


def test_paging_across_a_withheld_row(client, registry) -> None:
    """Offsets stay in store order: the page holding the withheld row is short."""
    _seed_search(registry, label=True)
    seen: list[str] = []
    pages: list[dict[str, Any]] = []
    for offset in range(0, 6, 2):
        page = _search(client, registry, BELOW, limit=2, offset=offset)
        pages.append(page)
        seen.extend(r["entity_id"] for r in page["results"])

    # Sorted by name ascending: m0 m1 | m2 m3 | m4 -- m2 is withheld.
    assert [p["count"] for p in pages] == [2, 1, 1]
    assert [p["offset"] for p in pages] == [0, 2, 4]
    assert seen == ["entity://m0", "entity://m1", "entity://m3", "entity://m4"]
    assert all(p["total"] == len(seen) for p in pages)


# ---------------------------------------------------------------------------
# depth > 1
# ---------------------------------------------------------------------------


def _entity(client, registry, scopes, depth) -> dict[str, Any]:
    return _get(
        client,
        f"/api/v1/entities/{ROOT}",
        {"depth": depth},
        _headers(registry, scopes),
    )


def _node_ids(body: dict[str, Any]) -> set[str]:
    return {n["node_id"] for n in body["subgraph"]["nodes"]}


def _edge_ends(body: dict[str, Any]) -> set[str]:
    return {
        end
        for e in body["subgraph"]["edges"]
        for end in (e["source_id"], e["target_id"])
    }


def _seed_chain(registry: StoreRegistry, *, label: bool) -> None:
    """root -> gate -> far, with the gate optionally labelled."""
    graph = registry.knowledge.graph_store
    graph.upsert_node(ROOT, "entity", {"name": "Root"})
    graph.upsert_node(GATE, "entity", _props("Gate", label=label))
    graph.upsert_node(FAR, "entity", {"name": "Far"})
    graph.upsert_edge(ROOT, GATE, "feeds_into")
    graph.upsert_edge(GATE, FAR, "feeds_into")


def test_depth_two_leaves_out_a_node_reached_only_through_a_withheld_one(
    client, registry
) -> None:
    _seed_chain(registry, label=True)
    # A second node behind the gate, joined to far: the edge between two
    # cut-off nodes goes with them.
    graph = registry.knowledge.graph_store
    graph.upsert_node("entity://far2", "entity", {"name": "Far two"})
    graph.upsert_edge(GATE, "entity://far2", "feeds_into")
    graph.upsert_edge(FAR, "entity://far2", "pairs_with")
    below = _entity(client, registry, BELOW, 2)
    cleared = _entity(client, registry, CLEARED, 2)

    assert _node_ids(cleared) == {ROOT, GATE, FAR, "entity://far2"}
    assert "pairs_with" in json.dumps(cleared), "the premise: the edge is fetched"
    assert _node_ids(below) == {ROOT}
    assert below["subgraph"]["edges"] == []
    assert "Far" not in json.dumps(below)


def test_depth_two_keeps_a_node_with_a_readable_path(client, registry) -> None:
    """Diamond: root -> gate -> far and root -> side -> far."""
    _seed_chain(registry, label=True)
    graph = registry.knowledge.graph_store
    graph.upsert_node(SIDE, "entity", {"name": "Side"})
    graph.upsert_edge(ROOT, SIDE, "feeds_into")
    graph.upsert_edge(SIDE, FAR, "feeds_into")

    below = _entity(client, registry, BELOW, 2)
    assert _node_ids(below) == {ROOT, SIDE, FAR}
    assert GATE not in _edge_ends(below)
    assert len(below["subgraph"]["edges"]) == 2


def test_a_readable_path_longer_than_depth_does_not_count(client, registry) -> None:
    """far is two hops through the gate, three through side nodes: depth 2 drops it."""
    _seed_chain(registry, label=True)
    graph = registry.knowledge.graph_store
    graph.upsert_node(SIDE, "entity", {"name": "Side"})
    graph.upsert_node("entity://side2", "entity", {"name": "Side two"})
    graph.upsert_edge(ROOT, SIDE, "feeds_into")
    graph.upsert_edge(SIDE, "entity://side2", "feeds_into")
    graph.upsert_edge("entity://side2", FAR, "feeds_into")

    assert _node_ids(_entity(client, registry, CLEARED, 2)) == {
        ROOT,
        GATE,
        SIDE,
        "entity://side2",
        FAR,
    }
    assert _node_ids(_entity(client, registry, BELOW, 2)) == {
        ROOT,
        SIDE,
        "entity://side2",
    }
    assert FAR in _node_ids(_entity(client, registry, BELOW, 3))


# ---------------------------------------------------------------------------
# unchanged: cleared key, auth off, no labelled node
# ---------------------------------------------------------------------------


def _all_reads(client, registry, scopes) -> list[Any]:
    headers = _headers(registry, scopes) if scopes is not None else {}
    return [
        _get(client, "/api/v1/graph/search", {"q": QUERY}, headers),
        _get(client, "/api/v1/graph/search", {"q": QUERY, "limit": 2}, headers),
        _get(client, "/api/v1/graph/search/facets", {"q": QUERY}, headers),
        _get(client, f"/api/v1/entities/{ROOT}", {"depth": 2}, headers),
    ]


def _seed_all(registry: StoreRegistry, *, label: bool) -> None:
    _seed_search(registry, label=label)
    _seed_chain(registry, label=label)


def test_an_unlabelled_graph_answers_the_same_below_and_cleared(
    client, registry
) -> None:
    _seed_all(registry, label=False)
    below = _all_reads(client, registry, BELOW)
    assert below[0]["total"] == 5, "an empty answer proves nothing"
    assert FAR in json.dumps(below[3])
    assert below == _all_reads(client, registry, CLEARED)


def test_a_cleared_key_reads_a_labelled_graph_whole(client, registry) -> None:
    _seed_all(registry, label=True)
    cleared = _all_reads(client, registry, CLEARED)
    assert cleared[0]["total"] == 5
    assert cleared[2]["total"] == 5
    assert _node_ids(cleared[3]) == {ROOT, GATE, FAR}


def test_auth_off_reads_a_labelled_graph_whole(client, registry, monkeypatch) -> None:
    _seed_all(registry, label=True)
    cleared = _all_reads(client, registry, CLEARED)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
    open_reads = _all_reads(TestClient(create_app()), registry, None)
    assert open_reads == cleared
