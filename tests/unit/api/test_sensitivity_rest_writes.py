"""REST write routes honour the key's clearance (#194, #1029).

``POST /documents`` takes a ``sensitivity`` under the rules ``POST /evidence``
and MCP ``save_memory`` apply; ``POST /vectors`` will not replace the vector row
of a document the key may not read; and every credentialed write route builds
its executor at the key's clearance rather than at a trusted ``restricted``.
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
import trellis_api.routes.curate as curate_mod
import trellis_api.routes.ingest as ingest_mod
import trellis_api.routes.observations as observations_mod
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.core.vector_metadata import vector_metadata_diverges
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mcp.supersession import supersede_document
from trellis.mutate import build_curate_executor
from trellis.retrieve.embed_ingest_hook import (
    VECTOR_METADATA_EXCERPT_CHARS,
    build_vector_row,
)
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
    SearchStrategy,
    SemanticSearch,
)
from trellis.schemas.classification import (
    CONTENT_TYPE_VALUES,
    DATA_CLASSIFICATION_KEY,
    LIFECYCLE_KEY,
)
from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_workers.trace_embed.render import DOC_ID_PREFIX, trace_summary_doc_id

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


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


MUTATE = ["mutate"]
MUTATE_CONFIDENTIAL = ["mutate", "confidential"]
MUTATE_RESTRICTED = ["mutate", "restricted"]


# ── POST /documents: the producer ───────────────────────────────────────


class TestDocumentsProducer:
    def _post(self, client, headers, doc_id="doc-new", **extra):
        return client.post(
            "/api/v1/documents",
            json={"doc_id": doc_id, "content": OPEN, **extra},
            headers=headers,
        )

    def _stored(self, registry, doc_id="doc-new"):
        return registry.knowledge.document_store.get(doc_id)

    def test_a_key_below_the_label_is_refused_and_nothing_is_written(
        self, client, registry
    ):
        resp = self._post(client, _headers(registry, MUTATE), sensitivity="restricted")
        assert resp.status_code == 403, resp.text
        assert self._stored(registry) is None

    def test_a_key_may_label_up_to_its_own_clearance(self, client, registry):
        headers = _headers(registry, MUTATE_CONFIDENTIAL)
        ok = self._post(client, headers, "doc-a", sensitivity="confidential")
        assert ok.status_code == 200, ok.text
        too_high = self._post(client, headers, "doc-b", sensitivity="restricted")
        assert too_high.status_code == 403
        assert self._stored(registry, "doc-b") is None
        stored = self._stored(registry, "doc-a")
        assert stored["metadata"]["data_classification"]["sensitivity"] == (
            "confidential"
        )

    def test_admin_may_label_and_the_row_carries_it(self, client, registry):
        resp = self._post(
            client, _headers(registry, ["admin"]), sensitivity="restricted"
        )
        assert resp.status_code == 200, resp.text
        meta = self._stored(registry)["metadata"]
        assert meta["data_classification"]["sensitivity"] == "restricted"

    def test_the_label_hides_the_document_from_a_read_key(self, client, registry):
        self._post(client, _headers(registry, ["admin"]), sensitivity="restricted")
        resp = client.get(
            "/api/v1/documents/doc-new", headers=_headers(registry, ["read"])
        )
        assert resp.status_code == 404

    def test_an_unknown_label_is_refused(self, client, registry):
        resp = self._post(client, _headers(registry, ["admin"]), sensitivity="secret")
        assert resp.status_code == 422, resp.text
        assert self._stored(registry) is None

    def test_a_label_that_is_not_a_string_is_refused(self, client, registry):
        resp = self._post(client, _headers(registry, ["admin"]), sensitivity=["x"])
        assert resp.status_code == 422, resp.text
        assert self._stored(registry) is None

    @pytest.mark.parametrize("scopes", [MUTATE, ["admin"]], ids=["mutate", "admin"])
    def test_a_raw_label_in_metadata_is_not_a_way_to_write_one(
        self, client, registry, scopes
    ):
        resp = self._post(
            client,
            _headers(registry, scopes),
            metadata={"data_classification": {"sensitivity": "public"}},
        )
        assert resp.status_code == 422, resp.text
        assert self._stored(registry) is None

    def test_a_raw_label_is_refused_beside_a_sensitivity_too(self, client, registry):
        resp = self._post(
            client,
            _headers(registry, ["admin"]),
            sensitivity="internal",
            metadata={"data_classification": {"sensitivity": "public"}},
        )
        assert resp.status_code == 422, resp.text
        assert self._stored(registry) is None

    def test_metadata_that_is_not_an_object_is_refused(self, client, registry):
        resp = self._post(client, _headers(registry, MUTATE), metadata=["x"])
        assert resp.status_code == 422, resp.text
        assert self._stored(registry) is None

    def test_no_label_leaves_the_row_unlabelled(self, client, registry):
        resp = self._post(client, _headers(registry, MUTATE), metadata={"k": "v"})
        assert resp.status_code == 200, resp.text
        meta = self._stored(registry)["metadata"]
        assert "data_classification" not in meta
        assert meta["k"] == "v"

    def test_a_null_metadata_is_still_accepted(self, client, registry):
        resp = self._post(client, _headers(registry, MUTATE), metadata=None)
        assert resp.status_code == 200, resp.text

    def test_the_written_label_is_what_the_overwrite_check_compares(
        self, client, registry
    ):
        """A cleared key re-putting a restricted row at a lower label is refused."""
        resp = self._post(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            "doc-secret",
            sensitivity="confidential",
        )
        assert resp.status_code == 409, resp.text
        assert "lower" in resp.text
        assert registry.knowledge.document_store.get("doc-secret")["content"] == SECRET

    def test_a_cleared_key_may_replace_at_the_same_label(self, client, registry):
        resp = self._post(
            client,
            _headers(registry, MUTATE_RESTRICTED),
            "doc-secret",
            sensitivity="restricted",
        )
        assert resp.status_code == 200, resp.text
        assert registry.knowledge.document_store.get("doc-secret")["content"] == OPEN


class TestDocumentsRefuseItemPointerKeys:
    """``lifecycle`` and ``evidence_ref`` act on other items, by id.

    The pack builder withholds by id: a document whose ``lifecycle`` says it is
    superseded by another item's id, or whose ``evidence_ref`` makes it the
    successor another item names, withholds that item. Only governed writers set
    either, so the route refuses both before anything is written.
    """

    SUPERSEDED: ClassVar[dict[str, str]] = {
        "state": "superseded",
        "superseded_by": "doc-open",
    }

    def _post(self, client, headers, metadata, doc_id="doc-new"):
        return client.post(
            "/api/v1/documents",
            json={"doc_id": doc_id, "content": OPEN, "metadata": metadata},
            headers=headers,
        )

    @pytest.mark.parametrize("scopes", [MUTATE, ["admin"]], ids=["mutate", "admin"])
    @pytest.mark.parametrize(
        ("metadata", "key"),
        [
            ({LIFECYCLE_KEY: SUPERSEDED}, "lifecycle"),
            ({LIFECYCLE_KEY: {"state": "archived"}}, "lifecycle"),
            ({LIFECYCLE_KEY: None}, "lifecycle"),
            ({"evidence_ref": "doc-open"}, "evidence_ref"),
            ({"evidence_ref": None}, "evidence_ref"),
            ({"title": "t", "evidence_ref": "doc-x"}, "evidence_ref"),
        ],
    )
    def test_the_key_is_refused_and_nothing_is_written(
        self, client, registry, scopes, metadata, key
    ):
        resp = self._post(client, _headers(registry, scopes), metadata)
        assert resp.status_code == 422, resp.text
        assert f"metadata.{key}" in resp.json()["detail"]
        assert "Nothing was written" in resp.json()["detail"]
        assert registry.knowledge.document_store.get("doc-new") is None

    def test_the_lifecycle_refusal_names_the_governed_paths(self, client, registry):
        detail = self._post(
            client, _headers(registry, MUTATE), {LIFECYCLE_KEY: self.SUPERSEDED}
        ).json()["detail"]
        assert "save_memory(supersedes=" in detail
        assert "retention.restore" in detail

    def test_a_refused_write_leaves_an_existing_document_alone(self, client, registry):
        resp = self._post(
            client,
            _headers(registry, MUTATE),
            {LIFECYCLE_KEY: self.SUPERSEDED},
            doc_id="doc-open",
        )
        assert resp.status_code == 422, resp.text
        stored = registry.knowledge.document_store.get("doc-open")
        assert stored["content"] == OPEN
        assert stored["metadata"] == {"title": "Open"}

    def test_a_raw_data_classification_is_still_refused(self, client, registry):
        resp = self._post(
            client,
            _headers(registry, MUTATE),
            {DATA_CLASSIFICATION_KEY: {"sensitivity": "public"}},
        )
        assert resp.status_code == 422, resp.text
        assert DATA_CLASSIFICATION_KEY in resp.json()["detail"]
        assert registry.knowledge.document_store.get("doc-new") is None

    def test_an_ordinary_metadata_document_is_still_accepted(self, client, registry):
        metadata = {"title": "Plain", "source_system": "notes", "tags": ["a"]}
        resp = self._post(client, _headers(registry, MUTATE), metadata)
        assert resp.status_code == 200, resp.text
        stored = registry.knowledge.document_store.get("doc-new")
        assert stored["content"] == OPEN
        assert stored["metadata"]["title"] == "Plain"
        assert LIFECYCLE_KEY not in stored["metadata"]
        assert "evidence_ref" not in stored["metadata"]

    def test_save_memory_supersedes_still_stamps_a_lifecycle(self, registry):
        """The governed writer is untouched: the handler takes a ``lifecycle``."""
        registry.knowledge.document_store.put("doc-new", OPEN, {"title": "New"})
        error = supersede_document(
            registry,
            doc_id="doc-open",
            successor="doc-new",
            requested_by="mcp:save_memory",
            clearance="restricted",
        )
        assert error is None
        stamped = registry.knowledge.document_store.get("doc-open")["metadata"]
        assert stamped[LIFECYCLE_KEY]["superseded_by"] == "doc-new"


# ── POST /vectors: integrity of a labelled document's vector row ─────────


def _dims(registry) -> int:
    return registry.knowledge.vector_store.dimensions or 8


def _vector(registry, seed=0.5):
    return [seed] * _dims(registry)


def _post_vectors(client, registry, headers, *rows):
    return client.post(
        "/api/v1/vectors",
        json={
            "vectors": [
                {"item_id": item_id, "vector": _vector(registry), **extra}
                for item_id, extra in rows
            ]
        },
        headers=headers,
    )


def _vector_row(registry, item_id):
    return registry.knowledge.vector_store.get(item_id)


class TestVectorsWrite:
    @pytest.fixture
    def seeded(self, registry):
        store = registry.knowledge.vector_store
        store.upsert(
            item_id="doc-secret",
            vector=_vector(registry, 0.1),
            metadata={"title": "Secret", **label("restricted")},
        )
        return store

    @pytest.mark.parametrize("scopes", [["ingest"], ["ingest", "confidential"]])
    def test_a_key_below_the_label_cannot_replace_the_vector_row(
        self, client, registry, seeded, scopes
    ):
        before = _vector_row(registry, "doc-secret")
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, scopes),
            ("doc-secret", {"metadata": {"title": "pwned"}}),
        )
        assert resp.status_code == 409, resp.text
        assert "zebra ledger" not in resp.text
        assert "restricted" not in resp.text, "the refusal must not name the label"
        assert _vector_row(registry, "doc-secret") == before

    def test_a_refused_row_stores_nothing_of_the_batch(self, client, registry, seeded):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("doc-open", {"metadata": {}}),
            ("doc-secret", {"metadata": {}}),
        )
        assert resp.status_code == 409, resp.text
        assert _vector_row(registry, "doc-open") is None

    @pytest.mark.parametrize(
        "scopes", [["ingest", "restricted"], ["admin"]], ids=["restricted", "admin"]
    )
    def test_a_cleared_key_may_replace_it(self, client, registry, seeded, scopes):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, scopes),
            ("doc-secret", {"metadata": {"title": "refreshed"}}),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 1, "errors": 0}
        metadata = _vector_row(registry, "doc-secret")["metadata"]
        assert metadata["title"] == "refreshed"
        assert (
            metadata[DATA_CLASSIFICATION_KEY]
            == label("restricted")[DATA_CLASSIFICATION_KEY]
        ), "a rewrite must not strip the document's label from its row"

    def test_a_chunk_of_a_restricted_parent_is_refused(self, client, registry, seeded):
        chunk_id = chunk_doc_id("doc-secret", 0)
        registry.knowledge.document_store.put(chunk_id, SECRET, {})
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), (chunk_id, {})
        )
        assert resp.status_code == 409, resp.text
        assert _vector_row(registry, chunk_id) is None

    def test_a_chunk_id_with_no_row_of_its_own_is_judged_by_its_parent(
        self, client, registry, seeded
    ):
        chunk_id = chunk_doc_id("doc-secret", 7)
        assert registry.knowledge.document_store.get(chunk_id) is None
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), (chunk_id, {})
        )
        assert resp.status_code == 409, resp.text
        assert _vector_row(registry, chunk_id) is None

    def test_a_chunk_of_an_unlabelled_parent_is_written(self, client, registry):
        chunk_id = chunk_doc_id("doc-open", 0)
        registry.knowledge.document_store.put(chunk_id, OPEN, {})
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), (chunk_id, {})
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, chunk_id) is not None

    def test_an_unlabelled_document_and_an_id_naming_no_document_are_written(
        self, client, registry
    ):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("doc-open", {"metadata": {"title": "t"}}),
            ("entity:not-a-document", {}),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}

    @pytest.mark.parametrize("scopes", [["ingest"], ["admin"]], ids=["ingest", "admin"])
    def test_a_raw_label_in_vector_metadata_is_refused(self, client, registry, scopes):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, scopes),
            ("doc-open", {"metadata": label("public")}),
        )
        assert resp.status_code == 422, resp.text
        assert _vector_row(registry, "doc-open") is None

    def test_an_id_that_is_not_a_string_cannot_reach_a_labelled_row(
        self, client, registry
    ):
        """The store would coerce ``123`` to the text id ``'123'``; the gate cannot
        judge an id it cannot read as text, so the batch is refused."""
        registry.knowledge.document_store.put("123", SECRET, label("restricted"))
        registry.knowledge.vector_store.upsert(
            item_id="123", vector=_vector(registry, 0.1), metadata=label("restricted")
        )
        before = _vector_row(registry, "123")
        resp = client.post(
            "/api/v1/vectors",
            json={"vectors": [{"item_id": 123, "vector": _vector(registry)}]},
            headers=_headers(registry, ["ingest"]),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "vectors[0].item_id must be a string"
        assert _vector_row(registry, "123") == before

    def test_auth_off_is_trusted_local_and_writes_any_row(
        self, client, registry, seeded, monkeypatch
    ):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = _post_vectors(
            client, registry, {}, ("doc-secret", {"metadata": {"title": "t"}})
        )
        assert resp.status_code == 200, resp.text
        metadata = _vector_row(registry, "doc-secret")["metadata"]
        assert metadata["title"] == "t"
        assert (
            metadata[DATA_CLASSIFICATION_KEY]
            == label("restricted")[DATA_CLASSIFICATION_KEY]
        ), "auth off is not a way to strip the label from a row"

    def test_a_trusted_caller_pays_one_read_for_the_mirror_and_none_for_the_gate(
        self, client, registry, monkeypatch
    ):
        """A ``restricted`` caller cannot trip the gate, so the gate reads nothing;
        the one read is the mirror's, which every caller pays."""
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        resp = _post_vectors(
            client, registry, _headers(registry, ["admin"]), ("doc-open", {})
        )
        assert resp.status_code == 200, resp.text
        assert reads == ["doc-open"]


