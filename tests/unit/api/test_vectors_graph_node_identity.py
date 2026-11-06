"""``POST /vectors`` at an id that names a graph node and no stored document (#1057).

The semantic axis spreads a vector row's ``metadata`` into the pack item it
builds, and the pack builder keeps one copy of an id (the highest-scoring) and
judges that copy: ``node_role: structural`` drops it, a ``node_type`` of
``activity`` with a meta ``agent_id`` drops it, the form feeds the class-prior
gate and ``content`` is the text served. A row written at a graph node's id is a
second copy of the node, and it can be the one that wins.

The route therefore refuses an identity key at such an id unless it repeats what
the node says of itself. Real SQLite stores, the real route, the real pack
builder.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SemanticSearch
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

NODE_ID = "node-billing"
DESCRIPTION = "Billing service: issues invoices and retries failed charges."
FORGED = "forged excerpt: this is not what the node says"
OPEN = "failover runbook open: drain the write queue, promote the replica. " * 4

#: The keys the route judges, each with a value that contradicts the node below.
CONTRADICTIONS = {
    "node_role": "structural",
    "node_type": "activity",
    "agent_id": "meta:analyzer",
    "document_form": "conversation",
    "content_type": "conversation",
    "excerpt": FORGED,
    "content": FORGED,
}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.graph_store.upsert_node(
        node_id=NODE_ID,
        node_type="service",
        properties={"name": "Billing", "description": DESCRIPTION},
    )
    reg.knowledge.document_store.put("doc-open", OPEN, {"title": "Open"})
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


def _vector(registry, seed=0.5):
    return [seed] * (registry.knowledge.vector_store.dimensions or 8)


def _post(client, registry, *rows, scopes=("ingest",)):
    return client.post(
        "/api/v1/vectors",
        json={
            "vectors": [
                {"item_id": item_id, "vector": _vector(registry), **extra}
                for item_id, extra in rows
            ]
        },
        headers=_headers(registry, list(scopes)),
    )


def _footprint(registry, *item_ids):
    knowledge = registry.knowledge
    return {
        "vectors": {i: knowledge.vector_store.get(i) for i in item_ids},
        "nodes": {i: knowledge.graph_store.get_node(i) for i in item_ids},
        "events": registry.operational.event_log.count(),
    }


def _semantic_pack(registry):
    builder = PackBuilder(
        strategies=[
            SemanticSearch(
                registry.knowledge.vector_store,
                embedding_fn=lambda _text: _vector(registry),
            )
        ],
        document_store=registry.knowledge.document_store,
    )
    return builder.build("billing", clearance="internal")


class TestAContradictingIdentityKeyIsRefused:
    @pytest.mark.parametrize("key", sorted(CONTRADICTIONS))
    @pytest.mark.parametrize(
        "scopes", [["ingest"], ["admin"]], ids=["ingest-key", "admin-key"]
    )
    def test_refused_naming_the_row_and_key_and_nothing_is_written(
        self, client, registry, key, scopes
    ):
        before = _footprint(registry, NODE_ID)
        resp = _post(
            client,
            registry,
            (NODE_ID, {"metadata": {key: CONTRADICTIONS[key]}}),
            scopes=scopes,
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith(f"vectors[0].metadata.{key} must match the graph node")
        assert NODE_ID in detail
        # Neither the node's own value nor the caller's is in the refusal.
        assert FORGED not in detail
        assert DESCRIPTION not in detail
        assert _footprint(registry, NODE_ID) == before
        assert registry.knowledge.vector_store.get(NODE_ID) is None

    def test_a_forged_role_would_have_hidden_the_node_from_a_pack(
        self, client, registry
    ):
        """The reason for the rule: the row is a second copy of the node."""
        resp = _post(client, registry, (NODE_ID, {"metadata": {"title": "t"}}))
        assert resp.status_code == 200, resp.text
        assert NODE_ID in [i.item_id for i in _semantic_pack(registry).items]
        # The refused write cannot remove it.
        refused = _post(
            client,
            registry,
            (NODE_ID, {"metadata": {"node_role": "structural"}}),
        )
        assert refused.status_code == 422
        assert NODE_ID in [i.item_id for i in _semantic_pack(registry).items]

    def test_one_refused_row_stores_nothing_from_the_batch(self, client, registry):
        resp = _post(
            client,
            registry,
            ("free-standing", {"metadata": {"title": "fine"}}),
            (NODE_ID, {"metadata": {"node_role": "structural"}}),
        )
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("vectors[1].metadata.node_role")
        assert registry.knowledge.vector_store.get("free-standing") is None

    def test_a_key_the_node_does_not_carry_is_a_contradiction(self, client, registry):
        """The node has no ``agent_id`` property, so a row naming one claims more."""
        resp = _post(client, registry, (NODE_ID, {"metadata": {"agent_id": "x"}}))
        assert resp.status_code == 422


class TestAnHonestWriterIsUnchanged:
    def test_a_row_repeating_the_nodes_own_identity_is_stored_as_sent(
        self, client, registry
    ):
        supplied = {
            "node_role": "semantic",
            "node_type": "service",
            "content": DESCRIPTION,
            "excerpt": DESCRIPTION,
            "title": "Billing",
        }
        resp = _post(client, registry, (NODE_ID, {"metadata": supplied}))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 1, "errors": 0}
        assert registry.knowledge.vector_store.get(NODE_ID)["metadata"] == supplied

    def test_a_row_with_only_other_keys_is_stored_as_sent(self, client, registry):
        supplied = {"title": "Billing", "team": "payments", "tags": ["a", "b"]}
        resp = _post(client, registry, (NODE_ID, {"metadata": supplied}))
        assert resp.status_code == 200, resp.text
        assert registry.knowledge.vector_store.get(NODE_ID)["metadata"] == supplied

    def test_a_row_with_no_metadata_is_stored_empty(self, client, registry):
        resp = _post(client, registry, (NODE_ID, {}))
        assert resp.status_code == 200, resp.text
        assert registry.knowledge.vector_store.get(NODE_ID)["metadata"] == {}

    def test_a_node_without_a_description_is_served_by_its_name(self, client, registry):
        registry.knowledge.graph_store.upsert_node(
            node_id="node-named", node_type="service", properties={"name": "Named"}
        )
        resp = _post(
            client, registry, ("node-named", {"metadata": {"content": "Named"}})
        )
        assert resp.status_code == 200, resp.text

    def test_an_id_naming_neither_a_node_nor_a_document_is_stored_as_sent(
        self, client, registry
    ):
        supplied: dict[str, Any] = dict(CONTRADICTIONS)
        resp = _post(client, registry, ("free-standing", {"metadata": supplied}))
        assert resp.status_code == 200, resp.text
        row = registry.knowledge.vector_store.get("free-standing")
        assert row["metadata"] == supplied

    def test_a_stored_document_keeps_its_own_rule_even_when_a_node_shares_the_id(
        self, client, registry
    ):
        """The id names a document, so the row takes the document's identity."""
        registry.knowledge.graph_store.upsert_node(
            node_id="doc-open", node_type="service", properties={"name": "Shared"}
        )
        resp = _post(
            client,
            registry,
            ("doc-open", {"metadata": {"node_role": "structural"}}),
        )
        assert resp.status_code == 200, resp.text
        row = registry.knowledge.vector_store.get("doc-open")["metadata"]
        assert "node_role" not in row

    def test_a_superseded_node_version_is_not_the_current_node(self, client, registry):
        """Only the current version of a node makes an id a node's."""
        registry.knowledge.graph_store.upsert_node(
            node_id="node-gone", node_type="service", properties={"name": "Gone"}
        )
        registry.knowledge.graph_store.delete_node("node-gone")
        resp = _post(
            client,
            registry,
            ("node-gone", {"metadata": {"node_role": "structural"}}),
        )
        assert resp.status_code == 200, resp.text
