"""REST pack, search and document reads honour the key's clearance (#194)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.stores.base.event_log import EventType
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


def _post(client, path, headers, **body):
    resp = client.post(f"/api/v1/{path}", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp


FLAT = {"path": "packs", "intent": "failover runbook"}
SECTIONED = {
    "path": "packs/sectioned",
    "intent": "failover runbook",
    "sections": [{"name": "All"}],
}


class TestWithheldIdsAreNotAnOracle:
    """A below-``restricted`` caller learns how many, never which (#194)."""

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    @pytest.mark.parametrize("scopes", [["read"], ["read", "confidential"]])
    def test_the_withheld_id_is_absent_from_the_whole_response(
        self, client, registry, request_body, scopes
    ):
        resp = _post(client, headers=_headers(registry, scopes), **request_body)
        assert "doc-secret" not in resp.text
        withholding = resp.json()["withholding"]
        assert withholding["by_reason"]["sensitivity"] == 1, "the count must survive"
        assert withholding["total"] >= 1

    def test_the_flat_report_drops_the_sensitivity_rows_and_keeps_the_rest(
        self, client, registry
    ):
        body = _post(client, headers=_headers(registry, ["read"]), **FLAT).json()
        assert [
            row
            for row in body["retrieval_report"]["rejected_items"]
            if row["reason"] == "sensitivity"
        ] == []

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    def test_a_restricted_caller_is_served_the_document(
        self, client, registry, request_body, scopes
    ):
        resp = _post(client, headers=_headers(registry, scopes), **request_body)
        assert "doc-secret" in resp.text
        assert "sensitivity" not in resp.json()["withholding"]["by_reason"]

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    def test_the_event_payload_keeps_the_id_for_its_admin_readers(
        self, client, registry, request_body
    ):
        _post(client, headers=_headers(registry, ["read"]), **request_body)
        events = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert len(events) == 1
        payload = events[0].payload
        assert "doc-secret" in payload["withholding"]["withheld_item_ids"]
        assert "sensitivity_withheld_ids" not in payload


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


REPLACEMENT = "unrelated replacement text about queue depth dashboards"


def _stored(registry):
    doc = registry.knowledge.document_store.get("doc-secret")
    assert doc is not None
    return doc


def _write_documents(client, headers, **extra):
    return client.post(
        "/api/v1/documents",
        json={"doc_id": "doc-secret", "content": REPLACEMENT, **extra},
        headers=headers,
    )


def _write_evidence(client, headers, **extra):
    return client.post(
        "/api/v1/evidence",
        json={
            "evidence_id": "doc-secret",
            "evidence_type": "document",
            "content": REPLACEMENT,
            "source_origin": "manual",
            **extra,
        },
        headers=headers,
    )


WRITERS = pytest.mark.parametrize(
    ("write", "writer_scopes"),
    [(_write_documents, ["mutate"]), (_write_evidence, ["ingest"])],
    ids=["documents", "evidence"],
)


class TestOverwriteOfALabelledDocument:
    """A caller-chosen id cannot replace a row the key may not read (#194)."""

    @WRITERS
    def test_a_key_below_the_label_gets_409_and_the_row_is_intact(
        self, client, registry, write, writer_scopes
    ):
        resp = write(client, _headers(registry, writer_scopes))
        assert resp.status_code == 409, resp.text
        assert "zebra ledger" not in resp.text
        assert "restricted" not in resp.text, "the refusal must not name the label"
        stored = _stored(registry)
        assert stored["content"] == SECRET
        assert (
            stored["metadata"]["data_classification"]
            == label("restricted")["data_classification"]
        )

    @WRITERS
    def test_the_chunks_stay_withheld_after_the_refused_write(
        self, client, registry, write, writer_scopes
    ):
        chunk_id = chunk_doc_id("doc-secret", 0)
        registry.knowledge.document_store.put(chunk_id, SECRET, {})
        assert write(client, _headers(registry, writer_scopes)).status_code == 409
        resp = client.get(
            f"/api/v1/documents/{chunk_id}", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 404
        assert "zebra ledger" not in resp.text

    @WRITERS
    def test_a_cleared_key_may_not_lower_the_label(
        self, client, registry, write, writer_scopes
    ):
        resp = write(client, _headers(registry, [*writer_scopes, "restricted"]))
        assert resp.status_code == 409, resp.text
        assert "lower" in resp.text
        assert _stored(registry)["content"] == SECRET

    def test_a_cleared_key_may_replace_at_the_same_label(self, client, registry):
        resp = _write_documents(
            client, _headers(registry, ["admin"]), metadata=label("restricted")
        )
        assert resp.status_code == 200, resp.text
        assert _stored(registry)["content"] == REPLACEMENT

    def test_the_evidence_route_carries_no_label_so_even_admin_cannot_replace(
        self, client, registry
    ):
        """It writes no label, so a re-put of a labelled row is always a downgrade."""
        resp = _write_evidence(client, _headers(registry, ["admin"]))
        assert resp.status_code == 409, resp.text
        assert "lower" in resp.text
        assert _stored(registry)["content"] == SECRET

    @WRITERS
    def test_an_unlabelled_document_stays_replaceable(
        self, client, registry, write, writer_scopes
    ):
        """The guard is about labels: an ``internal`` row is the default posture."""
        registry.knowledge.document_store.put("doc-secret", OPEN, {})
        resp = write(client, _headers(registry, writer_scopes))
        assert resp.status_code == 200, resp.text
        assert _stored(registry)["content"] == REPLACEMENT

    @WRITERS
    def test_a_new_id_is_unaffected(self, client, registry, write, writer_scopes):
        registry.knowledge.document_store.delete("doc-secret")
        assert write(client, _headers(registry, writer_scopes)).status_code == 200
