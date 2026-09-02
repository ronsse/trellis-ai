"""Tests for RetentionPruneHandler and retention candidate resolution.

Covers the ``retention.prune`` gap: the verb shipped in the Operation enum
with an empty ``set()`` args schema and no registered handler, so every
command was rejected with "No handler registered" and there was no governed
path to dispose of a document at all — noise-tagged captures could only be
demoted-and-kept. Decision record: ``docs/design/adr-retention-prune.md``
(Option A, phase one = archival).

These tests pin, in particular, the two places the ADR's literal §3.2 was
wrong about the live corpus:

* ``signal_quality`` is a **document** facet — the demote loop
  (``apply_noise_tags``) writes it through ``document_store.put``, and no
  graph node has ever carried it. A resolver keyed on noise-tagged *nodes*
  can only return zero.
* Grace periods gate the age-based criteria only. A noise tag is a verdict,
  so requiring it to age would have made the motivating population
  unarchivable for a month.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, get_args

import pytest

from tests.document_recency import fake_document_clock, keyword_recency_ratio
from trellis.errors import ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    MAX_RETENTION_REASON_CHARS,
    RetentionPruneHandler,
    create_curate_handlers,
)
from trellis.mutate.retention import (
    ARCHIVED_STATE,
    CURRENT_STATE,
    UNSELECTABLE_STATES,
    RetentionCriteria,
    resolve_candidates,
)
from trellis.retrieve.lifecycle import exclude_archived, is_archived
from trellis.schemas.classification import LIFECYCLE_KEY, LifecycleState
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_CONFIRMED,
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.schemas.pack import PackItem
from trellis.stores.base.event_log import EventType
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _put_doc(
    registry: StoreRegistry,
    doc_id: str,
    *,
    signal_quality: str | None = None,
    lifecycle_state: str | None = None,
    title: str = "A doc",
) -> str:
    metadata: dict[str, Any] = {"title": title}
    if signal_quality is not None:
        metadata["content_tags"] = {"signal_quality": signal_quality}
    if lifecycle_state is not None:
        metadata[LIFECYCLE_KEY] = {"state": lifecycle_state}
    registry.knowledge.document_store.put(doc_id, f"content of {doc_id}", metadata)
    return doc_id


def _prune(
    criteria: dict[str, Any],
    *,
    reason: str = "noise captures (#312)",
    dry_run: bool = True,
) -> Command:
    return Command(
        operation=Operation.RETENTION_PRUNE,
        args={"criteria": criteria, "reason": reason, "dry_run": dry_run},
    )


class TestRegistration:
    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        assert Operation.RETENTION_PRUNE in create_curate_handlers(registry)

    def test_executor_no_longer_rejects_the_verb(self, registry: StoreRegistry) -> None:
        """The gap this closes: every command used to be REJECTED."""
        _put_doc(registry, "d1", signal_quality="noise")
        result = build_curate_executor(registry).execute(
            _prune({"noise_documents": True})
        )
        assert result.status == CommandStatus.SUCCESS
        assert "No handler registered" not in (result.message or "")

    def test_args_schema_requires_criteria_and_reason(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry).execute(
            Command(operation=Operation.RETENTION_PRUNE, args={})
        )
        assert result.status == CommandStatus.FAILED
        assert "Missing required args" in (result.message or "")
        assert "criteria" in (result.message or "")


class TestNoiseDocumentResolution:
    """The population that motivated the build — and the ADR's §3.2 error."""

    def test_noise_documents_are_candidates(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        _put_doc(registry, "good", signal_quality="high")
        report = resolve_candidates(RetentionCriteria(noise_documents=True), registry)
        assert [c.item_id for c in report.candidates] == ["noisy"]
        assert report.candidates[0].kind == "document"
        assert report.candidates[0].reason_code == "noise_document"

    def test_untagged_documents_are_not_candidates(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "plain")
        report = resolve_candidates(RetentionCriteria(noise_documents=True), registry)
        assert report.candidates == []

    def test_noise_is_not_gated_by_grace_period(self, registry: StoreRegistry) -> None:
        """A noise tag is a verdict, not an age.

        The 24 captures that motivated this were demoted the day before the
        prune. A grace period on this criterion would have made them
        unarchivable for a month — so a freshly-written noise document must
        be a candidate even with a long grace period configured.
        """
        _put_doc(registry, "fresh_noise", signal_quality="noise")
        report = resolve_candidates(
            RetentionCriteria(noise_documents=True, older_than_days=3650), registry
        )
        assert [c.item_id for c in report.candidates] == ["fresh_noise"]

    def test_criteria_off_by_default_resolves_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        report = resolve_candidates(RetentionCriteria(), registry)
        assert report.candidates == []

    def test_max_items_caps_the_batch(self, registry: StoreRegistry) -> None:
        for i in range(5):
            _put_doc(registry, f"n{i}", signal_quality="noise")
        report = resolve_candidates(
            RetentionCriteria(noise_documents=True, max_items=2), registry
        )
        assert len(report.candidates) == 2


class TestEntityResolution:
    def test_unconfirmed_mint_past_grace_is_a_candidate(
        self, registry: StoreRegistry
    ) -> None:
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id=None,
            node_type="person",
            properties={
                "name": "Mentioned Person",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
            },
        )
        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=0), registry
        )
        assert [c.item_id for c in report.candidates] == [node_id]
        assert report.candidates[0].reason_code == "unconfirmed_mint"

    def test_unconfirmed_mint_inside_grace_is_not_a_candidate(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.graph_store.upsert_node(
            node_id=None,
            node_type="person",
            properties={
                "name": "Recent Mint",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
            },
        )
        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30), registry
        )
        assert report.candidates == []

    def test_confirmed_entity_is_never_a_candidate(
        self, registry: StoreRegistry
    ) -> None:
        """Confirmation is a human's judgment; age is not evidence against it."""
        registry.knowledge.graph_store.upsert_node(
            node_id=None,
            node_type="person",
            properties={
                "name": "Real Person",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_CONFIRMED,
            },
        )
        report = resolve_candidates(
            RetentionCriteria(
                unconfirmed_mints=True,
                lifecycle_states=["deprecated"],
                older_than_days=0,
            ),
            registry,
        )
        assert report.candidates == []
        assert report.skipped_confirmed == 1


