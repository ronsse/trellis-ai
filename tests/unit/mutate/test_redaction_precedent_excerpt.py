"""``redaction.apply`` scrubs the source excerpt a promoted precedent copied.

A promoted precedent's description opens with up to 600 chars of its source
document. A redaction that purged the document's graph node but left that
text in the precedent would keep serving it, so the handler follows the
document (or a chunk of it) to the precedents that copied it. These tests
promote precedents through the real governed path, so the description is the
one the builder writes, then redact and read every surface that could still
hold the text: the node, its history, its edges and aliases, the audit
payload, and the precedent listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.errors import NotFoundError, StoreError
from trellis.learning.scoring import (
    SOURCE_EXCERPT_REDACTED,
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor, handlers
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import RedactionApplyHandler, create_curate_handlers
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.retrieve.precedents import list_precedents
from trellis.retrieve.strategies import NamespaceSeedExtractor
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.well_known import normalize_entity_name
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

COPIED_TEXT = "ZEBRA-QUOKKA confidential sentence copied from the source"
NAME_TEXT = "NARWHAL-OKAPI first line of the source"
TITLE_TEXT = "WALRUS-HERON title the source document carried"
EXPECTED_KEYS = {
    "target_id",
    "target_kind",
    "reason",
    "command_id",
    "requested_by",
    "node_versions_purged",
    "edges_purged",
    "aliases_purged",
    "vector_deleted",
    "document_ids",
    "linked_observation_ids",
    "linked_measurement_ids",
}


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _candidate(item_id: str, *, secret: str = COPIED_TEXT) -> dict[str, Any]:
    return {
        "candidate_id": f"cand:{item_id}",
        "intent_family": "asset_generation",
        "recommendation_type": "promote_guidance",
        "item_id": item_id,
        # The bare-id name holds no text from the source. (A title the item
        # carried does: redaction blanks the row's title, so the scrub renames
        # it, see ``test_a_title_name_is_renamed_as_the_row_loses_its_title``.)
        "precedent_name": f"Learning: asset_generation :: {item_id}",
        "precedent_name_source": "item_id",
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


def _promote(
    registry: StoreRegistry,
    item_id: str,
    *,
    body: str = COPIED_TEXT,
    source_named: bool = False,
    title_named: bool = False,
) -> str:
    """Store ``item_id`` as a document and promote a precedent from it.

    ``source_named`` promotes a title-less candidate, so its name is the
    first line of ``body``, derived by the real readable-name fallback.
    ``title_named`` promotes one named from the item's title, as the analyzer
    does when the document carries one.
    """
    knowledge = registry.knowledge
    if knowledge.document_store.get(item_id) is None:
        knowledge.document_store.put(item_id, body, {})
    candidate = _candidate(item_id)
    if title_named:
        candidate.update(
            title=TITLE_TEXT,
            precedent_name=f"Learning: asset_generation :: {TITLE_TEXT}",
            precedent_name_source="item_title",
        )
    if source_named:
        candidate.update(
            title=None,
            precedent_name=f"Learning: asset_generation :: {item_id}",
            precedent_name_source="item_id",
        )
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


def _node(registry: StoreRegistry, node_id: str, **kwargs: Any) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id, kwargs.pop("node_type", "CreativeWork"), {"name": node_id}, **kwargs
    )


def _redact(registry: StoreRegistry, target_id: str) -> dict[str, Any]:
    RedactionApplyHandler(registry).handle(
        Command(
            operation=Operation.REDACTION_APPLY,
            args={"target_id": target_id, "reason": "owner request"},
        )
    )
    (event,) = registry.operational.event_log.get_events(
        event_type=EventType.REDACTION_APPLIED
    )
    return event.payload


def _everything_stored(registry: StoreRegistry, node_id: str) -> str:
    graph = registry.knowledge.graph_store
    return repr(
        [
            graph.get_node(node_id),
            graph.get_node_history(node_id),
            graph.get_aliases(node_id),
        ]
    )


def _two_versions(registry: StoreRegistry, precedent_id: str) -> None:
    """A second SCD-2 version, as a later update leaves one."""
    graph = registry.knowledge.graph_store
    node = graph.get_node(precedent_id)
    assert node is not None
    graph.upsert_node(
        precedent_id,
        node["node_type"],
        {**node["properties"], "later_edit": True},
        document_ids=node.get("document_ids") or None,
    )
    assert len(graph.get_node_history(precedent_id)) == 2


class TestDocumentTarget:
    def test_excerpt_leaves_current_node_and_history(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        _two_versions(registry, precedent_id)
        assert COPIED_TEXT in _everything_stored(registry, precedent_id)

        payload = _redact(registry, "doc-a")

        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        node = graph.get_node(precedent_id)
        assert node is not None
        props = node["properties"]
        assert props["source_excerpt_status"] == SOURCE_EXCERPT_REDACTED
        # The rest of the precedent survives, description included.
        assert "Reviewed learning for intent family" in props["description"]
        assert "Source item 'doc-a' showed" in props["description"]
        assert "Review rationale: reviewed and approved" in props["description"]
        assert props["source_item_id"] == "doc-a"
        assert props["success_rate"] == 0.9
        assert props["later_edit"] is True
        assert props["name"] == "Learning: asset_generation :: doc-a"
        assert payload["scrubbed_precedent_ids"] == [precedent_id]
        assert payload["scrubbed_precedent_count"] == 1
        assert COPIED_TEXT not in repr(payload)

    def test_edges_aliases_and_vector_row(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        _node(registry, "entity://shared")
        precedent_id = _promote(registry, "doc-a")
        graph.upsert_edge(
            "entity://shared",
            precedent_id,
            "cites",
            {"k": "v"},
            confidence=0.5,
            evidence_ref="ev-1",
        )
        registry.knowledge.vector_store.upsert(precedent_id, [0.1, 0.2, 0.3])
        before = {
            (e["source_id"], e["target_id"], e["edge_type"])
            for e in graph.get_edges(precedent_id, direction="both")
        }
        aliases_before = graph.get_aliases(precedent_id)
        assert before
        assert aliases_before

        _redact(registry, "doc-a")

        edges = graph.get_edges(precedent_id, direction="both")
        assert {
            (e["source_id"], e["target_id"], e["edge_type"]) for e in edges
        } == before
        (cited,) = [e for e in edges if e["edge_type"] == "cites"]
        assert cited["properties"] == {"k": "v"}
        assert cited["confidence"] == 0.5
        assert cited["evidence_ref"] == "ev-1"
        assert {
            (a["source_system"], a["raw_id"]) for a in graph.get_aliases(precedent_id)
        } == {(a["source_system"], a["raw_id"]) for a in aliases_before}
        assert registry.knowledge.vector_store.get(precedent_id) is None

    def test_document_ids_and_role_survive(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        node = graph.get_node(precedent_id)
        assert node is not None
        graph.upsert_node(
            precedent_id, node["node_type"], node["properties"], document_ids=["d-1"]
        )

        _redact(registry, "doc-a")

        after = graph.get_node(precedent_id)
        assert after is not None
        assert after["document_ids"] == ["d-1"]
        assert after["node_role"] == "semantic"

    def test_through_the_executor_one_command(self, registry: StoreRegistry) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": "doc-a", "reason": "owner request"},
            )
        )
        assert result.status == CommandStatus.SUCCESS
        assert "1 precedent(s)" in result.message
        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)

    def test_listing_serves_scrubbed_description(self, registry: StoreRegistry) -> None:
        # PRECEDENT_PROMOTED is immutable and holds the description as
        # promoted; the lessons listing must not serve that copy.
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        graph = registry.knowledge.graph_store
        log = registry.operational.event_log
        (row,) = list_precedents(log, graph_store=graph)
        assert COPIED_TEXT in row["description"]
        assert COPIED_TEXT in repr(row["payload"])

        _redact(registry, "doc-a")

        (row,) = list_precedents(log, graph_store=graph)
        assert row["entity_id"] == precedent_id
        assert COPIED_TEXT not in row["description"]
        assert COPIED_TEXT not in repr(row["payload"])
        assert "Reviewed learning for intent family" in row["description"]


class TestChunkMatching:
    def test_parent_target_reaches_a_chunk_sourced_precedent(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-b")
        precedent_id = _promote(registry, "doc-b#chunk-0")

        payload = _redact(registry, "doc-b")

        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        assert payload["scrubbed_precedent_ids"] == [precedent_id]

    def test_chunk_target_reaches_only_that_chunk(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-c#chunk-1")
        hit = _promote(registry, "doc-c#chunk-1")
        sibling = _promote(registry, "doc-c#chunk-2")
        parent = _promote(registry, "doc-c")
        untouched = {p: _everything_stored(registry, p) for p in (sibling, parent)}

        payload = _redact(registry, "doc-c#chunk-1")

        assert COPIED_TEXT not in _everything_stored(registry, hit)
        assert payload["scrubbed_precedent_ids"] == [hit]
        for precedent_id, state in untouched.items():
            assert _everything_stored(registry, precedent_id) == state
            node = graph.get_node(precedent_id)
            assert node is not None
            assert node["properties"]["source_excerpt_status"] == "included"

    def test_prefix_lookalikes_are_untouched(self, registry: StoreRegistry) -> None:
        _node(registry, "doc-1")
        hit = _promote(registry, "doc-1")
        lookalikes = [
            _promote(registry, "doc-10"),
            _promote(registry, "doc-1-b"),
            _promote(registry, "doc-1x#chunk-0"),
            _promote(registry, "other"),
        ]
        before = {p: _everything_stored(registry, p) for p in lookalikes}

        payload = _redact(registry, "doc-1")

        assert payload["scrubbed_precedent_ids"] == [hit]
        assert {p: _everything_stored(registry, p) for p in lookalikes} == before


class TestCallerNamedTarget:
    """A caller-named ``<x>#chunk-intro`` is a parent, not a chunk of ``<x>`` (#1001).

    ``is_chunk_doc_id`` reads only the shape ingest mints, so redacting a
    document a caller named ``notes#chunk-intro`` blanks the chunks minted
    under it and scrubs precedents sourced from them, and leaves ``notes``
    and the minted chunks of ``notes`` alone.
    """

    TARGET = "notes#chunk-intro"

    def test_it_blanks_its_own_minted_chunks_and_nothing_of_notes(
        self, registry: StoreRegistry
    ) -> None:
        documents = registry.knowledge.document_store
        _node(registry, self.TARGET)
        for doc_id in ("notes", "notes#chunk-0", self.TARGET + "#chunk-0"):
            documents.put(doc_id, COPIED_TEXT, {})
        documents.put(self.TARGET, COPIED_TEXT, {})
        documents.put(self.TARGET + "#chunk-1", COPIED_TEXT, {})

        payload = _redact(registry, self.TARGET)

        assert payload["documents_tombstoned"] == [
            self.TARGET + "#chunk-0",
            self.TARGET + "#chunk-1",
            self.TARGET,
        ]
        for blanked in payload["documents_tombstoned"]:
            row = documents.get(blanked)
            assert row is not None
            assert COPIED_TEXT not in repr(row)
        for kept in ("notes", "notes#chunk-0"):
            row = documents.get(kept)
            assert row is not None
            assert COPIED_TEXT in repr(row)

    def test_it_scrubs_precedents_sourced_from_its_chunks(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, self.TARGET)
        own = _promote(registry, self.TARGET)
        minted = _promote(registry, self.TARGET + "#chunk-0")
        untouched = [
            _promote(registry, "notes"),
            _promote(registry, "notes#chunk-0"),
            _promote(registry, "notes#chunk-intro-b"),
        ]
        before = {p: _everything_stored(registry, p) for p in untouched}

        payload = _redact(registry, self.TARGET)

        assert sorted(payload["scrubbed_precedent_ids"]) == sorted([own, minted])
        for precedent_id in (own, minted):
            assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        assert {p: _everything_stored(registry, p) for p in untouched} == before


class TestTargetNamedLikeAMintedChunk:
    """A document named ``x#chunk-5`` is a parent whatever its id looks like (#1008).

    ``is_chunk_doc_id`` cannot tell that id from a chunk ingest minted, so a
    redaction that took it for one blanked only the row itself, left every
    ``x#chunk-5#chunk-N`` serving the text, skipped the precedent scrub, and
    reported neither. Ingest now refuses such an id, but a row named so before
    that refusal is still stored, and these tests write one directly.

    Defective subject: restore the ``is_chunk_doc_id`` guards in
    ``_tombstone_redacted_documents`` and ``_precedents_sourced_from`` and
    all but the last test here fail.
    """

    TARGET = "notes#chunk-5"

    def _store(self, registry: StoreRegistry) -> None:
        # ``chunk_count`` of 3 with chunk 1 absent: the declared count bridges
        # the gap, as it does for any parent.
        _put_doc(registry, self.TARGET, chunk_count=3)
        _put_doc(registry, self.TARGET + "#chunk-0")
        _put_doc(registry, self.TARGET + "#chunk-2")

    def test_no_child_row_keeps_content_and_the_result_names_each(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, self.TARGET)
        self._store(registry)
        registry.knowledge.vector_store.upsert(self.TARGET + "#chunk-0", [0.1, 0.2])
        assert _served(registry) == [
            self.TARGET,
            self.TARGET + "#chunk-0",
            self.TARGET + "#chunk-2",
        ]

        payload = _redact(registry, self.TARGET)

        assert payload["documents_tombstoned"] == [
            self.TARGET + "#chunk-0",
            self.TARGET + "#chunk-2",
            self.TARGET,
        ]
        assert _served(registry) == []
        documents = registry.knowledge.document_store
        for doc_id in payload["documents_tombstoned"]:
            row = documents.get(doc_id)
            assert row is not None
            assert COPIED_TEXT not in repr(row)
        assert registry.knowledge.vector_store.get(self.TARGET + "#chunk-0") is None

    def test_it_scrubs_precedents_sourced_from_itself_and_its_chunks(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, self.TARGET)
        own = _promote(registry, self.TARGET)
        child = _promote(registry, self.TARGET + "#chunk-0")
        untouched = [
            _promote(registry, "notes"),
            _promote(registry, "notes#chunk-50"),
            _promote(registry, "notes#chunk-6"),
        ]
        before = {p: _everything_stored(registry, p) for p in untouched}

        payload = _redact(registry, self.TARGET)

        assert sorted(payload["scrubbed_precedent_ids"]) == sorted([own, child])
        for precedent_id in (own, child):
            assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        assert {p: _everything_stored(registry, p) for p in untouched} == before

    def test_siblings_sharing_the_prefix_keep_their_text(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, self.TARGET)
        self._store(registry)
        siblings = ("notes", "notes#chunk-4", "notes#chunk-50", "notes#chunk-5x")
        for doc_id in siblings:
            _put_doc(registry, doc_id)

        _redact(registry, self.TARGET)

        assert _served(registry) == sorted(siblings)

    def test_a_minted_chunk_target_still_blanks_only_itself(
        self, registry: StoreRegistry
    ) -> None:
        """A real chunk has no children, so cascading from it reaches nothing."""
        _node(registry, "notes#chunk-1")
        for index in range(3):
            _put_doc(registry, f"notes#chunk-{index}", chunk_count=3)
        _put_doc(registry, "notes", chunk_count=3)

        payload = _redact(registry, "notes#chunk-1")

        assert payload["documents_tombstoned"] == ["notes#chunk-1"]
        assert _served(registry) == ["notes", "notes#chunk-0", "notes#chunk-2"]


class TestNothingToScrub:
    def test_no_precedents_leaves_the_payload_as_it_was(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-z")
        payload = _redact(registry, "doc-z")
        assert set(payload) == EXPECTED_KEYS

    def test_precedent_that_never_copied_an_excerpt_is_left_alone(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-n")
        # Sourced from the target, but promoted with no document store, so
        # its description carries no excerpt.
        payloads = build_learning_promotion_payloads(
            candidate=_candidate("doc-n"), promotion_name="", rationale=""
        )
        props = dict(payloads["entity_payload"]["properties"])
        assert props["source_excerpt_status"] == "no_document_store"
        graph.upsert_node("prec-n", "precedent", {"name": "p", **props})
        graph.upsert_node("prec-n", "precedent", {"name": "p", **props, "v": 2})
        before = _everything_stored(registry, "prec-n")

        payload = _redact(registry, "doc-n")

        assert set(payload) == EXPECTED_KEYS
        assert _everything_stored(registry, "prec-n") == before

    def test_second_redaction_finds_the_precedent_already_scrubbed(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        _redact(registry, "doc-a")
        graph = registry.knowledge.graph_store
        state = _everything_stored(registry, precedent_id)
        _node(registry, "doc-a")

        RedactionApplyHandler(registry).handle(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": "doc-a", "reason": "again"},
            )
        )

        second = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )[-1]
        assert "scrubbed_precedent_ids" not in second.payload
        assert _everything_stored(registry, precedent_id) == state
        assert graph.get_node("doc-a") is None


class TestPrecedentNames:
    """A name derived from the source's first line is source text too."""

    BODY = f"{NAME_TEXT}\n\n{COPIED_TEXT}"

    def test_a_name_taken_from_the_source_is_renamed(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", body=self.BODY, source_named=True)
        graph = registry.knowledge.graph_store
        node = graph.get_node(precedent_id)
        assert node is not None
        assert NAME_TEXT in node["properties"]["name"]
        assert node["properties"]["name_source"] == "source_text"
        (promoted,) = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED
        )
        assert promoted.payload["name_source"] == "source_text"
        assert NAME_TEXT in promoted.payload["title"]

        _two_versions(registry, precedent_id)
        _redact(registry, "doc-a")

        node = graph.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["name"] == (
            f"Learning: asset_generation :: {precedent_id}"
        )
        assert node["properties"]["name_source"] == "neutral"
        everything = _everything_stored(registry, precedent_id)
        assert NAME_TEXT not in everything
        assert COPIED_TEXT not in everything

    def test_the_source_name_alias_goes_with_the_name(
        self, registry: StoreRegistry
    ) -> None:
        # ENTITY_CREATE binds a ``name`` alias whose raw_id and raw_name are
        # the name, so the alias holds the source text the rename removes.
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", body=self.BODY, source_named=True)
        graph.upsert_alias(precedent_id, "ext", "ext-1", raw_name="External label")
        old_name = f"Learning: asset_generation :: {NAME_TEXT}"
        old_key = normalize_entity_name(old_name)
        assert [a["raw_name"] for a in graph.get_aliases(precedent_id, "name")] == [
            old_name
        ]
        resolved = graph.resolve_alias("name", old_key)
        assert resolved is not None
        assert resolved["entity_id"] == precedent_id

        result = build_curate_executor(registry, clearance="restricted").execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": "doc-a", "reason": "owner request"},
            )
        )

        assert result.status == CommandStatus.SUCCESS
        assert NAME_TEXT not in _everything_stored(registry, precedent_id)
        assert NAME_TEXT.lower() not in repr(graph.get_aliases(precedent_id)).lower()
        assert graph.resolve_alias("name", old_key) is None
        # Not the discriminating line: seeding already skips an alias whose
        # node was renamed. Pinned so a change to that check cannot reopen it.
        assert NamespaceSeedExtractor(graph).extract(old_name) == []
        # An alias from another system carries no source-derived name.
        assert [
            (a["source_system"], a["raw_id"]) for a in graph.get_aliases(precedent_id)
        ] == [("ext", "ext-1")]

    def test_an_earlier_source_name_alias_goes_when_the_name_has_changed(
        self, registry: StoreRegistry
    ) -> None:
        # A rename binds the new name and leaves the old alias, so the alias
        # of a source-derived name outlives the version that held the name.
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", body=self.BODY, source_named=True)
        node = graph.get_node(precedent_id)
        assert node is not None
        graph.upsert_node(
            precedent_id,
            node["node_type"],
            {**node["properties"], "name": "Reviewed title", "name_source": "reviewer"},
            document_ids=node.get("document_ids") or None,
        )
        graph.upsert_alias(
            precedent_id, "name", "reviewed title", raw_name="Reviewed title"
        )
        assert len(graph.get_aliases(precedent_id, "name")) == 2

        _redact(registry, "doc-a")

        assert NAME_TEXT not in _everything_stored(registry, precedent_id)
        assert [a["raw_name"] for a in graph.get_aliases(precedent_id, "name")] == [
            "Reviewed title"
        ]
        current = graph.get_node(precedent_id)
        assert current is not None
        assert current["properties"]["name"] == "Reviewed title"

    def test_a_source_name_alias_matches_on_its_key_or_its_raw_name(
        self, registry: StoreRegistry
    ) -> None:
        # The binding ENTITY_CREATE writes has raw_id == normalize(raw_name),
        # so it cannot tell the two halves of the match apart. Here one alias
        # carries only the key and another only a raw name that normalizes
        # to it; each half alone must drop its alias.
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", body=self.BODY, source_named=True)
        old_name = f"Learning: asset_generation :: {NAME_TEXT}"
        old_key = normalize_entity_name(old_name)
        graph.upsert_alias(precedent_id, "name", old_key, raw_name=None)
        graph.upsert_alias(precedent_id, "name", "legacy-key", raw_name=old_name)
        by_id = {a["raw_id"]: a["raw_name"] for a in graph.get_aliases(precedent_id)}
        assert by_id == {old_key: None, "legacy-key": old_name}

        _redact(registry, "doc-a")

        assert graph.get_aliases(precedent_id, "name") == []
        assert graph.resolve_alias("name", old_key) is None
        assert graph.resolve_alias("name", "legacy-key") is None

    def test_a_name_alone_is_scrubbed_when_no_excerpt_was_copied(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-n")
        payloads = build_learning_promotion_payloads(
            candidate={
                **_candidate("doc-n"),
                "precedent_name": f"Learning: asset_generation :: {NAME_TEXT}",
                "precedent_name_source": "source_text",
            },
            promotion_name="",
            rationale="",
        )
        props = payloads["entity_payload"]["properties"]
        assert props["source_excerpt_status"] == "no_document_store"
        graph.upsert_node(
            "prec-n", "precedent", {**props, "name": payloads["entity_payload"]["name"]}
        )

        payload = _redact(registry, "doc-n")

        assert payload["scrubbed_precedent_ids"] == ["prec-n"]
        node = graph.get_node("prec-n")
        assert node is not None
        assert NAME_TEXT not in repr(node)
        assert node["properties"]["name_source"] == "neutral"
        assert node["properties"]["source_excerpt_status"] == "no_document_store"

    def test_a_title_name_is_renamed_as_the_row_loses_its_title(
        self, registry: StoreRegistry
    ) -> None:
        """The tombstone blanks the document row's title (``_tombstone_document``),
        so a name copied from the item's title is the redacted document's text
        too (#1032). Only a reviewer's name and the bare id stay."""
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", title_named=True)
        assert TITLE_TEXT in _everything_stored(registry, precedent_id)
        _redact(registry, "doc-a")
        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["name"] == (
            f"Learning: asset_generation :: {precedent_id}"
        )
        assert node["properties"]["name_source"] == "neutral"
        assert TITLE_TEXT not in _everything_stored(registry, precedent_id)

    @pytest.mark.parametrize("marker", ["reviewer", "item_id"])
    def test_a_reviewer_or_bare_id_name_survives(
        self, registry: StoreRegistry, marker: str
    ) -> None:
        _node(registry, "doc-r")
        precedent_id = _promote(registry, "doc-r")
        graph = registry.knowledge.graph_store
        row = graph.get_node(precedent_id)
        assert row is not None
        graph.upsert_node(
            precedent_id,
            "precedent",
            {**row["properties"], "name": "Kept name", "name_source": marker},
        )
        _redact(registry, "doc-r")
        node = graph.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["name"] == "Kept name"
        assert node["properties"]["name_source"] == marker

    def test_an_unmarked_name_fails_closed_unless_it_is_a_bare_id(
        self, registry: StoreRegistry
    ) -> None:
        # Promoted before names were marked: nothing says where it came from.
        graph = registry.knowledge.graph_store
        _node(registry, "doc-u")
        base = {
            "intent_family": "asset_generation",
            "source_item_id": "doc-u",
            "source_excerpt_status": "no_content",
        }
        graph.upsert_node(
            "prec-text", "precedent", {**base, "name": f"Learning: x :: {NAME_TEXT}"}
        )
        bare = "Learning: asset_generation :: doc-u"
        graph.upsert_node("prec-bare", "precedent", {**base, "name": bare})

        payload = _redact(registry, "doc-u")

        assert payload["scrubbed_precedent_ids"] == ["prec-text"]
        assert NAME_TEXT not in _everything_stored(registry, "prec-text")
        node = graph.get_node("prec-bare")
        assert node is not None
        assert node["properties"]["name"] == bare

    def test_listing_serves_the_neutral_title(self, registry: StoreRegistry) -> None:
        # PRECEDENT_PROMOTED is immutable and holds the title as promoted.
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a", body=self.BODY, source_named=True)
        graph = registry.knowledge.graph_store
        log = registry.operational.event_log
        (row,) = list_precedents(log, graph_store=graph)
        assert NAME_TEXT in row["title"]

        _redact(registry, "doc-a")

        (row,) = list_precedents(log, graph_store=graph)
        assert row["title"] == f"Learning: asset_generation :: {precedent_id}"
        assert NAME_TEXT not in repr(row)


class TestPrecedentScan:
    """The precedent lookup reads every precedent, or the redaction fails."""

    def test_a_precedent_beyond_the_first_page_is_scrubbed(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Precedents are read in node-id order; "z" sorts after every other.
        for name in ("a1", "a2", "a3", "a4"):
            _promote(registry, f"doc-{name}")
        _node(registry, "doc-z")
        last = _promote(registry, "doc-z")
        monkeypatch.setattr(handlers, "_PRECEDENT_PAGE_SIZE", 2)
        graph = registry.knowledge.graph_store
        pages: list[int] = []
        real = graph.search_nodes

        def counting(**kwargs: Any) -> Any:
            pages.append(kwargs["offset"])
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", counting)

        payload = _redact(registry, "doc-z")

        assert pages == [0, 2, 4]
        assert payload["scrubbed_precedent_ids"] == [last]
        assert COPIED_TEXT not in _everything_stored(registry, last)
        assert "precedent_scan_capped" not in payload

    def test_a_scan_that_fails_part_way_refuses_before_any_purge(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        for name in ("b1", "b2", "b3"):
            _promote(registry, f"doc-{name}")
        monkeypatch.setattr(handlers, "_PRECEDENT_PAGE_SIZE", 2)
        graph = registry.knowledge.graph_store
        real = graph.search_nodes

        def fail_on_second_page(**kwargs: Any) -> Any:
            if kwargs["offset"]:
                msg = "page read failed"
                raise StoreError(msg, store="graph")
            return real(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", fail_on_second_page)
        before = _everything_stored(registry, precedent_id)

        with pytest.raises(StoreError, match="page read failed"):
            _redact(registry, "doc-a")

        assert graph.get_node("doc-a") is not None
        assert _everything_stored(registry, precedent_id) == before
        assert not registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )

    def test_a_set_that_changes_size_mid_scan_refuses_before_any_purge(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Offset paging skips a row when an earlier one vanishes between
        # pages, so a changed total must refuse, not finish a partial scan.
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        for name in ("b1", "b2", "b3"):
            _promote(registry, f"doc-{name}")
        monkeypatch.setattr(handlers, "_PRECEDENT_PAGE_SIZE", 2)
        graph = registry.knowledge.graph_store
        real = graph.search_nodes

        def shrinking(**kwargs: Any) -> Any:
            rows, total = real(**kwargs)
            return rows, total - 1 if kwargs["offset"] else total

        monkeypatch.setattr(graph, "search_nodes", shrinking)
        before = _everything_stored(registry, precedent_id)

        with pytest.raises(StoreError, match="changed during the redaction scan"):
            _redact(registry, "doc-a")

        assert graph.get_node("doc-a") is not None
        assert _everything_stored(registry, precedent_id) == before
        assert not registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )


class TestFailureOrder:
    def test_failed_scrub_leaves_the_target_in_place(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        real = graph.upsert_node

        def flaky(node_id: Any, *args: Any, **kwargs: Any) -> str:
            if node_id == precedent_id:
                msg = "backend down"
                raise RuntimeError(msg)
            return real(node_id, *args, **kwargs)

        monkeypatch.setattr(graph, "upsert_node", flaky)

        with pytest.raises(StoreError, match="could not be re-created"):
            RedactionApplyHandler(registry).handle(
                Command(
                    operation=Operation.REDACTION_APPLY,
                    args={"target_id": "doc-a", "reason": "owner request"},
                )
            )

        assert graph.get_node("doc-a") is not None
        assert not registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        # The node is gone, and the listing does not fall back to the
        # excerpt the immutable PRECEDENT_PROMOTED event still holds.
        assert graph.get_node(precedent_id) is None
        log = registry.operational.event_log
        (row,) = list_precedents(log, graph_store=graph)
        assert row["entity_id"] == precedent_id
        assert COPIED_TEXT not in repr(row)
        assert "Reviewed learning for intent family" in row["description"]

        # A re-run purges the target and still serves no excerpt.
        monkeypatch.setattr(graph, "upsert_node", real)
        _redact(registry, "doc-a")
        assert graph.get_node("doc-a") is None
        (row,) = list_precedents(log, graph_store=graph)
        assert COPIED_TEXT not in repr(row)

    def test_a_failed_document_write_leaves_every_precedent_unscrubbed(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scrub is irreversible, so nothing that can fail runs after it."""
        graph = registry.knowledge.graph_store
        store = registry.knowledge.document_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        _put_doc(registry, "doc-a", chunk_count=1)
        _put_doc(registry, "doc-a#chunk-0")
        # Deleted before the documents are touched, and a re-run cannot
        # report it, so the failure log is its only record.
        registry.knowledge.vector_store.upsert("doc-a", [0.1, 0.2, 0.3])
        history_before = _everything_stored(registry, precedent_id)
        real_put = store.put

        def failing_put(doc_id: str | None, *args: Any, **kwargs: Any) -> str:
            if doc_id == "doc-a":
                msg = "document write failed"
                raise StoreError(msg, store="document")
            return real_put(doc_id, *args, **kwargs)

        monkeypatch.setattr(store, "put", failing_put)
        with capture_logs() as logs, pytest.raises(StoreError, match="document write"):
            _redact(registry, "doc-a")

        # The precedent was not touched: still carrying the excerpt it was
        # promoted with, so the re-run finds and reports it.
        assert _everything_stored(registry, precedent_id) == history_before
        assert COPIED_TEXT in _everything_stored(registry, precedent_id)
        assert graph.get_node("doc-a") is not None
        (blanked,) = (
            e
            for e in logs
            if e["event"] == "redaction.documents_blanked_before_failure"
        )
        assert blanked["documents_blanked"] == ["doc-a#chunk-0"]
        assert blanked["target_vector_deleted"] is True
        assert not any(
            e["event"] == "redaction.precedents_scrubbed_before_failure" for e in logs
        )

        monkeypatch.setattr(store, "put", real_put)
        payload = _redact(registry, "doc-a")

        assert payload["scrubbed_precedent_ids"] == [precedent_id]
        assert payload["documents_tombstoned"] == ["doc-a#chunk-0", "doc-a"]
        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        assert graph.get_node("doc-a") is None

    def test_a_row_whose_write_fails_after_its_vector_delete_is_named(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not blanked, so not in ``documents_blanked``; its vector is gone."""
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a", chunk_count=2)
        _put_doc(registry, "doc-a#chunk-0")
        _put_doc(registry, "doc-a#chunk-1")
        vectors = registry.knowledge.vector_store
        for doc_id in ("doc-a#chunk-0", "doc-a#chunk-1"):
            vectors.upsert(doc_id, [0.1, 0.2, 0.3])
        store = registry.knowledge.document_store
        real_put = store.put

        def failing_put(doc_id: str | None, *args: Any, **kwargs: Any) -> str:
            if doc_id == "doc-a#chunk-1":
                msg = "document write failed"
                raise StoreError(msg, store="document")
            return real_put(doc_id, *args, **kwargs)

        monkeypatch.setattr(store, "put", failing_put)
        with capture_logs() as logs, pytest.raises(StoreError, match="document write"):
            _redact(registry, "doc-a")

        (blanked,) = (
            e
            for e in logs
            if e["event"] == "redaction.documents_blanked_before_failure"
        )
        assert blanked["documents_blanked"] == ["doc-a#chunk-0"]
        assert blanked["documents_vector_deleted_not_blanked"] == ["doc-a#chunk-1"]
        assert vectors.get("doc-a#chunk-1") is None
        unblanked = store.get("doc-a#chunk-1")
        assert unblanked is not None
        assert unblanked["content"]

    def test_a_failed_purge_names_no_row_that_lost_only_its_vector(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a")
        real_delete = graph.delete_node
        monkeypatch.setattr(
            graph,
            "delete_node",
            lambda node_id: False if node_id == "doc-a" else real_delete(node_id),
        )

        with capture_logs() as logs, pytest.raises(NotFoundError):
            _redact(registry, "doc-a")

        (blanked,) = (
            e
            for e in logs
            if e["event"] == "redaction.documents_blanked_before_failure"
        )
        assert blanked["documents_vector_deleted_not_blanked"] == []

    def test_a_failed_purge_logs_what_the_run_had_already_done(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        _put_doc(registry, "doc-a")
        real_delete = graph.delete_node
        monkeypatch.setattr(
            graph,
            "delete_node",
            lambda node_id: False if node_id == "doc-a" else real_delete(node_id),
        )

        with capture_logs() as logs, pytest.raises(NotFoundError):
            _redact(registry, "doc-a")

        events = {e["event"]: e for e in logs}
        scrubbed = events["redaction.precedents_scrubbed_before_failure"]
        assert scrubbed["scrubbed_precedent_ids"] == [precedent_id]
        blanked = events["redaction.documents_blanked_before_failure"]
        assert blanked["documents_blanked"] == ["doc-a"]
        assert blanked["target_vector_deleted"] is False

    def test_a_failed_purge_with_no_documents_still_names_the_deleted_vector(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        registry.knowledge.vector_store.upsert("doc-a", [0.1, 0.2, 0.3])
        real_delete = graph.delete_node
        monkeypatch.setattr(
            graph,
            "delete_node",
            lambda node_id: False if node_id == "doc-a" else real_delete(node_id),
        )

        with capture_logs() as logs, pytest.raises(NotFoundError):
            _redact(registry, "doc-a")

        assert registry.knowledge.vector_store.get("doc-a") is None
        events = {e["event"]: e for e in logs}
        blanked = events["redaction.documents_blanked_before_failure"]
        assert blanked["documents_blanked"] == []
        assert blanked["target_vector_deleted"] is True

    def test_failed_edge_restore_names_every_unrestored_link(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        _node(registry, "entity://shared")
        precedent_id = _promote(registry, "doc-a")
        graph.upsert_edge("entity://shared", precedent_id, "cites", {})
        before = graph.get_edges(precedent_id, direction="both")
        assert before
        real = graph.upsert_edge

        def flaky(source_id: Any, target_id: Any, *args: Any, **kwargs: Any) -> str:
            if precedent_id in (source_id, target_id):
                msg = "backend down"
                raise RuntimeError(msg)
            return real(source_id, target_id, *args, **kwargs)

        monkeypatch.setattr(graph, "upsert_edge", flaky)

        with pytest.raises(StoreError, match="could not be restored") as raised:
            _redact(registry, "doc-a")

        for edge in before:
            assert f"-{edge['edge_type']}->" in str(raised.value)
        assert "entity://shared -cites->" in str(raised.value)
        assert "could not be re-created" not in str(raised.value)
        node = graph.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["source_excerpt_status"] == SOURCE_EXCERPT_REDACTED
        assert graph.get_node("doc-a") is not None


class TestHistoryOnlyExcerpt:
    def test_an_excerpt_left_only_in_history_is_still_purged(
        self, registry: StoreRegistry
    ) -> None:
        # A later version that no longer carries the excerpt does not make
        # the precedent clean: get_node_history and as_of still read it.
        from trellis.learning.scoring import strip_source_excerpt

        graph = registry.knowledge.graph_store
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        node = graph.get_node(precedent_id)
        assert node is not None
        props = dict(node["properties"])
        props["description"] = strip_source_excerpt(props["description"], "doc-a")
        props["source_excerpt_status"] = "document_not_found"
        graph.upsert_node(
            precedent_id,
            node["node_type"],
            props,
            document_ids=node.get("document_ids") or None,
        )
        current = graph.get_node(precedent_id)
        assert current is not None
        assert COPIED_TEXT not in repr(current)
        assert COPIED_TEXT in _everything_stored(registry, precedent_id)

        payload = _redact(registry, "doc-a")

        assert payload["scrubbed_precedent_ids"] == [precedent_id]
        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)


def _put_doc(registry: StoreRegistry, doc_id: str, **metadata: Any) -> None:
    registry.knowledge.document_store.put(doc_id, COPIED_TEXT, metadata)


def _served(registry: StoreRegistry) -> list[str]:
    """Ids a keyword search for the copied text returns, chunks included."""
    hits = registry.knowledge.document_store.search("ZEBRA", include_chunks=True)
    return sorted(hit["doc_id"] for hit in hits)


class TestDocumentRows:
    """#935: redaction blanks the document the target is, and its chunks."""

    def test_the_targets_document_stops_serving_its_text(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a", title="TITLE-WALRUS", document_form="memo")
        registry.knowledge.vector_store.upsert("doc-a", [0.1, 0.2, 0.3])
        assert _served(registry) == ["doc-a"]

        payload = _redact(registry, "doc-a")

        assert _served(registry) == []
        row = registry.knowledge.document_store.get("doc-a")
        assert row is not None
        assert row["content"] == ""
        assert "TITLE-WALRUS" not in repr(row)
        assert set(row["metadata"]) == {"lifecycle", "document_form"}
        assert row["metadata"]["lifecycle"]["state"] == "archived"
        assert row["metadata"]["lifecycle"]["deprecation_reason"] == "redacted"
        assert registry.knowledge.vector_store.get("doc-a") is None
        assert payload["documents_tombstoned"] == ["doc-a"]
        assert COPIED_TEXT not in repr(payload)

    def test_a_document_the_node_only_links_is_left_alone(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a", document_ids=["linked"])
        _put_doc(registry, "doc-a")
        registry.knowledge.document_store.put("linked", "LINKED-BODY", {})

        _redact(registry, "doc-a")

        linked = registry.knowledge.document_store.get("linked")
        assert linked is not None
        assert linked["content"] == "LINKED-BODY"

    def test_a_node_with_no_document_adds_no_key(self, registry: StoreRegistry) -> None:
        _node(registry, "doc-a")

        payload = _redact(registry, "doc-a")

        assert set(payload) == EXPECTED_KEYS
        assert registry.knowledge.document_store.get("doc-a") is None

    def test_a_parent_takes_its_chunks_with_it(self, registry: StoreRegistry) -> None:
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a", chunk_count=2)
        _put_doc(registry, "doc-a#chunk-0", parent_doc_id="doc-a")
        _put_doc(registry, "doc-a#chunk-1", parent_doc_id="doc-a")
        _put_doc(registry, "doc-ab#chunk-0")  # another document's chunk
        registry.knowledge.vector_store.upsert("doc-a#chunk-1", [0.1, 0.2, 0.3])

        payload = _redact(registry, "doc-a")

        assert _served(registry) == ["doc-ab#chunk-0"]
        assert payload["documents_tombstoned"] == [
            "doc-a#chunk-0",
            "doc-a#chunk-1",
            "doc-a",
        ]
        assert registry.knowledge.vector_store.get("doc-a#chunk-1") is None

    def test_a_chunk_target_blanks_only_that_chunk(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a#chunk-1")
        _put_doc(registry, "doc-a", chunk_count=2)
        _put_doc(registry, "doc-a#chunk-0")
        _put_doc(registry, "doc-a#chunk-1")

        payload = _redact(registry, "doc-a#chunk-1")

        assert _served(registry) == ["doc-a", "doc-a#chunk-0"]
        assert payload["documents_tombstoned"] == ["doc-a#chunk-1"]

    def test_a_failed_write_leaves_the_target_and_a_rerun_finishes(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a", chunk_count=2)
        _put_doc(registry, "doc-a#chunk-0")
        _put_doc(registry, "doc-a#chunk-1")
        store = registry.knowledge.document_store
        real_put = store.put

        def failing_put(doc_id: str | None, *args: Any, **kwargs: Any) -> str:
            if doc_id == "doc-a":
                msg = "document write failed"
                raise StoreError(msg, store="document")
            return real_put(doc_id, *args, **kwargs)

        monkeypatch.setattr(store, "put", failing_put)
        with pytest.raises(StoreError, match="document write failed"):
            _redact(registry, "doc-a")

        # Chunks went first and the parent kept its count, so the target is
        # still there to redact and the chunks can still be found.
        assert registry.knowledge.graph_store.get_node("doc-a") is not None
        assert _served(registry) == ["doc-a"]
        parent = store.get("doc-a")
        assert parent is not None
        assert parent["metadata"]["chunk_count"] == 2

        monkeypatch.setattr(store, "put", real_put)
        payload = _redact(registry, "doc-a")

        assert _served(registry) == []
        assert payload["documents_tombstoned"][-1] == "doc-a"


class TestPolicyScope:
    """The gate is consulted about the precedents the scrub rewrites."""

    @staticmethod
    def _executor(
        registry: StoreRegistry, *, scope: str, action: str, enforcement: Enforcement
    ) -> MutationExecutor:
        policy = Policy(
            policy_id="pol-redact",
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value=scope),
            rules=[PolicyRule(operation="redaction.apply", action=action)],  # type: ignore[arg-type]
            enforcement=enforcement,
        )
        return MutationExecutor(
            event_log=registry.operational.event_log,
            handlers=create_curate_handlers(registry),
            policy_gate=DefaultPolicyGate([policy]),
        )

    @staticmethod
    def _command() -> Command:
        return Command(
            operation=Operation.REDACTION_APPLY,
            args={"target_id": "doc-a", "reason": "owner request"},
            target_id="doc-a",
            target_type="entity",
        )

    def test_a_policy_on_precedents_blocks_a_redaction_that_would_rewrite_one(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        executor = self._executor(
            registry, scope="precedent", action="deny", enforcement=Enforcement.ENFORCE
        )

        result = executor.execute(self._command())

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_violation"
        assert precedent_id in result.message
        # Nothing was written: target, precedent text and document all stand.
        assert registry.knowledge.graph_store.get_node("doc-a") is not None
        assert COPIED_TEXT in _everything_stored(registry, precedent_id)
        assert _served(registry) == sorted(["doc-a", precedent_id])

    def test_the_same_policy_does_not_fire_when_no_precedent_is_reached(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        executor = self._executor(
            registry, scope="precedent", action="deny", enforcement=Enforcement.ENFORCE
        )

        result = executor.execute(self._command())

        assert result.status == CommandStatus.SUCCESS

    def test_a_warning_on_a_precedent_rides_the_result(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        _promote(registry, "doc-a")
        executor = self._executor(
            registry, scope="precedent", action="warn", enforcement=Enforcement.ENFORCE
        )

        result = executor.execute(self._command())

        assert result.status == CommandStatus.SUCCESS
        assert any("pol-redact" in w for w in result.warnings)

    def test_an_unreadable_precedent_set_refuses_before_any_write(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a")
        graph = registry.knowledge.graph_store

        def unreadable(**_kwargs: Any) -> Any:
            msg = "graph unavailable"
            raise StoreError(msg, store="graph")

        monkeypatch.setattr(graph, "search_nodes", unreadable)
        executor = self._executor(
            registry, scope="precedent", action="deny", enforcement=Enforcement.ENFORCE
        )

        result = executor.execute(self._command())

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "policy_scope_unresolved"
        assert "graph unavailable" not in result.message
        assert graph.get_node("doc-a") is not None
        assert _served(registry) == ["doc-a"]

    def test_an_unreadable_precedent_set_does_not_refuse_a_warn_only_policy(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gate that can only warn must not block what it could never block."""
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        graph = registry.knowledge.graph_store
        real_search = graph.search_nodes
        reads: list[int] = []

        def unreadable_once(**kwargs: Any) -> Any:
            # The policy-scope read is the first; the scrub's own read, which
            # runs inside the handler, is not the failure under test.
            reads.append(1)
            if len(reads) == 1:
                msg = "graph unavailable"
                raise StoreError(msg, store="graph")
            return real_search(**kwargs)

        monkeypatch.setattr(graph, "search_nodes", unreadable_once)
        executor = self._executor(
            registry, scope="precedent", action="warn", enforcement=Enforcement.ENFORCE
        )

        result = executor.execute(self._command())

        assert result.status == CommandStatus.SUCCESS
        assert graph.get_node("doc-a") is None
        assert COPIED_TEXT not in _everything_stored(registry, precedent_id)
        (executed,) = (
            event
            for event in registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            if event.payload["operation"] == "redaction.apply"
        )
        assert executed.payload["policy_scope_unevaluated"] == "warn_only"
        (warning,) = result.warnings
        assert warning.startswith("policy_scope_unevaluated: ")
        assert "(StoreError)" in warning
        assert "graph unavailable" not in warning

    def test_an_empty_gate_neither_walks_the_set_twice_nor_refuses(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No policy, no scope: the outcome and the reads are the ungated ones."""
        _node(registry, "doc-a")
        _put_doc(registry, "doc-a")
        graph = registry.knowledge.graph_store
        reads: list[int] = []

        def unreadable(**_kwargs: Any) -> Any:
            reads.append(1)
            msg = "graph unavailable"
            raise StoreError(msg, store="graph")

        monkeypatch.setattr(graph, "search_nodes", unreadable)
        outcomes = []
        for gate in (None, DefaultPolicyGate([])):
            reads.clear()
            result = MutationExecutor(
                event_log=registry.operational.event_log,
                handlers=create_curate_handlers(registry),
                policy_gate=gate,
            ).execute(self._command())
            outcomes.append(
                (result.status, result.message, result.metadata, len(reads))
            )

        assert outcomes[0] == outcomes[1]
        status, _message, metadata, walks = outcomes[1]
        assert status == CommandStatus.FAILED
        assert "rejection_reason" not in metadata
        assert walks == 1


class TestPrecedentVectorRecord:
    """A scrub deletes the vector row; the event says when none came back."""

    def test_an_unembedded_vector_row_is_named_in_the_event(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        registry.knowledge.vector_store.upsert(precedent_id, [0.1, 0.2, 0.3])

        payload = _redact(registry, "doc-a")

        assert registry.knowledge.vector_store.get(precedent_id) is None
        assert payload["precedent_vectors_to_reembed"] == [precedent_id]
        assert payload["scrubbed_precedent_ids"] == [precedent_id]

    def test_a_precedent_with_no_vector_row_has_nothing_to_record(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "doc-a")
        _promote(registry, "doc-a")

        payload = _redact(registry, "doc-a")

        assert "precedent_vectors_to_reembed" not in payload

    def test_a_reembedded_row_is_not_reported_lost(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trellis.retrieve.embed_ingest_hook.embed_on_ingest_enabled",
            lambda registry=None: True,
        )
        monkeypatch.setattr(
            type(registry),
            "embedding_fn",
            property(lambda _self: lambda _t: [1.0, 0.0]),
        )
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        assert registry.knowledge.vector_store.get(precedent_id) is not None

        payload = _redact(registry, "doc-a")

        assert registry.knowledge.vector_store.get(precedent_id) is not None
        assert "precedent_vectors_to_reembed" not in payload
