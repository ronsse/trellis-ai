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

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest

from tests.document_recency import fake_document_clock, keyword_recency_ratio
from tests.unit.mutate._purge_race import redaction, rival_inside_first_read
from trellis.errors import ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate import handlers as handlers_module
from trellis.mutate import retention as retention_module
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
        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"noise_documents": True})
        )
        assert result.status == CommandStatus.SUCCESS
        assert "No handler registered" not in (result.message or "")

    def test_args_schema_requires_criteria_and_reason(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(operation=Operation.RETENTION_PRUNE, args={})
        )
        assert result.status == CommandStatus.REJECTED
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
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
            _prune({"noise_documents": True}, reason="job-desc noise", dry_run=False)
        )
        doc = registry.knowledge.document_store.get("noisy")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["deprecation_reason"] == "job-desc noise"

    def test_rerun_is_a_no_op(self, registry: StoreRegistry) -> None:
        """Already-archived items are filtered during resolution."""
        _put_doc(registry, "noisy", signal_quality="noise")
        executor = build_curate_executor(registry, clearance="restricted")
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
        build_curate_executor(registry, clearance="restricted").execute(
            _prune({"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False)
        )
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert len(graph.get_node_history(node_id)) == 2


class TestAuditTrail:
    def test_emits_retention_pruned(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "noisy", signal_quality="noise")
        build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        before = builder.build(intent="kangaroo", clearance="restricted")
        assert any(i.item_id == "target" for i in before.items)

        doc = registry.knowledge.document_store.get("target")
        assert doc is not None
        metadata = dict(doc["metadata"])
        metadata[LIFECYCLE_KEY] = {"state": ARCHIVED_STATE}
        registry.knowledge.document_store.put("target", doc["content"], metadata)

        after = builder.build(intent="kangaroo", clearance="restricted")
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
        executor = build_curate_executor(registry, clearance="restricted")
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
            i.item_id == "target"
            for i in builder.build(intent="wombat", clearance="restricted").items
        )

        build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": ["target"], "reason": "restored"},
            )
        )
        assert any(
            i.item_id == "target"
            for i in builder.build(intent="wombat", clearance="restricted").items
        )

    def test_non_archived_id_is_skipped_not_raised(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "plain")
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        executor = build_curate_executor(registry, clearance="restricted")
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

    def test_archived_non_precedent_node_restores_to_current(
        self, registry: StoreRegistry
    ) -> None:
        """Pins mutant M6 (gate-876): a mutant that routes ``_restore``'s
        graph-node branch through ``_pre_archival_lifecycle`` for every
        ``node_type``, not only ``"precedent"``, raises here instead of
        restoring -- this node has no SCD-2 history to walk at all, because
        it was minted archived (a deprecated concept extracted straight
        into the archived state), the same shape a legitimately
        never-current non-precedent node can have.
        ``TestNodePurgedMidCommand::test_restore_skips_it`` exercises this
        branch too, but only incidentally -- it is there to pin the purge
        race, and this is the direct case the review asked for.
        """
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None,
            node_type="concept",
            properties={
                "name": "Deprecated widget",
                LIFECYCLE_KEY: {"state": ARCHIVED_STATE},
            },
        )
        assert len(graph.get_node_history(node_id)) == 1

        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={
                    "item_ids": [node_id],
                    "reason": "re-evaluated, still in use",
                },
            )
        )

        assert result.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == CURRENT_STATE

    def test_deprecated_concept_archived_by_a_real_prune_restores_to_current(
        self, registry: StoreRegistry
    ) -> None:
        """The full round trip rather than a node minted straight into
        ``archived``: a concept that genuinely held
        ``"deprecated"`` before ``retention.prune`` archived it comes back
        ``"current"`` on restore, not ``"deprecated"`` -- ``_restore``'s
        ``else`` branch (:func:`~trellis.mutate.handlers._restore`, the
        non-precedent half) sets ``current`` unconditionally and never
        consults ``_pre_archival_lifecycle`` for anything but a precedent,
        per that function's own docstring: "every other node type has no
        'demoted' concept to launder". The node's SCD-2 history is genuine
        here -- v1 ``"deprecated"``, v2 ``"archived"`` -- so this is not the
        degenerate single-version shape
        ``test_archived_non_precedent_node_restores_to_current`` covers.

        Defective subject: route the non-precedent branch through
        ``_pre_archival_lifecycle`` too (over-generalising the precedent
        fix) and this node comes back ``"deprecated"`` instead.
        """
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None,
            node_type="concept",
            properties={
                "name": "Deprecated widget",
                LIFECYCLE_KEY: {"state": "deprecated"},
            },
        )
        assert len(graph.get_node_history(node_id)) == 1

        executor = build_curate_executor(registry, clearance="restricted")
        prune_result = executor.execute(
            _prune(
                {"lifecycle_states": ["deprecated"], "older_than_days": 0},
                dry_run=False,
            )
        )
        assert prune_result.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        # Genuine two-version history: the pre-archival state really was
        # "deprecated", unlike the minted-archived test above.
        # get_node_history() returns newest first, so index 0 is the
        # archived version and index 1 is the pre-archival "deprecated" one.
        history = graph.get_node_history(node_id)
        assert len(history) == 2
        assert history[0]["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert history[1]["properties"][LIFECYCLE_KEY]["state"] == "deprecated"

        restore_result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [node_id], "reason": "re-evaluated, still in use"},
            )
        )

        assert restore_result.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == CURRENT_STATE


