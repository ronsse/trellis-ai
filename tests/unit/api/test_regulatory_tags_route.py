"""``POST /api/v1/documents/{id}/regulatory-tags``; tagging never moves who reads what.

Two things are held here. The route answers as its sibling
``.../sensitivity`` does (200 on success, 400 on a refusal, 403 below the
``mutate`` scope). And the serving test, the hard requirement of the feature:
for a key at each ceiling (``read``, ``read,confidential``, ``read,restricted``,
``read,external``) every read surface serves the same documents after tagging as
before. A ``public`` document is the one label tagging would change, which is why
the route refuses it; the serving test fails if that refusal is removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    public_label_record,
)
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

WORD = "failover"
#: One distinct body per document, so a pack's near-duplicate collapse cannot hide
#: a document that the label (and not the text) would have served.
BODIES: dict[str, str] = {
    "doc-unlabelled": f"{WORD} runbook zebra ledger: rotate the vault seal keys.",
    "doc-internal": f"{WORD} drill for the quartz harbour pump rota.",
    "doc-confidential": f"{WORD} notes on the amber orchard invoices.",
    "doc-restricted": f"{WORD} checklist for the cobalt lantern archive.",
    "doc-public": f"{WORD} public status page: winter maintenance windows.",
}

#: ``doc_id -> stored metadata`` covering every label a document can carry.
DOCS: dict[str, dict[str, Any]] = {
    "doc-unlabelled": {"title": "t"},
    "doc-internal": label("internal"),
    "doc-confidential": label("confidential"),
    "doc-restricted": label("restricted"),
    "doc-public": {DATA_CLASSIFICATION_KEY: public_label_record()},
}

TAGGABLE = [d for d in DOCS if d != "doc-public"]

CEILINGS: dict[str, list[str]] = {
    "read": ["read"],
    "confidential": ["read", "confidential"],
    "restricted": ["read", "restricted"],
    "external": ["read", "external"],
}

MUTATE_RESTRICTED = ["mutate", "restricted"]


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


def _headers(registry: StoreRegistry, scopes: list[str]) -> dict[str, str]:
    token, record = generate_api_key("k-" + "-".join(scopes), scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _seed(registry: StoreRegistry) -> None:
    for doc_id, metadata in DOCS.items():
        registry.knowledge.document_store.put(doc_id, BODIES[doc_id], dict(metadata))


def _tag(client: TestClient, headers: dict[str, str], doc_id: str, **body: Any) -> Any:
    return client.post(
        f"/api/v1/documents/{doc_id}/regulatory-tags", json=body, headers=headers
    )


def _served(client: TestClient, headers: dict[str, str]) -> dict[str, set[str]]:
    """The ids each read surface serves to ``headers``'s key."""
    listing = client.get("/api/v1/documents", params={"limit": 100}, headers=headers)
    query = client.get("/api/v1/documents", params={"q": WORD}, headers=headers)
    search = client.get("/api/v1/search", params={"q": WORD}, headers=headers)
    pack = client.post("/api/v1/packs", json={"intent": WORD}, headers=headers)
    for response in (listing, query, search, pack):
        assert response.status_code == 200, response.text
    by_id = {
        i
        for i in DOCS
        if client.get(f"/api/v1/documents/{i}", headers=headers).status_code == 200
    }
    return {
        "by-id": by_id,
        "list": {d["doc_id"] for d in listing.json()["documents"]},
        "documents-q": {d["doc_id"] for d in query.json()["documents"]},
        "search": {r["doc_id"] for r in search.json()["results"]},
        "pack": {i["item_id"] for i in pack.json()["items"]},
    }


