"""REST write routes honour the key's clearance (#194, #1029).

``POST /documents`` takes a ``sensitivity`` under the rules ``POST /evidence``
and MCP ``save_memory`` apply; ``POST /vectors`` will not replace the vector row
of a document the key may not read; and every credentialed write route builds
its executor at the key's clearance rather than at a trusted ``restricted``.
"""

from __future__ import annotations

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
from trellis.mutate import build_curate_executor
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SemanticSearch
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, LIFECYCLE_KEY
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
        judge an id it cannot read as text, so the row is counted, not written."""
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
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "upserted": 0, "errors": 1}
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
                        "content_tags": {"domain": "forged"},
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
