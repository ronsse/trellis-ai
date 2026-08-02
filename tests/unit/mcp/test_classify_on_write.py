"""Classify-on-write coverage for the MCP document write paths.

The MCP tools are the highest-volume agent write path, so ``save_memory``
(both tiers) and ``save_knowledge``'s auto-created evidence document all tag
inline under ``TRELLIS_ENABLE_CLASSIFY_ON_INGEST``. The shared seam's own
contract lives in ``tests/unit/classify/test_ingest.py``; these tests prove
each path calls it, at the right moment, with the four safety properties
intact end-to-end against the temp stores.

The reconcile tier funnels every doc-storing verdict through
``_store_new_memory``, which has two callers — ``_save_memory_reconciled``'s
clean ADD (no candidate, no model call) and ``_commit_reconcile_verdict``
(a verdict was reached). Both are exercised below; neither makes a network
call (the model client boundary is pinned to ``None``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

import trellis.classify.ingest as ingest_mod
import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.classify.ingest import CLASSIFY_ON_INGEST_FLAG
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_knowledge = unwrap_tool(server_mod.save_knowledge)
save_memory = unwrap_tool(server_mod.save_memory)

#: Content the deterministic classifiers confidently domain-tag — the whole
#: point of the domain-drop assertions below.
_INFRA = "kubernetes deployment infra helm terraform rollout"

#: A near-duplicate of _INFRA (MinHash Jaccard above the 0.85 threshold) so
#: the reconcile tier surfaces a candidate to adjudicate.
_INFRA_NEAR = "kubernetes deployment infra helm terraform rollouts"

#: Unrelated to _INFRA (no MinHash overlap), so it is stored rather than
#: deduped away — used as the positive control in absence-assertion tests.
_UNRELATED = "grocery list milk bread apples for saturday dinner with friends"

#: Dotted path handed to TRELLIS_EMBEDDING_FN; the registry resolves it
#: lazily, so setting the env inside a test is picked up on first use.
_EMBED_FN_PATH = "tests.unit.mcp.test_classify_on_write._fake_embed"


def _fake_embed(text: str) -> list[float]:
    return [1.0, 0.0, 0.5]


class _BoomPipeline:
    def classify(self, *args: object, **kwargs: object) -> None:
        msg = "classifier exploded"
        raise RuntimeError(msg)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLASSIFY_ON_INGEST_FLAG, "1")


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLASSIFY_ON_INGEST_FLAG, raising=False)


def _doc_id(result: str) -> str:
    return result.rsplit(":", 1)[-1].strip()


def _stored_metadata(registry: StoreRegistry, doc_id: str) -> dict[str, Any]:
    doc = registry.knowledge.document_store.get(doc_id)
    assert doc is not None
    return dict(doc["metadata"] or {})


# ---------------------------------------------------------------------------
# save_memory — deterministic tier
# ---------------------------------------------------------------------------


class TestSaveMemoryClassifyOnWrite:
    def test_flag_off_stores_untagged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable(monkeypatch)
        doc_id = _doc_id(save_memory(_INFRA))
        assert "content_tags" not in _stored_metadata(temp_registry, doc_id)

    def test_flag_on_stores_tagged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        doc_id = _doc_id(save_memory(_INFRA, metadata={"source": "agent"}))
        meta = _stored_metadata(temp_registry, doc_id)
        assert meta["source"] == "agent"
        assert meta["content_tags"]["signal_quality"]
        assert isinstance(meta["auto_importance"], float)

    def test_domain_facet_always_dropped(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        doc_id = _doc_id(save_memory(_INFRA))
        assert _stored_metadata(temp_registry, doc_id)["content_tags"]["domain"] == []

    def test_existing_content_tags_not_clobbered(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The absence assertion alone would also pass if the seam were never
        # wired, so a tag-less control write in the same test proves it is.
        _enable(monkeypatch)
        caller_tags = {"domain": ["backend"], "signal_quality": "high"}
        doc_id = _doc_id(save_memory(_INFRA, metadata={"content_tags": caller_tags}))
        assert _stored_metadata(temp_registry, doc_id)["content_tags"] == caller_tags

        control_id = _doc_id(save_memory(_UNRELATED))
        control_meta = _stored_metadata(temp_registry, control_id)
        assert control_meta["content_tags"]["domain"] == []

    def test_classifier_error_still_stores_the_document(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())
        result = save_memory(_INFRA)
        assert result.startswith("Memory saved:")
        assert "content_tags" not in _stored_metadata(temp_registry, _doc_id(result))

    def test_tags_ride_the_memory_stored_payload(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The post-store tail (MEMORY_STORED payload, embed hook's vector row)
        # must see what was actually persisted, not the pre-classify metadata.
        _enable(monkeypatch)
        save_memory(_INFRA)
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED, limit=10
        )
        assert len(events) == 1
        assert events[0].payload["metadata"]["content_tags"]["domain"] == []

    def test_tags_ride_the_embed_hooks_vector_row(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of the post-store tail, and the stated reason
        # _store_new_memory returns the metadata it persisted: SemanticSearch
        # reads importance/tags off the vector row, not the document row.
        _enable(monkeypatch)
        monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")
        monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _EMBED_FN_PATH)

        doc_id = _doc_id(save_memory(_INFRA))
        row = temp_registry.knowledge.vector_store.get(doc_id)
        assert row is not None
        assert row["metadata"]["content_tags"]["domain"] == []
        assert isinstance(row["metadata"]["auto_importance"], float)


# ---------------------------------------------------------------------------
# save_memory — reconcile tier (_store_new_memory, from both its callers)
# ---------------------------------------------------------------------------


class TestReconcileTierClassifyOnWrite:
    """Both ``_store_new_memory`` callers tag; no model is ever contacted."""

    def test_clean_add_tags(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No near match -> _save_memory_reconciled stores directly, no verdict.
        _enable(monkeypatch)
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: None)

        doc_id = _doc_id(save_memory(_INFRA))
        meta = _stored_metadata(temp_registry, doc_id)
        assert meta["content_tags"]["domain"] == []
        assert "reconciliation" not in meta

    def test_committed_verdict_tags_alongside_the_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A near match with an unavailable model -> fallback ADD committed by
        # _commit_reconcile_verdict, which is the second _store_new_memory
        # caller. Its reconciliation marker and the tags must coexist.
        _enable(monkeypatch)
        save_memory(_INFRA)
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: None)

        doc_id = _doc_id(save_memory(_INFRA_NEAR))
        meta = _stored_metadata(temp_registry, doc_id)
        assert meta["reconciliation"]
        assert meta["content_tags"]["domain"] == []

    def test_caller_tags_survive_alongside_the_marker(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reconcile tier is the one remaining path where a caller CAN
        # supply content_tags: they ride save_memory's ``metadata`` through
        # _commit_reconcile_verdict's ``{**metadata, marker}`` into
        # _store_new_memory. Fill-if-absent must hold there too.
        _enable(monkeypatch)
        save_memory(_INFRA)
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: None)

        caller_tags = {"domain": ["backend"], "signal_quality": "high"}
        doc_id = _doc_id(
            save_memory(_INFRA_NEAR, metadata={"content_tags": caller_tags})
        )
        meta = _stored_metadata(temp_registry, doc_id)
        assert meta["reconciliation"]
        assert meta["content_tags"] == caller_tags

    def test_classifier_error_still_stores_the_document(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: None)
        monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())

        result = save_memory(_INFRA)
        assert result.startswith("Memory saved:")
        assert "content_tags" not in _stored_metadata(temp_registry, _doc_id(result))

    def test_flag_off_leaves_the_reconcile_tier_untagged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable(monkeypatch)
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        monkeypatch.setattr(server_mod, "_build_llm_client", lambda _registry: None)

        doc_id = _doc_id(save_memory(_INFRA))
        assert "content_tags" not in _stored_metadata(temp_registry, doc_id)


# ---------------------------------------------------------------------------
# save_knowledge — the auto-created evidence document
# ---------------------------------------------------------------------------


class TestEvidenceDocumentClassifyOnWrite:
    def _evidence_doc_id(self, result: str) -> str:
        line = next(
            ln for ln in result.splitlines() if ln.startswith("Evidence document:")
        )
        return line.split(":", 1)[1].strip()

    def test_flag_off_stores_untagged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable(monkeypatch)
        result = save_knowledge("rollout note", content=_INFRA)
        doc_id = self._evidence_doc_id(result)
        assert "content_tags" not in _stored_metadata(temp_registry, doc_id)

    def test_flag_on_stores_tagged_without_domain(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        result = save_knowledge("rollout note", content=_INFRA)
        meta = _stored_metadata(temp_registry, self._evidence_doc_id(result))
        # The pointer metadata the evidence seam writes survives the merge.
        assert meta["entity_name"] == "rollout note"
        assert meta["content_tags"]["domain"] == []
        assert isinstance(meta["auto_importance"], float)

    def test_classifier_error_still_creates_the_document(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())
        result = save_knowledge("rollout note", content=_INFRA)
        assert "Entity created" in result
        doc_id = self._evidence_doc_id(result)
        assert "content_tags" not in _stored_metadata(temp_registry, doc_id)
