"""REST pack, search and document reads honour the key's clearance (#194)."""

from __future__ import annotations

import gc
import time
import weakref

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
import trellis_api.routes.explore as explore_mod
from tests.unit.retrieve.test_sensitivity import _lifecycle, label
from trellis.auth import generate_api_key
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mutate.retention import ARCHIVED_STATE
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

# Distinct bodies: near-identical ones are collapsed by the pack's semantic dedup.
OLD = {
    "doc-old": "failover runbook, first edition: pause the nightly export jobs. " * 4,
    "doc-stale": "failover runbook, second edition: raise the replica lag alarm. " * 4,
    "doc-fine": "failover runbook, third edition: rehearse the dns cutover. " * 4,
}


class TestAServedLoserNamesNoUnreadableSuccessor:
    """A kept loser's stored ``superseded_by`` is not an id leak (#1029).

    A loser whose successor the key cannot read is served, and its own stored
    lifecycle record still names that successor. The name must not reach a key
    that may not read the successor, whether the successor matched the query
    (``doc-old``, successor ``doc-secret``) or did not (``doc-stale``, successor
    ``doc-newer``): the store decides, not the pool, so the response cannot say
    which documents matched.
    """

    @pytest.fixture(autouse=True)
    def _losers(self, registry):
        store = registry.knowledge.document_store
        successors = {
            "doc-old": "doc-secret",
            "doc-stale": "doc-newer",
            # A loser whose successor anyone may read keeps naming it.
            "doc-fine": "doc-fresh",
        }
        for doc_id, successor in successors.items():
            store.put(
                doc_id,
                OLD[doc_id],
                {"title": doc_id, **_lifecycle("superseded", successor)},
            )
        store.put("doc-newer", REPLACEMENT, {"title": "Newer", **label("restricted")})
        store.put("doc-fresh", REPLACEMENT + " fresh", {"title": "Fresh"})

    @staticmethod
    def _items(body, path):
        if path == "packs":
            return {i["item_id"]: i for i in body["items"]}
        return {i["item_id"]: i for s in body["sections"] for i in s["items"]}

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    @pytest.mark.parametrize("scopes", [["read"], ["read", "confidential"]])
    def test_a_pack_serves_the_loser_without_the_id(
        self, client, registry, request_body, scopes
    ):
        resp = _post(client, headers=_headers(registry, scopes), **request_body)
        items = self._items(resp.json(), request_body["path"])
        assert {"doc-old", "doc-stale", "doc-fine"} <= set(items), (
            "the losers are served"
        )
        for hidden in ("doc-secret", "doc-newer"):
            assert hidden not in resp.text
        for loser in ("doc-old", "doc-stale"):
            assert items[loser]["metadata"]["lifecycle"] == {"state": "superseded"}
        assert (
            items["doc-fine"]["metadata"]["lifecycle"]["superseded_by"] == "doc-fresh"
        )

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    def test_a_restricted_key_is_served_the_record_as_stored(
        self, client, registry, request_body
    ):
        resp = _post(
            client, headers=_headers(registry, ["read", "restricted"]), **request_body
        )
        items = self._items(resp.json(), request_body["path"])
        assert "doc-old" not in items, "its readable successor supersedes it"
        assert (
            items["doc-stale"]["metadata"]["lifecycle"]["superseded_by"] == "doc-newer"
        )

    @pytest.mark.parametrize(
        ("doc_id", "successor"), [("doc-old", "doc-secret"), ("doc-stale", "doc-newer")]
    )
    def test_a_by_id_read_serves_the_loser_without_the_id(
        self, client, registry, doc_id, successor
    ):
        resp = client.get(
            f"/api/v1/documents/{doc_id}", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 200, resp.text
        assert successor not in resp.text
        assert resp.json()["document"]["metadata"]["lifecycle"] == {
            "state": "superseded"
        }
        assert resp.json()["document"]["content"] == OLD[doc_id]

    @pytest.mark.parametrize("scopes", [["admin"], ["read", "restricted"]])
    @pytest.mark.parametrize(
        ("doc_id", "successor"), [("doc-old", "doc-secret"), ("doc-stale", "doc-newer")]
    )
    def test_a_restricted_by_id_read_is_unchanged(
        self, client, registry, scopes, doc_id, successor
    ):
        resp = client.get(
            f"/api/v1/documents/{doc_id}", headers=_headers(registry, scopes)
        )
        stored = registry.knowledge.document_store.get(doc_id)
        assert resp.json()["document"] == stored
        assert stored["metadata"]["lifecycle"]["superseded_by"] == successor

    def test_a_readable_successor_is_still_named_by_id(self, client, registry):
        resp = client.get(
            "/api/v1/documents/doc-fine", headers=_headers(registry, ["read"])
        )
        assert (
            resp.json()["document"]["metadata"]["lifecycle"]["superseded_by"]
            == "doc-fresh"
        )

    def test_the_stored_record_is_never_rewritten(self, client, registry):
        before = registry.knowledge.document_store.get("doc-old")
        client.get("/api/v1/documents/doc-old", headers=_headers(registry, ["read"]))
        _post(client, headers=_headers(registry, ["read"]), **FLAT)
        assert registry.knowledge.document_store.get("doc-old") == before
        assert before["metadata"]["lifecycle"]["superseded_by"] == "doc-secret"

    def test_the_event_payload_names_no_stored_lifecycle(self, client, registry):
        """``PACK_ASSEMBLED`` records no item metadata: nothing to hide there."""
        _post(client, headers=_headers(registry, ["read"]), **FLAT)
        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )
        assert "superseded_by" not in str(event.payload)


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