def _semantic_pack(registry, clearance):
    """A pack built from the vector store alone, as the semantic axis builds it."""
    builder = PackBuilder(
        strategies=[
            SemanticSearch(
                registry.knowledge.vector_store,
                embedding_fn=lambda _text: _vector(registry),
            )
        ],
        document_store=registry.knowledge.document_store,
    )
    return builder.build("failover runbook", clearance=clearance)


def _served_ids(pack):
    return [item.item_id for item in pack.items]


ARCHIVED = {"state": "archived", "superseded_by": None}


class TestVectorsRewriteKeepsTheDocumentsMirroredMetadata:
    """The row mirrors its document, so a rewrite may not strip what the
    semantic axis judges the row by (decision-ledger T-8: a re-put may not lower
    the stored label)."""

    @pytest.mark.parametrize(
        "scopes", [["ingest", "restricted"], ["admin"]], ids=["restricted", "admin"]
    )
    def test_a_rewrite_of_a_restricted_document_stays_withheld_at_internal(
        self, client, registry, scopes
    ):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, scopes),
            ("doc-secret", {"metadata": {"title": "t", "content": SECRET[:200]}}),
        )
        assert resp.status_code == 200, resp.text
        row = _vector_row(registry, "doc-secret")
        assert (
            row["metadata"][DATA_CLASSIFICATION_KEY]
            == (label("restricted")[DATA_CLASSIFICATION_KEY])
        )
        assert "doc-secret" not in _served_ids(_semantic_pack(registry, "internal"))
        assert "doc-secret" in _served_ids(_semantic_pack(registry, "restricted"))

    def test_auth_off_rewrite_of_a_restricted_document_stays_withheld(
        self, client, registry, monkeypatch
    ):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = _post_vectors(
            client,
            registry,
            {},
            ("doc-secret", {"metadata": {"content": SECRET[:200]}}),
        )
        assert resp.status_code == 200, resp.text
        assert "doc-secret" not in _served_ids(_semantic_pack(registry, "internal"))

    def test_a_rewrite_of_an_archived_document_stays_archived(self, client, registry):
        registry.knowledge.document_store.put(
            "doc-old", OPEN, {"title": "Old", LIFECYCLE_KEY: ARCHIVED}
        )
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("doc-old", {"metadata": {"content": OPEN[:200]}}),
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, "doc-old")["metadata"][LIFECYCLE_KEY] == ARCHIVED
        assert "doc-old" not in _served_ids(_semantic_pack(registry, "internal"))

    def test_the_row_takes_every_mirrored_key_from_the_document(self, client, registry):
        """Present on the document: copied over the caller's. Absent: removed."""
        tags = {"domain": "ops", "signal_quality": "standard"}
        registry.knowledge.document_store.put(
            "doc-tagged", OPEN, {"content_tags": tags, "auto_importance": 0.2}
        )
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            (
                "doc-tagged",
                {
                    "metadata": {
                        # Stamped, or the pre-pass refuses 0.99 outright; both
                        # are replaced by the document's, which is the point.
                        "content_tags": {
                            "domain": "forged",
                            "importance_scored_at": "2026-01-01T00:00:00+00:00",
                        },
                        "auto_importance": 0.99,
                        LIFECYCLE_KEY: ARCHIVED,
                        "title": "kept",
                    }
                },
            ),
        )
        assert resp.status_code == 200, resp.text
        stored = registry.knowledge.document_store.get("doc-tagged")["metadata"]
        row = _vector_row(registry, "doc-tagged")["metadata"]
        assert row["content_tags"] == tags
        assert row["auto_importance"] == 0.2
        assert LIFECYCLE_KEY not in row
        assert row["title"] == "kept", "only the mirrored keys are the document's"
        assert not vector_metadata_diverges(stored, row)

    def test_a_chunk_row_mirrors_its_own_document_and_is_judged_by_its_parent(
        self, client, registry
    ):
        """A chunk is mirrored as the chunk embed path mirrors it: from its own
        stored document. Its parent's label is not copied onto it; serving reads
        the parent through the chunk id, so the row is still withheld."""
        chunk_id = chunk_doc_id("doc-secret", 0)
        registry.knowledge.document_store.put(
            chunk_id, SECRET, {"content_tags": {"domain": "ops"}}
        )
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest", "restricted"]),
            (chunk_id, {"metadata": {"content": SECRET[:200]}}),
        )
        assert resp.status_code == 200, resp.text
        row = _vector_row(registry, chunk_id)["metadata"]
        assert row["content_tags"] == {"domain": "ops"}
        assert DATA_CLASSIFICATION_KEY not in row
        assert chunk_id not in _served_ids(_semantic_pack(registry, "internal"))

    def test_an_id_naming_no_document_is_written_as_supplied(self, client, registry):
        supplied = {"title": "t", "content_tags": {"domain": "ops"}}
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("entity:not-a-document", {"metadata": supplied}),
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, "entity:not-a-document")["metadata"] == supplied

    def test_a_document_that_cannot_be_read_is_an_error_not_a_stripped_row(
        self, client, registry, monkeypatch
    ):
        """A failed read must not fall through to writing the row unmirrored."""

        def boom(_doc_id):
            msg = "store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.document_store, "get", boom)
        resp = _post_vectors(
            client, registry, _headers(registry, ["admin"]), ("doc-secret", {})
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 0, "errors": 1}
        assert _vector_row(registry, "doc-secret") is None


