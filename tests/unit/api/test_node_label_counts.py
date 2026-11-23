"""Counts and shape on the REST graph doors agree with what they withhold (#1264).

A node labelled above the key is left out of ``GET /graph/search``'s page
(#1029), of ``total`` and of every ``/graph/search/facets`` bucket, exactly
as a withheld promoted precedent is (#1080): nothing reports it. ``offset``
counts readable matches, so a withheld row shortens no page and stepping
``offset`` by ``limit`` serves each readable match once, in the requested
order, however many store pages the walk spans.

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
import trellis_api.routes.retrieve as retrieve_routes
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
    """Offsets count readable matches: the withheld row shortens no page."""
    _seed_search(registry, label=True)
    seen: list[str] = []
    pages: list[dict[str, Any]] = []
    for offset in range(0, 6, 2):
        page = _search(client, registry, BELOW, limit=2, offset=offset)
        pages.append(page)
        seen.extend(r["entity_id"] for r in page["results"])

    # Sorted by name ascending, readable rows only: m0 m1 | m3 m4 | (empty).
    assert [p["count"] for p in pages] == [2, 2, 0]
    assert [p["offset"] for p in pages] == [0, 2, 4]
    assert seen == ["entity://m0", "entity://m1", "entity://m3", "entity://m4"]
    assert all(p["total"] == len(seen) for p in pages)


#: Twelve matches; every third one is labelled above ``BELOW``.
_WIDE = 12


def _seed_wide(
    registry: StoreRegistry, write_order: list[int] | None = None
) -> list[str]:
    """Seed ``_WIDE`` matches and return the readable ids in name order.

    ``write_order`` is the order the indices are written in, which is the
    ``created_at`` order; by default it is name order.
    """
    graph = registry.knowledge.graph_store
    readable: list[str] = []
    for i in write_order if write_order is not None else range(_WIDE):
        node_id = f"entity://w{i:02d}"
        labelled = i % 3 == 1
        graph.upsert_node(
            node_id,
            "table" if i % 2 else "service",
            _props(f"Orchard {i:02d}", label=labelled),
        )
        if not labelled:
            readable.append(node_id)
    return sorted(readable)


#: A write order that is neither name order nor its reverse, so a walk that
#: sorted by name whatever it was asked would not match ``created_at``.
_SHUFFLED = [7, 2, 10, 0, 5, 11, 3, 8, 1, 6, 9, 4]


def _walk(client, registry, scopes, limit, **params) -> tuple[list[str], set[int]]:
    """Every id served stepping ``offset`` by ``limit``, and each page's total."""
    served: list[str] = []
    totals: set[int] = set()
    offset = 0
    while True:
        page = _search(client, registry, scopes, limit=limit, offset=offset, **params)
        totals.add(page["total"])
        if not page["results"]:
            return served, totals
        assert page["offset"] == offset
        served.extend(r["entity_id"] for r in page["results"])
        offset += limit


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("limit", [1, 3, 5])
def test_stepping_offset_serves_each_readable_row_once_in_order(
    client, registry, monkeypatch, order, limit
) -> None:
    """The walk spans several store pages and keeps the requested sort."""
    monkeypatch.setattr(retrieve_routes, "_PRECEDENT_SCAN_PAGE", 2)
    readable = _seed_wide(registry)
    expected = readable if order == "asc" else list(reversed(readable))

    served, totals = _walk(client, registry, BELOW, limit, order=order)

    assert served == expected
    assert totals == {len(served)}


@pytest.mark.parametrize("sort", ["created_at", "node_type"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("limit", [1, 3, 5])
def test_every_sort_pages_the_stored_order_less_the_withheld_rows(
    client, registry, monkeypatch, sort, order, limit
) -> None:
    """The walk below the label keeps each sort's stored order, labelled rows out.

    The oracle is the cleared key's single store call for the same sort and
    order. Rows are written out of name order, so ``created_at`` differs from
    ``name``. ``node_type`` ties six rows per type, so the ``node_id`` tie-break
    decides the order inside a type, and the walk's store pages of two split
    those ties.
    """
    monkeypatch.setattr(retrieve_routes, "_PRECEDENT_SCAN_PAGE", 2)
    readable = set(_seed_wide(registry, _SHUFFLED))
    stored = _get(
        client,
        "/api/v1/graph/search",
        {"q": QUERY, "sort": sort, "order": order, "limit": _WIDE},
        _headers(registry, CLEARED),
    )
    stored_ids = [r["entity_id"] for r in stored["results"]]
    by_name = sorted(stored_ids)
    assert stored_ids not in (by_name, by_name[::-1]), "the sort must differ"
    expected = [i for i in stored_ids if i in readable]
    # In stored order a labelled row lies within the first three rows, ahead
    # of readable ones, so withholding it moves every page boundary after it.
    first_labelled = min(
        n for n, node_id in enumerate(stored_ids) if node_id not in readable
    )
    assert first_labelled <= 2
    assert len(stored_ids) == _WIDE
    assert len(expected) > limit

    served, totals = _walk(client, registry, BELOW, limit, sort=sort, order=order)

    assert served == expected
    assert totals == {len(expected)}


def test_a_full_page_is_never_short_while_rows_remain(
    client, registry, monkeypatch
) -> None:
    monkeypatch.setattr(retrieve_routes, "_PRECEDENT_SCAN_PAGE", 2)
    readable = _seed_wide(registry)

    for offset in range(0, len(readable), 4):
        page = _search(client, registry, BELOW, limit=4, offset=offset)
        assert page["count"] == min(4, len(readable) - offset)


def test_wide_facets_sum_to_total(client, registry, monkeypatch) -> None:
    monkeypatch.setattr(retrieve_routes, "_PRECEDENT_SCAN_PAGE", 2)
    readable = _seed_wide(registry)

    facets = _facets(client, registry, BELOW)

    assert facets["total"] == len(readable)
    assert sum(f["count"] for f in facets["node_types"]) == facets["total"]
    assert facets["total"] == _search(client, registry, BELOW)["total"]


def test_a_restricted_caller_makes_one_store_call(
    client, registry, monkeypatch
) -> None:
    """A cleared key reads the stored page as it did before the walk."""
    monkeypatch.setattr(retrieve_routes, "_PRECEDENT_SCAN_PAGE", 2)
    _seed_wide(registry)
    graph = registry.knowledge.graph_store
    stored, stored_total = graph.search_nodes(
        search=QUERY, sort="name", descending=False, limit=3, offset=4
    )
    calls: list[dict[str, Any]] = []
    real = graph.search_nodes

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(graph, "search_nodes", spy)
    body = _search(client, registry, CLEARED, limit=3, offset=4)

    assert len(calls) == 1
    assert body["total"] == stored_total == _WIDE
    assert [r["entity_id"] for r in body["results"]] == [r["node_id"] for r in stored]


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
