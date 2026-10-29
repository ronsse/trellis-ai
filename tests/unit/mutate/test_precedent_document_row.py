"""A promoted precedent has a document row, and every lifecycle path keeps it in step.

Keyword retrieval reads document rows and semantic retrieval reads vector rows;
the graph axis reads the node. A promotion that wrote only the node was
reachable through seeds or the recency window alone. These tests pin that the
learning promotion writes a document (and embeds it when that is on), that
demote, reinstate and the redaction scrub carry the row with the node, and that
a withheld precedent stays out of every axis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.classify.feedback import apply_noise_tags
from trellis.classify.pipeline import ClassifierPipeline
from trellis.classify.protocol import ClassificationContext, ClassificationResult
from trellis.classify.refresh import (
    REASON_PRECEDENT,
    reclassify_item,
    reclassify_stale,
)
from trellis.core.document_write import put_document
from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.errors import StoreError
from trellis.learning.scoring import (
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    precedent_document_content,
    resolve_source_excerpt,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor, build_evidence_ingest_command
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.handlers import (
    PrecedentDemoteHandler,
    PrecedentReinstateHandler,
    RedactionApplyHandler,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch, KeywordSearch, SemanticSearch
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

TITLE = "Quokka widget pooling"
DESCRIPTION = "Reuse pooled widgets across requests."
NODE_ID = "precedent://learning/quokka"
COPIED_TEXT = "ZEBRA-QUOKKA confidential sentence copied from the source"
NAME_TEXT = "NARWHAL-OKAPI first line of the source"
AXES = ("keyword", "semantic", "graph")


def _embed(_text: str) -> list[float]:
    return [1.0, 0.0, 0.0]


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def embed_on(registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trellis.retrieve.embed_ingest_hook.embed_on_ingest_enabled",
        lambda registry=None: True,
    )
    monkeypatch.setattr(type(registry), "embedding_fn", property(lambda _self: _embed))


def _promote(
    registry: StoreRegistry,
    *,
    create_node: bool = True,
    **extra_args: Any,
) -> CommandStatus:
    """A real learning-style ``precedent.promote`` through the executor."""
    if create_node:
        registry.knowledge.graph_store.upsert_node(
            NODE_ID, "precedent", {"name": TITLE, "description": DESCRIPTION}
        )
    args: dict[str, Any] = {
        "title": TITLE,
        "description": DESCRIPTION,
        "entity_type": "precedent",
        **extra_args,
    }
    result = build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.PRECEDENT_PROMOTE,
            args=args,
            target_id=NODE_ID,
            target_type="entity",
            requested_by="test",
        )
    )
    return result.status


def _demote_command(reason: str = "superseded") -> Command:
    return Command(
        operation=Operation.PRECEDENT_DEMOTE,
        args={"target_id": NODE_ID, "reason": reason},
        target_id=NODE_ID,
        target_type="precedent",
        requested_by="test",
    )


def _reinstate_command() -> Command:
    return Command(
        operation=Operation.PRECEDENT_REINSTATE,
        args={"target_id": NODE_ID, "reason": "reviewed again"},
        target_id=NODE_ID,
        target_type="precedent",
        requested_by="test",
    )


def _axis(registry: StoreRegistry, name: str) -> Any:
    knowledge = registry.knowledge
    return {
        "keyword": lambda: KeywordSearch(knowledge.document_store),
        "semantic": lambda: SemanticSearch(knowledge.vector_store, _embed),
        "graph": lambda: GraphSearch(knowledge.graph_store),
    }[name]()


def _served(registry: StoreRegistry, axis: str, item_id: str = NODE_ID) -> bool:
    """Whether a pack built from one axis alone serves ``item_id``."""
    pack = PackBuilder(strategies=[_axis(registry, axis)]).build(
        TITLE, limit_per_strategy=10, clearance="restricted"
    )
    return item_id in {i.item_id for i in pack.items}


def _document(registry: StoreRegistry, doc_id: str = NODE_ID) -> dict[str, Any]:
    document = registry.knowledge.document_store.get(doc_id)
    assert document is not None
    return document


def _break_document_writes(
    registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        msg = "document store unavailable"
        raise StoreError(msg, store="document")

    monkeypatch.setattr(registry.knowledge.document_store, "put", boom)


class TestPromotion:
    def test_writes_a_document_row_from_the_node(self, registry: StoreRegistry) -> None:
        assert _promote(registry) == CommandStatus.SUCCESS

        document = _document(registry)
        assert document["content"] == f"{TITLE}\n\n{DESCRIPTION}"
        metadata = document["metadata"]
        assert metadata["document_form"] == "precedent"
        assert metadata["title"] == TITLE
        assert metadata["content_tags"]["content_type"] == "decision"
        # A domain tag hard-excludes on mismatch; a precedent is reached by intent.
        assert metadata["content_tags"]["domain"] == []
        assert LIFECYCLE_KEY not in metadata

    def test_a_trace_sourced_promotion_writes_none(
        self, registry: StoreRegistry
    ) -> None:
        status = _promote(
            registry, create_node=False, trace_id="trace-1", entity_type="trace"
        )

        assert status == CommandStatus.SUCCESS
        assert registry.knowledge.document_store.get(NODE_ID) is None

    def test_a_promotion_with_no_node_writes_none(
        self, registry: StoreRegistry
    ) -> None:
        assert _promote(registry, create_node=False) == CommandStatus.SUCCESS
        assert registry.knowledge.document_store.get(NODE_ID) is None

    def test_embeds_the_row_only_when_embed_on_ingest_is_on(
        self,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _promote(registry)
        assert registry.knowledge.vector_store.get(NODE_ID) is None

        monkeypatch.setattr(
            "trellis.retrieve.embed_ingest_hook.embed_on_ingest_enabled",
            lambda registry=None: True,
        )
        monkeypatch.setattr(
            type(registry), "embedding_fn", property(lambda _self: _embed)
        )
        _promote(registry, create_node=False)

        assert registry.knowledge.vector_store.get(NODE_ID) is not None

    def test_a_document_failure_fails_the_promotion_before_its_event(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry.knowledge.graph_store.upsert_node(
            NODE_ID, "precedent", {"name": TITLE, "description": DESCRIPTION}
        )
        _break_document_writes(registry, monkeypatch)

        status = _promote(registry, create_node=False)

        assert status == CommandStatus.FAILED
        assert not registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED
        )


class TestPromotionOutsideTheNamespace:
    """The row's exemptions key on the id namespace alone.

    A row written for a precedent-typed node outside ``precedent://learning/``
    would be open to the noise pass, retention and classify refresh, so the
    promotion records the event and writes no row.

    Defective subject: drop the ``is_promoted_precedent_document`` branch in
    ``PrecedentPromoteHandler`` and the first test finds a row.
    """

    OUTSIDE_ID = "precedent-by-hand"

    def _promote_outside(self, registry: StoreRegistry) -> CommandResult:
        registry.knowledge.graph_store.upsert_node(
            self.OUTSIDE_ID, "precedent", {"name": TITLE, "description": DESCRIPTION}
        )
        return build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.PRECEDENT_PROMOTE,
                args={
                    "title": TITLE,
                    "description": DESCRIPTION,
                    "entity_type": "precedent",
                },
                target_id=self.OUTSIDE_ID,
                target_type="entity",
                requested_by="test",
            )
        )

    def test_a_precedent_typed_target_outside_it_gets_the_event_and_no_row(
        self, registry: StoreRegistry, embed_on: None
    ) -> None:
        assert self._promote_outside(registry).status == CommandStatus.SUCCESS

        assert registry.knowledge.document_store.get(self.OUTSIDE_ID) is None
        assert registry.knowledge.vector_store.get(self.OUTSIDE_ID) is None
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED
        )
        assert [e.entity_id for e in events] == [self.OUTSIDE_ID]

    def test_the_skip_is_recorded_in_the_event_and_the_result(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: drop the ``document_row`` payload key and both
        assertions fail; the warning alone reaches no event reader."""
        result = self._promote_outside(registry)

        assert result.message.endswith(
            "(no document row: id outside precedent://learning/)"
        )
        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED
        )
        assert event.payload["document_row"] == "skipped_outside_namespace"

    def test_the_skip_is_logged_where_the_default_cli_shows_it(
        self, registry: StoreRegistry
    ) -> None:
        with capture_logs() as logs:
            self._promote_outside(registry)

        skipped = [
            e
            for e in logs
            if e["event"] == "precedent_document_skipped_outside_namespace"
        ]
        assert [(e["log_level"], e["target_id"]) for e in skipped] == [
            ("warning", self.OUTSIDE_ID)
        ]

    def test_entity_create_accepts_a_namespace_id_because_promotion_uses_it(
        self, registry: StoreRegistry
    ) -> None:
        """``entity.create`` cannot refuse the namespace: ``submit_learning_promotion``
        writes a promoted precedent's node with it. The row is what the namespace
        protects, and only ``precedent.promote`` writes that."""
        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_type": "precedent",
                    "entity_id": NODE_ID,
                    "name": TITLE,
                },
                requested_by="test",
            )
        )

        assert result.status == CommandStatus.SUCCESS, result.message
        assert registry.knowledge.graph_store.get_node(NODE_ID) is not None
        assert registry.knowledge.document_store.get(NODE_ID) is None

    def test_a_learning_promotion_still_writes_its_row(
        self, registry: StoreRegistry
    ) -> None:
        entity_id = _promote_from_source(registry, "doc-source")

        assert is_promoted_precedent_document(entity_id)
        assert registry.knowledge.document_store.get(entity_id) is not None


