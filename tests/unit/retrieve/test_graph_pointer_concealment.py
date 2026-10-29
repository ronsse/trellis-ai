"""A graph node names no document a below-``restricted`` caller may not read.

A node carries no label (#1036), so a graph read serves it whole; what it can
leak is the id of a document in three places: ``document_ids``, the string
``properties["evidence_ref"]`` that ``save_knowledge`` writes beside it, and the
``properties["lifecycle"]["superseded_by"]`` that ``supersede_entity`` stamps. The
helper drops an unreadable id from a copy, judging every id the response names
in one pass, by the store and never by what else the response holds.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.file_context import build_file_context
from trellis.retrieve.lifecycle import partition_superseded
from trellis.retrieve.sensitivity import (
    EVIDENCE_POINTER_PATHS,
    SUPERSESSION_POINTER_PATHS,
    conceal_node_pointers,
    conceal_unreadable_successors,
)
from trellis.schemas.classification import Lifecycle
from trellis.schemas.pack import PackItem
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.graph import SQLiteGraphStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class CountingStore:
    """A document store that counts every ``get`` and can fail on demand."""

    def __init__(self, inner: SQLiteDocumentStore) -> None:
        self._inner = inner
        self.reads: list[str] = []
        self.fail = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get(self, doc_id: str) -> dict[str, Any] | None:
        self.reads.append(doc_id)
        if self.fail:
            msg = "store down"
            raise RuntimeError(msg)
        return self._inner.get(doc_id)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CountingStore]:
    inner = SQLiteDocumentStore(tmp_path / "documents.db")
    inner.put("doc-open", "open body", {})
    inner.put("doc-secret", "secret body", label("restricted"))
    inner.put("doc-conf", "confidential body", label("confidential"))
    yield CountingStore(inner)
    inner.close()


def _node(
    node_id: str = "node-1",
    document_ids: Any = None,
    **properties: Any,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "entity",
        "properties": {"name": node_id, **properties},
        "document_ids": document_ids,
    }


class TestDocumentIds:
    def test_an_unreadable_id_is_dropped_and_readable_ones_keep_their_order(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["doc-open", "doc-secret", "doc-ghost", "doc-conf"])
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out["document_ids"] == ["doc-open", "doc-ghost"]
        assert out["properties"] == node["properties"]
        assert out["node_id"] == "node-1"

    def test_a_confidential_clearance_still_hides_restricted(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["doc-conf", "doc-secret"])
        (out,) = conceal_node_pointers([node], store, "confidential")  # type: ignore[arg-type]
        assert out["document_ids"] == ["doc-conf"]

    def test_an_id_with_no_document_row_is_no_verdict_and_is_kept(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["evidence:ghost", "trace/abc"])
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node

    def test_a_chunk_is_judged_by_its_parent(self, store: CountingStore) -> None:
        store._inner.put(chunk_doc_id("doc-secret", 0), "chunk of the secret", {})
        store._inner.put(chunk_doc_id("doc-open", 0), "chunk of the open one", {})
        node = _node(
            document_ids=[chunk_doc_id("doc-secret", 0), chunk_doc_id("doc-open", 0)]
        )
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out["document_ids"] == [chunk_doc_id("doc-open", 0)]

    def test_a_store_error_hides_the_id(self, store: CountingStore) -> None:
        store.fail = True
        node = _node(document_ids=["doc-open"], evidence_ref="doc-open")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out["document_ids"] == []
        assert "evidence_ref" not in out["properties"]

    @pytest.mark.parametrize("malformed", ["doc-secret", {"doc-secret": 1}, 5, None])
    def test_a_document_ids_that_is_not_a_list_is_left_as_stored(
        self, store: CountingStore, malformed: Any
    ) -> None:
        node = _node(document_ids=malformed)
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node
        assert out["document_ids"] == malformed

    def test_a_non_string_member_is_kept_and_the_string_ids_are_still_judged(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["doc-open", 7, "doc-secret", None, ["doc-secret"]])
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out["document_ids"] == ["doc-open", 7, None, ["doc-secret"]]
        assert node["document_ids"][2] == "doc-secret"

    def test_a_node_with_no_document_ids_key_is_returned_as_is(
        self, store: CountingStore
    ) -> None:
        node = {"node_id": "bare", "node_type": "entity"}
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node
        assert store.reads == []


class TestEvidenceRef:
    def test_an_unreadable_evidence_ref_is_removed_not_blanked(
        self, store: CountingStore
    ) -> None:
        node = _node(
            document_ids=["doc-open"],
            evidence_ref="doc-secret",
            note="kept",
            confidence=0.5,
        )
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert "evidence_ref" not in out["properties"]
        assert out["properties"] == {
            "name": "node-1",
            "note": "kept",
            "confidence": 0.5,
        }
        assert out["document_ids"] == ["doc-open"]

    def test_a_readable_evidence_ref_and_every_other_property_stay(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["doc-open"], evidence_ref="doc-open", note="kept")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node

    @pytest.mark.parametrize(
        "value", [7, ["doc-secret"], {"id": "doc-secret"}, "", None]
    )
    def test_a_non_string_evidence_ref_is_left_as_stored(
        self, store: CountingStore, value: Any
    ) -> None:
        node = _node(document_ids=["doc-open"], evidence_ref=value)
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node
        assert out["properties"]["evidence_ref"] == value

    def test_a_node_whose_properties_is_not_a_mapping_is_left_as_stored(
        self, store: CountingStore
    ) -> None:
        node = {"node_id": "n", "properties": "doc-secret", "document_ids": []}
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node

    def test_a_save_knowledge_shaped_node_shows_neither_carrier(
        self, store: CountingStore
    ) -> None:
        """Both carriers hold the same id; dropping only one still leaks it."""
        node = _node(
            document_ids=["doc-secret"], evidence_ref="doc-secret", name="the claim"
        )
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out["document_ids"] == []
        assert "evidence_ref" not in out["properties"]
        assert "doc-secret" not in repr(out)

    def test_an_evidence_ref_alone_is_judged(self, store: CountingStore) -> None:
        node = _node(document_ids=None, evidence_ref="doc-secret")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert "evidence_ref" not in out["properties"]

    def test_a_chunk_evidence_ref_is_judged_by_its_parent(
        self, store: CountingStore
    ) -> None:
        store._inner.put(chunk_doc_id("doc-secret", 0), "chunk", {})
        node = _node(evidence_ref=chunk_doc_id("doc-secret", 0))
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert "evidence_ref" not in out["properties"]


class TestNoReadsAndBatching:
    @pytest.mark.parametrize("store_kind", ["restricted", "no-store"])
    def test_restricted_and_a_missing_store_read_nothing_and_change_nothing(
        self, store: CountingStore, store_kind: str
    ) -> None:
        nodes = [_node(document_ids=["doc-secret"], evidence_ref="doc-secret")]
        if store_kind == "restricted":
            out = conceal_node_pointers(nodes, store, "restricted")  # type: ignore[arg-type]
        else:
            out = conceal_node_pointers(nodes, None, "internal")
        assert store.reads == []
        assert all(a is b for a, b in zip(out, nodes, strict=True))

    def test_no_node_naming_an_id_costs_no_read(self, store: CountingStore) -> None:
        nodes = [_node("a", document_ids=[]), _node("b", document_ids=None)]
        out = conceal_node_pointers(nodes, store, "internal")  # type: ignore[arg-type]
        assert store.reads == []
        assert all(a is b for a, b in zip(out, nodes, strict=True))

    def test_many_nodes_cost_one_read_per_distinct_id(
        self, store: CountingStore
    ) -> None:
        nodes = [
            _node(
                f"n{i}",
                document_ids=["doc-secret", "doc-open"],
                evidence_ref="doc-secret",
            )
            for i in range(6)
        ] + [_node("tail", document_ids=["doc-conf"])]
        out = conceal_node_pointers(nodes, store, "internal")  # type: ignore[arg-type]
        assert sorted(store.reads) == ["doc-conf", "doc-open", "doc-secret"]
        assert [n["document_ids"] for n in out[:6]] == [["doc-open"]] * 6
        assert out[6]["document_ids"] == []

    def test_readable_ness_is_the_stores_not_the_responses(
        self, store: CountingStore
    ) -> None:
        """An id named by no other node is judged all the same."""
        nodes = [
            _node("a", document_ids=["doc-open"]),
            _node("b", document_ids=["doc-secret"]),
        ]
        out = conceal_node_pointers(nodes, store, "internal")  # type: ignore[arg-type]
        assert out[0]["document_ids"] == ["doc-open"]
        assert out[1]["document_ids"] == []
        assert "doc-secret" in store.reads

    def test_nodes_come_back_in_order_one_for_one(self, store: CountingStore) -> None:
        nodes = [_node(f"n{i}", document_ids=["doc-secret"]) for i in range(4)]
        out = conceal_node_pointers(iter(nodes), store, "internal")  # type: ignore[arg-type]
        assert [n["node_id"] for n in out] == ["n0", "n1", "n2", "n3"]


class TestCopyOnWrite:
    def test_a_node_naming_only_readable_ids_is_the_same_object(
        self, store: CountingStore
    ) -> None:
        node = _node(document_ids=["doc-open"], evidence_ref="doc-open")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node

    def test_the_input_is_never_mutated(self, store: CountingStore) -> None:
        node = _node(
            document_ids=["doc-open", "doc-secret"],
            evidence_ref="doc-secret",
            note="kept",
        )
        before = copy.deepcopy(node)
        props = node["properties"]
        ids = node["document_ids"]
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is not node
        assert node == before
        assert node["properties"] is props
        assert node["document_ids"] is ids
        assert out["properties"] is not props

    def test_only_the_node_that_names_a_hidden_id_is_copied(
        self, store: CountingStore
    ) -> None:
        clean = _node("clean", document_ids=["doc-open"])
        dirty = _node("dirty", document_ids=["doc-secret"])
        out = conceal_node_pointers([clean, dirty], store, "internal")  # type: ignore[arg-type]
        assert out[0] is clean
        assert out[1] is not dirty


class TestBuildFileContext:
    """An entity anchored by a readable document lists only readable ids.

    ``build_file_context`` is the shared builder; its entries carry the node's
    ``document_ids``, which the anchoring document does not make safe to list
    whole.
    """

    @pytest.fixture
    def graph(self, tmp_path: Path) -> Iterator[SQLiteGraphStore]:
        g = SQLiteGraphStore(tmp_path / "graph.db")
        g.upsert_node(
            "node-a",
            "entity",
            {"name": "claim a", "evidence_ref": "doc-secret"},
            document_ids=["doc-anchor", "doc-secret"],
        )
        g.upsert_node(
            "node-b",
            "entity",
            {"name": "claim b"},
            document_ids=["doc-secret", "doc-anchor", "doc-open"],
        )
        yield g
        g.close()

    @pytest.fixture
    def files(self, store: CountingStore) -> CountingStore:
        store._inner.put("doc-anchor", "anchor body", {"source_path": "svc/a.py"})
        return store

    def _entities(
        self, files: CountingStore, graph: SQLiteGraphStore, clearance: Any
    ) -> dict[str, dict[str, Any]]:
        result = build_file_context(
            files,  # type: ignore[arg-type]
            graph,
            ["svc/a.py", "svc/a.py"],
            clearance=clearance,
        )
        (entry,) = result["paths"]
        return {e["entity_id"]: e for e in entry["entities"]}

    def test_an_anchored_entity_lists_no_unreadable_id(
        self, files: CountingStore, graph: SQLiteGraphStore
    ) -> None:
        entities = self._entities(files, graph, "internal")
        assert set(entities) == {"node-a", "node-b"}
        assert entities["node-a"]["document_ids"] == ["doc-anchor"]
        assert entities["node-b"]["document_ids"] == ["doc-anchor", "doc-open"]
        assert "doc-secret" not in repr(entities)

    def test_the_entity_is_still_found_when_its_hidden_id_is_dropped(
        self, files: CountingStore, graph: SQLiteGraphStore
    ) -> None:
        """Anchoring runs on the stored ids; only the entry is concealed."""
        entities = self._entities(files, graph, "internal")
        assert entities["node-a"]["name"] == "claim a"

    def test_a_restricted_caller_lists_every_id(
        self, files: CountingStore, graph: SQLiteGraphStore
    ) -> None:
        entities = self._entities(files, graph, "restricted")
        assert entities["node-a"]["document_ids"] == ["doc-anchor", "doc-secret"]
        assert entities["node-b"]["document_ids"] == [
            "doc-secret",
            "doc-anchor",
            "doc-open",
        ]

    def test_the_unreadable_id_is_judged_once_for_every_entity(
        self, files: CountingStore, graph: SQLiteGraphStore
    ) -> None:
        files.reads.clear()
        self._entities(files, graph, "internal")
        assert files.reads.count("doc-secret") == 1


def _graph_item(item_id: str, evidence_ref: Any) -> PackItem:
    """A graph-axis pack item: the node's properties are spread into metadata."""
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt="node",
        relevance_score=0.5,
        metadata={"name": item_id, "evidence_ref": evidence_ref, "note": "kept"},
    )


