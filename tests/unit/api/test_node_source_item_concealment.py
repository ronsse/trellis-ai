"""A learning-sourced node's ``source_item_id`` is a pointer a graph read conceals.

Follows ``test_entities_pointer_concealment.py``. A precedent node promoted from
the learning loop carries ``properties["source_item_id"]``, the id of the served
item it came from, which is often a document. ``GET /entities/{id}`` served it
whole, so a key below the document's label learned the id of a document that
``GET /documents/{id}`` answers 404 for. The node walk now reads it
(``NODE_SOURCE_POINTER_PATHS``); the document-metadata walk a pack runs does
not, because no document row carries the key and the shared path tuple is
what packs drop.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve import sensitivity
from trellis.retrieve.sensitivity import (
    NODE_SOURCE_POINTER_PATHS,
    conceal_document_rows,
    conceal_node_pointers,
    conceal_unreadable_successors,
)
from trellis.schemas.pack import PackItem
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

HIDDEN_ID = "doc-hidden"
OPEN_ID = "doc-open"
NO_ROW_ID = "doc-never-stored"
CHUNK_ID = chunk_doc_id(HIDDEN_ID, 0)
ROOT = "node-root"
HIDDEN_SRC = "precedent-hidden"
OPEN_SRC = "precedent-open"
NO_ROW_SRC = "precedent-no-row"
CHUNK_SRC = "precedent-chunk"
BARE = "precedent-bare"

#: What a promoted precedent's properties look like, minus the pointer.
_PRECEDENT: dict[str, Any] = {
    "category": "promote_guidance",
    "intent_family": "family-a",
    "support_count": 4,
    "note": "kept",
}


def _seed(registry: StoreRegistry, level: str) -> None:
    docs = registry.knowledge.document_store
    docs.put(OPEN_ID, "open body", {})
    docs.put(HIDDEN_ID, "hidden body", label(level))
    docs.put(CHUNK_ID, "chunk body", {})
    graph = registry.knowledge.graph_store
    graph.upsert_node(ROOT, "entity", {"name": "root"})
    for node_id, source in (
        (HIDDEN_SRC, HIDDEN_ID),
        (OPEN_SRC, OPEN_ID),
        (NO_ROW_SRC, NO_ROW_ID),
        (CHUNK_SRC, CHUNK_ID),
    ):
        graph.upsert_node(
            node_id,
            "precedent",
            {"name": node_id, "source_item_id": source, **_PRECEDENT},
        )
        graph.upsert_edge(ROOT, node_id, "related_to")
    graph.upsert_node(BARE, "precedent", {"name": BARE, **_PRECEDENT})
    graph.upsert_edge(ROOT, BARE, "related_to")


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


def _entity(client, headers=None, node_id=ROOT):
    return client.get(f"/api/v1/entities/{node_id}", headers=headers or {})


def _nodes(resp) -> dict[str, dict[str, Any]]:
    assert resp.status_code == 200, resp.text
    return {n["node_id"]: n for n in resp.json()["subgraph"]["nodes"]}


class TestEntityConcealsSourceItemId:
    def test_a_key_below_the_label_sees_the_id_on_no_node(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _entity(client, _headers(registry, scopes))
            nodes = _nodes(resp)
            assert HIDDEN_ID not in resp.text, scopes
            assert "source_item_id" not in nodes[HIDDEN_SRC]["properties"], scopes
            # Every other property stays, and the node is still served.
            assert nodes[HIDDEN_SRC]["properties"] == {
                "name": HIDDEN_SRC,
                **_PRECEDENT,
            }, scopes

    def test_a_chunk_of_a_hidden_document_is_judged_by_its_parent(
        self, client, registry, level
    ):
        """A chunk row carries no label of its own: it takes its parent's."""
        for scopes in _blind(level):
            props = _nodes(_entity(client, _headers(registry, scopes)))[CHUNK_SRC][
                "properties"
            ]
            assert "source_item_id" not in props, scopes

    def test_a_key_that_may_read_the_label_sees_the_id(self, client, registry, level):
        for scopes in _cleared(level):
            nodes = _nodes(_entity(client, _headers(registry, scopes)))
            props = nodes[HIDDEN_SRC]["properties"]
            assert props["source_item_id"] == HIDDEN_ID, scopes
            assert props == {
                "name": HIDDEN_SRC,
                "source_item_id": HIDDEN_ID,
                **_PRECEDENT,
            }

    def test_a_readable_source_and_one_with_no_document_row_are_served(
        self, client, registry
    ):
        nodes = _nodes(_entity(client, _headers(registry, ["read"])))
        assert nodes[OPEN_SRC]["properties"]["source_item_id"] == OPEN_ID
        assert nodes[NO_ROW_SRC]["properties"]["source_item_id"] == NO_ROW_ID

    def test_a_node_with_no_source_gains_no_key(self, client, registry):
        blind = _nodes(_entity(client, _headers(registry, ["read"])))[BARE]
        full = _nodes(_entity(client, _headers(registry, ["admin"])))[BARE]
        assert "source_item_id" not in blind["properties"]
        assert blind == full

    def test_the_node_list_does_not_change_shape(self, client, registry):
        blind = _nodes(_entity(client, _headers(registry, ["read"])))
        full = _nodes(_entity(client, _headers(registry, ["admin"])))
        assert list(blind) == list(full)
        for node_id, node in blind.items():
            assert set(node) == set(full[node_id])

    def test_the_stored_node_is_unchanged(self, client, registry):
        _entity(client, _headers(registry, ["read"]))
        stored = registry.knowledge.graph_store.get_node(HIDDEN_SRC)
        assert stored["properties"]["source_item_id"] == HIDDEN_ID


