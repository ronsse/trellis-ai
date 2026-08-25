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

from pathlib import Path
from typing import Any

import pytest

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
    RetentionCriteria,
    resolve_candidates,
)
from trellis.retrieve.lifecycle import exclude_archived, is_archived
from trellis.schemas.classification import LIFECYCLE_KEY
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

    def test_executor_no_longer_rejects_the_verb(
        self, registry: StoreRegistry
    ) -> None:
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
        report = resolve_candidates(
            RetentionCriteria(noise_documents=True), registry
        )
        assert [c.item_id for c in report.candidates] == ["noisy"]
        assert report.candidates[0].kind == "document"
        assert report.candidates[0].reason_code == "noise_document"

    def test_untagged_documents_are_not_candidates(
        self, registry: StoreRegistry
    ) -> None:
        _put_doc(registry, "plain")
        report = resolve_candidates(
            RetentionCriteria(noise_documents=True), registry
        )
        assert report.candidates == []

    def test_noise_is_not_gated_by_grace_period(
        self, registry: StoreRegistry
    ) -> None:
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
        report = resolve_candidates(
            RetentionCriteria(noise_documents=True), registry
        )
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
            _prune(
                {"unconfirmed_mints": True, "older_than_days": 0}, dry_run=False
            )
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
    def test_traces_are_structurally_unreachable(
        self, registry: StoreRegistry
    ) -> None:
        """Traces are immutable — the resolver must not read the trace store."""
        import inspect

        from trellis.mutate import retention

        source = inspect.getsource(retention)
        assert "trace_store" not in source

    def test_event_log_is_not_a_candidate_source(
        self, registry: StoreRegistry
    ) -> None:
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

    def test_archived_document_leaves_the_pack(
        self, registry: StoreRegistry
    ) -> None:
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