class TestPackItemEvidenceRef:
    """A served pack item carries its node's ``evidence_ref`` in its metadata.

    The pack's concealment pass drops it next to the supersession pointers, by
    its own path constant: the supersession tuple is pinned to the reconcile
    writers' keys, and this key is none of theirs.
    """

    def test_the_path_is_its_own_constant(self) -> None:
        assert EVIDENCE_POINTER_PATHS == (("evidence_ref",),)
        assert ("evidence_ref",) not in SUPERSESSION_POINTER_PATHS

    def test_an_unreadable_ref_is_dropped_and_the_item_is_still_served(
        self, store: CountingStore
    ) -> None:
        pool = [_graph_item("tool:a", "doc-secret"), _graph_item("tool:b", "doc-open")]
        out = conceal_unreadable_successors(pool, store, "internal")  # type: ignore[arg-type]
        assert [i.item_id for i in out] == ["tool:a", "tool:b"]
        assert "evidence_ref" not in out[0].metadata
        assert out[0].metadata["note"] == "kept"
        assert out[1].metadata["evidence_ref"] == "doc-open"

    def test_a_restricted_caller_is_served_the_same_objects_with_no_read(
        self, store: CountingStore
    ) -> None:
        pool = [_graph_item("tool:a", "doc-secret")]
        out = conceal_unreadable_successors(pool, store, "restricted")  # type: ignore[arg-type]
        assert out[0] is pool[0]
        assert store.reads == []

    def test_a_chunk_ref_is_judged_by_its_parent(self, store: CountingStore) -> None:
        chunk = chunk_doc_id("doc-secret", 0)
        store._inner.put(chunk, "slice", {})
        (out,) = conceal_unreadable_successors(
            [_graph_item("tool:a", chunk)],
            store,  # type: ignore[arg-type]
            "internal",
        )
        assert "evidence_ref" not in out.metadata

    @pytest.mark.parametrize("ref", [7, "", None, ["doc-secret"]])
    def test_a_malformed_ref_is_left_as_stored(
        self, store: CountingStore, ref: Any
    ) -> None:
        item = _graph_item("tool:a", ref)
        (out,) = conceal_unreadable_successors([item], store, "internal")  # type: ignore[arg-type]
        assert out.metadata["evidence_ref"] == ref
        assert store.reads == []

    def test_the_stored_metadata_is_never_mutated(self, store: CountingStore) -> None:
        item = _graph_item("tool:a", "doc-secret")
        conceal_unreadable_successors([item], store, "internal")  # type: ignore[arg-type]
        assert item.metadata["evidence_ref"] == "doc-secret"

    def test_the_ref_is_judged_in_one_batch_with_the_supersession_pointers(
        self, store: CountingStore
    ) -> None:
        pool = [
            _graph_item("tool:a", "doc-secret"),
            _graph_item("tool:b", "doc-secret"),
            PackItem(
                item_id="doc-old",
                item_type="document",
                excerpt="old",
                relevance_score=0.4,
                metadata={
                    "lifecycle": {"state": "superseded", "superseded_by": "doc-secret"}
                },
            ),
        ]
        conceal_unreadable_successors(pool, store, "internal")  # type: ignore[arg-type]
        assert store.reads.count("doc-secret") == 1

    def test_supersession_through_the_ref_is_resolved_before_and_unaffected(
        self, store: CountingStore
    ) -> None:
        """The loser is withheld while the node names its successor; only the
        served node's copy loses the id afterwards."""
        loser = PackItem(
            item_id="doc-old",
            item_type="document",
            excerpt="old",
            relevance_score=0.4,
            metadata={"lifecycle": {"state": "superseded", "superseded_by": "doc-new"}},
        )
        node = _graph_item("tool:new", "doc-new")
        kept, withheld = partition_superseded([loser, node])
        assert [i.item_id for i in kept] == ["tool:new"]
        assert [i.item_id for i in withheld] == ["doc-old"]
        store._inner.put("doc-new", "new body", label("restricted"))
        (served,) = conceal_unreadable_successors(kept, store, "internal")  # type: ignore[arg-type]
        assert served.item_id == "tool:new"
        assert "evidence_ref" not in served.metadata
        assert node.metadata["evidence_ref"] == "doc-new"