class TestLifecycle:
    @pytest.mark.parametrize("axis", AXES)
    def test_a_promoted_precedent_is_served_on_every_axis(
        self, registry: StoreRegistry, embed_on: None, axis: str
    ) -> None:
        _promote(registry)

        assert _served(registry, axis)

    @pytest.mark.parametrize("axis", AXES)
    def test_demotion_withholds_it_on_every_axis_and_reinstatement_restores_it(
        self, registry: StoreRegistry, embed_on: None, axis: str
    ) -> None:
        _promote(registry)

        PrecedentDemoteHandler(registry).handle(_demote_command())
        assert not _served(registry, axis)
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "demoted"
        vector_row = registry.knowledge.vector_store.get(NODE_ID)
        assert vector_row is not None
        assert vector_row["metadata"][LIFECYCLE_KEY]["state"] == "demoted"

        PrecedentReinstateHandler(registry).handle(_reinstate_command())
        assert _served(registry, axis)
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "current"

    def test_demotion_does_not_rewrite_the_document_clock_or_text(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        before = _document(registry)

        PrecedentDemoteHandler(registry).handle(_demote_command())

        after = _document(registry)
        assert after["content"] == before["content"]
        assert after["updated_at"] == before["updated_at"]

    def test_a_precedent_promoted_without_a_document_is_not_given_one(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.graph_store.upsert_node(
            NODE_ID, "precedent", {"name": TITLE, "description": DESCRIPTION}
        )

        PrecedentDemoteHandler(registry).handle(_demote_command())
        PrecedentReinstateHandler(registry).handle(_reinstate_command())

        assert registry.knowledge.document_store.get(NODE_ID) is None

    def test_a_failed_document_stamp_stops_the_demotion_before_the_node_changes(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _promote(registry)
        _break_document_writes(registry, monkeypatch)

        with pytest.raises(StoreError):
            PrecedentDemoteHandler(registry).handle(_demote_command())

        node = registry.knowledge.graph_store.get_node(NODE_ID)
        assert node is not None
        assert LIFECYCLE_KEY not in node["properties"]

    def test_a_failed_document_stamp_on_reinstatement_is_reported_and_stays_withheld(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())
        _break_document_writes(registry, monkeypatch)
        command = _reinstate_command()

        PrecedentReinstateHandler(registry).handle(command)

        warnings = command.metadata["handler_warnings"]
        assert any("precedent_reinstate_document_sync_failed" in w for w in warnings)
        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert event.payload["document_sync_failed"] is True
        # The node is current again; the row is still withheld, never served stale.
        assert not _served(registry, "keyword")


def _candidate(item_id: str) -> dict[str, Any]:
    return {
        "candidate_id": f"cand:{item_id}",
        "intent_family": "asset_generation",
        "recommendation_type": "promote_guidance",
        "item_id": item_id,
        "precedent_name": f"Learning :: {item_id}",
        "precedent_name_source": "item_title",
        "precedent_properties": {
            "intent_family": "asset_generation",
            "source_item_id": item_id,
            "success_rate": 0.9,
            "retry_rate": 0.1,
            "support_count": 5,
        },
        "target_entity_ids": ["entity://shared"],
        "supporting_run_ids": ["run-1"],
        "phases": ["GENERATE"],
        "metrics": {"success_rate": 0.9, "retry_rate": 0.1},
        "citation_evidence": {"helpful_count": 2, "unhelpful_count": 0},
    }


def _promote_from_source(
    registry: StoreRegistry, item_id: str, *, source_named: bool = False
) -> str:
    """Promote a precedent that copies ``item_id``'s text, as a reviewer would."""
    knowledge = registry.knowledge
    knowledge.document_store.put(item_id, COPIED_TEXT, {})
    # A graph node for the source, so it can be redacted.
    knowledge.graph_store.upsert_node(item_id, "CreativeWork", {"name": item_id})
    candidate = _candidate(item_id)
    if source_named:
        candidate.update(
            title=None,
            precedent_name=f"Learning: asset_generation :: {item_id}",
            precedent_name_source="item_id",
        )
        knowledge.document_store.put(item_id, f"{NAME_TEXT}\n\n{COPIED_TEXT}", {})
        _apply_readable_name_fallback(candidate, knowledge.document_store)
        assert NAME_TEXT in candidate["precedent_name"]
    payloads = build_learning_promotion_payloads(
        candidate=candidate,
        promotion_name="",
        rationale="reviewed and approved",
        document_store=knowledge.document_store,
    )
    out = submit_learning_promotion(
        build_curate_executor(registry, clearance="restricted"),
        payloads["entity_payload"],
        payloads["edge_payloads"],
        requested_by="test",
        graph_store=knowledge.graph_store,
    )
    assert out["status"] == "promoted"
    return str(payloads["entity_id"])


def _redact_command(target_id: str) -> Command:
    return Command(
        operation=Operation.REDACTION_APPLY,
        args={"target_id": target_id, "reason": "owner request"},
    )


class TestRedaction:
    def test_the_scrub_removes_the_excerpt_from_the_document_as_from_the_node(
        self, registry: StoreRegistry, embed_on: None
    ) -> None:
        precedent_id = _promote_from_source(registry, "doc-source", source_named=True)
        assert COPIED_TEXT in _document(registry, precedent_id)["content"]
        assert NAME_TEXT in _document(registry, precedent_id)["metadata"]["title"]

        RedactionApplyHandler(registry).handle(_redact_command("doc-source"))

        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        props = node["properties"]
        document = _document(registry, precedent_id)
        assert document["content"] == precedent_document_content(
            props["name"], props["description"]
        )
        assert document["metadata"]["title"] == props["name"]
        assert COPIED_TEXT not in repr(document)
        assert NAME_TEXT not in repr(document)
        # Re-embedded from the scrubbed text, not left deleted or stale.
        vector_row = registry.knowledge.vector_store.get(precedent_id)
        assert vector_row is not None
        assert COPIED_TEXT not in repr(vector_row)
        assert NAME_TEXT not in repr(vector_row)

    def test_the_scrub_keeps_a_withheld_precedent_withheld(
        self, registry: StoreRegistry
    ) -> None:
        precedent_id = _promote_from_source(registry, "doc-source")
        command = _demote_command()
        command.args["target_id"] = precedent_id
        PrecedentDemoteHandler(registry).handle(command)

        RedactionApplyHandler(registry).handle(_redact_command("doc-source"))

        state = _document(registry, precedent_id)["metadata"][LIFECYCLE_KEY]["state"]
        assert state == "demoted"

    def test_a_failed_document_rewrite_aborts_before_anything_is_purged(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        precedent_id = _promote_from_source(registry, "doc-source")
        _break_document_writes(registry, monkeypatch)

        with pytest.raises(StoreError):
            RedactionApplyHandler(registry).handle(_redact_command("doc-source"))

        # Nothing was purged, so the redaction can simply be run again.
        assert registry.knowledge.graph_store.get_node("doc-source") is not None
        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        assert COPIED_TEXT in node["properties"]["description"]

    def test_redacting_the_precedent_itself_blanks_its_document(
        self, registry: StoreRegistry, embed_on: None
    ) -> None:
        precedent_id = _promote_from_source(registry, "doc-source")
        assert COPIED_TEXT in _document(registry, precedent_id)["content"]

        RedactionApplyHandler(registry).handle(_redact_command(precedent_id))

        document = _document(registry, precedent_id)
        assert document["content"] == ""
        assert COPIED_TEXT not in repr(document)
        assert registry.knowledge.vector_store.get(precedent_id) is None
        for axis in ("keyword", "semantic"):
            assert not _served(registry, axis, precedent_id)

    def test_a_precedents_own_document_is_never_a_source_excerpt(
        self, registry: StoreRegistry
    ) -> None:
        precedent_id = _promote_from_source(registry, "doc-source")
        assert registry.knowledge.document_store.get(precedent_id) is not None

        excerpt, reason = resolve_source_excerpt(
            precedent_id, registry.knowledge.document_store
        )

        # A second precedent copying this row would carry the first one's
        # source text past a redaction that matches on ``source_item_id``.
        assert excerpt is None
        assert reason == "document_not_found"


# ---------------------------------------------------------------------------
# A hand-promoted precedent is exempt from automatic demotion, and its document
# row must not be a way round that. Each document-plane loop below is tested
# against the row a real promotion wrote.
# ---------------------------------------------------------------------------

FORGED_ID = "memory-that-claims-to-be-a-precedent"


def _signal_quality(registry: StoreRegistry, doc_id: str = NODE_ID) -> Any:
    return _document(registry, doc_id)["metadata"]["content_tags"].get("signal_quality")


def _tag_noise_directly(registry: StoreRegistry, doc_id: str = NODE_ID) -> None:
    """Write the noise tag the way a test (or a past bug) could: bypassing the pass."""
    document = _document(registry, doc_id)
    metadata = dict(document["metadata"])
    tags = metadata.get("content_tags") or {}
    metadata["content_tags"] = {**tags, "signal_quality": "noise"}
    put_document(
        registry.knowledge.document_store,
        registry.knowledge.vector_store,
        doc_id,
        document["content"],
        metadata,
        preserve_updated_at=True,
    )


def _put_memory_claiming_to_be_a_precedent(registry: StoreRegistry) -> None:
    """An ordinary caller's document that sets ``document_form`` to ``precedent``."""
    result = build_curate_executor(
        registry, evidence_embed="none", clearance="restricted"
    ).execute(
        build_evidence_ingest_command(
            doc_id=FORGED_ID,
            content=f"{TITLE}\n\n{DESCRIPTION}",
            metadata={"document_form": "precedent"},
            requested_by="test",
            embed_mode="none",
        )
    )
    assert result.status == CommandStatus.SUCCESS, result.message


class TestPredicateIsUnforgeable:
    def test_it_reads_the_id_namespace_and_nothing_else(self) -> None:
        assert is_promoted_precedent_document(NODE_ID)
        assert not is_promoted_precedent_document(FORGED_ID)
        assert not is_promoted_precedent_document(None)

    def test_a_caller_cannot_choose_an_id_in_the_reserved_namespace(
        self, registry: StoreRegistry
    ) -> None:
        command = build_evidence_ingest_command(
            doc_id="precedent://learning/mine",
            content="a memory that wants a precedent's exemptions",
            requested_by="test",
            embed_mode="none",
        )

        result = build_curate_executor(
            registry, evidence_embed="none", clearance="restricted"
        ).execute(command)

        assert result.status in (CommandStatus.FAILED, CommandStatus.REJECTED)
        assert "reserved precedent namespace" in (result.message or "")
        assert (
            registry.knowledge.document_store.get("precedent://learning/mine") is None
        )

    def test_setting_document_form_to_precedent_earns_no_exemption(
        self, registry: StoreRegistry
    ) -> None:
        _put_memory_claiming_to_be_a_precedent(registry)

        result = apply_noise_tags(
            [FORGED_ID],
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
        )

        assert result.updated == 1
        assert result.refused_not_document == []
        assert _signal_quality(registry, FORGED_ID) == "noise"


class TestNoisePassSparesPromotedPrecedents:
    def test_the_noise_pass_refuses_the_row_and_leaves_its_tags_alone(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        before = _document(registry)["metadata"]["content_tags"]

        result = apply_noise_tags(
            [NODE_ID],
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
        )

        assert result.updated == 0
        assert result.refused_not_document == [NODE_ID]
        assert _document(registry)["metadata"]["content_tags"] == before
        assert _signal_quality(registry) != "noise"
        assert _served(registry, "keyword")

    def test_a_demote_then_reinstate_leaves_no_noise_tag_to_clear(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        apply_noise_tags(
            [NODE_ID],
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
        )
        PrecedentDemoteHandler(registry).handle(_demote_command())
        PrecedentReinstateHandler(registry).handle(_reinstate_command())

        assert _signal_quality(registry) != "noise"
        assert _served(registry, "keyword")


class TestRetentionSparesPromotedPrecedents:
    def _prune(self, registry: StoreRegistry, criteria: dict[str, Any]) -> Any:
        return build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_PRUNE,
                args={"criteria": criteria, "reason": "hygiene", "dry_run": False},
            )
        )

    def test_a_noise_tagged_precedent_row_is_not_archived(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        _tag_noise_directly(registry)
        before = _document(registry)

        result = self._prune(registry, {"noise_documents": True})

        assert result.status == CommandStatus.SUCCESS
        assert _document(registry) == before
        assert LIFECYCLE_KEY not in _document(registry)["metadata"]

    def test_a_demoted_precedent_row_is_not_selected_by_lifecycle_state(
        self, registry: StoreRegistry
    ) -> None:
        from trellis.mutate.retention import RetentionCriteria, resolve_candidates

        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())

        report = resolve_candidates(
            RetentionCriteria(
                noise_documents=True,
                lifecycle_states=["demoted", "deprecated", "superseded"],  # type: ignore[list-item]
                older_than_days=0,
            ),
            registry,
        )

        # Every candidate kind, not only documents: the node shares the row's
        # id, and a ``kind == "document"`` filter hid the node being selected.
        assert NODE_ID not in {c.item_id for c in report.candidates}
        assert report.skipped_precedent == 2  # the row and the node

    def test_a_pruned_demoted_precedent_is_never_split_and_stays_reinstatable(
        self, registry: StoreRegistry
    ) -> None:
        """promote, demote, prune, restore, reinstate: node and row end current.

        Before the node was exempt, the prune skipped the row (still
        ``demoted``) but archived the node. ``retention.restore`` reads the row
        first, found it not archived and reported "not archived (skipped)"
        without reaching the node, and ``precedent.reinstate`` then refused an
        ``archived`` node, so the pair could not be recovered.
        """
        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())
        graph = registry.knowledge.graph_store

        pruned = self._prune(
            registry, {"lifecycle_states": ["demoted"], "older_than_days": 0}
        )

        assert pruned.status == CommandStatus.SUCCESS
        node = graph.get_node(NODE_ID)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "demoted"

        restored = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": [NODE_ID], "reason": "undo"},
            )
        )
        assert restored.status == CommandStatus.SUCCESS

        PrecedentReinstateHandler(registry).handle(_reinstate_command())

        node = graph.get_node(NODE_ID)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "current"
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert _served(registry, "keyword")

    def test_a_node_archived_before_the_exemption_counts_as_already_archived(
        self, registry: StoreRegistry
    ) -> None:
        """A node pruned before #955 carries ``archived``; the skip that says so
        is ``skipped_already_archived``, not ``skipped_precedent``, which counts
        only what the exemption spared.

        Defective subject: run the precedent skip before the archived check in
        ``_resolve_entities`` and the node lands in ``skipped_precedent``.
        """
        from trellis.mutate.retention import RetentionCriteria, resolve_candidates

        registry.knowledge.graph_store.upsert_node(
            NODE_ID,
            "precedent",
            {"name": TITLE, LIFECYCLE_KEY: {"state": "archived"}},
        )
        spared = "precedent://learning/spared"
        registry.knowledge.graph_store.upsert_node(
            spared, "precedent", {"name": "Other", LIFECYCLE_KEY: {"state": "demoted"}}
        )

        report = resolve_candidates(
            RetentionCriteria(
                lifecycle_states=["demoted"],  # type: ignore[list-item]
                older_than_days=0,
            ),
            registry,
        )

        assert report.candidates == []
        assert report.skipped_already_archived == 1
        assert report.skipped_precedent == 1

    def test_an_ordinary_noise_row_is_still_archived(
        self, registry: StoreRegistry
    ) -> None:
        _put_memory_claiming_to_be_a_precedent(registry)
        _tag_noise_directly(registry, FORGED_ID)

        self._prune(registry, {"noise_documents": True})

        lifecycle = _document(registry, FORGED_ID)["metadata"][LIFECYCLE_KEY]
        assert lifecycle["state"] == "archived"


def _refusal_text(registry: StoreRegistry) -> str:
    """The refused item's message, as the ``RETENTION_RESTORED`` event records it."""
    (event,) = registry.operational.event_log.get_events(
        event_type=EventType.RETENTION_RESTORED
    )
    (item,) = event.payload["refused_items"]
    return str(item["message"])


class TestRestoreRefusesAnOrphanedPrecedentRow:
    """``retention.restore`` will not un-archive a self-redacted precedent's row (#957).

    Redacting a precedent purges its node and leaves its document row as an empty
    archived tombstone. Restore read the document store first, found the archived
    row and re-stamped it ``current`` while the node stayed deleted.

    Defective subject: drop ``_refuse_unrestorable_row`` from
    ``RetentionRestoreHandler._restore`` and the tombstone is restored.
    """

    def _restore(self, registry: StoreRegistry, item_ids: list[str]) -> CommandResult:
        return build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": item_ids, "reason": "corrective"},
            )
        )

    def _redact_the_precedent(self, registry: StoreRegistry) -> dict[str, Any]:
        _promote(registry)
        RedactionApplyHandler(registry).handle(_redact_command(NODE_ID))
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None
        tombstone = _document(registry)
        assert tombstone["content"] == ""
        assert tombstone["metadata"][LIFECYCLE_KEY]["state"] == "archived"
        return tombstone

    def test_the_tombstone_stays_archived_and_the_refusal_says_why(
        self, registry: StoreRegistry
    ) -> None:
        tombstone = self._redact_the_precedent(registry)

        result = self._restore(registry, [NODE_ID])

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_precedent_node_missing"
        )
        assert _document(registry) == tombstone
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None
        for axis in ("keyword", "semantic"):
            assert not _served(registry, axis, NODE_ID)

    def test_one_orphan_does_not_cost_the_rest_of_the_batch_its_restore(
        self, registry: StoreRegistry
    ) -> None:
        self._redact_the_precedent(registry)
        archived = {LIFECYCLE_KEY: {"state": "archived"}}
        registry.knowledge.document_store.put("plain-doc", "an ordinary note", archived)

        result = self._restore(registry, [NODE_ID, "plain-doc"])

        assert result.status == CommandStatus.SUCCESS
        assert "retention_restore_precedent_node_missing" in result.message
        assert any(NODE_ID in w for w in result.warnings)
        plain = _document(registry, "plain-doc")
        assert plain["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "archived"
        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.RETENTION_RESTORED
        )
        assert event.payload["refused_ids"] == [NODE_ID]
        assert event.payload["restored_ids"] == ["plain-doc"]

    def test_an_archived_row_beside_a_live_node_is_still_restored(
        self, registry: StoreRegistry
    ) -> None:
        """Only an orphan is refused: with its node present the row restores."""
        _promote(registry)
        document_store = registry.knowledge.document_store
        row = _document(registry)
        metadata = {**row["metadata"], LIFECYCLE_KEY: {"state": "archived"}}
        put_document(
            document_store,
            registry.knowledge.vector_store,
            NODE_ID,
            row["content"],
            metadata,
            preserve_updated_at=True,
        )

        result = self._restore(registry, [NODE_ID])

        assert result.status == CommandStatus.SUCCESS
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "current"

    def test_the_refusal_is_true_when_the_row_keeps_its_content(
        self, registry: StoreRegistry
    ) -> None:
        """A node gone without a blanking (a failed scrub re-create) reads true.

        The row is archived with its real text and no node: nothing was
        redacted from it, so the message must not call it a tombstone.
        """
        _promote(registry)
        row = _document(registry)
        metadata = {**row["metadata"], LIFECYCLE_KEY: {"state": "archived"}}
        put_document(
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
            NODE_ID,
            row["content"],
            metadata,
            preserve_updated_at=True,
        )
        assert registry.knowledge.graph_store.delete_node(NODE_ID)

        result = self._restore(registry, [NODE_ID])

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_precedent_node_missing"
        )
        text = _refusal_text(registry)
        assert "no longer exists" in text
        for untrue in ("blank", "tombstone", "redacted"):
            assert untrue not in text
        assert _document(registry)["content"] == row["content"]