class TestArchival:
    def test_dry_run_writes_nothing(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        result = build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=True)
        )
        assert result.status == CommandStatus.SUCCESS
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert LIFECYCLE_KEY not in (doc.get("metadata") or {})

    def test_dry_run_is_the_default_when_arg_omitted(
        self, registry: StoreRegistry
    ) -> None:
        """Destructive-by-default is wrong for a predicate-driven batch."""
        _put_doc(registry, "noisy", signal_quality="noise")
        build_curate_executor(registry).execute(
            Command(
                operation=Operation.RETENTION_PRUNE,
                args={"criteria": {"noise_documents": True}, "reason": "r"},
            )
        )
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert LIFECYCLE_KEY not in (doc.get("metadata") or {})

    def test_apply_archives_the_document(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        result = build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        assert result.status == CommandStatus.SUCCESS
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

    def test_archival_preserves_content_and_other_metadata(
        self, registry: StoreRegistry
    ) -> None:
        """Archival is not deletion — the content must survive."""
        _put_doc(registry, "noisy", signal_quality="noise", title="Keep me")
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["content"] == "content of noisy"
        assert doc["metadata"]["title"] == "Keep me"

    def test_reason_is_recorded_on_the_lifecycle_record(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, reason="job-desc noise", dry_run=False)
        )
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["deprecation_reason"] == "job-desc noise"

    def test_rerun_is_a_no_op(self, registry: StoreRegistry) -> None:
        """Already-archived items are filtered during resolution."""
        _put_doc(registry, "noisy", signal_quality="noise")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        report = resolve_candidates(RetentionCriteria(noise_documents=True), registry)
        assert report.candidates == []
        assert report.skipped_already_archived == 1

    def test_entity_archival_creates_a_new_scd2_version(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None,
            node_type="person",
            properties={
                "name": "Stale Mint",
                EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
            },
        )
        build_curate_executor(registry).execute(
            _prune({"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False)
        )
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert len(graph.get_node_history(node_id)) == 2


class TestAuditTrail:
    def test_emits_retention_pruned(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["dry_run"] is False
        assert payload["phase"] == "archival"
        assert payload["archived"] == 1
        assert payload["by_reason"] == {"noise_document": 1}
        assert payload["item_ids"] == ["noisy"]

    def test_dry_run_also_emits_flagged(self, registry: StoreRegistry) -> None:
        """A preview must be as auditable as the real thing."""
        _put_doc(registry, "noisy", signal_quality="noise")
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=True)
        )
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert len(events) == 1
        assert events[0].payload["dry_run"] is True
        assert events[0].payload["archived"] == 0
        assert events[0].payload["candidates"] == 1

    def test_reason_required(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValidationError) as exc:
            RetentionPruneHandler(registry).handle(
                _prune({"noise_documents": True}, reason="   ")
            )
        assert exc.value.code == "retention_reason_required"

    def test_reason_length_capped(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValidationError) as exc:
            RetentionPruneHandler(registry).handle(
                _prune(
                    {"noise_documents": True},
                    reason="x" * (MAX_RETENTION_REASON_CHARS + 1),
                )
            )
        assert exc.value.code == "retention_reason_too_long"

    def test_non_dry_run_refuses_null_event_log(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The event is the only record the archival happened."""
        monkeypatch.setattr(
            type(registry.operational),
            "event_log",
            property(lambda _self: NullEventLog()),
        )
        with pytest.raises(ValidationError) as exc:
            RetentionPruneHandler(registry).handle(
                _prune({"noise_documents": True}, dry_run=False)
            )
        assert exc.value.code == "retention_requires_event_log"

    def test_dry_run_tolerates_null_event_log(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            type(registry.operational),
            "event_log",
            property(lambda _self: NullEventLog()),
        )
        _, message = RetentionPruneHandler(registry).handle(
            _prune({"noise_documents": True}, dry_run=True)
        )
        assert "dry run" in message


class TestHardExclusions:
    def test_traces_are_structurally_unreachable(self, registry: StoreRegistry) -> None:
        """Traces are immutable — the resolver must not read the trace store."""
        import inspect

        from trellis.mutate import retention

        source = inspect.getsource(retention)
        assert "trace_store" not in source

    def test_event_log_is_not_a_candidate_source(self, registry: StoreRegistry) -> None:
        import inspect

        from trellis.mutate import retention

        source = inspect.getsource(retention)
        assert "event_log" not in source


class TestRetrievalExclusion:
    def test_is_archived_reads_the_lifecycle_key(self) -> None:
        assert is_archived({LIFECYCLE_KEY: {"state": ARCHIVED_STATE}}) is True
        assert is_archived({LIFECYCLE_KEY: {"state": "current"}}) is False
        assert is_archived({}) is False
        assert is_archived(None) is False

    def test_malformed_lifecycle_reads_as_not_archived(self) -> None:
        """Fail open: a bad record must never silently shrink a pack."""
        assert is_archived({LIFECYCLE_KEY: "archived"}) is False
        assert is_archived({LIFECYCLE_KEY: None}) is False

    def test_exclude_archived_drops_only_archived_items(self) -> None:
        keep = PackItem(
            item_id="keep", item_type="document", excerpt="x", relevance_score=1.0
        )
        drop = PackItem(
            item_id="drop",
            item_type="document",
            excerpt="y",
            relevance_score=1.0,
            metadata={LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        assert [i.item_id for i in exclude_archived([keep, drop])] == ["keep"]

    def test_archived_document_leaves_the_pack(self, registry: StoreRegistry) -> None:
        """End-to-end: archival must actually stop retrieval serving it."""
        from trellis.retrieve.pack_builder import PackBuilder
        from trellis.retrieve.strategies import KeywordSearch

        registry.knowledge.document_store.put(
            "target", "distinctive kangaroo content", {"title": "t"}
        )
        strategy = KeywordSearch(registry.knowledge.document_store)
        builder = PackBuilder(strategies=[strategy])
        before = builder.build(intent="kangaroo")
        assert any(i.item_id == "target" for i in before.items)

        doc = registry.knowledge.document_store.get("target")
        assert doc is not None
        metadata = dict(doc["metadata"])
        metadata[LIFECYCLE_KEY] = {"state": ARCHIVED_STATE}
        registry.knowledge.document_store.put("target", doc["content"], metadata)

        after = builder.build(intent="kangaroo")
        assert not any(i.item_id == "target" for i in after.items)


class TestRestore:
    """The half that makes phase-one archival's reversibility claim true.

    Archival is chosen over purge *because* "a wrong prune is walked back by
    re-stamping". Re-stamping needs a governed path — direct store writes are
    not an option — so without this verb an over-prune had no remedy.
    """

    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        assert Operation.RETENTION_RESTORE in create_curate_handlers(registry)

    def test_round_trip_restores_the_document(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))

        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

        result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["noisy"], "reason": "mis-tagged by demote loop"},
            )
        )
        assert result.status == CommandStatus.SUCCESS
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == "current"

    def test_restored_document_is_servable_again(self, registry: StoreRegistry) -> None:
        from trellis.retrieve.pack_builder import PackBuilder
        from trellis.retrieve.strategies import KeywordSearch

        registry.knowledge.document_store.put(
            "target", "distinctive wombat content", {"title": "t"}
        )
        strategy = KeywordSearch(registry.knowledge.document_store)
        builder = PackBuilder(strategies=[strategy])

        doc = registry.knowledge.document_store.get("target")
        assert doc is not None
        metadata = dict(doc["metadata"])
        metadata[LIFECYCLE_KEY] = {"state": ARCHIVED_STATE}
        registry.knowledge.document_store.put("target", doc["content"], metadata)
        assert not any(
            i.item_id == "target" for i in builder.build(intent="wombat").items
        )

        build_curate_executor(registry).execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["target"], "reason": "restored"},
            )
        )
        assert any(i.item_id == "target" for i in builder.build(intent="wombat").items)

    def test_non_archived_id_is_skipped_not_raised(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "plain")
        result = build_curate_executor(registry).execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["plain", "nonexistent"], "reason": "corrective"},
            )
        )
        assert result.status == CommandStatus.SUCCESS
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert events[0].payload["restored"] == 0
        assert events[0].payload["skipped"] == 2

    def test_emits_restored_event_with_ids(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["noisy"], "reason": "demote loop mis-fired"},
            )
        )
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert len(events) == 1
        assert events[0].payload["restored_ids"] == ["noisy"]
        assert events[0].payload["reason"] == "demote loop mis-fired"

    def test_empty_ids_rejected(self, registry: StoreRegistry) -> None:
        from trellis.mutate.handlers import RetentionRestoreHandler

        with pytest.raises(ValidationError) as exc:
            RetentionRestoreHandler(registry).handle(
                Command(
                    operation=Operation.RETENTION_RESTORE,
                    args={"item_ids": [], "reason": "x"},
                )
            )
        assert exc.value.code == "retention_restore_ids_required"


class TestVectorRowSync:
    """Archival must reach the *vector* row, not just the document store.

    A vector row's metadata is a snapshot taken at embed time and the
    semantic strategy serves from it. An archival written only through
    ``document_store.put`` leaves that path serving the item unchanged —
    ``exclude_archived`` reads ``item.metadata`` and never sees a lifecycle
    key. The first production prune hit exactly this: 35 documents archived,
    35 vector rows still reading ``signal_quality="standard"``, and a
    pack-level test written against ``KeywordSearch`` alone could not see it.
    """

    @staticmethod
    def _embed(registry: StoreRegistry, doc_id: str) -> None:
        registry.knowledge.vector_store.upsert(
            doc_id,
            [0.1, 0.2, 0.3],
            {"excerpt": f"excerpt of {doc_id}", "content_tags": {}},
        )

    def test_archival_stamps_the_vector_row(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        self._embed(registry, "noisy")

        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )

        row = registry.knowledge.vector_store.get("noisy")
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

    def test_archival_preserves_the_embedding_and_other_metadata(
        self, registry: StoreRegistry
    ) -> None:
        """Metadata-only update — nothing is re-embedded."""
        _put_doc(registry, "noisy", signal_quality="noise")
        self._embed(registry, "noisy")

        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )

        row = registry.knowledge.vector_store.get("noisy")
        assert row is not None
        assert row["vector"] == pytest.approx([0.1, 0.2, 0.3])
        assert row["metadata"]["excerpt"] == "excerpt of noisy"

    def test_archived_vector_item_is_excluded_from_a_pack(self) -> None:
        """The synced row is what makes the semantic path honour archival."""
        archived = PackItem(
            item_id="v1",
            item_type="document",
            excerpt="x",
            relevance_score=1.0,
            metadata={
                "source_strategy": "semantic",
                LIFECYCLE_KEY: {"state": ARCHIVED_STATE},
            },
        )
        assert exclude_archived([archived]) == []

    def test_restore_clears_the_vector_row_stamp(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        self._embed(registry, "noisy")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["noisy"], "reason": "mis-demoted"},
            )
        )
        row = registry.knowledge.vector_store.get("noisy")
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == "current"

    def test_missing_vector_row_is_not_an_error(self, registry: StoreRegistry) -> None:
        """Un-embedded documents must still be archivable."""
        _put_doc(registry, "never_embedded", signal_quality="noise")
        result = build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        assert result.status == CommandStatus.SUCCESS
        doc = registry.knowledge.document_store.get("never_embedded")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE


class TestRestoreIsDurable:
    """A restored item must survive the next prune.

    ``retention.restore`` un-archives but does not re-classify — the noise
    tag that selected the item is the classify layer's data and stays on the
    document. Without a guard the very next criteria-driven prune re-archives
    everything a human just rescued, which is exactly the state the first
    production restore left behind: 10 documents restored, all 10 still
    carrying ``signal_quality="noise"``.
    """

    def test_restored_document_survives_a_second_prune(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "rescued", signal_quality="noise")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["rescued"], "reason": "demote loop mis-fired"},
            )
        )

        executor.execute(_prune({"noise_documents": True}, dry_run=False))

        doc = registry.knowledge.document_store.get("rescued")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == "current"

    def test_restored_item_is_reported_as_protected(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "rescued", signal_quality="noise")
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["rescued"], "reason": "r"},
            )
        )
        executor.execute(_prune({"noise_documents": True}, dry_run=False))

        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[-1].payload["skipped_restored"] == 1
        assert events[-1].payload["archived"] == 0

    def test_operator_can_still_archive_a_restored_item_by_name(
        self, registry: StoreRegistry
    ) -> None:
        """Protected from the criteria, not from the operator."""
        _put_doc(registry, "rescued", signal_quality="noise")
        report = resolve_candidates(RetentionCriteria(noise_documents=True), registry)
        assert [c.item_id for c in report.candidates] == ["rescued"]


class TestVectorResyncBackfill:
    """Re-running a prune repairs vector rows stamped before the sync existed."""

    def test_stale_archived_vector_row_is_resynced(
        self, registry: StoreRegistry
    ) -> None:
        # Simulate the pre-fix state: document archived, vector row stale.
        _put_doc(registry, "old", signal_quality="noise", lifecycle_state="archived")
        registry.knowledge.vector_store.upsert(
            "old", [0.1, 0.2, 0.3], {"excerpt": "stale snapshot"}
        )

        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )

        row = registry.knowledge.vector_store.get("old")
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert row["metadata"]["excerpt"] == "stale snapshot"

    def test_resync_count_rides_the_audit_payload(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "old", signal_quality="noise", lifecycle_state="archived")
        registry.knowledge.vector_store.upsert("old", [0.1, 0.2, 0.3], {})
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[-1].payload["vector_rows_resynced"] == 1

    def test_steady_state_rerun_resyncs_nothing(self, registry: StoreRegistry) -> None:
        """An already-correct row is left alone, so a re-run reports zero."""
        _put_doc(registry, "noisy", signal_quality="noise")
        registry.knowledge.vector_store.upsert("noisy", [0.1, 0.2, 0.3], {})
        executor = build_curate_executor(registry)
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[-1].payload["vector_rows_resynced"] == 0

    def test_dry_run_resyncs_nothing(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "old", signal_quality="noise", lifecycle_state="archived")
        registry.knowledge.vector_store.upsert("old", [0.1, 0.2, 0.3], {})
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=True)
        )
        row = registry.knowledge.vector_store.get("old")
        assert row is not None
        assert LIFECYCLE_KEY not in row["metadata"]


class TestArchiveAndRestorePreserveRecency:
    """Neither lifecycle stamp may re-date the row it stamps (#406).

    Why is argued once at each call site — ``RetentionPruneHandler._archive``
    and ``RetentionRestoreHandler._restore`` — and not restated here, so the
    two cannot drift apart.

    What is *not* argued there, because it is a property of another module:
    those comments scope the damage away from ``mutate.retention``'s
    ``lifecycle_states`` age gate, and that scoping rests entirely on
    ``_classify_document`` returning for ``archived`` and ``current`` before
    reaching it — the *order* of three branches. Nothing else in the suite
    goes red if someone moves the gate above them, so it is pinned below
    rather than assumed.
    """

    def test_archive_keeps_the_prior_updated_at(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails against the un-fixed ``_archive``, which re-stamps the row.

        This is the only site whose coverage rests on a single test, so the
        clock binding is asserted rather than trusted: if
        ``fake_document_clock``'s
        module-path patch ever stopped reaching the store under test, a
        preserved stamp would still equal itself and this test would pass
        while covering nothing. The sibling ranking test is the suite-level
        alarm for that, but it is insensitive to the ``_archive`` write.
        """
        docs = registry.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        _put_doc(registry, "noisy", signal_quality="noise")
        before = docs.get("noisy")["updated_at"]
        assert before == (now - timedelta(days=365)).isoformat(), (
            "the fake clock is not reaching the store; this test would pass vacuously"
        )

        clock["now"] = now
        build_curate_executor(registry).execute(
            _prune({"noise_documents": True}, dry_run=False)
        )

        doc = docs.get("noisy")
        assert doc is not None
        # The archival landed...
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        # ...and the row does not claim to have been modified by it.
        assert doc["updated_at"] == before

    def test_restore_keeps_the_prior_updated_at(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails against the un-fixed ``_restore``.

        The archived state is seeded directly rather than by running a prune
        first, which isolates the assertion to the restore write: reverting
        ``_archive`` alone cannot turn this test red, and reverting
        ``_restore`` alone cannot leave it green.
        """
        docs = registry.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        docs.put(
            "stale",
            "a year-old note about widget calibration",
            {"title": "t", LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        before = docs.get("stale")["updated_at"]

        clock["now"] = now
        result = build_curate_executor(registry).execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["stale"], "reason": "prune over-selected"},
            )
        )

        assert result.status == CommandStatus.SUCCESS
        doc = docs.get("stale")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert doc["updated_at"] == before

    def test_restored_document_does_not_outrank_a_fresh_one(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consequence the stamp assertions stand for.

        Un-fixed, the restore stamps the year-old note with its own clock and
        the two come back level — the restore having promoted an item the
        operator meant only to make visible again.

        Why a *margin* rather than an ordering, and why the half-life is
        pinned, are argued once at
        :func:`tests.document_recency.keyword_recency_ratio`.
        """
        docs = registry.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]
        body = "Widget calibration runs at sixty hertz."

        clock["now"] = now - timedelta(days=365)
        docs.put("old-doc", body, {LIFECYCLE_KEY: {"state": ARCHIVED_STATE}})

        clock["now"] = now
        docs.put("new-doc", body, {})
        build_curate_executor(registry).execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["old-doc"], "reason": "prune over-selected"},
            )
        )

        ratio = keyword_recency_ratio(
            docs, "calibration", older="old-doc", fresher="new-doc"
        )
        # Twelve halvings put the decay term near zero, so a correctly-dated
        # year-old row lands at about the floor, 0.30. Anything near 1.0 means
        # the restore re-stamped it.
        assert ratio < 0.5, ratio
        # Demoted, not excluded. ``strategies.RECENCY_FLOOR`` (0.3) is what
        # keeps a restored document servable at all, which is the whole point
        # of restoring it — a restored row scoring ~0 would satisfy the line
        # above and still defeat the operation. Deliberately coupled to that
        # constant in both directions: dropping the floor below 0.25 fails
        # here, and raising it above 0.5 fails the line above. Either should
        # be argued for, not absorbed silently.
        assert ratio > 0.25, ratio

    def test_only_superseded_reaches_the_lifecycle_age_gate(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The premise behind "the retention gate is unreachable from here".

        One consumer of ``updated_at`` is ``_classify_document``'s
        ``lifecycle_states`` age gate, and the two states these handlers write
        return before it — ``archived`` at the already-archived guard,
        ``current`` at the restored guard. So a bumped stamp from either could
        never have changed a prune's selection.

        **That is the only exemption the call-site comments claim**, and the
        precision matters: an earlier draft of this docstring finished the
        sentence "…which is why the source comments above scope the blast
        radius to the keyword axis", and that was false when written.
        ``retrieve.file_context`` reads the same column off
        ``list_documents`` with no lifecycle predicate, so ``_archive`` moves
        ``newest_item_at`` immediately. One consumer being unreachable
        exempts nothing else — and this paragraph drifted out of step with
        the comments it points at inside a single PR, which is the practical
        case for keeping the argument at the call site.

        The exemption itself lives in another function as the *order* of
        three branches. Nothing else in the suite fails if someone moves the
        age gate above them, so the claim would quietly become false — hence
        a test rather than a comment.

        Not a fix test: it passes against the un-fixed code, by design.
        """
        clock = fake_document_clock(monkeypatch)
        clock["now"] = clock["now"] - timedelta(days=365)
        _put_doc(registry, "arch", lifecycle_state=ARCHIVED_STATE)
        _put_doc(registry, "curr", lifecycle_state="current")
        _put_doc(registry, "supr", lifecycle_state="superseded")

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=["archived", "current", "superseded"],
                older_than_days=30,
            ),
            registry,
        )

        assert [c.item_id for c in report.candidates] == ["supr"]
        assert report.skipped_already_archived == 1
        assert report.skipped_restored == 1


class TestLifecycleStateReachability:
    """#419 — every ``LifecycleState`` lands on one side of the age gate.

    ``lifecycle_states`` accepts the whole vocabulary, and phase one
    short-circuits two of its members *before* the age gate. That made
    ``--lifecycle-state archived --older-than-days 90`` an unfalsifiable
    criterion: it returns zero whatever the corpus holds, and nothing in the
    result distinguishes "no rows match" from "no row can ever match".

    The split is now named once (``UNSELECTABLE_STATES``) and pinned here by
    behaviour, exhaustively over ``LifecycleState`` — the two parametrize
    lists partition ``get_args(LifecycleState)`` by construction, so a sixth
    state cannot be added without landing on one side and being asserted
    against it.

    **The issue's other half is wrong, and these tests say so.** #419 argued
    ``draft`` and ``deprecated`` have "zero writers" and should therefore be
    narrowed out. That enumerated ``Lifecycle(...)`` constructions inside
    ``src/`` — but ``lifecycle`` is an ordinary key in an open metadata /
    property bag, and MCP ``save_memory(metadata=...)`` /
    ``save_knowledge(properties=...)`` and the governed ``entity.update``
    property merge all forward a caller's bag verbatim to the store. Both
    states are reachable through surfaces that ship today, and Phase 2 of
    ``adr-tag-vocabulary-split.md`` plans a classifier that writes them from
    inside. Narrowing would have rejected criteria that can genuinely match.
    """

    SELECTABLE = sorted(set(get_args(LifecycleState)) - UNSELECTABLE_STATES)

    def test_the_partition_is_exhaustive_and_disjoint(self) -> None:
        """Guard on the parametrize lists below, not a restatement of them.

        Both lists derive from ``LifecycleState``, so this fails only if
        ``UNSELECTABLE_STATES`` grows a member the enum does not have — at
        which point the "selectable" parametrization would silently stop
        covering it.
        """
        assert set(get_args(LifecycleState)) >= UNSELECTABLE_STATES
        assert set(self.SELECTABLE) | UNSELECTABLE_STATES == set(
            get_args(LifecycleState)
        )

    @pytest.mark.parametrize("state", SELECTABLE)
    def test_a_selectable_state_selects_a_matching_row(
        self,
        state: str,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reachability, for the three states #419 proposed narrowing away.

        Not a fix test: it passes against the un-fixed code, by design. It is
        the evidence for the decision *not* to narrow — the criterion selects
        a real row whose state was written through an ordinary metadata bag,
        so rejecting the value would reject a working query.
        """
        clock = fake_document_clock(monkeypatch)
        clock["now"] = clock["now"] - timedelta(days=365)
        _put_doc(registry, "d", lifecycle_state=state)

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=[state], older_than_days=30),
            registry,
        )

        assert [c.item_id for c in report.candidates] == ["d"]
        assert [c.reason_code for c in report.candidates] == ["lifecycle_stale"]
        assert report.unselectable_lifecycle_states == []

    @pytest.mark.parametrize("state", sorted(UNSELECTABLE_STATES))
    def test_an_unselectable_state_reports_itself_instead_of_scanning(
        self,
        state: str,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero, and the report says the zero came from the criteria.

        Also asserts the scan was skipped: before #419 a request naming only
        unselectable states paged the whole corpus (and every graph node) to
        arrive at a guaranteed empty answer.
        """
        clock = fake_document_clock(monkeypatch)
        clock["now"] = clock["now"] - timedelta(days=365)
        _put_doc(registry, "d", lifecycle_state=state)
        registry.knowledge.graph_store.upsert_node(
            node_id="n", node_type="concept", properties={"name": "n"}
        )

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=[state], older_than_days=30),
            registry,
        )

        assert report.candidates == []
        assert report.unselectable_lifecycle_states == [state]
        assert report.documents_scanned == 0
        assert report.entities_scanned == 0

    def test_a_mixed_request_still_scans_for_the_selectable_half(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting an inert state must not disarm the rest of the criteria."""
        clock = fake_document_clock(monkeypatch)
        clock["now"] = clock["now"] - timedelta(days=365)
        _put_doc(registry, "arch", lifecycle_state=ARCHIVED_STATE)
        _put_doc(registry, "supr", lifecycle_state="superseded")

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=[ARCHIVED_STATE, "superseded"], older_than_days=30
            ),
            registry,
        )

        assert [c.item_id for c in report.candidates] == ["supr"]
        assert report.unselectable_lifecycle_states == [ARCHIVED_STATE]
        assert report.documents_scanned == 2
        assert report.skipped_already_archived == 1

    def test_an_unselectable_state_reaches_the_audit_event_and_the_operator(
        self, registry: StoreRegistry
    ) -> None:
        """A report nobody can read is the silence it was meant to replace."""
        result = build_curate_executor(registry).execute(
            _prune({"lifecycle_states": [ARCHIVED_STATE]})
        )

        assert result.status == CommandStatus.SUCCESS
        assert "cannot be selected by phase one" in (result.message or "")
        assert ARCHIVED_STATE in (result.message or "")

        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert len(events) == 1
        assert events[0].payload["unselectable_lifecycle_states"] == [ARCHIVED_STATE]

    def test_an_ordinary_run_asserts_the_converse(
        self, registry: StoreRegistry
    ) -> None:
        """Empty is a claim, not an absence: every state reached the gate."""
        _put_doc(registry, "noisy", signal_quality="noise")
        result = build_curate_executor(registry).execute(
            _prune({"noise_documents": True, "lifecycle_states": ["deprecated"]})
        )

        assert result.status == CommandStatus.SUCCESS
        assert "cannot be selected" not in (result.message or "")
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[0].payload["unselectable_lifecycle_states"] == []


