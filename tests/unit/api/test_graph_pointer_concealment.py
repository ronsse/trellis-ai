"""``GET /graph/history`` names no document the key cannot read.

A node carries no label (#1036), so every version is served; the id of a
document above the caller's clearance, held in a version's ``document_ids`` or
in its ``evidence_ref`` property, is not. The document itself answers 404 at
``GET /documents/{id}``, so an id here would be a name for something the key
may not touch.
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
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

HIDDEN_ID = "doc-hidden"
OPEN_ID = "doc-open"
NODE = "node-claim"


def _seed(registry: StoreRegistry, level: str) -> None:
    docs = registry.knowledge.document_store
    docs.put(OPEN_ID, "open body", {})
    docs.put(HIDDEN_ID, "hidden body", label(level))
    graph = registry.knowledge.graph_store
    # Two SCD-2 versions: the first carries both carriers the way a
    # ``save_knowledge`` node does, the second adds an unrelated property.
    graph.upsert_node(
        NODE,
        "entity",
        {"name": "claim", "evidence_ref": HIDDEN_ID},
        document_ids=[OPEN_ID, HIDDEN_ID],
    )
    graph.upsert_node(
        NODE,
        "entity",
        {"name": "claim", "evidence_ref": HIDDEN_ID, "note": "kept"},
        document_ids=[OPEN_ID, HIDDEN_ID],
    )
    # Nodes the pack's graph axis serves by namespace seeding (``tool:<intent>``).
    # Only ``properties`` carries the pointer: no ``document_ids``.
    chunk = chunk_doc_id(HIDDEN_ID, 0)
    docs.put(chunk, "chunk body", {})
    for key, ref in {
        "zebra": HIDDEN_ID,
        "okapi": OPEN_ID,
        "quagga": chunk,
        "gnu": 7,
    }.items():
        graph.upsert_node(
            f"tool:{key}", "tool", {"name": key, "evidence_ref": ref, "note": "kept"}
        )


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


def _history(client, headers=None) -> Any:
    return client.get(
        "/api/v1/graph/history", params={"entity_id": NODE}, headers=headers or {}
    )


def _cleared(level: str) -> list[list[str]]:
    """The scope sets that may read a document labelled ``level``."""
    out = [["admin"], ["read", "restricted"]]
    if level == "confidential":
        out.append(["read", "confidential"])
    return out


def _blind(level: str) -> list[list[str]]:
    return [["read"]] + ([["read", "confidential"]] if level == "restricted" else [])


class TestHistoryConcealsHiddenPointers:
    def test_the_document_itself_is_withheld_so_an_id_is_a_name_for_nothing(
        self, client, registry
    ):
        resp = client.get(
            f"/api/v1/documents/{HIDDEN_ID}", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 404

    def test_a_key_below_the_label_sees_the_id_in_no_version(
        self, client, registry, level
    ):
        for scopes in _blind(level):
            resp = _history(client, _headers(registry, scopes))
            assert resp.status_code == 200, resp.text
            assert HIDDEN_ID not in resp.text, scopes
            body = resp.json()
            assert body["count"] == 2
            for version in body["versions"]:
                assert version["document_ids"] == [OPEN_ID]
                assert "evidence_ref" not in version["properties"]
                assert version["properties"]["name"] == "claim"
            assert body["versions"][0]["properties"]["note"] == "kept"

    def test_a_key_that_may_read_the_label_sees_every_pointer(
        self, client, registry, level
    ):
        for scopes in _cleared(level):
            resp = _history(client, _headers(registry, scopes))
            assert resp.status_code == 200, resp.text
            for version in resp.json()["versions"]:
                assert version["document_ids"] == [OPEN_ID, HIDDEN_ID]
                assert version["properties"]["evidence_ref"] == HIDDEN_ID

    def test_auth_off_reads_everything(self, client, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = _history(client)
        assert resp.status_code == 200, resp.text
        assert HIDDEN_ID in resp.text
        assert resp.json()["versions"][0]["document_ids"] == [OPEN_ID, HIDDEN_ID]

    def test_the_stored_node_is_unchanged(self, client, registry, level):
        _history(client, _headers(registry, ["read"]))
        resp = _history(client, _headers(registry, ["read", "restricted"]))
        for version in resp.json()["versions"]:
            assert version["document_ids"] == [OPEN_ID, HIDDEN_ID]
            assert version["properties"]["evidence_ref"] == HIDDEN_ID
        stored = registry.knowledge.graph_store.get_node(NODE)
        assert stored["document_ids"] == [OPEN_ID, HIDDEN_ID]
        assert stored["properties"]["evidence_ref"] == HIDDEN_ID

    def test_a_missing_node_still_answers_404(self, client, registry):
        resp = client.get(
            "/api/v1/graph/history",
            params={"entity_id": "node-ghost"},
            headers=_headers(registry, ["read"]),
        )
        assert resp.status_code == 404

    def test_a_chunk_pointer_whose_parent_is_hidden_is_hidden(self, client, registry):
        chunk = chunk_doc_id(HIDDEN_ID, 0)
        registry.knowledge.document_store.put(chunk, "chunk body", {})
        registry.knowledge.graph_store.upsert_node(
            "node-chunky",
            "entity",
            {"name": "chunky", "evidence_ref": chunk},
            document_ids=[chunk, OPEN_ID],
        )
        resp = client.get(
            "/api/v1/graph/history",
            params={"entity_id": "node-chunky"},
            headers=_headers(registry, ["read"]),
        )
        assert resp.status_code == 200
        assert chunk not in resp.text
        assert resp.json()["versions"][0]["document_ids"] == [OPEN_ID]


def _pack(client, headers, intent, *, sectioned: bool = False) -> Any:
    if sectioned:
        resp = client.post(
            "/api/v1/packs/sectioned",
            json={"intent": intent, "sections": [{"name": "All"}]},
            headers=headers,
        )
    else:
        resp = client.post("/api/v1/packs", json={"intent": intent}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp


def _items(resp) -> dict[str, dict[str, Any]]:
    data = resp.json()
    rows = (
        [i for s in data["sections"] for i in s["items"]]
        if "sections" in data
        else data["items"]
    )
    return {i["item_id"]: i["metadata"] for i in rows}


SURFACES = pytest.mark.parametrize(
    "sectioned", [False, True], ids=["flat", "sectioned"]
)


class TestPacksConcealAGraphItemsEvidenceRef:
    """A graph-axis item carries its node's ``evidence_ref`` in its metadata."""

    @SURFACES
    def test_the_probe_a_read_key_is_served_the_node_without_the_hidden_id(
        self, client, registry, level, sectioned
    ):
        for scopes in _blind(level):
            resp = _pack(
                client, _headers(registry, scopes), "zebra", sectioned=sectioned
            )
            items = _items(resp)
            assert "tool:zebra" in items, (scopes, resp.text)
            assert HIDDEN_ID not in resp.text, scopes
            assert "evidence_ref" not in items["tool:zebra"]
            assert items["tool:zebra"]["note"] == "kept"

    @SURFACES
    def test_a_key_that_may_read_the_label_is_served_the_ref(
        self, client, registry, level, sectioned
    ):
        for scopes in _cleared(level):
            resp = _pack(
                client, _headers(registry, scopes), "zebra", sectioned=sectioned
            )
            assert _items(resp)["tool:zebra"]["evidence_ref"] == HIDDEN_ID

    @SURFACES
    def test_a_readable_ref_is_kept(self, client, registry, sectioned):
        resp = _pack(client, _headers(registry, ["read"]), "okapi", sectioned=sectioned)
        assert _items(resp)["tool:okapi"]["evidence_ref"] == OPEN_ID

    @SURFACES
    def test_a_chunk_ref_is_judged_by_its_parent(self, client, registry, sectioned):
        resp = _pack(
            client, _headers(registry, ["read"]), "quagga", sectioned=sectioned
        )
        items = _items(resp)
        assert "tool:quagga" in items
        assert chunk_doc_id(HIDDEN_ID, 0) not in resp.text
        assert "evidence_ref" not in items["tool:quagga"]

    @SURFACES
    def test_a_non_string_ref_is_left_as_stored(self, client, registry, sectioned):
        resp = _pack(client, _headers(registry, ["read"]), "gnu", sectioned=sectioned)
        assert _items(resp)["tool:gnu"]["evidence_ref"] == 7

    def test_the_items_served_are_the_same_whatever_the_clearance(
        self, client, registry
    ):
        """Only a served item's metadata differs, never which items or their order."""
        served = {
            tuple(scopes): list(
                _items(_pack(client, _headers(registry, scopes), "zebra"))
            )
            for scopes in (["read"], ["admin"])
        }
        assert served[("read",)] == served[("admin",)]

    @SURFACES
    def test_a_restricted_caller_is_served_unchanged_with_no_store_read_of_the_ref(
        self, client, registry, monkeypatch, sectioned
    ):
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        bodies = []
        for scopes in (["admin"], ["read", "restricted"]):
            reads.clear()
            resp = _pack(
                client, _headers(registry, scopes), "zebra", sectioned=sectioned
            )
            assert reads.count(HIDDEN_ID) == 0, scopes
            bodies.append(json.dumps(_items(resp), sort_keys=True))
        # A below-clearance key does judge the ref, so the count above is not vacuous.
        reads.clear()
        _pack(client, _headers(registry, ["read"]), "zebra", sectioned=sectioned)
        assert reads.count(HIDDEN_ID) >= 1
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        reads.clear()
        bodies.append(
            json.dumps(
                _items(_pack(client, {}, "zebra", sectioned=sectioned)), sort_keys=True
            )
        )
        assert reads.count(HIDDEN_ID) == 0
        assert len(set(bodies)) == 1
        assert json.loads(bodies[0])["tool:zebra"]["evidence_ref"] == HIDDEN_ID

    @SURFACES
    def test_the_stored_node_is_unchanged(self, client, registry, sectioned):
        _pack(client, _headers(registry, ["read"]), "zebra", sectioned=sectioned)
        stored = registry.knowledge.graph_store.get_node("tool:zebra")
        assert stored["properties"]["evidence_ref"] == HIDDEN_ID
