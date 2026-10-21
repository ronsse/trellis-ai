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

from trellis.errors import StoreError
from trellis.learning.scoring import (
    SOURCE_EXCERPT_REDACTED,
    _apply_readable_name_fallback,
    build_learning_promotion_payloads,
    submit_learning_promotion,
)
from trellis.mutate import build_curate_executor, handlers
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import RedactionApplyHandler
from trellis.retrieve.precedents import list_precedents
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

COPIED_TEXT = "ZEBRA-QUOKKA confidential sentence copied from the source"
NAME_TEXT = "NARWHAL-OKAPI first line of the source"
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
        "precedent_name": f"Learning :: {item_id}",
        # A title the item carried: holds no text from the source body.
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


def _promote(
    registry: StoreRegistry,
    item_id: str,
    *,
    body: str = COPIED_TEXT,
    source_named: bool = False,
) -> str:
    """Store ``item_id`` as a document and promote a precedent from it.

    ``source_named`` promotes a title-less candidate, so its name is the
    first line of ``body``, derived by the real readable-name fallback.
    """
    knowledge = registry.knowledge
    if knowledge.document_store.get(item_id) is None:
        knowledge.document_store.put(item_id, body, {})
    candidate = _candidate(item_id)
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
        build_curate_executor(registry),
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
    return repr([graph.get_node(node_id), graph.get_node_history(node_id)])


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
        assert props["name"] == "Learning :: doc-a"
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
        result = build_curate_executor(registry).execute(
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

    def test_a_title_name_survives(self, registry: StoreRegistry) -> None:
        _node(registry, "doc-a")
        precedent_id = _promote(registry, "doc-a")
        _redact(registry, "doc-a")
        node = registry.knowledge.graph_store.get_node(precedent_id)
        assert node is not None
        assert node["properties"]["name"] == "Learning :: doc-a"
        assert node["properties"]["name_source"] == "item_title"

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
