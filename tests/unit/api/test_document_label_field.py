"""``GET /documents`` and ``GET /documents/{id}`` say how each row is labelled.

A key that reads only ``public`` documents is sent metadata cut down to a title
and a form, so the page could not work the label out from ``metadata``: it
would have shown every document that key reads as unlabelled. Each row carries
``label`` (``sensitivity``, ``stored``, ``external_readable``), computed from
the stored row before the cut, and only for rows the caller already passed the
read gate for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.schemas.classification import public_label_record
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

PUBLIC = {"sensitivity": "public", "external_readable": True, "stored": True}
UNLABELLED = {"sensitivity": "internal", "external_readable": False, "stored": False}
BARE_PUBLIC = {"sensitivity": "public", "external_readable": False, "stored": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    docs = reg.knowledge.document_store
    docs.put("doc-unlabelled", "plain note", {"title": "Plain"})
    docs.put(
        "doc-public",
        "open note",
        {
            "title": "Open",
            "entity_id": "e1",
            "data_classification": public_label_record(),
        },
    )
    docs.put(
        "doc-bare",
        "bare note",
        {"title": "Bare", "data_classification": {"sensitivity": "public"}},
    )
    docs.put(
        "doc-secret",
        "secret note",
        {
            "title": "Secret",
            "data_classification": {
                **public_label_record(),
                "sensitivity": "restricted",
            },
        },
    )
    yield TestClient(create_app()), reg
    reg.close()
    app_module._registry = None


def _headers(reg, scopes):
    token, record = generate_api_key("k", scopes)
    reg.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _labels(client, headers):
    resp = client.get("/api/v1/documents", headers=headers)
    assert resp.status_code == 200, resp.text
    return {row["doc_id"]: row["label"] for row in resp.json()["documents"]}


def test_list_labels_every_row_for_a_full_clearance_key(client) -> None:
    http, reg = client
    labels = _labels(http, _headers(reg, ["admin"]))
    assert labels["doc-unlabelled"] == UNLABELLED
    assert labels["doc-public"] == PUBLIC
    assert labels["doc-bare"] == BARE_PUBLIC
    assert labels["doc-secret"] == {**PUBLIC, "sensitivity": "restricted"} | {
        "external_readable": False
    }


def test_external_key_is_told_public_though_its_metadata_carries_no_label(
    client,
) -> None:
    http, reg = client
    headers = _headers(reg, ["read", "external"])
    resp = http.get("/api/v1/documents", headers=headers)
    rows = resp.json()["documents"]
    # Only the document labelled by a relabel is readable to this key, and its
    # metadata is the allowlisted cut: no classification for a page to read.
    assert [r["doc_id"] for r in rows] == ["doc-public"]
    assert rows[0]["metadata"] == {"title": "Open"}
    assert rows[0]["label"] == PUBLIC
    detail = http.get("/api/v1/documents/doc-public", headers=headers).json()
    assert detail["document"]["metadata"] == {"title": "Open"}
    assert detail["document"]["label"] == PUBLIC


def test_a_label_is_never_reported_for_a_row_the_key_cannot_read(client) -> None:
    http, reg = client
    headers = _headers(reg, ["read", "external"])
    assert set(_labels(http, headers)) == {"doc-public"}
    for hidden in ("doc-unlabelled", "doc-bare", "doc-secret", "doc-missing"):
        resp = http.get(f"/api/v1/documents/{hidden}", headers=headers)
        assert resp.status_code == 404
        assert resp.json() == {"detail": f"Document not found: {hidden}"}
    # Below restricted the list drops the row too: no label, no id.
    read_labels = _labels(http, _headers(reg, ["read"]))
    assert "doc-secret" not in read_labels
    assert read_labels["doc-unlabelled"] == UNLABELLED


def test_detail_label_for_a_read_key_matches_the_list(client) -> None:
    http, reg = client
    headers = _headers(reg, ["read"])
    body = http.get("/api/v1/documents/doc-unlabelled", headers=headers).json()
    assert body["document"]["label"] == UNLABELLED