LONG = "Drain the write queue first, then promote the replica. " * 30
FORGED = "forged excerpt: this is not what the document says"


def _served_item(pack, item_id):
    return next((i for i in pack.items if i.item_id == item_id), None)


IDENTITY_KEYS = [
    "node_role",
    "node_type",
    "agent_id",
    "document_form",
    "content_type",
    "excerpt",
]


class TestVectorsRowTakesItsDocumentsIdentity:
    """A row at a stored document carries that document's content and role.

    The semantic axis builds the pack item from the row: the excerpt from
    ``metadata["content"]``, the rest spread into the item, where
    ``node_role: structural`` withholds it. The upsert replaces the document's
    own row, so a forged copy would be its only semantic copy.
    """

    def _post(self, client, registry, item_id, metadata, scopes=("ingest",)):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, list(scopes)),
            (item_id, {"metadata": metadata}),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["upserted"] == 1
        return _vector_row(registry, item_id)["metadata"]

    def test_a_forged_content_does_not_reach_the_pack_excerpt(self, client, registry):
        row = self._post(client, registry, "doc-open", {"content": FORGED})
        assert row["content"] == OPEN
        item = _served_item(_semantic_pack(registry, "internal"), "doc-open")
        assert item is not None
        assert FORGED not in item.excerpt
        assert item.excerpt.startswith(OPEN[:60])

    def test_a_forged_structural_role_does_not_withhold_the_document(
        self, client, registry
    ):
        row = self._post(client, registry, "doc-open", {"node_role": "structural"})
        assert "node_role" not in row
        assert "doc-open" in _served_ids(_semantic_pack(registry, "internal"))

    def test_the_content_is_cut_as_the_embed_path_cuts_it(self, client, registry):
        registry.knowledge.document_store.put("doc-long", LONG, {"title": "Long"})
        row = self._post(client, registry, "doc-long", {"content": FORGED})
        assert row["content"] == truncate_excerpt(LONG, VECTOR_METADATA_EXCERPT_CHARS)
        assert len(row["content"]) <= VECTOR_METADATA_EXCERPT_CHARS < len(LONG)

    def test_a_forged_doc_id_is_replaced_by_the_id(self, client, registry):
        assert self._post(client, registry, "doc-open", {"doc_id": "doc-secret"})[
            "doc_id"
        ] == ("doc-open")

    @pytest.mark.parametrize("key", IDENTITY_KEYS)
    def test_a_key_the_document_lacks_is_removed_from_the_row(
        self, client, registry, key
    ):
        row = self._post(client, registry, "doc-open", {key: "forged", "title": "t"})
        assert key not in row
        assert row["title"] == "t", "only the identity keys are the document's"

    @pytest.mark.parametrize("key", IDENTITY_KEYS)
    def test_a_key_the_document_carries_is_copied_over_the_callers(
        self, client, registry, key
    ):
        registry.knowledge.document_store.put("doc-own", OPEN, {key: "own"})
        row = self._post(client, registry, "doc-own", {key: "forged"})
        assert row[key] == "own"

    def test_a_foreign_flat_content_type_cannot_set_a_form_on_the_row(
        self, client, registry
    ):
        """``document_form_of`` reads an out-of-vocabulary flat value as a form."""
        assert "conversation" not in CONTENT_TYPE_VALUES
        assert document_form_of({"content_type": "conversation"}) == "conversation"
        row = self._post(client, registry, "doc-open", {"content_type": "conversation"})
        assert document_form_of(row) is None
        assert document_form_of(_vector_row(registry, "doc-open")["metadata"]) is None

    def test_an_in_vocabulary_flat_content_type_mirrors_the_document(
        self, client, registry
    ):
        mine, forged = sorted(CONTENT_TYPE_VALUES)[:2]
        registry.knowledge.document_store.put("doc-own", OPEN, {"content_type": mine})
        row = self._post(client, registry, "doc-own", {"content_type": forged})
        assert row["content_type"] == mine
        assert document_form_of(row) is None

    def test_a_documents_own_legacy_form_still_reaches_its_row(self, client, registry):
        registry.knowledge.document_store.put(
            "doc-legacy", OPEN, {"content_type": "conversation"}
        )
        row = self._post(client, registry, "doc-legacy", {"content_type": "forged"})
        assert document_form_of(row) == "conversation"

    def test_a_chunk_takes_its_own_documents_identity(self, client, registry):
        chunk_id = chunk_doc_id("doc-open", 0)
        registry.knowledge.document_store.put(
            chunk_id, LONG, {"document_form": "note", "parent_doc_id": "doc-open"}
        )
        row = self._post(
            client,
            registry,
            chunk_id,
            {"content": FORGED, "document_form": "forged", "node_role": "structural"},
        )
        assert row["content"] == truncate_excerpt(LONG, VECTOR_METADATA_EXCERPT_CHARS)
        assert row["document_form"] == "note"
        assert "node_role" not in row
        assert row["doc_id"] == chunk_id

    def test_an_id_naming_no_document_is_stored_as_supplied(self, client, registry):
        supplied = {
            "content": FORGED,
            "excerpt": "kept",
            "doc_id": "elsewhere",
            "node_role": "structural",
            "node_type": "Thing",
            "agent_id": "someone",
            "document_form": "note",
            "content_type": "a-form",
            "title": "t",
        }
        row = self._post(client, registry, "entity:not-a-document", supplied)
        assert row == supplied

    @pytest.mark.parametrize(
        ("item_id", "doc_metadata"),
        [
            (
                "doc-honest",
                {
                    "title": "Honest",
                    "document_form": "note",
                    "content_tags": {"domain": "ops"},
                    "auto_importance": 0.2,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            ),
            (
                chunk_doc_id("doc-open", 3),
                {"parent_doc_id": "doc-open", "node_role": "semantic"},
            ),
            ("doc-bare", {}),
        ],
        ids=["document", "chunk", "no-metadata"],
    )
    def test_a_row_the_embed_path_built_is_stored_unchanged(
        self, client, registry, item_id, doc_metadata
    ):
        """Honest writers are untouched: byte for byte, key order included."""
        registry.knowledge.document_store.put(item_id, LONG, doc_metadata)
        honest = build_vector_row(
            item_id, LONG, doc_metadata, lambda _t: _vector(registry)
        )["metadata"]
        stored = self._post(client, registry, item_id, honest)
        assert json.dumps(stored) == json.dumps(honest)


class TestVectorsRowShape:
    """A malformed row is the caller's 422, not a stored poison or a 500."""

    def _post_raw(self, client, registry, rows, scopes=("ingest",)):
        return client.post(
            "/api/v1/vectors",
            json={"vectors": rows},
            headers=_headers(registry, list(scopes)),
        )

    @pytest.mark.parametrize("bad", ["x", ["a"], 5, True], ids=repr)
    def test_metadata_that_is_not_an_object_is_refused_and_nothing_is_written(
        self, client, registry, bad
    ):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        resp = self._post_raw(
            client,
            registry,
            [
                good,
                {"item_id": "doc-other", "vector": _vector(registry), "metadata": bad},
            ],
        )
        assert resp.status_code == 422, resp.text
        assert "metadata must be an object" in resp.json()["detail"]
        assert _vector_row(registry, "doc-open") is None
        assert _vector_row(registry, "doc-other") is None

    def test_null_metadata_is_stored_as_an_empty_object(self, client, registry):
        resp = self._post_raw(
            client,
            registry,
            [{"item_id": "entity:n", "vector": _vector(registry), "metadata": None}],
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, "entity:n")["metadata"] == {}

    @pytest.mark.parametrize("bad", ["x", 5, None, ["a"]], ids=repr)
    def test_a_row_that_is_not_an_object_is_refused_and_nothing_is_written(
        self, client, registry, bad
    ):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        resp = self._post_raw(client, registry, [good, bad])
        assert resp.status_code == 422, resp.text
        assert "must be an object" in resp.json()["detail"]
        assert _vector_row(registry, "doc-open") is None


class TestVectorsKeepAChunksParent:
    """``parent_doc_id`` is read before the id prefix, at serve time, so a row
    may not carry one the chunk's own document does not.

    Only a ``restricted`` key (or auth off) can write a restricted chunk's row
    at all: a lower key is judged on the chunk id and refused (409) before the
    row is built. The lower-key cases below use a chunk of an open parent."""

    FORGED: ClassVar = {
        "metadata": {"parent_doc_id": "doc-open", "content": SECRET[:200]}
    }

    def _parent(self, registry, item_id):
        return _vector_row(registry, item_id)["metadata"]["parent_doc_id"]

    def test_a_supplied_parent_is_replaced_by_the_chunk_documents_own(
        self, client, registry
    ):
        chunk_id = chunk_doc_id("doc-secret", 0)
        registry.knowledge.document_store.put(
            chunk_id, SECRET, {"parent_doc_id": "doc-secret"}
        )
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest", "restricted"]),
            (chunk_id, self.FORGED),
        )
        assert resp.status_code == 200, resp.text
        # The end effect, through the real partition: still judged by the
        # restricted parent, not by the open one the caller named.
        assert chunk_id not in _served_ids(_semantic_pack(registry, "internal"))
        assert chunk_id in _served_ids(_semantic_pack(registry, "restricted"))
        assert self._parent(registry, chunk_id) == "doc-secret"

    def test_the_chunk_documents_own_parent_wins_over_the_id_prefix(
        self, client, registry
    ):
        chunk_id = chunk_doc_id("doc-open", 0)
        registry.knowledge.document_store.put(
            chunk_id, OPEN, {"parent_doc_id": "doc-relocated"}
        )
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), (chunk_id, self.FORGED)
        )
        assert resp.status_code == 200, resp.text
        assert self._parent(registry, chunk_id) == "doc-relocated"

    @pytest.mark.parametrize("own", [None, "", 5, ["doc-secret"]], ids=repr)
    def test_a_chunk_document_without_a_usable_parent_gets_the_one_the_id_names(
        self, client, registry, own
    ):
        chunk_id = chunk_doc_id("doc-open", 3)
        metadata = {} if own is None else {"parent_doc_id": own}
        registry.knowledge.document_store.put(chunk_id, OPEN, metadata)
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), (chunk_id, self.FORGED)
        )
        assert resp.status_code == 200, resp.text
        assert self._parent(registry, chunk_id) == "doc-open"

    def test_a_chunk_with_no_stored_document_gets_the_one_the_id_names(
        self, client, registry
    ):
        chunk_id = chunk_doc_id("doc-secret", 7)
        assert registry.knowledge.document_store.get(chunk_id) is None
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest", "restricted"]),
            (chunk_id, self.FORGED),
        )
        assert resp.status_code == 200, resp.text
        assert self._parent(registry, chunk_id) == "doc-secret"
        assert chunk_id not in _served_ids(_semantic_pack(registry, "internal"))

    def test_a_lower_key_cannot_point_a_chunk_of_an_open_parent_at_a_restricted_one(
        self, client, registry
    ):
        chunk_id = chunk_doc_id("doc-open", 0)
        registry.knowledge.document_store.put(chunk_id, OPEN, {})
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            (chunk_id, {"metadata": {"parent_doc_id": "doc-secret"}}),
        )
        assert resp.status_code == 200, resp.text
        assert self._parent(registry, chunk_id) == "doc-open"

    @pytest.mark.parametrize(
        "item_id", ["entity:not-a-document", "doc-open", "notes#chunk-intro"]
    )
    def test_an_id_that_is_not_a_chunk_keeps_the_parent_it_supplied(
        self, client, registry, item_id
    ):
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            (item_id, {"metadata": {"parent_doc_id": "doc-x"}}),
        )
        assert resp.status_code == 200, resp.text
        assert self._parent(registry, item_id) == "doc-x"

    def test_a_non_chunk_id_with_no_parent_gets_none(self, client, registry):
        resp = _post_vectors(
            client, registry, _headers(registry, ["ingest"]), ("entity:plain", {})
        )
        assert resp.status_code == 200, resp.text
        assert "parent_doc_id" not in _vector_row(registry, "entity:plain")["metadata"]


