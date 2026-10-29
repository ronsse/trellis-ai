"""REST pack, search and document reads honour the key's clearance (#194)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

SECRET = "failover runbook zebra ledger: rotate the vault seal keys. " * 4
OPEN = "failover runbook open: drain the write queue, promote the replica. " * 4


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.document_store.put("doc-open", OPEN, {"title": "Open"})
    reg.knowledge.document_store.put(
        "doc-secret", SECRET, {"title": "Secret", **label("restricted")}
    )
    yield reg
    reg.close()
    app_module._registry = None


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _pack_text(client, headers) -> str:
    resp = client.post(
        "/api/v1/packs", json={"intent": "failover runbook"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.text


class TestRestSurfaces:
    def test_pack_withholds_for_a_read_key_and_serves_an_admin(self, client, registry):
        assert "zebra ledger" not in _pack_text(client, _headers(registry, ["read"]))
        assert "zebra ledger" in _pack_text(client, _headers(registry, ["admin"]))
        assert "zebra ledger" in _pack_text(
            client, _headers(registry, ["read", "restricted"])
        )

    def test_sectioned_pack_withholds_for_a_read_key(self, client, registry):
        body = {"intent": "failover runbook", "sections": [{"name": "All"}]}
        read = client.post(
            "/api/v1/packs/sectioned", json=body, headers=_headers(registry, ["read"])
        )
        admin = client.post(
            "/api/v1/packs/sectioned", json=body, headers=_headers(registry, ["admin"])
        )
        assert "zebra ledger" not in read.text
        assert "zebra ledger" in admin.text

    def test_search_withholds(self, client, registry):
        read = client.get(
            "/api/v1/search",
            params={"q": "failover"},
            headers=_headers(registry, ["read"]),
        )
        admin = client.get(
            "/api/v1/search",
            params={"q": "failover"},
            headers=_headers(registry, ["admin"]),
        )
        assert "doc-secret" not in read.text
        assert "doc-open" in read.text
        assert "doc-secret" in admin.text

    def test_a_withheld_document_is_404_exactly_like_a_missing_one(
        self, client, registry
    ):
        headers = _headers(registry, ["read"])
        hidden = client.get("/api/v1/documents/doc-secret", headers=headers)
        missing = client.get("/api/v1/documents/doc-ghost", headers=headers)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.text.replace("doc-secret", "X") == missing.text.replace(
            "doc-ghost", "X"
        )
        assert (
            client.get(
                "/api/v1/documents/doc-secret", headers=_headers(registry, ["admin"])
            ).status_code
            == 200
        )

    def test_auth_off_reads_everything(self, client, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        assert (
            "zebra ledger"
            in client.post("/api/v1/packs", json={"intent": "failover runbook"}).text
        )
