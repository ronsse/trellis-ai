"""``POST /commands/batch`` ``evidence.ingest`` and the pointer keys (#1057).

The pack builder withholds items by id, so a document whose ``lifecycle`` says
``superseded`` withholds every item under its id and a document's
``evidence_ref`` makes it the successor another item names. A caller may not
use either key to withhold or redirect. The documented operator restore sends a
document's metadata bag back with ``lifecycle`` set to ``current``, so that form
and a round-tripped, unchanged ``evidence_ref`` stay accepted. Real SQLite
stores, the real route and the real handler.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import generate_api_key
from trellis.core.document_write import is_redaction_tombstone
from trellis.mcp.supersession import supersede_document
from trellis.mutate.evidence_ingest import _stable_document_id
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

OLD = "failover runbook old: drain the write queue, promote the replica. " * 4
NEW = "failover runbook new: drain the write queue, then promote. " * 4
MUTATE = ["mutate"]
REF = "doc-successor"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.document_store.put("doc-old", OLD, {"title": "Old"})
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    return TestClient(create_app())


def _headers(registry, scopes=MUTATE):
    token, record = generate_api_key("k", scopes)
    registry.operational.api_key_store.create(record)
    return {"Authorization": f"Bearer {token}"}


def _ingest(
    metadata: Any, doc_id: str = "doc-fresh", content: str = NEW
) -> dict[str, Any]:
    return {
        "operation": "evidence.ingest",
        "args": {
            "evidence": {"doc_id": doc_id, "content": content, "metadata": metadata}
        },
    }


def _batch(
    client, headers, *commands: dict[str, Any], strategy: str = "continue_on_error"
):
    return client.post(
        "/api/v1/commands/batch",
        json={"commands": list(commands), "strategy": strategy},
        headers=headers,
    )


def _footprint(registry, *doc_ids: str) -> dict[str, Any]:
    """Everything a write could touch: documents, vectors and events."""
    knowledge = registry.knowledge
    return {
        "documents": knowledge.document_store.count(),
        "rows": {d: knowledge.document_store.get(d) for d in doc_ids},
        "vectors": {d: knowledge.vector_store.get(d) for d in doc_ids},
        "events": registry.operational.event_log.count(),
    }


SUPERSEDED = {"state": "superseded", "superseded_by": "doc-fresh"}


class TestAWithholdingLifecycleIsRefused:
    @pytest.mark.parametrize(
        "lifecycle",
        [
            SUPERSEDED,
            {"state": "archived"},
            {"state": "demoted"},
            {"state": "current", "superseded_by": "doc-fresh"},
            {"state": "current", "valid_until": "2020-01-01T00:00:00+00:00"},
            {"state": "current", "unknown_key": 1},
            {"state": "current", "deprecation_reason": "redacted"},
            {"state": "current", "valid_from": "2020-01-01T00:00:00+00:00"},
            {"state": "current", "classification_version": 2},
            {},
            "current",
            ["current"],
            None,
        ],
        ids=[
            "superseded",
            "archived",
            "demoted",
            "current-with-superseded-by",
            "current-with-valid-until",
            "current-with-unknown-key",
            "current-with-deprecation-reason",
            "current-with-valid-from",
            "current-with-classification-version",
            "empty-record",
            "string",
            "list",
            "null",
        ],
    )
    @pytest.mark.parametrize("doc_id", ["doc-fresh", "doc-old"])
    def test_refused_whole_batch_and_nothing_is_written(
        self, client, registry, lifecycle, doc_id
    ):
        before = _footprint(registry, "doc-fresh", "doc-old")
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"title": "x"}, doc_id="doc-fresh", content="innocent " * 8),
            _ingest({LIFECYCLE_KEY: lifecycle}, doc_id=doc_id),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "metadata.lifecycle may not be set" in detail
        assert "save_memory(supersedes=" in detail
        assert "retention.restore" in detail
        assert '{"state": "current"}' in detail
        # The innocent first command did not run either: a whole-batch refusal.
        assert _footprint(registry, "doc-fresh", "doc-old") == before

    def test_the_target_id_names_the_document_when_the_body_has_none(
        self, client, registry
    ):
        """The id judged is the id the command would write."""
        before = _footprint(registry, "doc-old")
        resp = _batch(
            client,
            _headers(registry),
            {
                "operation": "evidence.ingest",
                "target_id": "doc-old",
                "args": {
                    "evidence": {
                        "content": NEW,
                        "metadata": {LIFECYCLE_KEY: SUPERSEDED},
                    }
                },
            },
        )
        assert resp.status_code == 422, resp.text
        assert _footprint(registry, "doc-old") == before

    def test_a_forged_redaction_tombstone_is_refused_and_nothing_is_written(
        self, client, registry
    ):
        """``current`` plus a redaction reason on empty content is a tombstone."""
        forged = {"state": "current", "deprecation_reason": "redacted"}
        command = _ingest({LIFECYCLE_KEY: forged}, doc_id="doc-old", content="")
        command["args"]["evidence"]["uri"] = "https://example.invalid/page"
        before = _footprint(registry, "doc-old")
        resp = _batch(client, _headers(registry), command)
        assert resp.status_code == 422, resp.text
        assert "metadata.lifecycle may not be set" in resp.json()["detail"]
        assert _footprint(registry, "doc-old") == before
        stored = registry.knowledge.document_store.get("doc-old")
        assert not is_redaction_tombstone(stored)

    @pytest.mark.parametrize("strategy", ["sequential", "stop_on_error"])
    def test_every_strategy_refuses_before_any_command_runs(
        self, client, registry, strategy
    ):
        before = _footprint(registry, "doc-fresh")
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"title": "x"}),
            _ingest({LIFECYCLE_KEY: SUPERSEDED}, doc_id="doc-other"),
            strategy=strategy,
        )
        assert resp.status_code == 422, resp.text
        assert _footprint(registry, "doc-fresh") == before


class TestAnEvidenceRefThatIsNewOrChangedIsRefused:
    def test_a_new_evidence_ref_is_refused(self, client, registry):
        before = _footprint(registry, "doc-fresh")
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"title": "x", "evidence_ref": REF}),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "metadata.evidence_ref may not be set" in detail
        assert "save_knowledge(evidence_ref=" in detail
        assert _footprint(registry, "doc-fresh") == before

    def test_a_new_evidence_ref_on_a_document_that_holds_none_is_refused(
        self, client, registry
    ):
        before = _footprint(registry, "doc-old")
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"evidence_ref": REF}, doc_id="doc-old"),
        )
        assert resp.status_code == 422, resp.text
        assert _footprint(registry, "doc-old") == before

    def test_a_changed_evidence_ref_is_refused(self, client, registry):
        registry.knowledge.document_store.put(
            "doc-linked", OLD, {"title": "L", "evidence_ref": REF}
        )
        before = _footprint(registry, "doc-linked")
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"evidence_ref": "doc-elsewhere"}, doc_id="doc-linked"),
        )
        assert resp.status_code == 422, resp.text
        assert "metadata.evidence_ref may not be set" in resp.json()["detail"]
        assert _footprint(registry, "doc-linked") == before

    @pytest.mark.parametrize("value", [None, "", 7, ["doc-old"]])
    def test_a_non_matching_value_is_refused(self, client, registry, value):
        registry.knowledge.document_store.put("doc-linked", OLD, {"evidence_ref": REF})
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"evidence_ref": value}, doc_id="doc-linked"),
        )
        assert resp.status_code == 422, resp.text
        stored = registry.knowledge.document_store.get("doc-linked")
        assert stored["metadata"] == {"evidence_ref": REF}

    def test_a_stored_value_above_the_keys_clearance_reads_as_absent(
        self, client, registry
    ):
        """No oracle: a key cannot learn a restricted document's ``evidence_ref``."""
        from tests.unit.retrieve.test_sensitivity import label

        registry.knowledge.document_store.put(
            "doc-secret", OLD, {"evidence_ref": REF, **label("restricted")}
        )
        resp = _batch(
            client,
            _headers(registry, MUTATE),
            _ingest({"evidence_ref": REF}, doc_id="doc-secret"),
        )
        assert resp.status_code == 422, resp.text
        assert "metadata.evidence_ref may not be set" in resp.json()["detail"]