class TestRoute:
    def test_sets_normalised_tags(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        resp = _tag(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            "doc-internal",
            tags=["PHI", "pii", " Pii"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation"] == "classification.tag_regulatory"
        assert body["status"] == "success"
        stored = registry.knowledge.document_store.get("doc-internal")
        assert stored is not None
        record = stored["metadata"][DATA_CLASSIFICATION_KEY]
        assert record["regulatory_tags"] == ["phi", "pii"]
        assert record["sensitivity"] == "internal"

    def test_clear_removes_the_set(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        headers = _headers(registry, MUTATE_RESTRICTED)
        assert _tag(client, headers, "doc-internal", tags=["pii"]).status_code == 200
        resp = _tag(client, headers, "doc-internal", clear=True)
        assert resp.status_code == 200, resp.text
        stored = registry.knowledge.document_store.get("doc-internal")
        assert stored is not None
        assert stored["metadata"][DATA_CLASSIFICATION_KEY]["regulatory_tags"] == []

    def test_a_public_document_is_refused_with_400_and_not_written(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        before = registry.knowledge.document_store.get("doc-public")
        resp = _tag(
            client, _headers(registry, MUTATE_RESTRICTED), "doc-public", tags=["pii"]
        )
        assert resp.status_code == 400, resp.text
        assert "public" in resp.json()["detail"]
        assert registry.knowledge.document_store.get("doc-public") == before

    @pytest.mark.parametrize(
        "body", [{}, {"tags": []}, {"tags": ["pii"], "clear": True}]
    )
    def test_no_tags_or_both_is_refused_with_400(
        self, client: TestClient, registry: StoreRegistry, body: dict[str, Any]
    ) -> None:
        _seed(registry)
        resp = _tag(
            client, _headers(registry, MUTATE_RESTRICTED), "doc-internal", **body
        )
        assert resp.status_code == 400, resp.text

    def test_an_unknown_body_field_is_refused_with_422(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        resp = _tag(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            "doc-internal",
            tags=["pii"],
            sensitivity="public",
        )
        assert resp.status_code == 422, resp.text

    def test_the_cap_counts_distinct_tags_after_normalisation(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """33 entries holding a duplicate are 32 tags; 33 distinct are refused."""
        _seed(registry)
        headers = _headers(registry, MUTATE_RESTRICTED)
        names = [f"t{i:02d}" for i in range(32)]

        accepted = _tag(client, headers, "doc-internal", tags=[*names, "T00"])
        assert accepted.status_code == 200, accepted.text
        stored = registry.knowledge.document_store.get("doc-internal")
        assert stored is not None
        assert stored["metadata"][DATA_CLASSIFICATION_KEY]["regulatory_tags"] == names

        before = registry.knowledge.document_store.get("doc-confidential")
        refused = _tag(client, headers, "doc-confidential", tags=[*names, "t32"])
        assert refused.status_code == 400, refused.text
        assert "32" in refused.json()["detail"]
        assert registry.knowledge.document_store.get("doc-confidential") == before

    def test_a_missing_document_is_400(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = _tag(client, _headers(registry, MUTATE_RESTRICTED), "nope", tags=["pii"])
        assert resp.status_code == 400, resp.text

    def test_a_key_below_the_label_cannot_tell_the_document_exists(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        before = registry.knowledge.document_store.get("doc-restricted")
        weak = _tag(
            client,
            _headers(registry, ["mutate", "confidential"]),
            "doc-restricted",
            tags=["pii"],
        )
        missing = _tag(
            client,
            _headers(registry, ["mutate", "confidential"]),
            "nope",
            tags=["pii"],
        )
        assert weak.status_code == missing.status_code == 400
        assert "not found" in weak.json()["detail"]
        assert "not found" in missing.json()["detail"]
        assert registry.knowledge.document_store.get("doc-restricted") == before

    def test_a_read_only_key_is_refused_with_403(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        resp = _tag(client, _headers(registry, ["read"]), "doc-internal", tags=["pii"])
        assert resp.status_code == 403, resp.text

    def test_a_requested_by_claim_is_not_trusted(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        resp = _tag(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            "doc-internal",
            tags=["pii"],
            requested_by="auto:learning_candidate",
        )
        assert resp.status_code == 200, resp.text


class TestServingIsUnchangedForEveryCeiling:
    def test_every_read_surface_serves_the_same_documents_after_tagging(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        keys = {name: _headers(registry, scopes) for name, scopes in CEILINGS.items()}
        before = {name: _served(client, headers) for name, headers in keys.items()}

        # Sanity: the corpus really does differ by ceiling, so the comparison
        # below is not between four identical sets. A public document reads at
        # every ceiling; the others only at or above their own label.
        assert before["read"]["by-id"] == {
            "doc-unlabelled",
            "doc-internal",
            "doc-public",
        }
        assert before["confidential"]["by-id"] == before["read"]["by-id"] | {
            "doc-confidential"
        }
        assert before["restricted"]["by-id"] == set(DOCS)
        assert before["external"]["by-id"] == {"doc-public"}
        assert before["external"]["pack"] == {"doc-public"}
        assert before["restricted"]["pack"] == set(DOCS)

        tagger = _headers(registry, MUTATE_RESTRICTED)
        statuses = {
            doc_id: _tag(client, tagger, doc_id, tags=["PII", "phi"]).status_code
            for doc_id in DOCS
        }

        # Serving first: if the public refusal is ever removed, this is the
        # assertion that shows the external key losing the document.
        after = {name: _served(client, headers) for name, headers in keys.items()}
        assert after == before
        assert statuses == {d: 400 if d == "doc-public" else 200 for d in DOCS}

    def test_the_tags_are_written_even_though_serving_did_not_move(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _seed(registry)
        tagger = _headers(registry, MUTATE_RESTRICTED)
        for doc_id in TAGGABLE:
            assert _tag(client, tagger, doc_id, tags=["pii"]).status_code == 200
            stored = registry.knowledge.document_store.get(doc_id)
            assert stored is not None
            record = stored["metadata"][DATA_CLASSIFICATION_KEY]
            assert record["regulatory_tags"] == ["pii"], doc_id
