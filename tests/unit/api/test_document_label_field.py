"""``GET /documents`` and ``GET /documents/{id}`` say how each row is labelled.

A key that reads only ``public`` documents is sent metadata cut down to a title
and a form, so the page could not work the label out from ``metadata``: it
would have shown every document that key reads as unlabelled. Each row carries
``label`` (``sensitivity``, ``stored``, ``corrupt``, ``external_readable``),
computed from the stored row before the cut, and only for rows the caller
already passed the read gate for. ``external_readable`` is compared, row by row,
with what an ``external`` key is actually served, and a row whose ``metadata``
is not an object answers an admin key as it did before the field existed.
"""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.schemas.classification import public_label_record
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

PUBLIC = {
    "sensitivity": "public",
    "external_readable": True,
    "stored": True,
    "corrupt": False,
}
UNLABELLED = {
    "sensitivity": "internal",
    "external_readable": False,
    "stored": False,
    "corrupt": False,
}
BARE_PUBLIC = {**PUBLIC, "external_readable": False}
RESTRICTED_CHUNK = {**UNLABELLED, "sensitivity": "restricted"}
#: ``metadata`` that is not an object holds no label to read, so the row reads
#: ``restricted``: a key below ``restricted`` is not served it, and the label
#: says so rather than showing the ``internal`` the row is withheld from.
UNREADABLE = {**UNLABELLED, "sensitivity": "restricted", "corrupt": True}


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


def _by_id(http, headers, doc_id):
    return http.get(
        "/api/v1/documents/" + urllib.parse.quote(doc_id, safe=""), headers=headers
    )


def _seed_chunks(reg) -> None:
    docs = reg.knowledge.document_store
    secret = {**public_label_record(), "sensitivity": "restricted"}
    docs.put("p-secret", "parent text", {"data_classification": secret})
    docs.put("p-open", "parent text", {"title": "Open parent"})
    docs.put("p-secret#chunk-0", "chunk text", {"parent_doc_id": "p-secret"})
    docs.put("p-open#chunk-0", "chunk text", {"parent_doc_id": "p-open"})
    docs.put(
        "p-secret#chunk-1",
        "chunk text",
        {"parent_doc_id": "p-secret", "data_classification": public_label_record()},
    )


def test_a_chunk_is_labelled_as_its_parent_reads(client) -> None:
    """A chunk with no label of its own reads its parent's, not ``internal``."""
    http, reg = client
    _seed_chunks(reg)
    admin = _headers(reg, ["admin"])
    listed = http.get(
        "/api/v1/documents?limit=100&include_chunks=true", headers=admin
    ).json()["documents"]
    labels = {row["doc_id"]: row["label"] for row in listed}
    assert labels["p-secret#chunk-0"] == RESTRICTED_CHUNK
    assert labels["p-open#chunk-0"] == UNLABELLED
    # A chunk's own public record cannot lower its parent's label.
    assert labels["p-secret#chunk-1"] == {**BARE_PUBLIC, "sensitivity": "restricted"}
    for doc_id in ("p-secret#chunk-0", "p-open#chunk-0", "p-secret#chunk-1"):
        detail = _by_id(http, admin, doc_id).json()["document"]["label"]
        assert detail == labels[doc_id], doc_id


def _seed_matrix(reg) -> None:
    docs = reg.knowledge.document_store
    exact = public_label_record()
    docs.put("m-exact", "alpha text", {"title": "t", "data_classification": exact})
    docs.put("m-bare", "alpha text", {"data_classification": {"sensitivity": "public"}})
    docs.put(
        "m-extra",
        "alpha text",
        {"data_classification": {"sensitivity": "public", "unknown_field": 1}},
    )
    docs.put("m-unlabelled", "alpha text", {"title": "t"})
    docs.put("m-corrupt", "alpha text", {"data_classification": "oops"})
    docs.put("m-exact#chunk-0", "alpha chunk", {"parent_doc_id": "m-exact"})
    docs.put(
        "m-exact#chunk-1",
        "alpha chunk",
        {"parent_doc_id": "m-exact", "data_classification": exact},
    )


def test_external_readable_is_what_an_external_key_is_actually_served(client) -> None:
    http, reg = client
    _seed_matrix(reg)
    admin = _headers(reg, ["admin"])
    external = _headers(reg, ["read", "external"])
    query = "/api/v1/documents?limit=100&include_chunks=true"
    rows = http.get(query, headers=admin).json()["documents"]
    said = {row["doc_id"]: row["label"]["external_readable"] for row in rows}
    served = {
        r["doc_id"] for r in http.get(query, headers=external).json()["documents"]
    }
    for doc_id, readable in said.items():
        status = _by_id(http, external, doc_id).status_code
        assert readable == (doc_id in served) == (status == 200), doc_id
    # The kinds the comparison is about were all in play: exact (readable),
    # bare, extra-key, unlabelled, corrupt and chunk rows (each not).
    assert said["m-exact"] is True
    for doc_id in (
        "m-bare",
        "m-extra",
        "m-unlabelled",
        "m-corrupt",
        "m-exact#chunk-0",
        "m-exact#chunk-1",
    ):
        assert said[doc_id] is False, doc_id


def test_a_corrupt_stored_record_is_flagged_and_a_chosen_restricted_is_not(
    client,
) -> None:
    http, reg = client
    docs = reg.knowledge.document_store
    docs.put("c-str", "x", {"data_classification": "oops"})
    docs.put("c-value", "x", {"data_classification": {"sensitivity": "secret"}})
    docs.put("c-none", "x", {"data_classification": None})
    docs.put(
        "c-chosen",
        "x",
        {"data_classification": {**public_label_record(), "sensitivity": "restricted"}},
    )
    labels = _labels(http, _headers(reg, ["admin"]))
    for doc_id in ("c-str", "c-value", "c-none"):
        assert labels[doc_id] == {
            "sensitivity": "restricted",
            "stored": True,
            "corrupt": True,
            "external_readable": False,
        }, doc_id
    assert labels["c-chosen"]["corrupt"] is False
    assert labels["doc-unlabelled"]["corrupt"] is False


@pytest.mark.parametrize("raw", ["[1,2]", '"str"', "5", "true"])
def test_a_row_whose_metadata_is_not_an_object_still_answers_an_admin_key(
    client, raw
) -> None:
    """These rows answered 200 before ``label`` existed; the label must not 500.

    An admin key is still served the row. The label reads it as the gate does:
    ``restricted`` and ``corrupt``, because a ``read`` key is not served it.
    """
    http, reg = client
    docs = reg.knowledge.document_store
    docs.put("bad", "plain text", {})
    conn = docs._conn if hasattr(docs, "_conn") else docs._get_conn()
    conn.execute("UPDATE documents SET metadata_json=? WHERE doc_id='bad'", (raw,))
    conn.commit()
    stored = docs.get("bad")
    admin = _headers(reg, ["admin"])
    resp = _by_id(http, admin, "bad")
    assert resp.status_code == 200, resp.text
    document = resp.json()["document"]
    assert document["label"] == UNREADABLE
    assert {k: v for k, v in document.items() if k != "label"} == stored
    listed = http.get("/api/v1/documents?limit=100", headers=admin)
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json()["documents"] if r["doc_id"] == "bad")
    assert row["label"] == UNREADABLE
