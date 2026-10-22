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

from trellis.errors import StoreError
from trellis.learning.scoring import (
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    precedent_document_content,
    resolve_source_excerpt,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
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
    result = build_curate_executor(registry).execute(
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
        TITLE, limit_per_strategy=10
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
        build_curate_executor(registry),
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
