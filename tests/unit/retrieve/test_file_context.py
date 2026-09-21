"""Tests for file-scoped context retrieval (#307, server-side half)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.document_recency import fake_document_clock
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.file_context import (
    _DOC_PAGE_SIZE,
    build_file_context,
    source_path_matches,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path


class TestSourcePathMatches:
    def test_exact_match(self) -> None:
        assert source_path_matches("notes/foo.md", "notes/foo.md")

    def test_absolute_query_matches_stored_relpath(self) -> None:
        assert source_path_matches("notes/foo.md", "/home/n/vault/notes/foo.md")

    def test_relpath_query_matches_stored_absolute(self) -> None:
        assert source_path_matches("/home/n/vault/notes/foo.md", "notes/foo.md")

    def test_suffix_must_sit_on_path_boundary(self) -> None:
        # "otes/foo.md" is a string suffix of "notes/foo.md" but not a
        # path suffix — matching it would cross a filename.
        assert not source_path_matches("notes/foo.md", "otes/foo.md")
        assert not source_path_matches("my-notes/foo.md", "notes/foo.md")

    def test_different_file_no_match(self) -> None:
        assert not source_path_matches("notes/foo.md", "notes/bar.md")

    def test_bare_basename_does_not_match_another_repos_file(self) -> None:
        # Corpus ingest stores vault-root files as a bare relpath. Every
        # repo on the machine also has a TODO.md, and a PreToolUse hook
        # fires on absolute paths — so a basename-only suffix match
        # would answer a read of one project's file with another's notes.
        assert not source_path_matches("TODO.md", "/home/n/projects/other/TODO.md")
        assert source_path_matches("TODO.md", "TODO.md")

    def test_conversation_title_shaped_like_a_filename_does_not_match(self) -> None:
        # Conversation ingest reuses ``source_path`` for the chat title,
        # which is free text and can read exactly like a basename.
        assert not source_path_matches("server.py", "/srv/app/server.py")

    def test_relpath_query_needs_a_directory_to_anchor_on(self) -> None:
        assert not source_path_matches("/home/n/vault/notes/foo.md", "foo.md")
        assert source_path_matches("/home/n/vault/notes/foo.md", "notes/foo.md")

    def test_non_string_or_empty_stored_never_matches(self) -> None:
        assert not source_path_matches(None, "notes/foo.md")
        assert not source_path_matches(42, "notes/foo.md")
        assert not source_path_matches("", "notes/foo.md")

    def test_empty_query_never_matches(self) -> None:
        assert not source_path_matches("notes/foo.md", "")


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True)
    return StoreRegistry(stores_dir=stores_dir)


def _build(registry: StoreRegistry, paths: list[str], **kwargs: object) -> dict:
    return build_file_context(
        registry.knowledge.document_store,
        registry.knowledge.graph_store,
        paths,
        **kwargs,  # type: ignore[arg-type]
    )


class TestBuildFileContext:
    def test_no_matches_returns_empty_entry_per_path(
        self, registry: StoreRegistry
    ) -> None:
        result = _build(registry, ["notes/foo.md"])
        assert result == {
            "paths": [
                {
                    "path": "notes/foo.md",
                    "documents": [],
                    "entities": [],
                    "newest_item_at": None,
                }
            ],
            "graph_scan_truncated": False,
        }

    def test_document_matched_by_source_path(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "corpus:vault:abc",
            "Gotcha: the API times out on cold start.",
            metadata={"source_path": "notes/foo.md", "source_system": "vault"},
        )
        result = _build(registry, ["/home/n/vault/notes/foo.md"])
        (entry,) = result["paths"]
        (doc,) = entry["documents"]
        assert doc["doc_id"] == "corpus:vault:abc"
        assert doc["source_path"] == "notes/foo.md"
        assert doc["source_system"] == "vault"
        assert "cold start" in doc["excerpt"]
        assert doc["created_at"] is not None
        assert entry["newest_item_at"] is not None

    def test_unrelated_document_not_matched(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "d1", "content", metadata={"source_path": "notes/bar.md"}
        )
        result = _build(registry, ["notes/foo.md"])
        assert result["paths"][0]["documents"] == []

    def test_chunk_documents_anchor_entities_but_are_not_listed(
        self, registry: StoreRegistry
    ) -> None:
        parent_id = "corpus:vault:abc"
        chunk_id = chunk_doc_id(parent_id, 0)
        registry.knowledge.document_store.put(
            parent_id, "parent content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.document_store.put(
            chunk_id,
            "chunk content",
            metadata={"source_path": "notes/foo.md", "parent_doc_id": parent_id},
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1",
            "concept",
            {"name": "Cold Start", "description": "API cold-start latency"},
            document_ids=[chunk_id],
        )
        result = _build(registry, ["notes/foo.md"])
        (entry,) = result["paths"]
        assert [d["doc_id"] for d in entry["documents"]] == [parent_id]
        (entity,) = entry["entities"]
        assert entity["entity_id"] == "ent-1"
        assert entity["name"] == "Cold Start"
        assert entity["document_ids"] == [chunk_id]

    def test_entity_linked_via_document_ids(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1",
            "Product",
            {"name": "Oura Ring", "description": "A sleep tracker option"},
            document_ids=["doc-1"],
        )
        result = _build(registry, ["notes/foo.md"])
        (entity,) = result["paths"][0]["entities"]
        assert entity["entity_type"] == "Product"
        assert entity["node_role"] == "semantic"
        assert entity["description"] == "A sleep tracker option"
        assert entity["updated_at"] is not None

    def test_entity_without_matching_doc_link_excluded(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-other", "concept", {"name": "Other"}, document_ids=["doc-other"]
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-unlinked", "concept", {"name": "Unlinked"}
        )
        result = _build(registry, ["notes/foo.md"])
        assert result["paths"][0]["entities"] == []

    def test_unconfirmed_mint_gated_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-unconfirmed",
            "Product",
            {"name": "Whoop", "extraction_status": "unconfirmed"},
            document_ids=["doc-1"],
        )
        assert _build(registry, ["notes/foo.md"])["paths"][0]["entities"] == []

        included = _build(registry, ["notes/foo.md"], include_unconfirmed=True)
        (entity,) = included["paths"][0]["entities"]
        assert entity["entity_id"] == "ent-unconfirmed"
        assert entity["extraction_status"] == "unconfirmed"

    def test_confirmed_mint_served_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-confirmed",
            "Product",
            {"name": "Oura", "extraction_status": "confirmed"},
            document_ids=["doc-1"],
        )
        (entity,) = _build(registry, ["notes/foo.md"])["paths"][0]["entities"]
        assert entity["extraction_status"] == "confirmed"

    def test_structural_node_gated_by_default(self, registry: StoreRegistry) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-structural",
            "file",
            {"name": "a column"},
            node_role="structural",
            document_ids=["doc-1"],
        )
        assert _build(registry, ["notes/foo.md"])["paths"][0]["entities"] == []

        included = _build(registry, ["notes/foo.md"], include_structural=True)
        (entity,) = included["paths"][0]["entities"]
        assert entity["node_role"] == "structural"

    def test_multiple_paths_grouped_independently(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-a", "content a", metadata={"source_path": "notes/a.md"}
        )
        registry.knowledge.document_store.put(
            "doc-b", "content b", metadata={"source_path": "notes/b.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-a", "concept", {"name": "A"}, document_ids=["doc-a"]
        )
        result = _build(registry, ["notes/a.md", "notes/b.md", "notes/none.md"])
        by_path = {entry["path"]: entry for entry in result["paths"]}
        assert [d["doc_id"] for d in by_path["notes/a.md"]["documents"]] == ["doc-a"]
        assert [e["entity_id"] for e in by_path["notes/a.md"]["entities"]] == ["ent-a"]
        assert [d["doc_id"] for d in by_path["notes/b.md"]["documents"]] == ["doc-b"]
        assert by_path["notes/b.md"]["entities"] == []
        assert by_path["notes/none.md"]["documents"] == []

    def test_blank_and_duplicate_paths_collapsed(self, registry: StoreRegistry) -> None:
        result = _build(registry, ["notes/foo.md", "  notes/foo.md  ", "", "   "])
        assert [entry["path"] for entry in result["paths"]] == ["notes/foo.md"]

    def test_all_blank_paths_short_circuit_before_scanning(
        self, registry: StoreRegistry
    ) -> None:
        calls: list[int] = []
        store = registry.knowledge.document_store
        original = store.list_documents

        def _counting(*args: object, **kwargs: object) -> list[dict]:
            calls.append(1)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        store.list_documents = _counting  # type: ignore[method-assign]
        result = _build(registry, ["", "   "])
        assert result == {"paths": [], "graph_scan_truncated": False}
        assert calls == []

    def test_newest_item_at_is_the_max_across_docs_and_entities(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-1", "concept", {"name": "A"}, document_ids=["doc-1"]
        )
        (entry,) = _build(registry, ["notes/foo.md"])["paths"]
        stamps = [
            i["updated_at"] or i["created_at"]
            for i in entry["documents"] + entry["entities"]
        ]
        assert entry["newest_item_at"] is not None
        # The reported stamp is the newest of the item stamps (all ISO-UTC,
        # so lexicographic max agrees with chronological max here).
        assert entry["newest_item_at"] >= max(str(s) for s in stamps)


class TestScanBoundaries:
    """The module's two scans — neither is reached by the fixture-sized
    cases above, where every corpus fits in one page and one node query."""

    def test_unlinked_nodes_do_not_crowd_out_a_doc_linked_one(
        self, registry: StoreRegistry
    ) -> None:
        """The graph cap must bite on doc-linked nodes, not on graph size.

        Without the doc-link filter a graph carrying more than the cap in
        nodes of *any* kind answered every file with zero entities: the
        scan spent its whole budget on rows that could not match.
        """
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_node(
            "ent-target", "concept", {"name": "Target"}, document_ids=["doc-1"]
        )
        registry.knowledge.graph_store.upsert_nodes_bulk(
            [
                {"node_id": f"filler-{i}", "node_type": "concept", "properties": {}}
                for i in range(20)
            ]
        )
        result = _build(registry, ["notes/foo.md"], graph_scan_limit=5)
        (entry,) = result["paths"]
        assert [e["entity_id"] for e in entry["entities"]] == ["ent-target"]
        assert result["graph_scan_truncated"] is False

    def test_saturated_graph_scan_is_reported_not_swallowed(
        self, registry: StoreRegistry
    ) -> None:
        """More doc-linked nodes than the cap: say so, don't imply absence."""
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        registry.knowledge.graph_store.upsert_nodes_bulk(
            [
                {
                    "node_id": f"linked-{i}",
                    "node_type": "concept",
                    "properties": {"name": f"Linked {i}"},
                    "document_ids": [f"doc-other-{i}"],
                }
                for i in range(6)
            ]
        )
        result = _build(registry, ["notes/foo.md"], graph_scan_limit=3)
        assert result["graph_scan_truncated"] is True

    def test_document_scan_pages_past_the_first_page(
        self, registry: StoreRegistry
    ) -> None:
        """A match beyond ``_DOC_PAGE_SIZE`` rows is still found."""
        store = registry.knowledge.document_store
        for i in range(_DOC_PAGE_SIZE + 5):
            store.put(f"filler-{i:04d}", "filler", metadata={"source_path": "x/f.md"})
        store.put("late", "the one", metadata={"source_path": "notes/late.md"})
        (entry,) = _build(registry, ["notes/late.md"])["paths"]
        assert [d["doc_id"] for d in entry["documents"]] == ["late"]

    def test_backend_without_a_dsl_compiler_falls_back_to_a_plain_scan(
        self, registry: StoreRegistry
    ) -> None:
        """``execute_node_query``'s default routing rejects ``exists``."""
        graph = registry.knowledge.graph_store
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        graph.upsert_node("ent-1", "concept", {"name": "T"}, document_ids=["doc-1"])

        def _no_compiler(_query: object) -> list[dict]:
            raise NotImplementedError

        graph.execute_node_query = _no_compiler  # type: ignore[method-assign]
        (entry,) = _build(registry, ["notes/foo.md"])["paths"]
        assert [e["entity_id"] for e in entry["entities"]] == ["ent-1"]