class TestTheRestoredGuardIsProvenanceIndependent:
    """#419's dangerous half: a docstring asserting an invariant that is false.

    ``_classify_document``'s ``current`` guard used to justify itself with
    "``Lifecycle`` has exactly one writer — retention — so an explicit
    ``state="current"`` can only have come from ``retention.restore``". That
    was false when written: ``mcp.reconcile.mark_document_superseded`` writes
    one, and every surface that forwards a caller's metadata / property bag
    verbatim can write any state at all.

    A false invariant in a comment is what the next call-site author trusts,
    and the repair is not a more careful list of writers — three successive
    enumerations of ``updated_at``'s readers were each wrong (#397, #406, and
    the review pass that found a third). The rule is about the **value**:
    ``current`` is an assertion that the item belongs in service, and
    retention does not override an explicit assertion by inference,
    *whoever* wrote it. These tests pin the rule by behaviour, using writers
    that are not retention, so the guard cannot silently acquire a
    provenance dependency the comment no longer claims.
    """

    def test_a_document_bag_written_outside_retention_is_still_protected(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``save_memory(metadata=...)`` shape — a raw store put.

        Not a fix test: the guard already behaved this way. What changed is
        the *justification*, and behaviour is the only durable place to keep
        it — a comment claiming the guard is safe because retention is the
        only writer cannot be falsified by a test suite, which is how it
        survived being false.
        """
        clock = fake_document_clock(monkeypatch)
        clock["now"] = clock["now"] - timedelta(days=365)
        # Deliberately not via retention.restore: no RETENTION_RESTORED event
        # exists for this row, so provenance cannot be what protects it.
        _put_doc(registry, "d", lifecycle_state="current")
        assert not registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=["current", "superseded"], older_than_days=30
            ),
            registry,
        )

        assert report.candidates == []
        assert report.skipped_restored == 1

    def test_an_entity_update_can_write_any_lifecycle_state(
        self, registry: StoreRegistry
    ) -> None:
        """The falsification, end to end through a governed in-repo verb.

        ``entity.update`` *merges* caller properties onto the node, so a
        state with no ``Lifecycle(...)`` construction anywhere in ``src/``
        reaches the store and is selected by the criterion #419 proposed
        deleting.

        Not a fix test: it passes against the un-fixed code, because the
        thing it refutes is a sentence, not a behaviour.
        """
        executor = build_curate_executor(registry)
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id="e1", node_type="concept", properties={"name": "thing"}
        )
        result = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": {LIFECYCLE_KEY: {"state": "deprecated"}},
                },
            )
        )
        assert result.status == CommandStatus.SUCCESS

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["deprecated"], older_than_days=0),
            registry,
        )

        assert [c.item_id for c in report.candidates] == [node_id]
        assert [c.kind for c in report.candidates] == ["entity"]

    def test_the_same_merge_can_write_the_protected_state(
        self, registry: StoreRegistry
    ) -> None:
        """And the guard holds for it — the rule is symmetric.

        The writer that proves ``deprecated`` is reachable proves ``current``
        is writable by something other than retention, which is exactly what
        the old comment denied.
        """
        executor = build_curate_executor(registry)
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id="e2", node_type="concept", properties={"name": "thing"}
        )
        executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": {LIFECYCLE_KEY: {"state": CURRENT_STATE}},
                },
            )
        )

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=[CURRENT_STATE, "deprecated"], older_than_days=0
            ),
            registry,
        )

        assert report.candidates == []
        assert report.skipped_restored == 1
        assert report.unselectable_lifecycle_states == [CURRENT_STATE]