class TestConcealNodePointersHelper:
    def _node(self, node_id: str, **properties: Any) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "node_type": "precedent",
            "properties": {"name": node_id, **properties},
            "document_ids": None,
        }

    def test_a_hidden_source_is_removed_from_a_copy_and_the_input_is_unchanged(
        self, registry
    ):
        store = registry.knowledge.document_store
        node = self._node("p-1", source_item_id=HIDDEN_ID, note="kept")
        before = copy.deepcopy(node)
        (out,) = conceal_node_pointers([node], store, "internal")
        assert out is not node
        assert out["properties"] == {"name": "p-1", "note": "kept"}
        assert node == before

    def test_a_readable_missing_or_malformed_source_comes_back_as_the_same_object(
        self, registry
    ):
        store = registry.knowledge.document_store
        nodes = [
            self._node("p-open", source_item_id=OPEN_ID),
            self._node("p-no-row", source_item_id=NO_ROW_ID),
            self._node("p-odd", source_item_id=7),
            self._node("p-empty", source_item_id=""),
            self._node("p-bare"),
        ]
        out = conceal_node_pointers(nodes, store, "internal")
        assert all(a is b for a, b in zip(out, nodes, strict=True))

    def test_the_source_is_judged_in_one_pass_with_the_other_pointers(
        self, registry, monkeypatch
    ):
        store = registry.knowledge.document_store
        seen: list[str] = []
        real_get = store.get

        def counting(doc_id):
            seen.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        nodes = [
            self._node("p-1", source_item_id=HIDDEN_ID),
            self._node("p-2", source_item_id=HIDDEN_ID, evidence_ref=HIDDEN_ID),
        ]
        out = conceal_node_pointers(nodes, store, "internal")
        assert all("source_item_id" not in n["properties"] for n in out)
        assert "evidence_ref" not in out[1]["properties"]
        assert seen.count(HIDDEN_ID) == 1

    def test_restricted_does_no_reads_and_serves_the_source(self, registry):
        store = registry.knowledge.document_store
        node = self._node("p-1", source_item_id=HIDDEN_ID)
        (out,) = conceal_node_pointers([node], store, "restricted")
        assert out is node


class TestPacksAreUntouched:
    """The node path is its own tuple: a pack walks document metadata, not nodes.

    No document row carries ``source_item_id`` (a precedent's row is built by
    ``precedent_document_metadata``, which does not copy it), so adding the key
    to the shared tuple would only change what a pack drops from a
    graph-axis item's spread properties and from any row that names it.
    """

    def test_the_node_path_is_its_own_constant(self) -> None:
        assert NODE_SOURCE_POINTER_PATHS == (("source_item_id",),)
        assert ("source_item_id",) not in sensitivity._CONCEALED_POINTER_PATHS
        assert ("source_item_id",) not in sensitivity.EVIDENCE_POINTER_PATHS
        assert ("source_item_id",) not in sensitivity.SUPERSESSION_POINTER_PATHS

    def test_a_document_row_keeps_a_source_item_id_naming_a_hidden_document(
        self, registry
    ):
        store = registry.knowledge.document_store
        row = {
            "doc_id": "d-1",
            "content": "body",
            "metadata": {"source_item_id": HIDDEN_ID, "evidence_ref": HIDDEN_ID},
        }
        (out,) = conceal_document_rows([row], store, "internal")
        # The shared walk still drops the pointers it owns ...
        assert "evidence_ref" not in out["metadata"]
        # ... and leaves this one exactly where it was.
        assert out["metadata"]["source_item_id"] == HIDDEN_ID

    def test_a_pack_item_keeps_a_source_item_id_naming_a_hidden_document(
        self, registry
    ):
        store = registry.knowledge.document_store
        item = PackItem(
            item_id="node-p",
            item_type="entity",
            excerpt="node",
            relevance_score=0.5,
            metadata={"source_item_id": HIDDEN_ID, "evidence_ref": HIDDEN_ID},
        )
        (out,) = conceal_unreadable_successors([item], store, "internal")
        assert "evidence_ref" not in out.metadata
        assert out.metadata["source_item_id"] == HIDDEN_ID