def _superseded(successor: str, **properties: Any) -> dict[str, Any]:
    """A node stamped the way ``supersede_entity`` stamps one."""
    stamp = Lifecycle(state="superseded", superseded_by=successor)
    return _node(lifecycle=stamp.model_dump(mode="json"), **properties)


class TestSupersededNode:
    """``lifecycle.superseded_by`` in a node's properties names a document too."""

    def test_an_unreadable_successor_is_dropped_and_the_state_stays(
        self, store: CountingStore
    ) -> None:
        node = _superseded("doc-secret", note="kept")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert "doc-secret" not in repr(out)
        assert out["properties"]["lifecycle"]["state"] == "superseded"
        assert "superseded_by" not in out["properties"]["lifecycle"]
        assert out["properties"]["note"] == "kept"
        assert out["properties"]["name"] == "node-1"

    def test_the_stored_node_is_never_mutated(self, store: CountingStore) -> None:
        node = _superseded("doc-secret")
        before = copy.deepcopy(node)
        conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert node == before

    def test_a_readable_successor_is_kept_as_the_same_object(
        self, store: CountingStore
    ) -> None:
        node = _superseded("doc-open")
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert out is node

    def test_a_chunk_successor_is_judged_by_its_parent(
        self, store: CountingStore
    ) -> None:
        chunk = chunk_doc_id("doc-secret", 0)
        store._inner.put(chunk, "slice", {})
        (out,) = conceal_node_pointers([_superseded(chunk)], store, "internal")  # type: ignore[arg-type]
        assert chunk not in repr(out)

    def test_a_restricted_caller_reads_nothing_and_gets_the_same_object(
        self, store: CountingStore
    ) -> None:
        node = _superseded("doc-secret")
        (out,) = conceal_node_pointers([node], store, "restricted")  # type: ignore[arg-type]
        assert out is node
        assert store.reads == []

    def test_all_three_carriers_of_one_id_cost_one_read(
        self, store: CountingStore
    ) -> None:
        node = _superseded("doc-secret", evidence_ref="doc-secret")
        node["document_ids"] = ["doc-secret"]
        (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
        assert "doc-secret" not in repr(out)
        assert store.reads == ["doc-secret"]

    def test_a_malformed_lifecycle_is_left_as_stored(
        self, store: CountingStore
    ) -> None:
        for lifecycle in ("doc-secret", ["doc-secret"], {"superseded_by": 7}, None):
            node = _node(lifecycle=lifecycle)
            (out,) = conceal_node_pointers([node], store, "internal")  # type: ignore[arg-type]
            assert out is node
        assert store.reads == []
