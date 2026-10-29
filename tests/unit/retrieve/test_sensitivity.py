"""The sensitivity gate: a document above the caller's clearance is not served (#194).

The security property is the **transparent default**: a store with no labels
serves every caller identically. The rest is what a label does once present,
at the collect seam every strategy shares.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.formatters import format_pack_as_markdown
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.sensitivity import (
    SENSITIVITY_REJECTION_REASON,
    DocumentClearance,
    partition_by_clearance,
)
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import withholding_from_payload
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    DataClassification,
    Sensitivity,
    sensitivity_of,
)
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_BODY = "the deploy checklist requires draining the write queue first"


def label(level: str) -> dict[str, Any]:
    return {
        DATA_CLASSIFICATION_KEY: DataClassification(
            sensitivity=level,  # type: ignore[arg-type]
            classified_by=["test"],
        ).model_dump(mode="json")
    }


def _item(item_id: str, metadata: dict[str, Any] | None = None) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=_BODY,
        relevance_score=0.5,
        metadata=metadata or {},
    )


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


@pytest.fixture
def doc_store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    yield store
    store.close()


class TestSensitivityOf:
    def test_a_missing_label_reads_internal(self) -> None:
        assert sensitivity_of({}) == "internal"
        assert sensitivity_of(None) == "internal"

    @pytest.mark.parametrize(
        "level", ["public", "internal", "confidential", "restricted"]
    )
    def test_a_stamped_label_reads_back(self, level: Sensitivity) -> None:
        assert sensitivity_of(label(level)) == level

    @pytest.mark.parametrize(
        "bad",
        ["restricted", None, 7, {}, {"sensitivity": "top-secret"}, {"sensitivity": 3}],
    )
    def test_a_malformed_label_reads_restricted(self, bad: Any) -> None:
        """Present but unparseable fails closed: the opposite of lifecycle."""
        assert sensitivity_of({DATA_CLASSIFICATION_KEY: bad}) == "restricted"


class TestPartition:
    def test_an_unlabelled_pool_is_served_whole_at_the_lowest_clearance(self) -> None:
        """Transparency: no label anywhere, so the gate removes nothing."""
        items = [_item("a"), _item("b", {"title": "x"}), _item("c", {"k": {"n": 1}})]
        kept, withheld = partition_by_clearance(items, "internal")
        assert [i.item_id for i in kept] == ["a", "b", "c"]
        assert withheld == []

    def test_each_level_is_withheld_only_above_the_clearance(self) -> None:
        items = [
            _item("pub", label("public")),
            _item("int", label("internal")),
            _item("conf", label("confidential")),
            _item("res", label("restricted")),
        ]
        for clearance, served in [
            ("internal", ["pub", "int"]),
            ("confidential", ["pub", "int", "conf"]),
            ("restricted", ["pub", "int", "conf", "res"]),
        ]:
            kept, _ = partition_by_clearance(items, clearance)  # type: ignore[arg-type]
            assert [i.item_id for i in kept] == served

    def test_a_malformed_record_is_withheld(self) -> None:
        kept, withheld = partition_by_clearance(
            [_item("bad", {DATA_CLASSIFICATION_KEY: "oops"})], "confidential"
        )
        assert kept == []
        assert [i.item_id for i in withheld] == ["bad"]

    def test_a_chunk_inherits_its_parents_label(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """A chunk row carries none of the parent's labels."""
        doc_store.put(
            "parent", "unrelated filler", {"title": "p", **label("restricted")}
        )
        chunk_id = chunk_doc_id("parent", 0)
        kept, withheld = partition_by_clearance(
            [_item(chunk_id)], "internal", ParentVerdictCache(doc_store)
        )
        assert kept == []
        assert [i.item_id for i in withheld] == [chunk_id]

    def test_a_chunk_of_an_unlabelled_parent_is_served(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "unrelated filler", {"title": "p"})
        kept, _ = partition_by_clearance(
            [_item(chunk_doc_id("parent", 0))],
            "internal",
            ParentVerdictCache(doc_store),
        )
        assert len(kept) == 1

    def test_a_chunk_whose_parent_cannot_be_read_is_withheld(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """An outage must not read as permission."""

        class _Broken:
            def get(self, _doc_id: str) -> dict[str, Any]:
                msg = "store down"
                raise RuntimeError(msg)

        kept, withheld = partition_by_clearance(
            [_item(chunk_doc_id("parent", 0))],
            "internal",
            ParentVerdictCache(_Broken()),  # type: ignore[arg-type]
        )
        assert kept == []
        assert len(withheld) == 1

    def test_a_chunk_of_a_missing_parent_is_served(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Missing is no verdict. Only a raised fetch is an outage."""
        kept, _ = partition_by_clearance(
            [_item(chunk_doc_id("ghost", 0))], "internal", ParentVerdictCache(doc_store)
        )
        assert len(kept) == 1


class TestDocumentClearance:
    def test_a_stdio_style_clearance_reads_everything(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        gate = DocumentClearance(doc_store, "restricted")
        assert gate.permits("d", label("restricted"))

    def test_internal_clearance_refuses_a_labelled_document(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        gate = DocumentClearance(doc_store, "internal")
        assert not gate.permits("d", label("confidential"))
        assert gate.permits("d", {})
        assert gate.permits("d", None)


class TestBuildersApplyTheGate:
    """The gate runs at the collect seam, so it covers every strategy."""

    def _pool(self) -> list[PackItem]:
        return [_item("open-doc"), _item("secret-doc", label("restricted"))]

    def test_a_flat_pack_withholds_and_reports_counts_only(self) -> None:
        builder = PackBuilder(strategies=[_strategy("keyword", self._pool())])
        pack = builder.build("deploy checklist", clearance="internal")

        assert [i.item_id for i in pack.items] == ["open-doc"]
        rejected = pack.retrieval_report.rejected_items
        assert [(r.item_id, r.reason) for r in rejected] == [
            ("secret-doc", SENSITIVITY_REJECTION_REASON)
        ]
        withholding = pack.metadata["withholding"]
        assert withholding["by_reason"] == {"sensitivity": 1}
        rendered = format_pack_as_markdown(
            [i.model_dump() for i in pack.items],
            "deploy checklist",
            withholding=withholding_from_payload(withholding),
        )
        assert "sensitivity 1" in rendered
        assert "secret-doc" not in rendered

    def test_a_semantic_only_document_is_withheld_too(self) -> None:
        """The label rides in the vector row's snapshot, not a keyword hit."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_item("open-doc")]),
                _strategy("semantic", [_item("vec-doc", label("confidential"))]),
            ]
        )
        pack = builder.build("deploy checklist", clearance="internal")
        assert [i.item_id for i in pack.items] == ["open-doc"]

    def test_a_sectioned_pack_withholds(self) -> None:
        builder = PackBuilder(strategies=[_strategy("keyword", self._pool())])
        pack = builder.build_sectioned(
            "deploy checklist",
            clearance="internal",
            sections=[SectionRequest(name="All")],
        )
        served = [i.item_id for s in pack.sections for i in s.items]
        assert served == ["open-doc"]

    def test_an_unlabelled_store_gives_every_clearance_the_same_pack(self) -> None:
        pool = [_item("a"), _item("b"), _item("c")]
        served = {}
        for clearance in ("internal", "confidential", "restricted"):
            builder = PackBuilder(strategies=[_strategy("keyword", list(pool))])
            pack = builder.build("deploy checklist", clearance=clearance)  # type: ignore[arg-type]
            served[clearance] = (
                [i.item_id for i in pack.items],
                pack.metadata["withholding"]["total"],
            )
        assert served["internal"] == served["confidential"] == served["restricted"]
        assert served["internal"][1] == 0

    def test_restricted_clearance_serves_the_labelled_document(self) -> None:
        builder = PackBuilder(strategies=[_strategy("keyword", self._pool())])
        pack = builder.build("deploy checklist", clearance="restricted")
        assert sorted(i.item_id for i in pack.items) == ["open-doc", "secret-doc"]