class TestRestorePrecedentHistory:
    """Prune-then-restore is not a second, unaudited way to undo a demotion.

    A precedent that was demoted and then archived (``retention.prune``)
    must come back **demoted** when restored, not ``current`` --
    ``RetentionRestoreHandler`` reads the pre-archival state from the
    node's SCD-2 history via ``_pre_archival_lifecycle`` rather than
    assuming ``"current"``. When that history cannot answer the question
    (every version on record reads as archived -- walking back finds no
    non-archived version, whether because there is only one version at
    all or because several consecutive ones carry "archived" forward),
    restore refuses rather than guesses.
    """

    def _node(self, registry: StoreRegistry, properties: dict[str, Any]) -> str:
        return registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="precedent", properties=properties
        )

    def test_restore_returns_a_demoted_precedent_to_demoted(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        precedent_id = self._node(registry, {"name": "Pooling pattern"})
        executor = build_curate_executor(registry, clearance="restricted")

        demote_result = executor.execute(
            Command(
                operation=Operation.PRECEDENT_DEMOTE,
                args={"target_id": precedent_id, "reason": "superseded"},
                target_id=precedent_id,
                target_type="precedent",
                requested_by="cli:demote-precedent",
            )
        )
        assert demote_result.status == CommandStatus.SUCCESS

        prune_result = executor.execute(
            _prune(
                {"lifecycle_states": ["demoted"], "older_than_days": 0},
                reason="sweeping demoted precedents",
                dry_run=False,
            )
        )
        assert prune_result.status == CommandStatus.SUCCESS
        node = graph.get_node(precedent_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

        restore_result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [precedent_id], "reason": "over-pruned"},
            )
        )
        assert restore_result.status == CommandStatus.SUCCESS
        node = graph.get_node(precedent_id)
        assert node is not None
        record = node["properties"][LIFECYCLE_KEY]
        # Restored to the state the precedent held before archival --
        # "demoted", not "current" -- so the prune/restore round trip
        # cannot launder the earlier demotion.
        assert record["state"] == "demoted"
        assert record["deprecation_reason"] == "superseded"

    def test_restore_refuses_a_precedent_with_insufficient_history(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        archived = {LIFECYCLE_KEY: {"state": ARCHIVED_STATE}}
        precedent_id = self._node(registry, {"name": "Minted archived", **archived})
        executor = build_curate_executor(registry, clearance="restricted")

        result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [precedent_id], "reason": "corrective"},
            )
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_history_unavailable"
        )
        # Refused, not guessed: the node is left exactly as it was.
        node = graph.get_node(precedent_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

    def test_a_refused_item_does_not_abort_the_rest_of_the_batch(
        self, registry: StoreRegistry
    ) -> None:
        """[ok, refused, ok]: one history-unavailable id in the middle of a
        batch must not cost the other two their restore.

        Before this fix the per-item ``ValidationError`` from
        ``_pre_archival_lifecycle`` escaped the whole loop, so the item
        after the refused one was never even attempted — only the item(s)
        *before* it in iteration order came back restored, and the
        command reported REJECTED even though two thirds of the batch
        actually succeeded.
        """
        graph = registry.knowledge.graph_store
        executor = build_curate_executor(registry, clearance="restricted")

        def demoted_then_pruned(name: str) -> str:
            precedent_id = self._node(registry, {"name": name})
            demote = executor.execute(
                Command(
                    operation=Operation.PRECEDENT_DEMOTE,
                    args={"target_id": precedent_id, "reason": "superseded"},
                    target_id=precedent_id,
                    target_type="precedent",
                    requested_by="cli:demote-precedent",
                )
            )
            assert demote.status == CommandStatus.SUCCESS
            prune = executor.execute(
                _prune(
                    {"lifecycle_states": ["demoted"], "older_than_days": 0},
                    reason="sweeping demoted precedents",
                    dry_run=False,
                )
            )
            assert prune.status == CommandStatus.SUCCESS
            return precedent_id

        first_ok = demoted_then_pruned("Pooling pattern A")
        refused_id = self._node(
            registry,
            {"name": "Minted archived", LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        second_ok = demoted_then_pruned("Pooling pattern B")

        result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={
                    "item_ids": [first_ok, refused_id, second_ok],
                    "reason": "corrective batch",
                },
            )
        )

        # Two of three restored: SUCCESS, not REJECTED.
        assert result.status == CommandStatus.SUCCESS
        assert "refused" in result.message
        assert any(refused_id in w for w in result.warnings)
        assert any(
            "retention_restore_history_unavailable" in w for w in result.warnings
        )

        for ok_id in (first_ok, second_ok):
            node = graph.get_node(ok_id)
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

        refused_node = graph.get_node(refused_id)
        assert refused_node is not None
        assert refused_node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE

        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["restored"] == 2
        assert payload["refused"] == 1
        assert set(payload["restored_ids"]) == {first_ok, second_ok}
        assert payload["refused_ids"] == [refused_id]
        assert payload["refused_items"][0]["item_id"] == refused_id
        assert (
            payload["refused_items"][0]["code"]
            == "retention_restore_history_unavailable"
        )

    def test_rejected_only_when_nothing_in_the_batch_was_restored(
        self, registry: StoreRegistry
    ) -> None:
        """A batch that mixes ``skipped`` and ``refused`` but restores
        nothing at all still rejects — "some ids were merely skipped"
        does not turn a wholly-unactioned batch into a reported success.
        """
        executor = build_curate_executor(registry, clearance="restricted")
        refused_id = self._node(
            registry,
            {"name": "Minted archived", LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        never_archived_id = self._node(registry, {"name": "Never archived"})

        result = executor.execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={
                    "item_ids": [never_archived_id, refused_id],
                    "reason": "corrective batch",
                },
            )
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_history_unavailable"
        )
        assert any(refused_id in w for w in result.warnings)
        # The rejection must name the whole batch, not only the half that
        # was refused -- an operator reading this message needs to see
        # that ``never_archived_id`` was merely skipped, not lost.
        assert refused_id in result.message
        assert never_archived_id in result.message

        # The audit event is emitted unconditionally -- even though the
        # command itself is REJECTED, an all-refused-or-skipped batch
        # still gets a RETENTION_RESTORED row with restored=0: the
        # refused/skipped ids are the only record of what was attempted,
        # and nothing in this codebase branches on the restored count.
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["restored"] == 0
        assert payload["restored_ids"] == []
        assert payload["skipped_ids"] == [never_archived_id]
        assert payload["refused_ids"] == [refused_id]


class TestNodePurgedMidCommand:
    """A purge that commits between a node's read and its rewrite stands.

    The purged id is skipped, as an id that vanished before the read is;
    the bystander in the same command is still written.
    """

    def _node(self, registry: StoreRegistry, properties: dict[str, Any]) -> str:
        return registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="person", properties=properties
        )

    def test_prune_skips_it(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        unconfirmed = {EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED}
        purged = self._node(registry, {"name": "Purged Mint", **unconfirmed})
        kept = self._node(registry, {"name": "Kept Mint", **unconfirmed})
        executor = build_curate_executor(registry, clearance="restricted")
        prune = _prune({"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False)

        with rival_inside_first_read(
            registry, executor, purged, redaction(purged)
        ) as rival:
            result = executor.execute(prune)

        [purge] = rival
        assert purge.status == CommandStatus.SUCCESS
        assert result.status == CommandStatus.SUCCESS
        assert graph.get_node(purged) is None
        assert graph.get_node_history(purged) == []
        node = graph.get_node(kept)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        [event] = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        counts = ("candidates", "archived", "skipped")
        assert [event.payload[key] for key in counts] == [2, 1, 1]

    def test_restore_skips_it(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        archived = {LIFECYCLE_KEY: {"state": ARCHIVED_STATE}}
        purged = self._node(registry, {"name": "Purged", **archived})
        kept = self._node(registry, {"name": "Kept", **archived})
        executor = build_curate_executor(registry, clearance="restricted")
        restore = Command(
            operation=Operation.RETENTION_RESTORE,
            args={"item_ids": [purged, kept], "reason": "corrective"},
        )

        with rival_inside_first_read(
            registry, executor, purged, redaction(purged)
        ) as rival:
            result = executor.execute(restore)

        [purge] = rival
        assert purge.status == CommandStatus.SUCCESS
        assert result.status == CommandStatus.SUCCESS
        assert graph.get_node(purged) is None
        assert graph.get_node_history(purged) == []
        node = graph.get_node(kept)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == CURRENT_STATE
        [event] = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert event.payload["restored_ids"] == [kept]
        assert event.payload["skipped_ids"] == [purged]


class TestRetentionRacingAnotherWrite:
    """A write committed between a node's read and its rewrite is kept.

    The rewrite loses its compare-and-set, re-reads the node and rebuilds
    from the version the rival wrote.
    """

    def test_prune_archives_the_updated_version(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        unconfirmed = {EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED}
        node_id = graph.upsert_node(
            node_id=None, node_type="person", properties={"name": "M", **unconfirmed}
        )
        executor = build_curate_executor(registry, clearance="restricted")
        update = Command(
            operation=Operation.ENTITY_UPDATE,
            args={"entity_id": node_id, "properties": {"note": "kept"}},
        )
        prune = _prune({"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False)

        with rival_inside_first_read(registry, executor, node_id, update) as rival:
            result = executor.execute(prune)

        assert [r.status for r in rival] == [CommandStatus.SUCCESS]
        assert result.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"]["note"] == "kept"
        assert node["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert len(graph.get_node_history(node_id)) == 3

    def test_restore_skips_a_node_already_restored(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None,
            node_type="person",
            properties={"name": "Raced", LIFECYCLE_KEY: {"state": ARCHIVED_STATE}},
        )
        executor = build_curate_executor(registry, clearance="restricted")

        def restore() -> Command:
            return Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [node_id], "reason": "corrective"},
            )

        with rival_inside_first_read(registry, executor, node_id, restore()) as rival:
            loser = executor.execute(restore())

        [winner] = rival
        assert winner.status == CommandStatus.SUCCESS
        assert loser.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == CURRENT_STATE
        assert len(graph.get_node_history(node_id)) == 2
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        ids = {
            e.payload["command_id"]: (
                e.payload["restored_ids"],
                e.payload["skipped_ids"],
            )
            for e in events
        }
        assert ids == {
            winner.command_id: ([node_id], []),
            loser.command_id: ([], [node_id]),
        }


class TestANodeThatNeverSettlesFailsTheRun:
    """The run fails, and the event still records what it wrote first.

    Items written before the failure stay written, and the event is the
    only record of them.
    """

    def _node(self, registry: StoreRegistry, properties: dict[str, Any]) -> str:
        return registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="person", properties=properties
        )

    def _only_the_first_node_settles(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        graph = registry.knowledge.graph_store
        write = graph.update_node_if_current
        seen: list[str] = []

        def cas(node_id: str, *args: Any, **kwargs: Any) -> bool:
            seen.append(node_id)
            return node_id == seen[0] and write(node_id, *args, **kwargs)

        monkeypatch.setattr(graph, "update_node_if_current", cas)
        return seen

    def test_prune(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        unconfirmed = {EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED}
        mints = {self._node(registry, {"name": n, **unconfirmed}) for n in "AB"}
        seen = self._only_the_first_node_settles(registry, monkeypatch)
        prune = _prune({"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False)

        result = build_curate_executor(registry, clearance="restricted").execute(prune)

        assert result.status == CommandStatus.FAILED
        [unsettled] = mints - {seen[0]}
        assert seen == [seen[0]] + [unsettled] * 5
        written = graph.get_node(seen[0])
        assert written is not None
        assert written["properties"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE
        assert len(graph.get_node_history(unsettled)) == 1
        log = registry.operational.event_log
        [event] = log.get_events(event_type=EventType.RETENTION_PRUNED)
        counts = ("candidates", "archived", "skipped")
        assert [event.payload[key] for key in counts] == [2, 1, 0]
        [rejected] = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert rejected.payload["command_id"] == event.payload["command_id"]
        assert rejected.payload["status"] == "failed"

    def test_restore(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        archived = {LIFECYCLE_KEY: {"state": ARCHIVED_STATE}}
        first = self._node(registry, {"name": "First", **archived})
        second = self._node(registry, {"name": "Second", **archived})
        seen = self._only_the_first_node_settles(registry, monkeypatch)
        restore = Command(
            operation=Operation.RETENTION_RESTORE,
            args={"item_ids": [first, second], "reason": "corrective"},
        )

        result = build_curate_executor(registry, clearance="restricted").execute(
            restore
        )

        assert result.status == CommandStatus.FAILED
        assert seen == [first] + [second] * 5
        states = []
        for node_id in (first, second):
            node = graph.get_node(node_id)
            assert node is not None
            states.append(node["properties"][LIFECYCLE_KEY]["state"])
        assert states == [CURRENT_STATE, ARCHIVED_STATE]
        log = registry.operational.event_log
        [event] = log.get_events(event_type=EventType.RETENTION_RESTORED)
        assert event.payload["restored_ids"] == [first]
        assert event.payload["skipped_ids"] == []
        [rejected] = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert rejected.payload["command_id"] == event.payload["command_id"]
        assert rejected.payload["status"] == "failed"


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

        build_curate_executor(registry, clearance="restricted").execute(
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

        build_curate_executor(registry, clearance="restricted").execute(
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
        executor = build_curate_executor(registry, clearance="restricted")
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
        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"noise_documents": True}, dry_run=False)
        )
        assert result.status == CommandStatus.SUCCESS
        doc = registry.knowledge.document_store.get("never_embedded")
        assert doc is not None
        assert doc["metadata"][LIFECYCLE_KEY]["state"] == ARCHIVED_STATE


class TestVectorRowSyncOnGraphNodeRestore:
    """``RetentionRestoreHandler``'s graph-node branch has no document write
    to piggyback a vector mirror onto the way the document branch above
    gets one for free from ``put_document`` -- it must re-stamp the vector
    row directly, the same gap ``PrecedentDemoteHandler`` and
    ``PrecedentReinstateHandler`` close with the same
    ``_sync_vector_lifecycle`` call. Without it, a restored node's vector
    row keeps reading ``archived`` and the semantic path keeps withholding
    an item ``get_node`` already reports as current again.

    End to end: a graph node is minted archived and embedded, restored,
    and run through the real collect seam -- the same ``SemanticSearch``
    strategy and ``PackBuilder`` every production surface uses
    (:func:`~trellis.retrieve.builder_factory.build_strategies`), not a
    hand-authored ``PackItem`` standing in for what the strategy would
    have produced.
    """

    @staticmethod
    def _embed(registry: StoreRegistry, node_id: str) -> None:
        registry.knowledge.vector_store.upsert(
            node_id,
            [0.1, 0.2, 0.3],
            {"excerpt": f"excerpt of {node_id}", "content_tags": {}},
        )

    def test_restore_re_syncs_the_vector_row_and_the_seam_serves_it_again(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: remove the ``_sync_vector_lifecycle(...)``
        call from ``RetentionRestoreHandler._restore``'s graph-node branch
        and the vector row below keeps reading ``archived`` after restore
        -- the real collect seam would then keep withholding a node that
        ``get_node`` already reports as current again.
        """
        from trellis.retrieve.builder_factory import build_strategies
        from trellis.retrieve.pack_builder import PackBuilder

        graph = registry.knowledge.graph_store
        node_id = graph.upsert_node(
            node_id=None,
            node_type="concept",
            properties={
                "name": "Deprecated widget",
                LIFECYCLE_KEY: {"state": ARCHIVED_STATE},
            },
        )
        self._embed(registry, node_id)

        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [node_id], "reason": "re-evaluated, still in use"},
            )
        )
        assert result.status == CommandStatus.SUCCESS

        row = registry.knowledge.vector_store.get(node_id)
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == CURRENT_STATE
        # Metadata-only: nothing was re-embedded.
        assert row["vector"] == pytest.approx([0.1, 0.2, 0.3])

        strategies = build_strategies(
            registry,  # type: ignore[arg-type]
            embedding_fn=lambda _text: [0.1, 0.2, 0.3],
        ).strategies
        semantic = next(s for s in strategies if s.name == "semantic")
        builder = PackBuilder(strategies=[semantic])
        pack = builder.build(intent="deprecated widget", clearance="restricted")
        assert node_id in [item.item_id for item in pack.items]


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
        executor = build_curate_executor(registry, clearance="restricted")
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
        executor = build_curate_executor(registry, clearance="restricted")
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

        build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        executor = build_curate_executor(registry, clearance="restricted")
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        executor.execute(_prune({"noise_documents": True}, dry_run=False))
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[-1].payload["vector_rows_resynced"] == 0

    def test_dry_run_resyncs_nothing(self, registry: StoreRegistry) -> None:
        _put_doc(registry, "old", signal_quality="noise", lifecycle_state="archived")
        registry.knowledge.vector_store.upsert("old", [0.1, 0.2, 0.3], {})
        build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        build_curate_executor(registry, clearance="restricted").execute(
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
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        result = build_curate_executor(registry, clearance="restricted").execute(
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
        executor = build_curate_executor(registry, clearance="restricted")
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
        executor = build_curate_executor(registry, clearance="restricted")
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


class TestEntityScanPaging:
    """The entity scan pages the graph, so ``max_items`` bounds what is
    *selected*, not what is *looked at*.

    The scan once asked the graph for its newest ``max_items`` rows and only
    then dropped the archived, the too-young and the wrong-state ones, so
    those rows used up the window: a second prune pass over the same graph
    archived nothing, and the report read ``scan_truncated=False`` beside
    eligible rows it had never reached.
    """

    @staticmethod
    def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        now = datetime.now(UTC)
        holder: dict[str, Any] = {"base": now, "now": now}
        monkeypatch.setattr(
            "trellis.stores.sqlite.graph.utc_now", lambda: holder["now"]
        )
        return holder

    @staticmethod
    def _node(
        registry: StoreRegistry,
        clock: dict[str, Any],
        node_id: str,
        *,
        age_days: int,
        mint: bool = False,
        state: str | None = None,
    ) -> str:
        """Seed one node stamped ``age_days`` old (older sorts later)."""
        clock["now"] = clock["base"] - timedelta(days=age_days)
        props: dict[str, Any] = {"name": node_id}
        if mint:
            props[EXTRACTION_STATUS_PROPERTY] = EXTRACTION_STATUS_UNCONFIRMED
        if state is not None:
            props[LIFECYCLE_KEY] = {"state": state}
        return registry.knowledge.graph_store.upsert_node(
            node_id=node_id, node_type="concept", properties=props
        )

    @staticmethod
    def _is_archived_node(registry: StoreRegistry, node_id: str) -> bool:
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        return (node["properties"].get(LIFECYCLE_KEY) or {}).get(
            "state"
        ) == ARCHIVED_STATE

    def test_unconfirmed_mint_prune_makes_progress_across_passes(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = self._clock(monkeypatch)
        ids = [
            self._node(registry, clock, f"m{i}", age_days=90 + i, mint=True)
            for i in range(6)
        ]
        executor = build_curate_executor(registry, clearance="restricted")
        criteria = {"unconfirmed_mints": True, "older_than_days": 30, "max_items": 2}

        for done in (2, 4, 6):
            result = executor.execute(_prune(criteria, dry_run=False))
            assert result.status == CommandStatus.SUCCESS, result.message
            archived = [n for n in ids if self._is_archived_node(registry, n)]
            # ``max_items`` still bounds what one pass archives.
            assert len(archived) == done

    def test_old_mints_reached_when_newest_window_is_young(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = self._clock(monkeypatch)
        for i in range(2):
            self._node(registry, clock, f"young{i}", age_days=1 + i, mint=True)
        for i in range(3):
            self._node(registry, clock, f"old{i}", age_days=90 + i, mint=True)

        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30, max_items=2),
            registry,
        )

        assert [c.item_id for c in report.candidates] == ["old0", "old1"]
        assert report.scan_truncated is False

    def test_lifecycle_entity_beyond_first_window_reached(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = self._clock(monkeypatch)
        for i in range(3):
            self._node(registry, clock, f"fresh{i}", age_days=1 + i)
        self._node(registry, clock, "stale", age_days=90, state="deprecated")

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=["deprecated"], older_than_days=30, max_items=3
            ),
            registry,
        )

        assert [(c.item_id, c.reason_code) for c in report.candidates] == [
            ("stale", "lifecycle_stale")
        ]

    def test_fewer_eligible_than_max_items_is_a_complete_scan(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = self._clock(monkeypatch)
        self._node(registry, clock, "young", age_days=1, mint=True)
        for i in range(3):
            self._node(registry, clock, f"old{i}", age_days=90 + i, mint=True)
        self._node(registry, clock, "plain", age_days=200)

        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30, max_items=10),
            registry,
        )

        assert len(report.candidates) == 3
        assert report.scan_truncated is False
        # The four mints; a mint-only pass never reads the plain node.
        assert report.entities_scanned == 4

    def test_a_mint_scan_counts_skips_only_for_mints(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Walking every node must not widen what a mint-only pass reports.

        The store used to answer the mint predicate itself, so an archived
        non-mint never reached the counters; the walk sees it and has to drop
        it unremarked.
        """
        clock = self._clock(monkeypatch)
        self._node(registry, clock, "other", age_days=90, state=ARCHIVED_STATE)
        self._node(registry, clock, "kept", age_days=91, state=CURRENT_STATE)
        self._node(registry, clock, "m0", age_days=92, mint=True)

        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30), registry
        )

        assert [c.item_id for c in report.candidates] == ["m0"]
        assert report.skipped_already_archived == 0
        assert report.skipped_restored == 0

    @pytest.mark.parametrize(
        ("nodes", "truncated"), [(4, False), (5, True)], ids=["exactly-cap", "past-cap"]
    )
    @pytest.mark.parametrize(
        "criteria",
        [{"unconfirmed_mints": True}, {"lifecycle_states": ["deprecated"]}],
        ids=["mint-only", "lifecycle"],
    )
    def test_the_cap_reports_only_when_rows_were_left_unread(
        self,
        criteria: dict[str, Any],
        nodes: int,
        truncated: bool,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 4)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 3)
        clock = self._clock(monkeypatch)
        for i in range(nodes):
            self._node(
                registry, clock, f"y{i}", age_days=1 + i, mint=True, state="deprecated"
            )

        report = resolve_candidates(
            RetentionCriteria(older_than_days=30, **criteria), registry
        )

        assert report.candidates == []
        assert report.entities_scanned == min(nodes, 4)
        assert report.scan_truncated is truncated

    def test_a_cap_that_hides_eligible_rows_says_so_to_the_operator(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 3)
        monkeypatch.setattr(handlers_module, "MAX_ENTITIES_SCANNED", 3)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        clock = self._clock(monkeypatch)
        for i in range(3):
            self._node(registry, clock, f"y{i}", age_days=1 + i, mint=True)
        self._node(registry, clock, "old", age_days=90, mint=True)

        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"unconfirmed_mints": True, "older_than_days": 30})
        )

        assert result.status == CommandStatus.SUCCESS
        assert "scan capped at 3 entities" in (result.message or "")
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[0].payload["scan_truncated"] is True
        assert events[0].payload["scan_truncated_planes"] == ["entity"]

    def test_dry_run_reads_only(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = self._clock(monkeypatch)
        for i in range(3):
            self._node(registry, clock, f"m{i}", age_days=90 + i, mint=True)

        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"unconfirmed_mints": True, "older_than_days": 30, "max_items": 2})
        )

        assert result.status == CommandStatus.SUCCESS
        graph = registry.knowledge.graph_store
        assert [len(graph.get_node_history(f"m{i}")) for i in range(3)] == [1, 1, 1]
        assert not any(self._is_archived_node(registry, f"m{i}") for i in range(3))

    def test_a_row_deleted_between_pages_does_not_hide_the_next_one(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An offset walk over a list that shrinks skips the row that slid up.

        Page one reads the two newest; deleting one of them before page two
        moves every later row up a place, so a plain ``offset += len(page)``
        resumes one row too far on.
        """
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        clock = self._clock(monkeypatch)
        ids = [
            self._node(registry, clock, f"m{i}", age_days=90 + i, mint=True)
            for i in range(6)
        ]
        graph = registry.knowledge.graph_store
        real = graph.search_nodes
        calls: list[int] = []

        def shrinking(**kwargs: Any) -> Any:
            calls.append(kwargs["offset"])
            if len(calls) == 2:
                assert graph.delete_node("m0")
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", shrinking)

        # A lifecycle state is what sends the pass through the walk; a
        # mint-only pass reads the mints in one query instead.
        report = resolve_candidates(
            RetentionCriteria(
                unconfirmed_mints=True,
                lifecycle_states=["deprecated"],
                older_than_days=30,
            ),
            registry,
        )

        # ``m0`` was selected from page one before it was deleted; ``m2`` is
        # the row a plain offset walk would have stepped over.
        assert sorted(c.item_id for c in report.candidates) == sorted(ids)
        assert len(calls) > 2
        # Each node counts once, though the walk re-reads rows after the
        # step-back.
        assert report.entities_scanned == len(ids)

    def test_rows_archived_between_pages_do_not_move_the_walk(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Archiving keeps a node current and keeps its place in the order."""
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        clock = self._clock(monkeypatch)
        for i in range(6):
            self._node(registry, clock, f"m{i}", age_days=90 + i, mint=True)
        graph = registry.knowledge.graph_store
        real = graph.search_nodes
        calls: list[int] = []

        def archiving(**kwargs: Any) -> Any:
            calls.append(kwargs["offset"])
            if len(calls) == 2:
                # One row already read, one not yet reached.
                for node_id in ("m0", "m4"):
                    node = graph.get_node(node_id)
                    assert node is not None
                    graph.upsert_node(
                        node_id=node_id,
                        node_type="concept",
                        properties={
                            **node["properties"],
                            LIFECYCLE_KEY: {"state": ARCHIVED_STATE},
                        },
                    )
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", archiving)

        # A lifecycle state is what sends the pass through the walk; a
        # mint-only pass reads the mints in one query instead.
        report = resolve_candidates(
            RetentionCriteria(
                unconfirmed_mints=True,
                lifecycle_states=["deprecated"],
                older_than_days=30,
            ),
            registry,
        )

        # ``m0`` was selected from page one before it was archived; ``m4``
        # was archived before its page was read.
        assert sorted(c.item_id for c in report.candidates) == [
            "m0",
            "m1",
            "m2",
            "m3",
            "m5",
        ]
        assert report.skipped_already_archived == 1

    @pytest.mark.parametrize(
        ("page", "nodes", "deleted"),
        [(2, 3, 1), (2, 4, 2), (500, 501, 1)],
        ids=["one-past-a-page", "two-deleted", "501-nodes"],
    )
    def test_a_deletion_at_the_end_of_the_graph_leaves_no_row_unread(
        self,
        page: int,
        nodes: int,
        deleted: int,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The last read can come back empty and still owe the walk a row.

        With ``page + 1`` nodes the second read starts at the very end. A node
        deleted behind the cursor between the two reads moves the last row up
        into the page already read, so the second read is empty, and its total
        is what shows that one row is still unread.
        """
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", page)
        clock = self._clock(monkeypatch)
        ids = [
            self._node(registry, clock, f"m{i:03d}", age_days=90 + i, mint=True)
            for i in range(nodes)
        ]
        graph = registry.knowledge.graph_store
        real = graph.search_nodes
        calls: list[int] = []

        def shrinking(**kwargs: Any) -> Any:
            calls.append(kwargs["offset"])
            if len(calls) == 2:
                for node_id in ids[:deleted]:
                    assert graph.delete_node(node_id)
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", shrinking)

        # A lifecycle state is what sends the pass through the walk.
        report = resolve_candidates(
            RetentionCriteria(
                unconfirmed_mints=True,
                lifecycle_states=["deprecated"],
                older_than_days=30,
                max_items=1000,
            ),
            registry,
        )

        assert sorted(c.item_id for c in report.candidates) == sorted(ids)
        assert report.entities_scanned == nodes
        assert report.scan_truncated is False

    def test_a_mint_pass_reaches_old_mints_behind_more_plain_nodes_than_the_cap(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plain nodes cost a mint-only pass nothing: the store filters them out.

        Six young plain nodes sit in front of two old mints with a cap of four.
        A walk would spend the cap on the plain ones and stop; the mint read
        never meets them.
        """
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 4)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        clock = self._clock(monkeypatch)
        for i in range(6):
            self._node(registry, clock, f"plain{i}", age_days=1 + i)
        for i in range(2):
            self._node(registry, clock, f"old{i}", age_days=90 + i, mint=True)
        graph = registry.knowledge.graph_store
        query_limits: list[int] = []
        real_query = graph.query

        def counting_query(**kwargs: Any) -> Any:
            query_limits.append(kwargs["limit"])
            return real_query(**kwargs)

        def no_walk(**kwargs: Any) -> Any:
            pytest.fail("a mint-only pass must not page the graph")

        monkeypatch.setattr(graph, "query", counting_query)
        monkeypatch.setattr(graph, "search_nodes", no_walk)

        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30), registry
        )

        assert [c.item_id for c in report.candidates] == ["old0", "old1"]
        assert report.scan_truncated is False
        assert report.entities_scanned == 2
        # One read, one row past the cap so exactly-cap reads as complete.
        assert query_limits == [5]

    def test_a_lifecycle_pass_behind_more_nodes_than_the_cap_says_it_was_cut_off(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same graph as above, asked for a lifecycle state, is not reached.

        The newest rows use the cap first and the store cannot filter on
        lifecycle, so the pass reports the cut instead of a clean zero.
        """
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 4)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        clock = self._clock(monkeypatch)
        for i in range(6):
            self._node(registry, clock, f"plain{i}", age_days=1 + i)
        self._node(registry, clock, "old", age_days=90, state="deprecated")
        graph = registry.knowledge.graph_store
        real = graph.search_nodes
        limits: list[int] = []

        def recording(**kwargs: Any) -> Any:
            limits.append(kwargs["limit"])
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", recording)

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["deprecated"], older_than_days=30),
            registry,
        )

        assert report.candidates == []
        assert report.scan_truncated is True
        assert report.scan_truncated_planes == ["entity"]
        # The cap ends the walk before a read that could return nothing.
        assert limits == [2, 2]

    @pytest.mark.parametrize(
        "criteria",
        [{"unconfirmed_mints": True}, {"lifecycle_states": ["deprecated"]}],
        ids=["mint-only", "lifecycle"],
    )
    def test_max_items_bounds_what_one_read_finds(
        self,
        criteria: dict[str, Any],
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Four eligible rows arrive together; the pass keeps only the first two."""
        clock = self._clock(monkeypatch)
        for i in range(4):
            self._node(
                registry, clock, f"m{i}", age_days=90 + i, mint=True, state="deprecated"
            )

        report = resolve_candidates(
            RetentionCriteria(older_than_days=30, max_items=2, **criteria), registry
        )

        assert [c.item_id for c in report.candidates] == ["m0", "m1"]
        assert report.scan_truncated is False

    def test_more_mints_than_the_cap_reports_truncated_on_the_entity_plane(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 3)
        clock = self._clock(monkeypatch)
        for i in range(5):
            self._node(registry, clock, f"m{i}", age_days=90 + i, mint=True)

        report = resolve_candidates(
            RetentionCriteria(unconfirmed_mints=True, older_than_days=30), registry
        )

        assert [c.item_id for c in report.candidates] == ["m0", "m1", "m2"]
        assert report.entities_scanned == 3
        assert report.scan_truncated is True
        assert report.scan_truncated_planes == ["entity"]

    @pytest.mark.parametrize(
        "props",
        [{"mint": True}, {}, {"state": "superseded"}],
        ids=["old-mint", "old-plain-node", "other-state"],
    )
    def test_a_lifecycle_only_pass_selects_nothing_it_did_not_name(
        self,
        props: dict[str, Any],
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each criterion selects on its own terms, and only when it was asked for.

        An old mint is a candidate of ``unconfirmed_mints`` and an old node
        with no state is a candidate of nothing; neither is a
        ``deprecated`` node, so a pass naming only ``deprecated`` leaves both,
        and a node in some other state, alone. The named one beside them shows
        the pass was selecting at all.
        """
        clock = self._clock(monkeypatch)
        self._node(registry, clock, "other", age_days=90, **props)
        self._node(registry, clock, "named", age_days=91, state="deprecated")

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["deprecated"], older_than_days=30),
            registry,
        )

        assert [(c.item_id, c.reason_code) for c in report.candidates] == [
            ("named", "lifecycle_stale")
        ]
        assert report.entities_scanned == 2

    def test_an_oscillating_total_cannot_loop_the_walk(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store whose total keeps falling and rising is stopped by the budget.

        Each fall steps the cursor back and each rise lets it advance again, so
        the walk revisits the same rows. Only the read budget ends it, and it
        reports the cut. The fake raises rather than looping so a regression
        fails here instead of hanging.
        """
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 20)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 3)
        budget = 4 * 20 // 3
        clock = self._clock(monkeypatch)
        for i in range(10):
            self._node(registry, clock, f"m{i}", age_days=90 + i, state="deprecated")
        graph = registry.knowledge.graph_store
        real = graph.search_nodes
        calls: list[int] = []

        def oscillating(**kwargs: Any) -> Any:
            calls.append(kwargs["offset"])
            if len(calls) > budget + 1:
                pytest.fail("the walk did not stop at its read budget")
            rows, _ = real(**kwargs)
            return rows, 10 if len(calls) % 2 else 4

        monkeypatch.setattr(graph, "search_nodes", oscillating)

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["deprecated"], older_than_days=30),
            registry,
        )

        assert len(calls) == budget
        assert report.scan_truncated is True
        assert report.scan_truncated_planes == ["entity"]

    def test_an_empty_page_the_total_contradicts_is_not_a_complete_scan(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 3)
        clock = self._clock(monkeypatch)
        for i in range(10):
            self._node(registry, clock, f"m{i}", age_days=90 + i, state="deprecated")
        graph = registry.knowledge.graph_store
        real = graph.search_nodes

        def gives_out(**kwargs: Any) -> Any:
            rows, total = real(**kwargs)
            return ([] if kwargs["offset"] >= 3 else rows), total

        monkeypatch.setattr(graph, "search_nodes", gives_out)

        report = resolve_candidates(
            RetentionCriteria(lifecycle_states=["deprecated"], older_than_days=30),
            registry,
        )

        assert report.entities_scanned == 3
        assert report.scan_truncated is True
        assert report.scan_truncated_planes == ["entity"]

    @pytest.mark.parametrize(
        "criteria",
        [{"unconfirmed_mints": True}, {"lifecycle_states": ["deprecated"]}],
        ids=["mint-only", "lifecycle"],
    )
    def test_documents_that_fill_max_items_leave_no_room_for_an_entity(
        self,
        criteria: dict[str, Any],
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = self._clock(monkeypatch)
        for i in range(3):
            _put_doc(registry, f"d{i}", signal_quality="noise")
        self._node(registry, clock, "old", age_days=90, mint=True, state="deprecated")

        report = resolve_candidates(
            RetentionCriteria(
                noise_documents=True, older_than_days=30, max_items=3, **criteria
            ),
            registry,
        )

        assert [c.kind for c in report.candidates] == ["document"] * 3
        assert report.entities_scanned == 0


class TestScanTruncatedPlanes:
    """A capped scan names the plane it capped, in the payload and the message."""

    @staticmethod
    def _capped_documents(
        registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retention_module, "MAX_DOCUMENTS_SCANNED", 2)
        monkeypatch.setattr(handlers_module, "MAX_DOCUMENTS_SCANNED", 2)
        monkeypatch.setattr(retention_module, "_SCAN_PAGE", 2)
        for i in range(4):
            _put_doc(registry, f"d{i}")

    def test_a_document_cap_is_named_on_the_document_plane(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._capped_documents(registry, monkeypatch)

        report = resolve_candidates(RetentionCriteria(noise_documents=True), registry)
        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"noise_documents": True})
        )

        assert report.scan_truncated is True
        assert report.scan_truncated_planes == ["document"]
        assert "scan capped at 2 documents" in (result.message or "")
        assert "entities" not in (result.message or "")
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[0].payload["scan_truncated_planes"] == ["document"]

    def test_both_planes_are_named_when_both_were_capped(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._capped_documents(registry, monkeypatch)
        monkeypatch.setattr(retention_module, "MAX_ENTITIES_SCANNED", 2)
        monkeypatch.setattr(handlers_module, "MAX_ENTITIES_SCANNED", 2)
        graph = registry.knowledge.graph_store
        for i in range(4):
            graph.upsert_node(
                node_id=f"m{i}",
                node_type="concept",
                properties={
                    "name": f"m{i}",
                    EXTRACTION_STATUS_PROPERTY: EXTRACTION_STATUS_UNCONFIRMED,
                },
            )

        result = build_curate_executor(registry, clearance="restricted").execute(
            _prune({"noise_documents": True, "unconfirmed_mints": True})
        )

        assert "scan capped at 2 documents and 2 entities" in (result.message or "")
        events = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_PRUNED
        )
        assert events[0].payload["scan_truncated_planes"] == ["document", "entity"]