_NOISE = {"content_tags": {"signal_quality": "noise"}}


class TestEveryRejectedCandidateIsConcealed:
    """A restricted document an earlier gate rejected is not named either (#1022)."""

    @pytest.fixture(
        params=[
            pytest.param(_NOISE, id="noise"),
            pytest.param(_lifecycle(ARCHIVED_STATE), id="archived"),
            pytest.param(_lifecycle("superseded", "doc-open"), id="superseded"),
        ]
    )
    def hidden_id(self, request, registry):
        registry.knowledge.document_store.put(
            "doc-rejected",
            SECRET,
            {"title": "Rejected", **label("restricted"), **request.param},
        )
        return "doc-rejected"

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    @pytest.mark.parametrize("scopes", [["read"], ["read", "confidential"]])
    def test_a_read_key_never_sees_the_id(
        self, client, registry, hidden_id, request_body, scopes
    ):
        resp = _post(client, headers=_headers(registry, scopes), **request_body)
        assert hidden_id not in resp.text

    @pytest.mark.parametrize(
        "request_body", [FLAT, SECTIONED], ids=["flat", "sectioned"]
    )
    def test_a_restricted_key_still_sees_the_row(
        self, client, registry, hidden_id, request_body
    ):
        headers = _headers(registry, ["read", "restricted"])
        resp = _post(client, headers=headers, **request_body)
        assert hidden_id in resp.text


