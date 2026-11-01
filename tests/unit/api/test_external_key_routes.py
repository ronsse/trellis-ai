"""An ``external`` key over REST: public documents answer, everything else is refused.

Built on a real minted ``read,external`` key under required auth, the way
``test_sensitivity_routes.py`` does for the higher rungs. A ``read`` key is run
through the same calls as the control, so a pass is not "this route is broken for
everyone".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.routes._principal import PUBLIC_CALLER_REFUSAL

PUBLIC = "failover runbook published: drain the write queue. " * 4
PLAIN = "failover runbook plain-sentinel: promote the replica. " * 4
SECRET = "failover runbook secret-sentinel: rotate the seal keys. " * 4

#: Every GET route a public-only caller is refused (the roster rule in
#: ``test_sensitivity_rest_roster_rule.py`` derives the same set from source).
REFUSED_GETS = [
    "/api/v1/graph/search?q=x",
    "/api/v1/graph/search/facets",
    "/api/v1/entities/node-1",
    "/api/v1/traces",
    "/api/v1/traces/domains",
    "/api/v1/traces/trace-1",
    "/api/v1/precedents",
    "/api/v1/precedents/domains",
    "/api/v1/graph/history?entity_id=node-1",
    "/api/v1/observations",
    "/api/v1/measurements",
    "/api/v1/policies",
    "/api/v1/policies/p-1",
    "/api/v1/classify/rules",
]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    docs = reg.knowledge.document_store
    docs.put("doc-public", PUBLIC, {"title": "Pub", **label("public")})
    docs.put("doc-plain", PLAIN, {"title": "Plain"})
    docs.put("doc-secret", SECRET, {"title": "Secret", **label("restricted")})
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


@pytest.fixture
def external(registry):
    return _headers(registry, ["read", "external"])


@pytest.fixture
def reader(registry):
    return _headers(registry, ["read"])


class TestRefusedRoutes:
    @pytest.mark.parametrize("path", REFUSED_GETS)
    def test_an_external_key_is_refused_with_a_stated_reason(
        self, client, external, path
    ) -> None:
        resp = client.get(path, headers=external)
        assert resp.status_code == 403, (path, resp.text)
        assert PUBLIC_CALLER_REFUSAL in resp.text

    @pytest.mark.parametrize("path", REFUSED_GETS)
    def test_a_read_key_is_not_refused_by_the_same_gate(
        self, client, reader, path
    ) -> None:
        resp = client.get(path, headers=reader)
        assert resp.status_code != 403, (path, resp.text)

    def test_the_refusal_names_what_the_key_can_do_instead(self) -> None:
        assert "public" in PUBLIC_CALLER_REFUSAL.lower()

    def test_an_external_key_cannot_write(self, client, external) -> None:
        resp = client.post(
            "/api/v1/commands/batch",
            json={"commands": [], "strategy": "sequential"},
            headers=external,
        )
        assert resp.status_code == 403


class TestAnsweringRoutes:
    def test_a_flat_pack_carries_only_the_public_document(
        self, client, external, reader
    ) -> None:
        for headers, sentinels in ((external, False), (reader, True)):
            resp = client.post(
                "/api/v1/packs", json={"intent": "failover runbook"}, headers=headers
            )
            assert resp.status_code == 200, resp.text
            assert ("plain-sentinel" in resp.text) is sentinels
            assert "secret-sentinel" not in resp.text
        assert (
            "published"
            in client.post(
                "/api/v1/packs", json={"intent": "failover runbook"}, headers=external
            ).text
        )

    def test_a_sectioned_pack_carries_only_the_public_document(
        self, client, external
    ) -> None:
        resp = client.post(
            "/api/v1/packs/sectioned",
            json={"intent": "failover runbook", "sections": [{"name": "All"}]},
            headers=external,
        )
        assert resp.status_code == 200, resp.text
        assert "published" in resp.text
        assert "plain-sentinel" not in resp.text
        assert "secret-sentinel" not in resp.text

    def test_a_pack_names_no_withheld_id(self, client, external) -> None:
        resp = client.post(
            "/api/v1/packs", json={"intent": "failover runbook"}, headers=external
        )
        assert "doc-plain" not in resp.text
        assert "doc-secret" not in resp.text

    def test_search_carries_only_the_public_document(self, client, external) -> None:
        resp = client.get("/api/v1/search", params={"q": "failover"}, headers=external)
        assert resp.status_code == 200, resp.text
        assert "doc-public" in resp.text
        assert "doc-plain" not in resp.text
        assert "plain-sentinel" not in resp.text
        assert "secret-sentinel" not in resp.text

    def test_documents_read_public_only_and_the_rest_answer_not_found(
        self, client, external, reader
    ) -> None:
        assert (
            client.get("/api/v1/documents/doc-public", headers=external).status_code
            == 200
        )
        hidden = client.get("/api/v1/documents/doc-plain", headers=external)
        missing = client.get("/api/v1/documents/doc-ghost", headers=external)
        assert hidden.status_code == missing.status_code == 404
        assert (
            client.get("/api/v1/documents/doc-plain", headers=reader).status_code == 200
        )

    def test_the_document_list_is_public_only(self, client, external) -> None:
        resp = client.get("/api/v1/documents", headers=external)
        assert resp.status_code == 200, resp.text
        assert "doc-public" in resp.text
        assert "doc-plain" not in resp.text
        assert "doc-secret" not in resp.text


class TestReadyz:
    def test_an_external_key_gets_the_anonymous_status_line(
        self, client, external, reader
    ) -> None:
        anonymous = client.get("/readyz").json()
        assert set(client.get("/readyz", headers=external).json()) == set(anonymous)
        assert "backends" not in client.get("/readyz", headers=external).json()
        assert "backends" in client.get("/readyz", headers=reader).json()