def _targeted(metadata: Any, target_id: str, doc_id: str | None = None) -> dict:
    evidence: dict[str, Any] = {"content": NEW, "metadata": metadata}
    if doc_id is not None:
        evidence["doc_id"] = doc_id
    return {
        "operation": "evidence.ingest",
        "target_id": target_id,
        "args": {"evidence": evidence},
    }


class TestTheCheckJudgesTheDocumentTheCommandWrites:
    """One id for the check and the write: the command the builder resolved."""

    def test_a_doc_id_and_a_target_id_that_differ_cannot_borrow_each_others_ref(
        self, client, registry
    ):
        """The write lands on ``doc_id``; the ref is held by ``target_id``."""
        registry.knowledge.document_store.put("doc-a", OLD, {"evidence_ref": REF})
        before = _footprint(registry, "doc-a", "doc-x")
        resp = _batch(
            client,
            _headers(registry),
            _targeted({"evidence_ref": REF}, target_id="doc-a", doc_id="doc-x"),
        )
        assert resp.status_code == 422, resp.text
        assert "metadata.evidence_ref may not be set" in resp.json()["detail"]
        assert _footprint(registry, "doc-a", "doc-x") == before
        assert registry.knowledge.document_store.get("doc-x") is None

    def test_the_target_id_is_judged_when_the_body_names_no_doc_id(
        self, client, registry
    ):
        registry.knowledge.document_store.put("doc-a", OLD, {"evidence_ref": REF})
        resp = _batch(
            client, _headers(registry), _targeted({"evidence_ref": REF}, "doc-a")
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == 1, resp.text
        refused = _batch(
            client, _headers(registry), _targeted({"evidence_ref": REF}, "doc-b")
        )
        assert refused.status_code == 422, refused.text
        assert registry.knowledge.document_store.get("doc-b") is None

    def test_an_idempotency_key_alone_names_the_document_the_builder_derives(
        self, client, registry
    ):
        """No ``doc_id``, no ``target_id``: the write is at ``evidence:<digest>``."""
        key = "restore-run-1"
        derived = _stable_document_id(key)
        registry.knowledge.document_store.put(derived, OLD, {"evidence_ref": REF})

        def command(ref: str) -> dict[str, Any]:
            return {
                "operation": "evidence.ingest",
                "idempotency_key": key,
                "args": {
                    "evidence": {"content": NEW, "metadata": {"evidence_ref": ref}}
                },
            }

        refused = _batch(client, _headers(registry), command("doc-elsewhere"))
        assert refused.status_code == 422, refused.text
        stored = registry.knowledge.document_store.get(derived)
        assert stored["metadata"] == {"evidence_ref": REF}

        kept = _batch(client, _headers(registry), command(REF))
        assert kept.status_code == 200, kept.text
        assert kept.json()["succeeded"] == 1, kept.text
        assert registry.knowledge.document_store.get(derived)["content"] == NEW

    def test_an_idempotency_key_alone_with_no_stored_row_is_a_new_document(
        self, client, registry
    ):
        before = _footprint(registry)
        resp = _batch(
            client,
            _headers(registry),
            {
                "operation": "evidence.ingest",
                "idempotency_key": "restore-run-2",
                "args": {
                    "evidence": {"content": NEW, "metadata": {"evidence_ref": REF}}
                },
            },
        )
        assert resp.status_code == 422, resp.text
        assert _footprint(registry) == before


class TestWhatStaysAccepted:
    def test_a_round_tripped_evidence_ref_is_accepted(self, client, registry):
        registry.knowledge.document_store.put(
            "doc-linked", OLD, {"title": "L", "evidence_ref": REF}
        )
        resp = _batch(
            client,
            _headers(registry),
            _ingest(
                {"title": "L2", "evidence_ref": REF},
                doc_id="doc-linked",
                content=NEW,
            ),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == 1, resp.text
        stored = registry.knowledge.document_store.get("doc-linked")
        assert stored["content"] == NEW
        assert stored["metadata"]["evidence_ref"] == REF

    def test_a_current_lifecycle_is_accepted(self, client, registry):
        resp = _batch(
            client,
            _headers(registry),
            _ingest({LIFECYCLE_KEY: {"state": "current"}}, doc_id="doc-old"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == 1, resp.text

    def test_metadata_without_a_pointer_key_is_untouched(self, client, registry):
        resp = _batch(
            client,
            _headers(registry),
            _ingest({"title": "plain", "tags": ["a"]}),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == 1, resp.text
        stored = registry.knowledge.document_store.get("doc-fresh")
        assert stored["metadata"]["title"] == "plain"

    def test_other_operations_are_not_inspected(self, client, registry):
        """The rule is about ``evidence.ingest``; a ``link.create`` is not one."""
        graph = registry.knowledge.graph_store
        a = graph.upsert_node(node_id=None, node_type="concept", properties={})
        b = graph.upsert_node(node_id=None, node_type="concept", properties={})
        resp = _batch(
            client,
            _headers(registry),
            {
                "operation": "link.create",
                "args": {
                    "source_id": a,
                    "target_id": b,
                    "edge_kind": "entity_related_to",
                    "properties": {LIFECYCLE_KEY: SUPERSEDED, "evidence_ref": REF},
                },
            },
        )
        assert resp.status_code == 200, resp.text

    def test_the_operator_restore_recipe_works_end_to_end(self, client, registry):
        """Stamp with the governed writer, then undo it through the batch door."""
        registry.knowledge.document_store.put("doc-new", NEW, {"title": "New"})
        error = supersede_document(
            registry,
            doc_id="doc-old",
            successor="doc-new",
            requested_by="mcp:save_memory",
            clearance="restricted",
        )
        assert error is None
        stamped = registry.knowledge.document_store.get("doc-old")
        assert stamped["metadata"][LIFECYCLE_KEY]["superseded_by"] == "doc-new"

        # The recipe: send the stamped bag back with the lifecycle set current.
        bag = {**stamped["metadata"], LIFECYCLE_KEY: {"state": "current"}}
        resp = _batch(
            client,
            _headers(registry),
            _ingest(bag, doc_id="doc-old", content=stamped["content"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == 1, resp.text
        restored = registry.knowledge.document_store.get("doc-old")
        assert restored["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert restored["metadata"][LIFECYCLE_KEY].get("superseded_by") is None


class TestOtherDoorsAreUnchanged:
    SUPERSEDED_BAG: ClassVar[dict[str, Any]] = {LIFECYCLE_KEY: SUPERSEDED}

    def test_post_documents_still_refuses_any_value_with_the_same_words(
        self, client, registry
    ):
        headers = _headers(registry)
        for key, value, words in (
            (LIFECYCLE_KEY, {"state": "current"}, "metadata.lifecycle may not be set"),
            ("evidence_ref", None, "metadata.evidence_ref may not be set"),
        ):
            resp = client.post(
                "/api/v1/documents",
                json={"doc_id": "doc-new", "content": NEW, "metadata": {key: value}},
                headers=headers,
            )
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert words in detail
            assert detail.endswith("Nothing was written")
            # The restore form is not offered where it is not accepted.
            assert '{"state": "current"}' not in detail
        assert registry.knowledge.document_store.get("doc-new") is None

    def test_post_evidence_never_stores_the_bodys_metadata(self, client, registry):
        resp = client.post(
            "/api/v1/evidence",
            json={
                "evidence_type": "document",
                "content": NEW,
                "source_origin": "manual",
                "metadata": {LIFECYCLE_KEY: SUPERSEDED, "evidence_ref": REF},
            },
            headers=_headers(registry, ["ingest"]),
        )
        assert resp.status_code in (200, 201), resp.text
        rows = registry.knowledge.document_store.list_documents(limit=50)
        stored = [r for r in rows if r["doc_id"] != "doc-old"]
        assert stored, "the evidence was stored"
        for row in stored:
            assert LIFECYCLE_KEY not in row["metadata"]
            assert "evidence_ref" not in row["metadata"]