class TestVectorsValueTypes:
    """A row ``SemanticSearch`` could not serve is the caller's 422."""

    STAMPED = "2026-01-01T00:00:00+00:00"

    def _post(self, client, registry, metadata, scopes=("ingest",)):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        bad = {"item_id": "entity:b", "vector": _vector(registry), "metadata": metadata}
        return client.post(
            "/api/v1/vectors",
            json={"vectors": [good, bad]},
            headers=_headers(registry, list(scopes)),
        )

    def _refused(self, client, registry, metadata, detail):
        resp = self._post(client, registry, metadata)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith(f"vectors[1].metadata.{detail}")
        assert registry.knowledge.vector_store.count() == 0

    @pytest.mark.parametrize(
        ("metadata", "detail"),
        [
            ({"content": 5}, "content must be a string"),
            ({"content": None}, "content must be a string"),
            ({"content": ["a"]}, "content must be a string"),
            ({"excerpt": 5}, "excerpt must be a string"),
            ({"excerpt": ["a"]}, "excerpt must be a string"),
            ({"excerpt": None}, "excerpt must be a string"),
            ({"content": "text", "excerpt": 5}, "excerpt must be a string"),
            ({"auto_importance": "high"}, "auto_importance must be a number"),
            ({"auto_importance": True}, "auto_importance must be a number"),
            ({"auto_importance": False}, "auto_importance must be a number"),
            ({"auto_importance": None}, "auto_importance must be a number"),
            ({"auto_importance": [0.5]}, "auto_importance must be a number"),
            ({"auto_importance": 10**400}, "auto_importance must be a finite number"),
        ],
        ids=repr,
    )
    def test_a_wrongly_typed_value_is_refused_and_nothing_is_written(
        self, client, registry, metadata, detail
    ):
        self._refused(client, registry, metadata, detail)

    @pytest.mark.parametrize(
        "metadata",
        [
            {"auto_importance": 0.99},
            {"auto_importance": 1},
            {"auto_importance": DEFAULT_IMPORTANCE_DECAY_THRESHOLD},
            {"auto_importance": 0.99, "content_tags": {"domain": "ops"}},
            {"auto_importance": 0.99, "content_tags": "ops"},
            {"auto_importance": 0.99, "content_tags": {"importance_scored_at": None}},
        ],
        ids=repr,
    )
    def test_an_unstamped_importance_at_the_threshold_is_refused(
        self, client, registry, metadata
    ):
        self._refused(
            client,
            registry,
            metadata,
            "auto_importance at or above "
            f"{DEFAULT_IMPORTANCE_DECAY_THRESHOLD} needs importance_scored_at",
        )

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_importance_is_refused_and_nothing_is_written(
        self, client, registry, literal
    ):
        """Starlette's JSON parser accepts the literals, so the route must not."""
        body = (
            '{"vectors": [{"item_id": "entity:b", "vector": '
            f'{_vector(registry)}, "metadata": {{"auto_importance": {literal}}}}}]}}'
        )
        resp = client.post(
            "/api/v1/vectors",
            content=body,
            headers={
                **_headers(registry, ["ingest"]),
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "must be a finite number" in resp.json()["detail"]
        assert registry.knowledge.vector_store.count() == 0

    @pytest.mark.parametrize(
        "metadata",
        [
            {"auto_importance": 0.99, "importance_scored_at": STAMPED},
            {
                "auto_importance": 0.99,
                "content_tags": {"importance_scored_at": STAMPED},
            },
            {"auto_importance": 0.2},
            {"auto_importance": 0},
            {"auto_importance": 1, "importance_scored_at": STAMPED},
            {"content": "text", "title": "t"},
            {"excerpt": "text"},
            {"content": "text", "excerpt": "other"},
            {"title": "no typed keys"},
            {},
            None,
        ],
        ids=repr,
    )
    def test_a_well_formed_row_is_accepted_and_readable_by_the_semantic_axis(
        self, client, registry, metadata
    ):
        resp = self._post(client, registry, metadata)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}
        # Not just stored: a semantic read over it does not raise.
        _semantic_pack(registry, "restricted")

    def test_the_check_does_not_depend_on_the_document_overwriting_the_key(
        self, client, registry
    ):
        """``auto_importance`` is mirrored, so a document's row would discard a
        bad supplied value; it is refused all the same, so the answer does not
        turn on what the store holds."""
        registry.knowledge.document_store.put("doc-tagged", OPEN, {})
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("doc-tagged", {"metadata": {"auto_importance": 0.99}}),
        )
        assert resp.status_code == 422, resp.text
        assert registry.knowledge.vector_store.count() == 0