class TestEvidenceLinksAreNotAnExistenceOracle:
    """A link to a labelled document answers as a missing one would (#194)."""

    def _ingest(self, client, registry, *evidence_ids):
        trace = {
            "source": "agent",
            "intent": "cite evidence",
            "steps": [],
            "context": {},
        }
        trace["evidence_used"] = [{"evidence_id": e} for e in evidence_ids]
        resp = client.post(
            "/api/v1/traces", json=trace, headers=_headers(registry, ["admin"])
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["trace_id"]

    def _links(self, client, headers, trace_id):
        resp = client.get(f"/api/v1/traces/{trace_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["evidence_links"]

    def test_a_restricted_document_reads_as_a_missing_one(self, client, registry):
        read = _headers(registry, ["read"])
        secret = self._ingest(client, registry, "doc-secret")
        missing = self._ingest(client, registry, "doc-missing")
        assert self._links(client, read, secret) == self._links(client, read, missing)
        assert self._links(client, read, secret) == {}

    def test_an_unlabelled_document_still_links(self, client, registry):
        trace_id = self._ingest(client, registry, "doc-open", "doc-secret")
        links = self._links(client, _headers(registry, ["read"]), trace_id)
        assert links == {"doc-open": {"document_id": "doc-open"}}

    def test_a_restricted_key_sees_the_link(self, client, registry):
        trace_id = self._ingest(client, registry, "doc-secret")
        headers = _headers(registry, ["read", "restricted"])
        assert self._links(client, headers, trace_id) == {
            "doc-secret": {"document_id": "doc-secret"}
        }


class TestExploreDocumentList:
    """The list and ``?q=`` show nothing of a document above the key (#194)."""

    def _get(self, client, headers, **params):
        resp = client.get("/api/v1/documents", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp

    def test_a_read_key_sees_no_trace_of_the_restricted_row(self, client, registry):
        resp = self._get(client, _headers(registry, ["read"]))
        assert "doc-secret" not in resp.text
        assert "zebra" not in resp.text
        assert [d["doc_id"] for d in resp.json()["documents"]] == ["doc-open"]
        assert resp.json()["total"] == 1

    def test_a_search_shows_a_read_key_nothing_of_it(self, client, registry):
        resp = self._get(client, _headers(registry, ["read"]), q="zebra")
        assert resp.json()["documents"] == []
        assert resp.json()["total"] == 0
        assert "doc-secret" not in resp.text

    def test_a_search_rank_is_withheld_from_a_key_that_cannot_read_everything(
        self, client, registry
    ):
        # The full-text score is computed over every document, so a hidden
        # one holding the term moves a visible row's rank.
        registry.knowledge.document_store.put("doc-visible", "zebra field notes", None)
        read = self._get(client, _headers(registry, ["read"]), q="zebra")
        assert [d["doc_id"] for d in read.json()["documents"]] == ["doc-visible"]
        assert "rank" not in read.json()["documents"][0]
        admin = self._get(client, _headers(registry, ["admin"]), q="zebra")
        assert all("rank" in d for d in admin.json()["documents"])

    def test_admin_sees_the_row_in_the_list_and_the_search(self, client, registry):
        admin = _headers(registry, ["admin"])
        listed = self._get(client, admin)
        assert listed.json()["total"] == 2
        assert "doc-secret" in listed.text
        found = self._get(client, admin, q="zebra")
        assert [d["doc_id"] for d in found.json()["documents"]] == ["doc-secret"]

    def test_a_page_is_not_short_because_a_row_was_hidden(self, client, registry):
        store = registry.knowledge.document_store
        for n in range(3):
            store.put(f"doc-more-{n}", OPEN, {"title": f"More {n}"})
        resp = self._get(client, _headers(registry, ["read"]), limit=2)
        assert resp.json()["count"] == 2
        # One readable row past the page, so there is a next page; the exact
        # count is not read out.
        assert (resp.json()["total"], resp.json()["total_exact"]) == (3, False)
        second = self._get(client, _headers(registry, ["read"]), limit=2, offset=2)
        assert second.json()["count"] == 2
        assert (second.json()["total"], second.json()["total_exact"]) == (4, True)
        ids = [d["doc_id"] for d in resp.json()["documents"]]
        ids += [d["doc_id"] for d in second.json()["documents"]]
        assert len(set(ids)) == 4
        assert "doc-secret" not in ids

    def test_a_chunk_of_a_restricted_parent_is_hidden(self, client, registry):
        registry.knowledge.document_store.put(
            chunk_doc_id("doc-secret", 0), SECRET, {"title": "chunk"}
        )
        resp = self._get(client, _headers(registry, ["read"]), include_chunks="true")
        assert "doc-secret" not in resp.text

    def _small_window(self, monkeypatch, *, window=10, batch=4):
        monkeypatch.setattr(explore_mod, "_SEARCH_SCAN_LIMIT", window)
        monkeypatch.setattr(explore_mod, "_SCAN_BATCH", batch)

    def test_a_partial_scan_reports_one_page_more_not_the_store_count(
        self, client, registry, monkeypatch
    ):
        """Stopping early must not turn the store's row count into a total."""
        self._small_window(monkeypatch)
        store = registry.knowledge.document_store
        for n in range(3):
            store.put(f"doc-secret-{n}", SECRET, {"title": "S", **label("restricted")})
        for n in range(20):
            store.put(f"doc-open-{n}", OPEN, {"title": f"O{n}"})
        body = self._get(client, _headers(registry, ["read"]), limit=5).json()
        assert (body["total"], body["total_exact"], body["count"]) == (6, False, 5)

    @pytest.mark.parametrize("secret_first", [True, False])
    def test_the_total_does_not_move_with_where_a_hidden_row_sits(
        self, client, registry, monkeypatch, secret_first
    ):
        """Same readable rows, one hidden row moved across a batch boundary."""
        self._small_window(monkeypatch, batch=4)
        store = registry.knowledge.document_store
        store.delete("doc-open")
        store.delete("doc-secret")
        if not secret_first:
            store.put("doc-secret-x", SECRET, {"title": "S", **label("restricted")})
        for n in range(12):
            store.put(f"doc-open-{n:02d}", OPEN, {"title": f"O{n}"})
            time.sleep(0.002)
        if secret_first:
            store.put("doc-secret-x", SECRET, {"title": "S", **label("restricted")})
        body = self._get(client, _headers(registry, ["read"]), limit=1).json()
        assert (body["total"], body["total_exact"], body["count"]) == (2, False, 1)

    def test_a_store_that_is_exactly_one_batch_reports_exact(
        self, client, registry, monkeypatch
    ):
        self._small_window(monkeypatch)
        store = registry.knowledge.document_store
        store.put("doc-open-a", OPEN, {"title": "A"})
        store.put("doc-open-b", OPEN, {"title": "B"})
        body = self._get(client, _headers(registry, ["read"]), limit=5).json()
        assert (body["total"], body["total_exact"]) == (3, True)

    def test_a_read_key_pages_past_the_old_window_like_admin(
        self, client, registry, monkeypatch
    ):
        """An unlabelled store reads the same to a read key as to admin."""
        self._small_window(monkeypatch)
        store = registry.knowledge.document_store
        store.delete("doc-secret")
        for n in range(20):
            store.put(f"doc-open-{n}", OPEN, {"title": f"O{n}"})
        paged = {"limit": 5, "offset": 15}
        read = self._get(client, _headers(registry, ["read"]), **paged).json()
        admin = self._get(client, _headers(registry, ["admin"]), **paged).json()
        assert read["count"] == admin["count"] == 5
        assert [d["doc_id"] for d in read["documents"]] == [
            d["doc_id"] for d in admin["documents"]
        ]

    def test_next_stays_enabled_across_a_batch_boundary_like_admin(
        self, client, registry
    ):
        """The explorer disables Next when ``offset + count >= total``.

        Real batch size: 600 unlabelled rows, a page that ends on the first
        batch boundary. A read key must be offered the next page exactly as
        admin is, and the last page must end it for both.
        """
        store = registry.knowledge.document_store
        store.delete("doc-open")
        store.delete("doc-secret")
        for n in range(600):
            store.put(f"doc-open-{n:04d}", OPEN, {"title": f"O{n}"})

        def next_disabled(body):
            return body["offset"] + body["count"] >= body["total"]

        read, admin = _headers(registry, ["read"]), _headers(registry, ["admin"])
        for offset, disabled in ((450, False), (500, False), (550, True)):
            paged = {"limit": 50, "offset": offset}
            r = self._get(client, read, **paged).json()
            a = self._get(client, admin, **paged).json()
            assert r["count"] == a["count"] == 50
            assert next_disabled(r) is next_disabled(a) is disabled, offset
        assert (r["total"], r["total_exact"]) == (600, True)

    def test_a_key_below_restricted_cannot_ask_for_an_offset_past_the_ceiling(
        self, client, registry, monkeypatch
    ):
        monkeypatch.setattr(explore_mod, "_MAX_FILTERED_OFFSET", 5)
        for scopes in (["read"], ["read", "confidential"]):
            ok = client.get(
                "/api/v1/documents",
                params={"offset": 5},
                headers=_headers(registry, scopes),
            )
            assert ok.status_code == 200, scopes
            refused = client.get(
                "/api/v1/documents",
                params={"offset": 6},
                headers=_headers(registry, scopes),
            )
            assert refused.status_code == 400, scopes
            assert "restricted scope" in refused.json()["detail"]
            assert "doc-secret" not in refused.text
        # A restricted or admin key reads the store directly: no ceiling.
        for scopes in (["admin"], ["read", "restricted"]):
            deep = client.get(
                "/api/v1/documents",
                params={"offset": 6},
                headers=_headers(registry, scopes),
            )
            assert deep.status_code == 200, scopes

    def test_the_ceiling_is_documented_in_the_openapi_spec(self, client):
        spec = client.get("/openapi.json").json()
        assert "400" in spec["paths"]["/api/v1/documents"]["get"]["responses"]

    def test_a_search_capture_filled_with_hidden_rows_is_widened(
        self, client, registry, monkeypatch
    ):
        """Exactness depends on rows the key can read, not on hidden ones."""
        self._small_window(monkeypatch, window=4)
        store = registry.knowledge.document_store
        for n in range(4):
            store.put(f"doc-secret-{n}", SECRET, {"title": "S", **label("restricted")})
        for n in range(2):
            store.put(f"doc-open-{n}", SECRET, {"title": f"O{n}"})
        resp = self._get(client, _headers(registry, ["read"]), q="zebra")
        assert resp.json()["total"] == 2
        assert resp.json()["total_exact"] is True
        assert "doc-secret" not in resp.text

    @pytest.mark.parametrize("hidden", [0, 1, 3])
    def test_a_search_total_is_clipped_and_ignores_hidden_matches(
        self, client, registry, monkeypatch, hidden
    ):
        """Past the window the answer is the same however many matches are hidden."""
        self._small_window(monkeypatch, window=4)
        store = registry.knowledge.document_store
        store.delete("doc-open")
        store.delete("doc-secret")
        for n in range(hidden):
            store.put(f"doc-secret-{n}", SECRET, {"title": "S", **label("restricted")})
        for n in range(9):
            store.put(f"doc-open-{n}", SECRET, {"title": f"O{n}"})
        read = self._get(client, _headers(registry, ["read"]), q="zebra").json()
        assert (read["total"], read["total_exact"]) == (4, False)
        assert not any(d["doc_id"].startswith("doc-secret") for d in read["documents"])
        tail = self._get(
            client, _headers(registry, ["read"]), q="zebra", limit=2, offset=3
        ).json()
        assert tail["count"] == 1


class _Row(dict):  # type: ignore[type-arg]
    """A document row that can be weakly referenced."""


class _ListStub:
    """Serves fresh rows on every call and keeps none of them.

    ``most_alive`` is the most rows of its own still referenced when it was
    asked for the next batch, so it measures what the caller retains while it
    scans.
    """

    def __init__(self, n: int, hidden: frozenset[int] = frozenset()) -> None:
        self.n = n
        self.hidden = hidden
        self.rows: list[weakref.ref[_Row]] = []
        self.most_alive = 0

    def list_documents(self, *, limit, offset, include_chunks):
        gc.collect()
        alive = sum(r() is not None for r in self.rows)
        self.most_alive = max(self.most_alive, alive)
        out = []
        for i in range(offset, min(offset + limit, self.n)):
            row = _Row(doc_id=f"H{i}" if i in self.hidden else f"R{i}", metadata={})
            self.rows.append(weakref.ref(row))
            out.append(row)
        return out


class _Gate:
    clearance = "internal"

    def permits(self, doc_id, metadata):
        return not doc_id.startswith("H")


class TestListScanHelpers:
    def test_only_the_page_is_kept_while_the_scan_walks_past_it(self, monkeypatch):
        monkeypatch.setattr(explore_mod, "_SCAN_BATCH", 10)
        store = _ListStub(1000)
        page, total, exact = explore_mod._list_readable(
            store, _Gate(), offset=300, limit=5, include_chunks=False
        )
        gc.collect()
        assert [d["doc_id"] for d in page] == [f"R{i}" for i in range(300, 305)]
        assert (total, exact) == (306, False)
        assert sum(r() is not None for r in store.rows) == len(page)
        # 31 batches were read; while scanning, never more than the batch in
        # hand plus the page so far was retained.
        assert len(store.rows) == 310
        assert store.most_alive <= 10 + 5

    def test_the_scan_stops_at_the_first_readable_row_past_the_page(self, monkeypatch):
        monkeypatch.setattr(explore_mod, "_SCAN_BATCH", 10)
        store = _ListStub(1000)
        explore_mod._list_readable(
            store, _Gate(), offset=0, limit=5, include_chunks=False
        )
        assert len(store.rows) == 10

    @pytest.mark.parametrize(
        ("n_rows", "hidden", "offset", "limit", "expect"),
        [
            (500, (), 0, 500, (500, 500, True)),
            (500, (), 0, 1, (1, 2, False)),
            (1000, (), 0, 1000, (1000, 1000, True)),
            (1000, (), 0, 999, (999, 1000, False)),
            (1001, (), 0, 1000, (1000, 1001, False)),
            (0, (), 0, 1, (0, 0, True)),
            (10, (0, 1, 2), 0, 7, (7, 7, True)),
            (10, (0, 1, 2), 0, 6, (6, 7, False)),
            (10, (7, 8, 9), 2, 5, (5, 7, True)),
        ],
    )
    def test_the_total_follows_readable_rows_only(
        self, n_rows, hidden, offset, limit, expect
    ):
        store = _ListStub(n_rows, frozenset(hidden))
        page, total, exact = explore_mod._list_readable(
            store, _Gate(), offset=offset, limit=limit, include_chunks=False
        )
        assert (len(page), total, exact) == expect

    def test_a_hidden_row_anywhere_does_not_change_the_answer(self, monkeypatch):
        monkeypatch.setattr(explore_mod, "_SCAN_BATCH", 4)
        args = {"offset": 2, "limit": 3, "include_chunks": False}
        _, ref_total, ref_exact = explore_mod._list_readable(
            _ListStub(15), _Gate(), **args
        )
        for pos in (0, 3, 4, 9, 15):
            hidden_store = _ListStub(16, frozenset({pos}))
            _, total, exact = explore_mod._list_readable(hidden_store, _Gate(), **args)
            assert (total, exact) == (ref_total, ref_exact), pos


class _SearchStub:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search(self, q, *, limit, include_chunks):
        self.calls.append(limit)
        return [{"doc_id": r, "metadata": {}} for r in self.rows[:limit]]


class _Everything:
    clearance = "restricted"

    def permits(self, *_):
        return True


class TestSearchScanHelpers:
    def test_one_hidden_match_does_not_move_the_total(self):
        readable = [f"R{n}" for n in range(70000)]
        plain = _SearchStub(readable)
        with_hidden = _SearchStub(["H0", *readable])
        a, a_exact = explore_mod._search_readable(plain, _Gate(), "q", False)
        b, b_exact = explore_mod._search_readable(with_hidden, _Gate(), "q", False)
        assert (len(a), a_exact) == (len(b), b_exact) == (2000, False)
        assert [m["doc_id"] for m in a] == [m["doc_id"] for m in b]

    def test_a_capture_that_runs_out_past_the_window_is_still_clipped(self):
        # One hidden match pushes the first capture short of the window, so
        # the second capture runs out of matches holding 2500 readable rows;
        # unclipped, that count would answer only because a hidden row ranked.
        readable = [f"R{n}" for n in range(2500)]
        plain = _SearchStub(readable)
        with_hidden = _SearchStub(["H0", *readable])
        a, a_exact = explore_mod._search_readable(plain, _Gate(), "q", False)
        b, b_exact = explore_mod._search_readable(with_hidden, _Gate(), "q", False)
        assert len(with_hidden.calls) == 2
        assert (len(a), a_exact) == (len(b), b_exact) == (2000, False)
        assert [m["doc_id"] for m in a] == [m["doc_id"] for m in b]

    def test_exactness_at_the_window_edge_does_not_move_with_a_hidden_match(self):
        exactly = [f"R{n}" for n in range(2000)]
        plain = _SearchStub(exactly)
        with_hidden = _SearchStub(["H0", *exactly])
        a = explore_mod._search_readable(plain, _Gate(), "q", False)
        b = explore_mod._search_readable(with_hidden, _Gate(), "q", False)
        assert (len(a[0]), a[1]) == (len(b[0]), b[1]) == (2000, False)

    def test_below_the_window_the_readable_count_is_exact(self):
        store = _SearchStub(["H0", "R0", "H1", "R1", "R2"])
        found, exact = explore_mod._search_readable(store, _Gate(), "q", False)
        assert (len(found), exact) == (3, True)

    def test_a_key_that_reads_everything_takes_one_capture(self):
        store = _SearchStub([f"R{n}" for n in range(5000)])
        found, exact = explore_mod._search_readable(store, _Everything(), "q", False)
        assert (len(found), exact, store.calls) == (2000, False, [2000])
        store = _SearchStub([f"R{n}" for n in range(1999)])
        found, exact = explore_mod._search_readable(store, _Everything(), "q", False)
        assert (len(found), exact, store.calls) == (1999, True, [2000])

    def test_the_widening_stops_at_the_growth_cap(self):
        rows = [f"H{n}" for n in range(200000)]
        store = _SearchStub(rows)
        found, exact = explore_mod._search_readable(store, _Gate(), "q", False)
        assert (found, exact) == ([], False)
        assert store.calls[-1] == 2000 * explore_mod._SEARCH_GROWTH_CAP


class TestRestIngestSensitivity:
    """``POST /evidence`` labels with the rules the MCP save tools use (#194)."""

    def _post(self, client, headers, evidence_id, **extra):
        body = {
            "evidence_id": evidence_id,
            "evidence_type": "snippet",
            "content": OPEN,
            "source_origin": "test",
            **extra,
        }
        return client.post("/api/v1/evidence", json=body, headers=headers)

    def test_a_key_below_the_label_is_refused_and_nothing_is_written(
        self, client, registry
    ):
        headers = _headers(registry, ["read", "ingest"])
        resp = self._post(client, headers, "ev-new", sensitivity="restricted")
        assert resp.status_code == 403
        assert registry.knowledge.document_store.get("ev-new") is None

    def test_admin_may_label_and_the_row_carries_it(self, client, registry):
        resp = self._post(
            client, _headers(registry, ["admin"]), "ev-new", sensitivity="restricted"
        )
        assert resp.status_code == 200, resp.text
        stored = registry.knowledge.document_store.get("ev-new")
        assert stored["metadata"]["data_classification"]["sensitivity"] == "restricted"

    def test_a_key_may_label_up_to_its_own_clearance(self, client, registry):
        headers = _headers(registry, ["read", "ingest", "confidential"])
        assert (
            self._post(client, headers, "ev-a", sensitivity="confidential").status_code
            == 200
        )
        assert (
            self._post(client, headers, "ev-b", sensitivity="restricted").status_code
            == 403
        )

    def test_an_unknown_label_is_refused(self, client, registry):
        resp = self._post(
            client, _headers(registry, ["admin"]), "ev-new", sensitivity="secret"
        )
        assert resp.status_code == 422
        assert registry.knowledge.document_store.get("ev-new") is None

    def test_a_raw_label_in_the_body_is_not_a_way_to_write_one(self, client, registry):
        resp = self._post(
            client,
            _headers(registry, ["read", "ingest"]),
            "ev-new",
            metadata={"data_classification": {"sensitivity": "public"}},
        )
        assert resp.status_code == 422
        assert registry.knowledge.document_store.get("ev-new") is None

    def test_no_label_leaves_the_row_unlabelled(self, client, registry):
        resp = self._post(client, _headers(registry, ["read", "ingest"]), "ev-new")
        assert resp.status_code == 200, resp.text
        stored = registry.knowledge.document_store.get("ev-new")
        assert "data_classification" not in stored["metadata"]