class TestRestoreRefusesARedactionTombstone:
    """``retention.restore`` will not un-archive the blank row a redaction left.

    ``redaction.apply`` blanks the row first and purges the node second. When
    the purge fails the row is already a blank archived tombstone beside a
    live node, and restore marked it ``current``.

    Defective subject: drop the ``is_redaction_tombstone`` branch of
    ``RetentionRestoreHandler._refuse_unrestorable_row``.
    """

    def _restore(self, registry: StoreRegistry, item_ids: list[str]) -> CommandResult:
        return build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.RETENTION_RESTORE,
                args={"item_ids": item_ids, "reason": "corrective"},
            )
        )

    def _redact_with_a_failing_purge(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Any]:
        graph = registry.knowledge.graph_store

        def _boom(_node_id: str) -> bool:
            msg = "purge failed"
            raise StoreError(msg, store="graph")

        monkeypatch.setattr(graph, "delete_node", _boom)
        with pytest.raises(StoreError, match="purge failed"):
            RedactionApplyHandler(registry).handle(_redact_command(NODE_ID))
        monkeypatch.undo()
        assert graph.get_node(NODE_ID) is not None
        tombstone = _document(registry)
        assert tombstone["content"] == ""
        return tombstone

    def test_a_purge_that_failed_leaves_a_tombstone_that_restore_refuses(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _promote(registry)
        tombstone = self._redact_with_a_failing_purge(registry, monkeypatch)

        result = self._restore(registry, [NODE_ID])

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_redaction_tombstone"
        )
        assert "redaction.apply" in _refusal_text(registry)
        assert _document(registry) == tombstone
        for axis in ("keyword", "semantic"):
            assert not _served(registry, axis, NODE_ID)

    def test_a_document_outside_the_namespace_is_refused_the_same_way(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        graph.upsert_node("plain-doc", "document", {"name": "plain"})
        registry.knowledge.document_store.put("plain-doc", "an ordinary note", {})
        RedactionApplyHandler(registry).handle(_redact_command("plain-doc"))
        tombstone = _document(registry, "plain-doc")
        assert tombstone["content"] == ""

        result = self._restore(registry, ["plain-doc"])

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "retention_restore_redaction_tombstone"
        )
        assert _document(registry, "plain-doc") == tombstone

    def test_an_archived_note_whose_reason_reads_redacted_still_restores(
        self, registry: StoreRegistry
    ) -> None:
        """The reason is caller-chosen text: only a *blank* row is a tombstone."""
        archived = {
            LIFECYCLE_KEY: {"state": "archived", "deprecation_reason": "redacted"}
        }
        registry.knowledge.document_store.put("plain-doc", "an ordinary note", archived)

        result = self._restore(registry, ["plain-doc"])

        assert result.status == CommandStatus.SUCCESS
        plain = _document(registry, "plain-doc")
        assert plain["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert plain["content"] == "an ordinary note"

    def test_two_different_refusals_reject_under_the_mixed_code(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        RedactionApplyHandler(registry).handle(_redact_command(NODE_ID))
        registry.knowledge.graph_store.upsert_node("plain-doc", "document", {})
        registry.knowledge.document_store.put("plain-doc", "an ordinary note", {})
        RedactionApplyHandler(registry).handle(_redact_command("plain-doc"))

        result = self._restore(registry, [NODE_ID, "plain-doc"])

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"] == "retention_restore_nothing_restored"
        )
        assert "retention_restore_precedent_node_missing" in result.message
        assert "retention_restore_redaction_tombstone" in result.message


class _FixedClassifier:
    name = "fixed"

    def classify(
        self, content: str, *, context: ClassificationContext | None = None
    ) -> ClassificationResult:
        return ClassificationResult(
            tags={"content_type": ["procedure"]},
            confidence=1.0,
            classifier_name=self.name,
        )


class TestClassifyRefreshSparesPromotedPrecedents:
    def test_reclassify_item_keeps_the_decision_tag(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        before = _document(registry)["metadata"]["content_tags"]

        outcome = reclassify_item(
            NODE_ID,
            pipeline=ClassifierPipeline(classifiers=[_FixedClassifier()]),
            document_store=registry.knowledge.document_store,
            vector_store=registry.knowledge.vector_store,
        )

        assert outcome.refreshed is False
        assert outcome.reason == REASON_PRECEDENT
        assert _document(registry)["metadata"]["content_tags"] == before
        assert before["content_type"] == "decision"

    def test_the_stale_scan_counts_it_and_re_tags_everything_else(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        _put_memory_claiming_to_be_a_precedent(registry)
        # Untagged, so the scan treats it as stale and refreshes it.
        registry.knowledge.document_store.put(
            "plain-doc", "an ordinary untagged memory", {}
        )

        result = reclassify_stale(
            pipeline=ClassifierPipeline(classifiers=[_FixedClassifier()]),
            document_store=registry.knowledge.document_store,
            max_age_days=0,
            vector_store=registry.knowledge.vector_store,
        )

        assert result.skipped_precedent == 1
        assert "plain-doc" in result.item_ids_refreshed
        assert NODE_ID not in result.item_ids_refreshed
        assert _document(registry)["metadata"]["content_tags"]["content_type"] == (
            "decision"
        )


class TestShadowPassOnlyAddsItsOwnKeyToAPromotedPrecedent:
    """The shadow pass is harmless to a precedent row, and this pins why (#957).

    ``shadow_classify_item`` overlays ``content_tags_shadow`` and nothing else,
    keeps the row's clock, and the serving boundary strips that key. So unlike
    ``worker enrich`` it needs no namespace skip: no served field of the row
    changes. ``tag_evolution`` and ``domain_normalization`` never write a
    document at all; they read shadow tags and emit candidate events.
    """

    def test_a_shadow_pass_leaves_every_served_field_alone(
        self, registry: StoreRegistry
    ) -> None:
        from trellis.classify.shadow import shadow_classify_item
        from trellis.retrieve.servable import strip_non_servable
        from trellis.schemas.classification import SHADOW_TAGS_KEY

        _promote(registry)
        before = _document(registry)

        outcome = shadow_classify_item(
            NODE_ID,
            classifier=_FixedClassifier(),
            document_store=registry.knowledge.document_store,
            vector_store=registry.knowledge.vector_store,
        )

        assert outcome.written is True
        after = _document(registry)
        assert SHADOW_TAGS_KEY in after["metadata"]
        assert {k: v for k, v in after["metadata"].items() if k != SHADOW_TAGS_KEY} == (
            before["metadata"]
        )
        assert after["content"] == before["content"]
        assert after["updated_at"] == before["updated_at"]
        assert after["metadata"]["content_tags"]["content_type"] == "decision"

        pack = PackBuilder(strategies=[_axis(registry, "keyword")]).build(
            TITLE, limit_per_strategy=10, clearance="restricted"
        )
        served = [i for i in strip_non_servable(pack.items) if i.item_id == NODE_ID]
        assert served
        assert SHADOW_TAGS_KEY not in served[0].metadata


class TestReinstateRepairsAStrandedRow:
    def _strand(self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Reinstate with a failing document stamp; returns the real ``put``."""
        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())
        store = registry.knowledge.document_store
        real_put = store.put
        _break_document_writes(registry, monkeypatch)
        PrecedentReinstateHandler(registry).handle(_reinstate_command())
        assert not _served(registry, "keyword")
        return real_put

    def test_the_warning_names_the_repair(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())
        _break_document_writes(registry, monkeypatch)
        command = _reinstate_command()

        PrecedentReinstateHandler(registry).handle(command)

        assert any(
            "Re-issue precedent.reinstate" in w
            for w in command.metadata["handler_warnings"]
        )

    def test_a_second_reinstate_re_stamps_the_row_and_touches_nothing_else(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_put = self._strand(registry, monkeypatch)
        monkeypatch.setattr(registry.knowledge.document_store, "put", real_put)
        versions_before = len(registry.knowledge.graph_store.get_node_history(NODE_ID))

        PrecedentReinstateHandler(registry).handle(_reinstate_command())

        assert _served(registry, "keyword")
        assert _document(registry)["metadata"][LIFECYCLE_KEY]["state"] == "current"
        assert (
            len(registry.knowledge.graph_store.get_node_history(NODE_ID))
            == versions_before
        )
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert events[-1].payload["document_resync_only"] is True

    def test_a_failing_repair_raises_rather_than_reporting_success(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._strand(registry, monkeypatch)

        with pytest.raises(StoreError):
            PrecedentReinstateHandler(registry).handle(_reinstate_command())

    def test_a_row_archived_beside_a_current_node_is_not_resynced(
        self, registry: StoreRegistry
    ) -> None:
        """The resync accepts a ``demoted`` row only.

        An ``archived`` row (a redaction or a prune stamped it) beside a
        current node is somebody else's state; a reinstate must refuse it
        rather than stamp the row ``current`` and un-archive redacted content.
        """
        _promote(registry)
        document = _document(registry)
        metadata = dict(document["metadata"])
        metadata[LIFECYCLE_KEY] = {
            "state": "archived",
            "deprecation_reason": "redacted",
        }
        put_document(
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
            NODE_ID,
            document["content"],
            metadata,
            preserve_updated_at=True,
        )

        with pytest.raises(Exception, match="not 'demoted'"):
            PrecedentReinstateHandler(registry).handle(_reinstate_command())

        stamped = _document(registry)["metadata"][LIFECYCLE_KEY]
        assert stamped["state"] == "archived"
        assert stamped["deprecation_reason"] == "redacted"

    def test_a_healthy_current_precedent_is_still_refused(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        PrecedentDemoteHandler(registry).handle(_demote_command())
        PrecedentReinstateHandler(registry).handle(_reinstate_command())

        with pytest.raises(Exception, match="not 'demoted'"):
            PrecedentReinstateHandler(registry).handle(_reinstate_command())