class TestVectorsReservedIds:
    """``trace-summary:<id>`` belongs to the trace-embed worker: a vector row
    there makes ``trace_is_embedded`` true and the worker skips the trace."""

    def _post(self, client, registry, item_id, scopes):
        return _post_vectors(
            client,
            registry,
            _headers(registry, scopes),
            ("doc-open", {}),
            (item_id, {}),
        )

    @pytest.mark.parametrize("scopes", [["ingest"], ["admin"]], ids=["ingest", "admin"])
    @pytest.mark.parametrize(
        "item_id", [f"{DOC_ID_PREFIX}x", trace_summary_doc_id("t-1"), DOC_ID_PREFIX]
    )
    def test_the_reserved_prefix_is_refused_and_nothing_is_written(
        self, client, registry, scopes, item_id
    ):
        resp = self._post(client, registry, item_id, scopes)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith("vectors[1].item_id")
        assert registry.knowledge.vector_store.count() == 0

    def test_the_prefix_the_route_reserves_is_the_workers(self):
        assert ingest_mod.TRACE_SUMMARY_ID_PREFIX == DOC_ID_PREFIX
        assert DOC_ID_PREFIX == "trace-summary:", (
            "operations.md and the route docstring name this literal"
        )

    @pytest.mark.parametrize(
        "item_id",
        ["trace:t-1", "trace-summary", "my-trace-summary:x", "Trace-Summary:x"],
    )
    def test_an_id_that_merely_resembles_it_is_written(self, client, registry, item_id):
        resp = self._post(client, registry, item_id, ["ingest"])
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, item_id) is not None


class TestVectorsPartialBatch:
    """The first pass is all-or-nothing; the second is not, as the docstring says."""

    def test_a_row_whose_document_read_fails_is_counted_and_the_rest_land(
        self, client, registry, monkeypatch
    ):
        store = registry.knowledge.document_store
        real_get = store.get

        def flaky(doc_id):
            if doc_id == "doc-secret":
                msg = "store down"
                raise RuntimeError(msg)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", flaky)
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["admin"]),
            ("doc-open", {}),
            ("doc-secret", {}),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 1, "errors": 1}
        assert _vector_row(registry, "doc-open") is not None
        assert _vector_row(registry, "doc-secret") is None


class _Absent:
    """A field left out of the payload.

    ``ids=repr`` puts this in a test id, and a bare ``object()`` renders as its
    memory address: the id then differs per process, and pytest-xdist aborts at
    collection because two workers disagree about which tests exist.
    """

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT = _Absent()