class TestNewestItemAtIsAStalenessGate:
    """``newest_item_at`` reads ``updated_at``, and two sweeps missed it (#406).

    #397 scoped the ``preserve_updated_at`` argument to ``KeywordSearch``'s
    recency decay; #406 found ``mutate.retention``'s ``lifecycle_states`` age
    gate reading the same column; three independent review passes on #418
    then found this one. Every one of those enumerations was written down as
    closed and every one was wrong, which is why this docstring gives no
    count either — and why these tests live beside the *reader* rather than
    beside any one writer.

    What makes this reader unlike the two before it is that it is a
    **gate, not a score**. The module docstring pins what the value is for:
    the client "compares that against the file's mtime and skips injection
    when the file changed after everything known about it was written." A
    metadata-only write that bumps ``updated_at`` makes memory look newer
    than the file, so the gate stops firing and the read hook injects the
    stale context it exists to suppress. There is no floor to soften that;
    it flips.

    And this surface has **no collect seam**. ``_matching_documents`` walks
    ``list_documents`` directly, so neither ``retrieve.lifecycle``'s
    ``exclude_archived`` nor ``retrieve.noise``'s ``exclude_noise`` applies —
    which is why #406's "latent" classification for
    ``RetentionPruneHandler._archive`` and ``classify.feedback`` does not
    hold here. That is pinned first, because the rest of the argument rests
    on it.
    """

    def test_archived_and_noise_documents_are_not_filtered_out(
        self, registry: StoreRegistry
    ) -> None:
        """The premise: this surface is not on ``PackBuilder``'s collect seam.

        Not a fix test — it passes against the un-fixed code, by design. It
        exists because the two "latent" arguments in ``mutate/handlers.py``
        and ``classify/feedback.py`` are scoped to the pack surfaces, and
        nothing else would fail if someone read them as unqualified.
        """
        from trellis.schemas.classification import LIFECYCLE_KEY

        docs = registry.knowledge.document_store
        docs.put("plain", "notes on widgets", {"source_path": "widget.py"})
        docs.put(
            "arch",
            "archived notes on widgets",
            {"source_path": "widget.py", LIFECYCLE_KEY: {"state": "archived"}},
        )
        docs.put(
            "noisy",
            "demoted notes on widgets",
            {
                "source_path": "widget.py",
                "content_tags": {"signal_quality": "noise"},
            },
        )

        (entry,) = _build(registry, ["widget.py"])["paths"]
        assert sorted(d["doc_id"] for d in entry["documents"]) == [
            "arch",
            "noisy",
            "plain",
        ]

    def test_a_metadata_only_demotion_does_not_move_the_gate(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails against an ``apply_noise_tags`` that omits the flag.

        Demoting a year-old note moved ``newest_item_at`` forward a full year
        — measured, not supposed — which is the whole staleness budget the
        read hook has. One assertion covers the shape for all four writers
        that reach this surface; the sibling writers' own suites pin that
        each of them passes the flag.
        """
        from datetime import timedelta

        from trellis.classify.feedback import apply_noise_tags

        docs = registry.knowledge.document_store
        clock = fake_document_clock(monkeypatch)
        now = clock["now"]

        clock["now"] = now - timedelta(days=365)
        docs.put("stale", "year-old notes on widgets", {"source_path": "widget.py"})
        before = _build(registry, ["widget.py"])["paths"][0]["newest_item_at"]
        assert before == (now - timedelta(days=365)).isoformat(), (
            "the fake clock is not reaching the store; this test would pass vacuously"
        )

        clock["now"] = now
        assert apply_noise_tags(["stale"], docs) == 1

        after = _build(registry, ["widget.py"])["paths"][0]["newest_item_at"]
        assert after == before


class TestFilesTouchedMatching:
    """The second key (#549).

    ``source_path`` is a self-description — *this is that file*. A trace
    summary's is ``trace/<id>``, a namespace, so no path-matching rule
    could ever reach it. ``files_touched`` is the other claim — *this is
    a record of changing that file* — and matching it is what makes the
    records of work on a file reachable from the file.
    """

    def test_a_trace_summary_is_reachable_by_a_file_it_edited(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Fixed the drift query; both paths were dead.",
            metadata={
                "source_path": "trace/t1",
                "source_system": "trellis-trace",
                "files_touched": ["src/trellis/retrieve/file_context.py"],
            },
        )
        result = _build(registry, ["src/trellis/retrieve/file_context.py"])
        (doc,) = result["paths"][0]["documents"]
        assert doc["doc_id"] == "trace-summary:t1"
        assert doc["matched_via"] == "files_touched"
        # The entry still reports the document's *own* path, unchanged —
        # the query path is the heading, not a property of the document.
        assert doc["source_path"] == "trace/t1"

    def test_source_path_alone_would_never_have_matched(
        self, registry: StoreRegistry
    ) -> None:
        """The baseline, measured in the same file rather than asserted from
        memory: with the touched key absent, the identical document is
        unreachable from the path — which is what every deployment did."""
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Fixed the drift query.",
            metadata={"source_path": "trace/t1", "source_system": "trellis-trace"},
        )
        result = _build(registry, ["src/trellis/retrieve/file_context.py"])
        assert result["paths"][0]["documents"] == []

    def test_source_path_match_is_labelled_and_wins(
        self, registry: StoreRegistry
    ) -> None:
        """A document satisfying both keys reports the stronger claim."""
        registry.knowledge.document_store.put(
            "doc-1",
            "content",
            metadata={
                "source_path": "notes/foo.md",
                "files_touched": ["notes/foo.md"],
            },
        )
        (doc,) = _build(registry, ["notes/foo.md"])["paths"][0]["documents"]
        assert doc["matched_via"] == "source_path"

    def test_every_entry_carries_the_key(self, registry: StoreRegistry) -> None:
        """Unconditional on both branches. A field present only on the new
        kind of match would make "matched by source_path" indistinguishable
        from "written before the field existed"."""
        registry.knowledge.document_store.put(
            "doc-1", "content", metadata={"source_path": "notes/foo.md"}
        )
        (doc,) = _build(registry, ["notes/foo.md"])["paths"][0]["documents"]
        assert doc["matched_via"] == "source_path"

    def test_members_are_matched_by_the_same_path_rule(
        self, registry: StoreRegistry
    ) -> None:
        """Per member, so the boundary and single-segment rules hold inside
        the list: a trace that edited a bare ``TODO.md`` must not answer a
        read of every other ``TODO.md`` on the machine."""
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Edited two files.",
            metadata={
                "source_path": "trace/t1",
                "files_touched": ["TODO.md", "src/trellis/retrieve/strategies.py"],
            },
        )
        assert (
            _build(registry, ["/home/n/projects/other/TODO.md"])["paths"][0][
                "documents"
            ]
            == []
        )
        # Same document, a member that does span a directory: reachable by
        # an absolute query, exactly as ``source_path`` would be.
        (doc,) = _build(
            registry, ["/home/n/projects/trellis-ai/src/trellis/retrieve/strategies.py"]
        )["paths"][0]["documents"]
        assert doc["doc_id"] == "trace-summary:t1"

    def test_the_unverified_companion_is_never_matched(
        self, registry: StoreRegistry
    ) -> None:
        """#308 splits attested evidence from model claims precisely so a
        join like this one can be trusted. Matching the companion would put
        a memory in front of an agent reading a file the trace never
        touched — the one place an unattested claim does the most damage."""
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Claimed more than it did.",
            metadata={
                "source_path": "trace/t1",
                "files_touched_unverified": ["src/trellis/retrieve/file_context.py"],
            },
        )
        result = _build(registry, ["src/trellis/retrieve/file_context.py"])
        assert result["paths"][0]["documents"] == []

    def test_a_malformed_value_costs_a_match_and_nothing_else(
        self, registry: StoreRegistry
    ) -> None:
        """The key is reachable through ``save_memory``'s free-form metadata,
        so a malformed value must not take down the whole scan for every
        path. Both shapes are here on purpose: a bare string is the likely
        mistake (a caller writing one path instead of a list) and is merely
        an unhelpful iterable, while a non-iterable is the one that raises —
        a guard tested only against the string is satisfied by ``is not
        None`` and never exercised."""
        registry.knowledge.document_store.put(
            "doc-str", "content", metadata={"files_touched": "notes/foo.md"}
        )
        registry.knowledge.document_store.put(
            "doc-int", "content", metadata={"files_touched": 42}
        )
        registry.knowledge.document_store.put(
            "doc-dict", "content", metadata={"files_touched": {"notes/foo.md": 1}}
        )
        registry.knowledge.document_store.put(
            "doc-good", "content", metadata={"source_path": "notes/foo.md"}
        )
        (doc,) = _build(registry, ["notes/foo.md"])["paths"][0]["documents"]
        assert doc["doc_id"] == "doc-good"

    def test_a_mixed_pool_is_partitioned_not_swept(
        self, registry: StoreRegistry
    ) -> None:
        """Three documents, one call: one matching on ``source_path``, one only
        via ``files_touched``, one matching neither. A pool where every member
        matches the same way cannot separate "reads the new key" from "returns
        everything" — the #447 uniformity trap, one layer up."""
        registry.knowledge.document_store.put(
            "doc-own",
            "The file's own notes.",
            metadata={"source_path": "src/trellis/retrieve/pack_builder.py"},
        )
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Edited the pack builder.",
            metadata={
                "source_path": "trace/t1",
                "files_touched": ["src/trellis/retrieve/pack_builder.py"],
            },
        )
        registry.knowledge.document_store.put(
            "trace-summary:t2",
            "Edited something else entirely.",
            metadata={
                "source_path": "trace/t2",
                "files_touched": ["src/trellis/retrieve/strategies.py"],
            },
        )
        (entry,) = _build(registry, ["src/trellis/retrieve/pack_builder.py"])["paths"]
        assert {d["doc_id"]: d["matched_via"] for d in entry["documents"]} == {
            "doc-own": "source_path",
            "trace-summary:t1": "files_touched",
        }

    def test_a_non_string_member_is_skipped_not_fatal(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "content",
            metadata={
                "source_path": "trace/t1",
                "files_touched": [42, None, "src/trellis/retrieve/strategies.py"],
            },
        )
        (doc,) = _build(registry, ["src/trellis/retrieve/strategies.py"])["paths"][0][
            "documents"
        ]
        assert doc["doc_id"] == "trace-summary:t1"

    def test_touched_documents_anchor_their_entities(
        self, registry: StoreRegistry
    ) -> None:
        """The Activity entity extracted from a trace that edited this file
        is file context by the same argument the trace summary is. Anchoring
        only the ``source_path`` half would return the document and hide the
        entities linked to it."""
        registry.knowledge.document_store.put(
            "trace-summary:t1",
            "Fixed the drift query.",
            metadata={
                "source_path": "trace/t1",
                "files_touched": ["src/trellis/retrieve/file_context.py"],
            },
        )
        registry.knowledge.graph_store.upsert_node(
            "trace:t1",
            "Activity",
            {"name": "Fix the drift query"},
            document_ids=["trace-summary:t1"],
        )
        result = _build(registry, ["src/trellis/retrieve/file_context.py"])
        (entity,) = result["paths"][0]["entities"]
        assert entity["entity_id"] == "trace:t1"

    def test_the_producer_and_the_matcher_agree(self, registry: StoreRegistry) -> None:
        """The seam this whole change lives or dies on, closed in one test
        rather than across two files: the metadata the trace-embed worker
        actually writes, fed to the matcher that actually reads it. A pair
        of tests each asserting its own side of a key name is how #325/#326
        shipped two readers of one seam that silently disagreed."""
        from trellis.schemas.enums import TraceSource
        from trellis.schemas.trace import Trace, TraceContext, TraceStep
        from trellis_workers.trace_embed.render import build_trace_metadata

        trace = Trace(
            trace_id="t1",
            source=TraceSource.AGENT,
            intent="Fix the drift query",
            context=TraceContext(domain="trellis-ops"),
            steps=[
                TraceStep(
                    step_type="tool_call",
                    name="Edit",
                    args={"file_path": "src/trellis/retrieve/file_context.py"},
                )
            ],
        )
        registry.knowledge.document_store.put(
            "trace-summary:t1", "Fixed it.", metadata=build_trace_metadata(trace)
        )
        result = _build(registry, ["src/trellis/retrieve/file_context.py"])
        (doc,) = result["paths"][0]["documents"]
        assert doc["doc_id"] == "trace-summary:t1"
        assert doc["matched_via"] == "files_touched"
