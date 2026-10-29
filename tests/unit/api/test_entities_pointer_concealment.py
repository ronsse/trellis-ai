"""``GET /entities/{id}`` and ``GET /graph/search`` hide unreadable documents.

Follows ``test_graph_pointer_concealment.py``. A node carries no label (#1036),
so the entity, every node of its subgraph and every search hit is served; the
id of a document above the caller's clearance, held in a node's
``document_ids``, in its ``evidence_ref`` property or as the ``superseded_by`` of
its lifecycle, is not. The document itself answers 404 at
``GET /documents/{id}``, so an id here would be a name for something the key may
not touch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mcp.supersession import supersede_entity
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

HIDDEN_ID = "doc-hidden"
OPEN_ID = "doc-open"
NODE = "node-claim"
NEIGHBOUR = "node-neighbour"
CHUNKY = "node-chunky"
OLD = "node-old"
CHUNK_ID = chunk_doc_id(HIDDEN_ID, 0)


def _seed(registry: StoreRegistry, level: str) -> None:
    docs = registry.knowledge.document_store
    docs.put(OPEN_ID, "open body", {})
    docs.put(HIDDEN_ID, "hidden body", label(level))
    docs.put(CHUNK_ID, "chunk body", {})
    graph = registry.knowledge.graph_store
    # The way a ``save_knowledge`` node carries its pointer: both carriers.
    graph.upsert_node(
        NODE,
        "entity",
        {"name": "claim", "evidence_ref": HIDDEN_ID, "note": "kept"},
        document_ids=[OPEN_ID, HIDDEN_ID],
    )
    graph.upsert_node(
        NEIGHBOUR,
        "entity",
        {"name": "neighbour", "evidence_ref": HIDDEN_ID, "note": "kept"},
        document_ids=[HIDDEN_ID, OPEN_ID],
    )
    graph.upsert_node(
        CHUNKY,
        "entity",
        {"name": "chunky", "evidence_ref": CHUNK_ID, "note": "kept"},
        document_ids=[CHUNK_ID],
    )
    graph.upsert_edge(NODE, NEIGHBOUR, "related_to")
    graph.upsert_edge(NODE, CHUNKY, "related_to")
    # A node whose lifecycle names a hidden successor, as ``supersede_entity`` stamps.
    graph.upsert_node(OLD, "entity", {"name": "old"})
    refused = supersede_entity(
        registry,
        node_id=OLD,
        successor=HIDDEN_ID,
        requested_by="test",
        clearance="restricted",
    )
    assert refused is None, refused
    graph.upsert_edge(NODE, OLD, "related_to")


@pytest.fixture(params=["restricted", "confidential"])
def level(request) -> str:
    return request.param


@pytest.fixture
def registry(tmp_path, monkeypatch, level):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    _seed(reg, level)
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _cleared(level: str) -> list[list[str]]:
    """The scope sets that may read a document labelled ``level``."""
    out = [["admin"], ["read", "restricted"]]
    if level == "confidential":
        out.append(["read", "confidential"])
    return out


def _blind(level: str) -> list[list[str]]:
    return [["read"]] + ([["read", "confidential"]] if level == "restricted" else [])


def _entity(client, node_id, headers=None, **params) -> Any:
    return client.get(
        f"/api/v1/entities/{node_id}", params=params, headers=headers or {}
    )


def _search(client, headers=None, **params) -> Any:
    return client.get("/api/v1/graph/search", params=params, headers=headers or {})


def _subgraph_nodes(resp) -> dict[str, dict[str, Any]]:
    return {n["node_id"]: n for n in resp.json()["subgraph"]["nodes"]}


class TestEntityConcealsHiddenPointers:
    def test_the_document_itself_is_withheld_so_an_id_is_a_name_for_nothing(
        self, client, registry
    ):
        resp = client.get(
            f"/api/v1/documents/{HIDDEN_ID}", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 404

    def test_a_key_below_the_label_sees_the_id_on_the_entity(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _entity(client, NODE, _headers(registry, scopes))
            assert resp.status_code == 200, resp.text
            assert HIDDEN_ID not in resp.text, scopes
            entity = resp.json()["entity"]
            assert entity["document_ids"] == [OPEN_ID]
            assert "evidence_ref" not in entity["properties"]
            assert entity["properties"]["name"] == "claim"
            assert entity["properties"]["note"] == "kept"

    def test_a_key_below_the_label_sees_the_id_on_no_subgraph_neighbour(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _entity(client, NODE, _headers(registry, scopes))
            nodes = _subgraph_nodes(resp)
            assert {NODE, NEIGHBOUR, CHUNKY, OLD} <= set(nodes), scopes
            assert HIDDEN_ID not in json.dumps(resp.json()["subgraph"]), scopes
            neighbour = nodes[NEIGHBOUR]
            assert neighbour["document_ids"] == [OPEN_ID]
            assert "evidence_ref" not in neighbour["properties"]
            assert neighbour["properties"]["note"] == "kept"
            # The subgraph's copy of the entity itself is concealed too.
            assert nodes[NODE]["document_ids"] == [OPEN_ID]
            assert "evidence_ref" not in nodes[NODE]["properties"]

    def test_the_shape_and_the_walk_do_not_change(self, client, registry, level):
        blind = _entity(client, NODE, _headers(registry, ["read"])).json()
        full = _entity(client, NODE, _headers(registry, ["admin"])).json()
        assert [n["node_id"] for n in blind["subgraph"]["nodes"]] == [
            n["node_id"] for n in full["subgraph"]["nodes"]
        ]
        assert blind["subgraph"]["edges"] == full["subgraph"]["edges"]
        assert blind["entity"]["node_id"] == full["entity"]["node_id"]
        assert blind["status"] == full["status"] == "ok"

    def test_a_key_that_may_read_the_label_sees_every_pointer(
        self, client, registry, level
    ):
        for scopes in _cleared(level):
            resp = _entity(client, NODE, _headers(registry, scopes))
            assert resp.status_code == 200, resp.text
            entity = resp.json()["entity"]
            assert entity["document_ids"] == [OPEN_ID, HIDDEN_ID]
            assert entity["properties"]["evidence_ref"] == HIDDEN_ID
            neighbour = _subgraph_nodes(resp)[NEIGHBOUR]
            assert neighbour["document_ids"] == [HIDDEN_ID, OPEN_ID]
            assert neighbour["properties"]["evidence_ref"] == HIDDEN_ID

    def test_a_readable_id_is_kept(self, client, registry):
        registry.knowledge.graph_store.upsert_node(
            "node-open",
            "entity",
            {"name": "open", "evidence_ref": OPEN_ID},
            document_ids=[OPEN_ID],
        )
        resp = _entity(client, "node-open", _headers(registry, ["read"]))
        entity = resp.json()["entity"]
        assert entity["document_ids"] == [OPEN_ID]
        assert entity["properties"]["evidence_ref"] == OPEN_ID

    def test_a_chunk_pointer_whose_parent_is_hidden_is_hidden(self, client, registry):
        resp = _entity(client, CHUNKY, _headers(registry, ["read"]))
        assert resp.status_code == 200
        assert CHUNK_ID not in resp.text
        assert resp.json()["entity"]["document_ids"] == []
        assert "evidence_ref" not in resp.json()["entity"]["properties"]

    def test_auth_off_reads_everything(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = _entity(client, NODE)
        assert resp.status_code == 200, resp.text
        assert resp.json()["entity"]["document_ids"] == [OPEN_ID, HIDDEN_ID]
        assert _subgraph_nodes(resp)[NEIGHBOUR]["properties"]["evidence_ref"] == (
            HIDDEN_ID
        )

    def test_the_stored_nodes_are_unchanged(self, client, registry):
        _entity(client, NODE, _headers(registry, ["read"]))
        resp = _entity(client, NODE, _headers(registry, ["read", "restricted"]))
        assert resp.json()["entity"]["properties"]["evidence_ref"] == HIDDEN_ID
        for node_id in (NODE, NEIGHBOUR):
            stored = registry.knowledge.graph_store.get_node(node_id)
            assert HIDDEN_ID in stored["document_ids"]
            assert stored["properties"]["evidence_ref"] == HIDDEN_ID

    def test_a_missing_entity_still_answers_404(self, client, registry):
        resp = _entity(client, "node-ghost", _headers(registry, ["read"]))
        assert resp.status_code == 404

    def test_the_entity_and_its_subgraph_are_judged_in_one_pass(
        self, client, registry, monkeypatch
    ):
        """One store read per distinct id, however many nodes name it."""
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        # The seed and its one neighbour both name both ids.
        resp = _entity(client, NEIGHBOUR, _headers(registry, ["read"]), depth=1)
        assert resp.status_code == 200
        assert {NODE, NEIGHBOUR} <= set(_subgraph_nodes(resp))
        assert reads.count(HIDDEN_ID) == 1
        assert reads.count(OPEN_ID) == 1

    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    def test_a_reader_of_everything_does_no_reads(
        self, client, registry, monkeypatch, scopes
    ):
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        headers = _headers(registry, scopes)
        reads.clear()
        assert _entity(client, NODE, headers).status_code == 200
        assert reads == []


class TestEntityConcealsASupersededNodesSuccessor:
    """``supersede_entity`` stamps ``lifecycle.superseded_by`` with a document id."""

    def test_a_key_below_the_label_sees_the_state_and_not_the_successor(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            headers = _headers(registry, scopes)
            as_entity = _entity(client, OLD, headers)
            as_neighbour = _entity(client, NODE, headers)
            assert HIDDEN_ID not in as_entity.text, scopes
            assert HIDDEN_ID not in as_neighbour.text, scopes
            for node in (
                as_entity.json()["entity"],
                _subgraph_nodes(as_neighbour)[OLD],
            ):
                assert node["properties"]["lifecycle"]["state"] == "superseded"
                assert "superseded_by" not in node["properties"]["lifecycle"]

    def test_a_key_that_may_read_the_label_sees_the_successor(
        self, client, registry, level
    ):
        for scopes in _cleared(level):
            headers = _headers(registry, scopes)
            lifecycle = _entity(client, OLD, headers).json()["entity"]["properties"][
                "lifecycle"
            ]
            assert lifecycle["superseded_by"] == HIDDEN_ID, scopes
            neighbour = _subgraph_nodes(_entity(client, NODE, headers))[OLD]
            assert neighbour["properties"]["lifecycle"]["superseded_by"] == HIDDEN_ID

    def test_auth_off_sees_the_node_as_stored(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        lifecycle = _entity(client, OLD).json()["entity"]["properties"]["lifecycle"]
        assert lifecycle["superseded_by"] == HIDDEN_ID

    def test_the_stored_node_is_unchanged(self, client, registry):
        _entity(client, OLD, _headers(registry, ["read"]))
        stored = registry.knowledge.graph_store.get_node(OLD)
        assert stored["properties"]["lifecycle"]["superseded_by"] == HIDDEN_ID


def _hits(resp) -> dict[str, dict[str, Any]]:
    return {r["entity_id"]: r for r in resp.json()["results"]}


class TestGraphSearchConcealsHiddenPointers:
    def test_a_key_below_the_label_sees_the_id_in_no_hit(self, client, registry, level):
        for scopes in _blind(level):
            resp = _search(client, _headers(registry, scopes), limit=100)
            assert resp.status_code == 200, resp.text
            assert HIDDEN_ID not in resp.text, scopes
            assert CHUNK_ID not in resp.text, scopes
            hits = _hits(resp)
            assert {NODE, NEIGHBOUR, CHUNKY, OLD} <= set(hits)
            for node_id in (NODE, NEIGHBOUR, CHUNKY):
                props = hits[node_id]["properties"]
                assert "evidence_ref" not in props, node_id
                assert props["note"] == "kept"
                assert hits[node_id]["name"] == props["name"]
            lifecycle = hits[OLD]["properties"]["lifecycle"]
            assert lifecycle["state"] == "superseded"
            assert "superseded_by" not in lifecycle

    def test_a_key_that_may_read_the_label_sees_every_pointer(
        self, client, registry, level
    ):
        for scopes in _cleared(level):
            hits = _hits(_search(client, _headers(registry, scopes), limit=100))
            for node_id in (NODE, NEIGHBOUR):
                assert hits[node_id]["properties"]["evidence_ref"] == HIDDEN_ID, scopes
            assert hits[OLD]["properties"]["lifecycle"]["superseded_by"] == HIDDEN_ID

    def test_a_readable_ref_is_kept(self, client, registry):
        registry.knowledge.graph_store.upsert_node(
            "node-open", "entity", {"name": "open", "evidence_ref": OPEN_ID}
        )
        resp = _search(client, _headers(registry, ["read"]), q="node-open")
        assert _hits(resp)["node-open"]["properties"]["evidence_ref"] == OPEN_ID

    def test_a_non_string_ref_is_left_as_stored(self, client, registry):
        registry.knowledge.graph_store.upsert_node(
            "node-gnu", "entity", {"name": "gnu", "evidence_ref": 7}
        )
        resp = _search(client, _headers(registry, ["read"]), q="node-gnu")
        assert _hits(resp)["node-gnu"]["properties"]["evidence_ref"] == 7

    def test_the_page_and_the_total_do_not_change(self, client, registry):
        blind = _search(client, _headers(registry, ["read"]), sort="name", limit=3)
        full = _search(client, _headers(registry, ["admin"]), sort="name", limit=3)
        for key in ("total", "count", "offset", "status"):
            assert blind.json()[key] == full.json()[key], key
        assert [r["entity_id"] for r in blind.json()["results"]] == [
            r["entity_id"] for r in full.json()["results"]
        ]

    def test_auth_off_reads_everything(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        hits = _hits(_search(client, limit=100))
        assert hits[NODE]["properties"]["evidence_ref"] == HIDDEN_ID

    def test_the_stored_node_is_unchanged(self, client, registry):
        _search(client, _headers(registry, ["read"]), limit=100)
        stored = registry.knowledge.graph_store.get_node(NODE)
        assert stored["properties"]["evidence_ref"] == HIDDEN_ID
        assert stored["document_ids"] == [OPEN_ID, HIDDEN_ID]

    def test_the_whole_page_is_judged_in_one_pass(self, client, registry, monkeypatch):
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        graph = registry.knowledge.graph_store
        for name in ("dup-a", "dup-b", "dup-c"):
            graph.upsert_node(name, "entity", {"name": name, "evidence_ref": HIDDEN_ID})
        resp = _search(client, _headers(registry, ["read"]), q="dup-")
        assert resp.json()["count"] == 3
        assert reads.count(HIDDEN_ID) == 1

    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    def test_a_reader_of_everything_does_no_reads(
        self, client, registry, monkeypatch, scopes
    ):
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        assert _search(client, _headers(registry, scopes), limit=100).status_code == 200
        assert reads == []


class TestFacetsCarryNoPointer:
    """The facets count nodes per ``node_type``; they aggregate no property.

    So there is no ``evidence_ref`` value in a facet to conceal, and concealing a
    pointer never removes a node, so the counts equal the list's ``total`` for
    every caller.
    """

    def _facets(self, client, headers, **params) -> Any:
        resp = client.get("/api/v1/graph/search/facets", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp

    def test_a_facet_names_no_document_and_counts_the_same_for_every_key(
        self, client, registry, level
    ):
        bodies = []
        for scopes in [["read"], ["admin"]]:
            resp = self._facets(client, _headers(registry, scopes))
            assert HIDDEN_ID not in resp.text, scopes
            bodies.append(resp.json())
        assert bodies[0] == bodies[1]
        total = _search(client, _headers(registry, ["read"]), limit=1).json()["total"]
        assert bodies[0]["total"] == total

    def test_a_query_cannot_reach_a_pointer_value(self, client, registry):
        """``q`` matches name, node id and type: a hidden ref is no search key."""
        for scopes in (["read"], ["admin"]):
            resp = _search(client, _headers(registry, scopes), q=HIDDEN_ID)
            assert resp.json()["total"] == 0, scopes
            facets = self._facets(client, _headers(registry, scopes), q=HIDDEN_ID)
            assert facets.json()["total"] == 0, scopes