class TestVectorsItemIds:
    """An id the gate cannot read as text is the caller's 422, before any write."""

    @pytest.mark.parametrize("bad", [5, 0, False, True, 1.5, ["a"], {"a": 1}], ids=repr)
    def test_an_id_that_is_not_a_string_is_refused_and_nothing_is_written(
        self, client, registry, bad
    ):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        resp = client.post(
            "/api/v1/vectors",
            json={"vectors": [good, {"item_id": bad, "vector": _vector(registry)}]},
            headers=_headers(registry, ["ingest"]),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "vectors[1].item_id must be a string"
        assert _vector_row(registry, "doc-open") is None
        assert registry.knowledge.vector_store.count() == 0

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("item_id", _ABSENT),
            ("item_id", None),
            ("item_id", ""),
            ("vector", _ABSENT),
            ("vector", None),
            ("vector", []),
        ],
        ids=repr,
    )
    def test_a_row_with_no_id_or_no_vector_is_counted_and_the_rest_land(
        self, client, registry, field, value
    ):
        """The documented second-pass errors: nothing to store, so nothing refused."""
        row: dict[str, Any] = {"item_id": "doc-x", "vector": _vector(registry)}
        if value is _ABSENT:
            del row[field]
        else:
            row[field] = value
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        resp = client.post(
            "/api/v1/vectors",
            json={"vectors": [good, row]},
            headers=_headers(registry, ["ingest"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 1, "errors": 1}
        assert _vector_row(registry, "doc-open") is not None
        assert registry.knowledge.vector_store.count() == 1


class TestVectorsVectorValues:
    """A vector that breaks every semantic query, or poisons its score, is a 422."""

    def _post(self, client, registry, vector=None, *, content=None):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        bad = {"item_id": "entity:b", "vector": vector}
        return client.post(
            "/api/v1/vectors",
            json={"vectors": [good, bad]} if content is None else None,
            content=content,
            headers={
                **_headers(registry, ["ingest"]),
                "Content-Type": "application/json",
            },
        )

    def _refused(self, client, registry, vector, detail):
        resp = self._post(client, registry, vector)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == f"vectors[1].{detail}"
        assert registry.knowledge.vector_store.count() == 0

    @pytest.mark.parametrize(
        "bad", ["abc", 5, {"a": 1}, True, 0, False, "", {}, 0.0], ids=repr
    )
    def test_a_vector_that_is_not_a_list_is_refused(self, client, registry, bad):
        """Falsy values included: ``0``, ``false``, ``""`` and ``{}`` are not the
        absent, ``null`` or empty-list vector the second pass counts as an error."""
        self._refused(client, registry, bad, "vector must be a list of numbers")

    @pytest.mark.parametrize("bad", ["x", None, True, False, [0.5]], ids=repr)
    def test_an_element_that_is_not_a_number_is_refused(self, client, registry, bad):
        vector = _vector(registry)
        vector[-1] = bad
        self._refused(
            client, registry, vector, f"vector[{len(vector) - 1}] must be a number"
        )

    @pytest.mark.parametrize(
        "bad", [1e39, -1e39, 2 * 3.4028234663852886e38, 10**400, -(10**400)], ids=repr
    )
    def test_an_element_past_float32_is_refused(self, client, registry, bad):
        """The store keeps float32, where these become ``inf``."""
        vector = _vector(registry)
        vector[0] = bad
        self._refused(
            client,
            registry,
            vector,
            "vector[0] must be a finite number within float32 range",
        )

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_element_is_refused(self, client, registry, literal):
        """Starlette's JSON parser accepts the literals, so the route must not."""
        elements = ", ".join([literal, *["0.5"] * (_dims(registry) - 1)])
        body = (
            '{"vectors": [{"item_id": "doc-open", "vector": '
            f"{_vector(registry)}}}, "
            f'{{"item_id": "entity:b", "vector": [{elements}]}}]}}'
        )
        resp = self._post(client, registry, content=body)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            "vectors[1].vector[0] must be a finite number within float32 range"
        )
        assert registry.knowledge.vector_store.count() == 0

    @pytest.mark.parametrize("magnitude", [3e38, 1e19], ids=repr)
    def test_a_vector_whose_norm_overflows_float32_is_refused(
        self, client, registry, magnitude
    ):
        """Every element fits float32, but the store's norm (a float32 sum of
        squares) is ``inf``, and the cosine score is NaN or a silent zero."""
        self._refused(
            client,
            registry,
            [magnitude] * _dims(registry),
            "vector is too large to compare: its squared length overflows float32",
        )

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_that_bound_is_not_cosmetic(self, registry):
        """The row the previous test refuses scores NaN once stored, so the
        element bound alone (float32's maximum) would not have been enough."""
        store = registry.knowledge.vector_store
        store.upsert(item_id="entity:b", vector=[3e38] * _dims(registry), metadata={})
        (hit,) = store.query(_vector(registry), top_k=1)
        assert math.isnan(hit["score"])

    @pytest.mark.parametrize(
        "make",
        [
            lambda dims: [0.0] * dims,
            lambda dims: [0] * dims,
            lambda dims: [-0.0] * dims,
            lambda dims: [1e-30] * dims,
            lambda dims: [1e-23] * dims,
            lambda dims: [1e-30, -1e-30] * (dims // 2),
        ],
        ids=["zeros", "int-zeros", "negative-zeros", "1e-30", "1e-23", "mixed-signs"],
    )
    def test_a_vector_with_no_float32_length_is_refused(self, client, registry, make):
        """Its float32 norm is 0.0 (all zeros, or a sum of squares that underflows),
        and the SQLite store skips a norm-0 row, so it would answer 200 and never
        be served."""
        self._refused(
            client,
            registry,
            make(_dims(registry)),
            "vector has no length: every element is zero or too small for "
            "float32, and the store never serves such a row",
        )

    def test_that_row_would_not_have_been_served(self, registry):
        """The premise of the refusal above, stated against the store directly."""
        store = registry.knowledge.vector_store
        store.upsert(item_id="entity:b", vector=[0.0] * _dims(registry), metadata={})
        assert store.query(_vector(registry), top_k=1) == []

    def test_a_vector_with_one_tiny_element_among_ordinary_ones_is_accepted(
        self, client, registry
    ):
        vector = [1e-30] * (_dims(registry) - 1) + [0.5]
        resp = self._post(client, registry, vector)
        assert resp.status_code == 200, resp.text
        assert "entity:b" in _served_ids(_semantic_pack(registry, "restricted"))

    @pytest.mark.parametrize(
        "make",
        [
            lambda dims: [1] * dims,
            lambda dims: [1e18] * dims,
            lambda dims: [1e-18] * dims,
            lambda dims: [(i + 1) / 10 for i in range(dims)],
            lambda dims: [-0.5] * dims,
            lambda dims: [(-1) ** i * (i + 1) / 10 for i in range(dims)],
        ],
        ids=[
            "ints",
            "large-but-safe",
            "small-but-live",
            "ascending",
            "negative",
            "mixed",
        ],
    )
    def test_a_well_formed_vector_is_accepted_and_served_by_the_semantic_axis(
        self, client, registry, make
    ):
        resp = self._post(client, registry, make(_dims(registry)))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}
        served = _served_ids(_semantic_pack(registry, "restricted"))
        assert sorted(served) == ["doc-open", "entity:b"]


class TestVectorsWidth:
    """Rows of different widths cannot be compared; the width is judged first."""

    @pytest.fixture
    def pinned(self, registry, monkeypatch):
        """A store that declares its width, as pgvector and the graph databases do."""
        width = _dims(registry)
        monkeypatch.setattr(
            type(registry.knowledge.vector_store),
            "dimensions",
            property(lambda _self: width),
        )
        return width

    def _post(self, client, registry, *vectors):
        rows = [
            {"item_id": f"entity:{index}", "vector": vector}
            for index, vector in enumerate(vectors)
        ]
        return client.post(
            "/api/v1/vectors",
            json={"vectors": rows},
            headers=_headers(registry, ["ingest"]),
        )

    @pytest.mark.parametrize("width", [3, 9], ids=["shorter", "longer"])
    def test_a_store_that_declares_a_width_refuses_another_and_stores_nothing(
        self, client, registry, pinned, width
    ):
        resp = self._post(client, registry, [0.5] * pinned, [0.5] * width)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            f"vectors[1].vector has {width} dimensions, expected {pinned} "
            "(the store's width)"
        )
        assert registry.knowledge.vector_store.count() == 0

    def test_a_store_that_declares_a_width_takes_that_width(
        self, client, registry, pinned
    ):
        resp = self._post(client, registry, [0.5] * pinned, [0.25] * pinned)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}

    def test_a_store_that_declares_no_width_holds_the_batch_to_one(
        self, client, registry
    ):
        """The first non-empty vector sets it, whatever rows come before."""
        assert registry.knowledge.vector_store.dimensions is None
        resp = self._post(client, registry, [], [0.5] * 8, [0.5] * 3)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == (
            "vectors[2].vector has 3 dimensions, expected 8 "
            "(the width of vectors[1].vector)"
        )
        assert registry.knowledge.vector_store.count() == 0

    def test_a_store_that_declares_no_width_is_not_checked_against_what_it_holds(
        self, client, registry
    ):
        """The documented limit: SQLite pins no width and the store interface offers
        no way to ask what width its rows have, so a later batch of another width
        is not refused here (it is for any backend that declares one)."""
        assert registry.knowledge.vector_store.dimensions is None
        assert self._post(client, registry, [0.5] * 8).status_code == 200
        assert self._post(client, registry, [0.5] * 3).status_code == 200

    def test_two_stored_widths_still_serve_the_rows_of_the_querys_width(
        self, client, registry
    ):
        """What the limit above costs on SQLite: nothing a pack can see. The store
        scores only the rows of the query's width (#1058), so a semantic-only build
        serves the 8-wide row and a multi-strategy build keeps its semantic axis
        beside the other one."""
        for item_id, width in (("entity:wide", 8), ("entity:narrow", 3)):
            resp = client.post(
                "/api/v1/vectors",
                json={"vectors": [{"item_id": item_id, "vector": [0.5] * width}]},
                headers=_headers(registry, ["ingest"]),
            )
            assert resp.status_code == 200, resp.text
        assert _served_ids(_semantic_pack(registry, "restricted")) == ["entity:wide"]
        other = PackItem(
            item_id="ent-01",
            item_type="entity",
            excerpt="found by another axis",
            relevance_score=0.9,
            estimated_tokens=10,
            metadata={},
        )
        assert set(_served_ids(_pack_with_another_axis(registry, other))) == {
            "ent-01",
            "entity:wide",
        }


