"""REST mutation doors honour the key's clearance (#194, #1023).

``/commands/batch`` and the curate routes carry ``request_clearance(ctx)`` into
the executor, so a key that cannot read a labelled document cannot overwrite,
relabel or lower it either, whichever door it knocks on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.schemas.classification import sensitivity_of
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

BODY = "failover runbook zebra ledger: rotate the vault seal keys. " * 4


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


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _batch(client, headers, *commands):
    resp = client.post(
        "/api/v1/commands/batch",
        json={"commands": list(commands), "strategy": "sequential"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def _ingest(doc_id, level=None, content="replacement"):
    return {
        "operation": "evidence.ingest",
        "args": {
            "evidence": {
                "doc_id": doc_id,
                "content": content,
                "metadata": label(level) if level else {},
            }
        },
    }


def _level(registry, doc_id):
    return sensitivity_of(registry.knowledge.document_store.get(doc_id)["metadata"])


MUTATE = ["mutate"]
MUTATE_CONFIDENTIAL = ["mutate", "confidential"]
MUTATE_RESTRICTED = ["mutate", "restricted"]


class TestEvidenceIngestViaBatch:
    def test_a_key_below_the_label_cannot_overwrite(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        [result] = _batch(client, _headers(registry, MUTATE), _ingest("doc"))
        assert result["status"] == "rejected"
        stored = registry.knowledge.document_store.get("doc")
        assert stored["content"] == BODY
        assert _level(registry, "doc") == "restricted"

    def test_a_key_below_the_label_cannot_overwrite_at_the_same_label(
        self, client, registry
    ):
        """No downgrade here, so only the clearance check can refuse."""
        registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        [result] = _batch(
            client, _headers(registry, MUTATE), _ingest("doc", "confidential")
        )
        assert result["status"] == "rejected"
        assert registry.knowledge.document_store.get("doc")["content"] == BODY

    def test_a_cleared_key_cannot_lower_the_label(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        headers = _headers(registry, MUTATE_RESTRICTED)
        [result] = _batch(client, headers, _ingest("doc", "confidential"))
        assert result["status"] == "rejected"
        assert _level(registry, "doc") == "restricted"

    @pytest.mark.parametrize("written", ["confidential", "restricted"])
    def test_a_cleared_key_may_rewrite_at_the_same_or_a_higher_label(
        self, client, registry, written
    ):
        registry.knowledge.document_store.put("doc", BODY, label("confidential"))
        headers = _headers(registry, MUTATE_RESTRICTED)
        [result] = _batch(client, headers, _ingest("doc", written))
        assert result["status"] == "success", result
        assert _level(registry, "doc") == written

    def test_an_unlabelled_overwrite_by_the_lowest_key_still_works(
        self, client, registry
    ):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        [result] = _batch(client, _headers(registry, MUTATE), _ingest("doc"))
        assert result["status"] == "success", result


class TestClassificationViaBatchAndRoute:
    def _set(self, doc_id, level):
        return {
            "operation": "classification.set",
            "args": {"doc_id": doc_id, "sensitivity": level},
        }

    def test_a_mutate_key_without_restricted_cannot_relabel_a_restricted_doc(
        self, client, registry
    ):
        registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        [result] = _batch(
            client,
            _headers(registry, MUTATE_CONFIDENTIAL),
            self._set("doc", "internal"),
        )
        assert result["status"] == "rejected"
        assert _level(registry, "doc") == "restricted"

    def test_a_mutate_key_cannot_push_a_doc_above_its_own_clearance(
        self, client, registry
    ):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        [result] = _batch(
            client, _headers(registry, MUTATE), self._set("doc", "confidential")
        )
        assert result["status"] == "rejected"
        assert _level(registry, "doc") == "internal"

    def test_a_restricted_mutate_key_can_relabel(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        [result] = _batch(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            self._set("doc", "restricted"),
        )
        assert result["status"] == "success", result
        assert _level(registry, "doc") == "restricted"

    def test_the_route_relabels_and_a_read_key_then_loses_the_document(
        self, client, registry
    ):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        read = _headers(registry, ["read"])

        def pack_text():
            resp = client.post(
                "/api/v1/packs", json={"intent": "failover runbook"}, headers=read
            )
            assert resp.status_code == 200, resp.text
            return resp.text

        assert "doc" in pack_text()
        resp = client.post(
            "/api/v1/documents/doc/sensitivity",
            json={"sensitivity": "restricted"},
            headers=_headers(registry, MUTATE_RESTRICTED),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"] == "classification.set"
        assert _level(registry, "doc") == "restricted"
        assert "zebra ledger" not in pack_text()

    def test_the_route_refuses_a_key_without_the_clearance(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, label("restricted"))
        resp = client.post(
            "/api/v1/documents/doc/sensitivity",
            json={"sensitivity": "internal"},
            headers=_headers(registry, MUTATE_CONFIDENTIAL),
        )
        assert resp.status_code == 400
        assert "restricted" not in resp.text
        assert _level(registry, "doc") == "restricted"

    def test_the_route_refuses_a_value_outside_the_vocabulary(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        resp = client.post(
            "/api/v1/documents/doc/sensitivity",
            json={"sensitivity": "top-secret"},
            headers=_headers(registry, MUTATE_RESTRICTED),
        )
        assert resp.status_code == 422

    def test_the_route_needs_the_mutate_scope(self, client, registry):
        registry.knowledge.document_store.put("doc", BODY, {"title": "t"})
        resp = client.post(
            "/api/v1/documents/doc/sensitivity",
            json={"sensitivity": "restricted"},
            headers=_headers(registry, ["read", "restricted"]),
        )
        assert resp.status_code == 403
        assert _level(registry, "doc") == "internal"