class _Fixed(SearchStrategy):
    """Serves fixed candidates: the item another axis found."""

    def __init__(self, items: list[PackItem]) -> None:
        self._items = items

    @property
    def name(self) -> str:
        return "keyword"

    def search(self, query, *, limit=20, filters=None):
        return [item.model_copy() for item in self._items[:limit]]


def _pack_with_another_axis(registry, *items):
    """The semantic axis beside another axis that found ``items``."""
    builder = PackBuilder(
        strategies=[
            SemanticSearch(
                registry.knowledge.vector_store,
                embedding_fn=lambda _text: _vector(registry),
            ),
            _Fixed(list(items)),
        ],
        document_store=registry.knowledge.document_store,
    )
    return builder.build("failover runbook", clearance="restricted")


class TestVectorsItemPointerKeys:
    """``lifecycle`` and ``evidence_ref`` act on other items, by id: a row at an id
    that names no document may not carry them."""

    FOUND_ELSEWHERE = PackItem(
        item_id="ent-01",
        item_type="entity",
        excerpt="a real entity another axis found",
        relevance_score=0.9,
        estimated_tokens=10,
        metadata={},
    )

    def _post(self, client, registry, metadata, item_id="ent-01", scopes=("ingest",)):
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        bad = {"item_id": item_id, "vector": _vector(registry), "metadata": metadata}
        return client.post(
            "/api/v1/vectors",
            json={"vectors": [good, bad]},
            headers=_headers(registry, list(scopes)),
        )

    @pytest.mark.parametrize(
        ("metadata", "key"),
        [
            (
                {LIFECYCLE_KEY: {"state": "superseded", "superseded_by": "doc-open"}},
                "lifecycle",
            ),
            ({LIFECYCLE_KEY: ARCHIVED}, "lifecycle"),
            ({LIFECYCLE_KEY: None}, "lifecycle"),
            ({LIFECYCLE_KEY: "superseded"}, "lifecycle"),
            ({"evidence_ref": "doc-open"}, "evidence_ref"),
            ({"evidence_ref": None}, "evidence_ref"),
            ({"title": "t", "evidence_ref": "doc-x"}, "evidence_ref"),
        ],
        ids=repr,
    )
    @pytest.mark.parametrize(
        "item_id",
        ["ent-01", "entity:probe", "doc-gone#chunk-1", "doc-open#chunk-1"],
    )
    def test_an_id_naming_no_document_may_not_carry_one_and_nothing_is_written(
        self, client, registry, metadata, key, item_id
    ):
        resp = self._post(client, registry, metadata, item_id=item_id)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"].startswith(
            f"vectors[1].metadata.{key} needs item_id to name a stored document"
        )
        assert registry.knowledge.vector_store.count() == 0

    def test_a_forged_supersession_would_have_withheld_another_axiss_item(
        self, client, registry
    ):
        """The end effect. Withholding is by id, so a row at ``ent-01`` that says
        it is superseded by a candidate withholds every copy of ``ent-01``,
        including the real one another axis found. Through the route it is
        refused and the item is served; written past the route it is not."""
        headers = _headers(registry, ["ingest"])
        assert (
            _post_vectors(client, registry, headers, ("doc-open", {})).status_code
            == 200
        )
        forged = {LIFECYCLE_KEY: {"state": "superseded", "superseded_by": "doc-open"}}

        resp = _post_vectors(
            client, registry, headers, ("ent-01", {"metadata": forged})
        )
        assert resp.status_code == 422, resp.text
        served = _served_ids(_pack_with_another_axis(registry, self.FOUND_ELSEWHERE))
        assert sorted(served) == ["doc-open", "ent-01"]

        registry.knowledge.vector_store.upsert(
            item_id="ent-01", vector=_vector(registry), metadata=forged
        )
        served = _served_ids(_pack_with_another_axis(registry, self.FOUND_ELSEWHERE))
        assert served == ["doc-open"], "premise: a row past the route withholds it"

    def test_a_forged_evidence_ref_would_have_withheld_an_item_that_names_a_successor(
        self, client, registry
    ):
        """``evidence_ref`` makes a row the successor another item's
        ``superseded_by`` resolves to; the old version is then withheld for a
        row that is not its successor."""
        docs = registry.knowledge.document_store
        docs.put(
            "doc-old",
            OPEN,
            {LIFECYCLE_KEY: {"state": "superseded", "superseded_by": "doc-new"}},
        )
        headers = _headers(registry, ["ingest"])
        mirror = _post_vectors(client, registry, headers, ("doc-old", {}))
        assert mirror.status_code == 200, mirror.text
        assert _served_ids(_pack_with_another_axis(registry)) == ["doc-old"], (
            "its successor is not a candidate, so the old version is served"
        )

        forged = {"evidence_ref": "doc-new"}
        resp = _post_vectors(
            client, registry, headers, ("entity:probe", {"metadata": forged})
        )
        assert resp.status_code == 422, resp.text
        assert _served_ids(_pack_with_another_axis(registry)) == ["doc-old"]

        registry.knowledge.vector_store.upsert(
            item_id="entity:probe", vector=_vector(registry), metadata=forged
        )
        assert _served_ids(_pack_with_another_axis(registry)) == ["entity:probe"], (
            "premise: a row stored past the route does withhold the old version"
        )

    @pytest.mark.parametrize("scopes", [["ingest"], ["admin"]], ids=["ingest", "admin"])
    def test_the_refusal_is_for_every_caller(self, client, registry, scopes):
        resp = self._post(
            client, registry, {LIFECYCLE_KEY: ARCHIVED}, scopes=tuple(scopes)
        )
        assert resp.status_code == 422, resp.text

    def test_a_stored_document_takes_its_own_pointer_keys_not_the_callers(
        self, client, registry
    ):
        """At a document the keys are not refused: the document's value is written
        (lifecycle by the mirror, ``evidence_ref`` the same way)."""
        resp = self._post(
            client,
            registry,
            {LIFECYCLE_KEY: ARCHIVED, "evidence_ref": "doc-new", "title": "t"},
            item_id="doc-open",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}
        row = _vector_row(registry, "doc-open")["metadata"]
        assert LIFECYCLE_KEY not in row
        assert "evidence_ref" not in row
        assert row["title"] == "t"

    def test_a_document_that_carries_an_evidence_ref_passes_it_to_its_row(
        self, client, registry
    ):
        registry.knowledge.document_store.put(
            "doc-pointing", OPEN, {"evidence_ref": "doc-target"}
        )
        resp = _post_vectors(
            client,
            registry,
            _headers(registry, ["ingest"]),
            ("doc-pointing", {"metadata": {"evidence_ref": "doc-forged"}}),
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, "doc-pointing")["metadata"]["evidence_ref"] == (
            "doc-target"
        )

    def test_a_chunk_with_a_stored_document_is_a_document(self, client, registry):
        chunk = chunk_doc_id("doc-open", 1)
        registry.knowledge.document_store.put(chunk, OPEN, {})
        resp = self._post(client, registry, {LIFECYCLE_KEY: ARCHIVED}, item_id=chunk)
        assert resp.status_code == 200, resp.text
        assert LIFECYCLE_KEY not in _vector_row(registry, chunk)["metadata"]

    def test_a_row_with_no_id_is_counted_whatever_keys_it_carries(
        self, client, registry
    ):
        """No id: nothing to look up or refuse, and the second pass counts it."""
        row = {"vector": _vector(registry), "metadata": {LIFECYCLE_KEY: ARCHIVED}}
        good = {"item_id": "doc-open", "vector": _vector(registry)}
        resp = client.post(
            "/api/v1/vectors",
            json={"vectors": [good, row]},
            headers=_headers(registry, ["ingest"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 1, "errors": 1}

    def test_the_other_keys_of_a_row_at_such_an_id_are_still_written(
        self, client, registry
    ):
        resp = self._post(
            client,
            registry,
            {"title": "t", "content": "text", "content_tags": {"domain": ["ops"]}},
        )
        assert resp.status_code == 200, resp.text
        assert _vector_row(registry, "ent-01")["metadata"]["title"] == "t"

    def test_a_row_without_those_keys_costs_no_read_in_the_first_pass(
        self, client, registry, monkeypatch
    ):
        store = registry.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id):
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        resp = self._post(client, registry, {"title": "t"}, scopes=("admin",))
        assert resp.status_code == 200, resp.text
        assert reads == ["doc-open", "ent-01"], "the mirror's read, once per row"

    def test_a_document_deleted_after_the_first_pass_cannot_carry_pointer_keys_in(
        self, client, registry, monkeypatch
    ):
        """The first pass reads the document and finds it; the mirror's read, later
        in the same request, finds it gone. The keys the first pass would have
        refused are dropped, and the rest of the row is written as sent."""
        docs = registry.knowledge.document_store
        docs.put("doc-vanishing", OPEN, {})
        real_get = docs.get
        seen: set[str] = set()

        def vanishing(doc_id):
            if doc_id == "doc-vanishing" and doc_id in seen:
                return None
            seen.add(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(docs, "get", vanishing)
        resp = self._post(
            client,
            registry,
            {
                LIFECYCLE_KEY: {"state": "superseded", "superseded_by": "doc-open"},
                "evidence_ref": "doc-open",
                "title": "t",
            },
            item_id="doc-vanishing",
            scopes=("admin",),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 2, "errors": 0}
        row = _vector_row(registry, "doc-vanishing")["metadata"]
        assert LIFECYCLE_KEY not in row
        assert "evidence_ref" not in row
        assert row["title"] == "t"

    def test_a_document_that_cannot_be_read_while_judging_the_keys_is_a_server_error(
        self, client, registry, monkeypatch
    ):
        """Not an accepted row: with no answer to "is it a document", nothing is
        written (and the failure is not hidden as a 422 about the caller)."""

        def boom(_doc_id):
            msg = "store down"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.document_store, "get", boom)
        quiet = TestClient(create_app(), raise_server_exceptions=False)
        resp = quiet.post(
            "/api/v1/vectors",
            json={
                "vectors": [
                    {
                        "item_id": "ent-01",
                        "vector": _vector(registry),
                        "metadata": {LIFECYCLE_KEY: ARCHIVED},
                    }
                ]
            },
            # Admin: an ``ingest`` key makes ``refuse_vector_write`` read the
            # document too, and that read, not this check, would be the 500.
            headers=_headers(registry, ["admin"]),
        )
        assert resp.status_code == 500, resp.text
        assert registry.knowledge.vector_store.count() == 0


# ── credentialed write routes build their executor at the key's clearance ──
#
# None of these operations names a document, so the Stage-2 floor has nothing to
# refuse on them today and no below-clearance request can be shown refused. What
# these tests pin is the thing that would silently change that: the executor is
# built from the request's clearance, not from a trusted ``restricted``.

_TRACE = {
    "source": "agent",
    "intent": "check clearance",
    "steps": [{"step_type": "tool_call", "name": "grep"}],
    "context": {"agent_id": "a1", "domain": "backend"},
}
_NODE = {"subject_entity_id": "n-1", "subject_entity_type": "Dataset"}

WRITE_ROUTES = [
    pytest.param(
        "/api/v1/links",
        {
            "source_id": "a",
            "target_id": "b",
            "edge_kind": "related_to",
            "allow_dangling": True,
        },
        ["mutate"],
        id="links",
    ),
    pytest.param(
        "/api/v1/entities",
        {"entity_type": "Dataset", "name": "users", "properties": {}},
        ["mutate"],
        id="entities",
    ),
    pytest.param(
        "/api/v1/feedback",
        {"target_id": "t-1", "rating": 0.5},
        ["mutate"],
        id="feedback",
    ),
    pytest.param("/api/v1/traces", _TRACE, ["ingest"], id="traces"),
    pytest.param(
        "/api/v1/observations",
        {
            **_NODE,
            "observer_agent_id": "agent-1",
            "content": "seen",
            "confidence": 0.9,
        },
        ["read", "mutate"],
        id="observations",
    ),
    pytest.param(
        "/api/v1/measurements",
        {
            **_NODE,
            "metric_name": "null_rate",
            "metric_value": 0.07,
            "unit": "percent",
            "observer_agent_id": "agent-1",
        },
        ["read", "mutate"],
        id="measurements",
    ),
]


class TestWriteRoutesThreadTheClearance:
    @pytest.fixture
    def built_at(self, monkeypatch):
        seen: list[str] = []

        def build(registry, *, clearance, **kwargs):
            seen.append(clearance)
            return build_curate_executor(registry, clearance=clearance, **kwargs)

        for module in (curate_mod, ingest_mod, observations_mod):
            monkeypatch.setattr(module, "build_curate_executor", build)
        return seen

    @pytest.mark.parametrize(("path", "body", "scopes"), WRITE_ROUTES)
    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ([], "internal"),
            (["confidential"], "confidential"),
            (["restricted"], "restricted"),
        ],
        ids=["internal", "confidential", "restricted"],
    )
    def test_the_executor_is_built_at_the_keys_clearance(
        self, client, registry, built_at, path, body, scopes, extra, expected
    ):
        resp = client.post(
            path, json=body, headers=_headers(registry, [*scopes, *extra])
        )
        assert resp.status_code in (200, 201), resp.text
        assert built_at == [expected]

    @pytest.mark.parametrize(("path", "body", "scopes"), WRITE_ROUTES)
    def test_auth_off_is_trusted_local(
        self, client, registry, built_at, monkeypatch, path, body, scopes
    ):
        del scopes
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = client.post(path, json=body)
        assert resp.status_code in (200, 201), resp.text
        assert built_at == ["restricted"]
