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
    SENSITIVITY_WITHHELD_IDS_KEY,
    DocumentClearance,
    OverwriteRefusedError,
    conceal_withheld_ids,
    partition_by_clearance,
    refuse_overwrite,
    withheld_ids,
    withheld_ids_metadata,
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
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["secret-doc"]

    def test_a_flat_pack_records_which_ids_the_sensitivity_gate_withheld(self) -> None:
        """A response route needs them to strip; the event payload lacks the key."""
        builder = PackBuilder(strategies=[_strategy("keyword", self._pool())])
        pack = builder.build("deploy checklist", clearance="internal")
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["secret-doc"]
        assert SENSITIVITY_WITHHELD_IDS_KEY not in pack.metadata["withholding"]

    def test_a_pack_that_withheld_nothing_carries_no_ids_key(self) -> None:
        """Absent, not empty: a held-out pack must stay like one that found nothing."""
        builder = PackBuilder(strategies=[_strategy("keyword", [_item("open-doc")])])
        assert (
            SENSITIVITY_WITHHELD_IDS_KEY
            not in builder.build("deploy checklist", clearance="internal").metadata
        )
        assert withheld_ids_metadata([]) == {}

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


def _report(*rows: tuple[str, str]) -> dict[str, Any]:
    return {
        "rejected_items": [
            {"item_id": i, "reason": r, "relevance_score": 0.9} for i, r in rows
        ],
        "other": "kept",
    }


def _withholding(*ids: str) -> dict[str, Any]:
    return {
        "total": len(ids),
        "by_reason": {"sensitivity": 1, "token_budget": len(ids) - 1},
        "withheld_item_ids": list(ids),
    }


class TestConcealWithheldIds:
    """A response shows a below-``restricted`` caller how many, never which (#194)."""

    def test_a_restricted_caller_is_shown_everything(self) -> None:
        report = _report(("secret", "sensitivity"), ("big", "token_budget"))
        withholding = _withholding("secret", "big")
        shown_report, shown_withholding = conceal_withheld_ids(
            report, withholding, ["secret"], "restricted"
        )
        assert shown_report == report
        assert shown_withholding == withholding

    @pytest.mark.parametrize("clearance", ["public", "internal", "confidential"])
    def test_a_lower_caller_loses_the_ids_but_keeps_every_count(
        self, clearance: Sensitivity
    ) -> None:
        report = _report(("secret", "sensitivity"), ("big", "token_budget"))
        withholding = _withholding("secret", "big")
        shown_report, shown_withholding = conceal_withheld_ids(
            report, withholding, ["secret"], clearance
        )
        assert shown_report is not None
        assert shown_withholding is not None
        assert "secret" not in str(shown_report)
        assert "secret" not in str(shown_withholding)
        assert [r["item_id"] for r in shown_report["rejected_items"]] == ["big"]
        assert shown_report["other"] == "kept"
        assert shown_withholding["withheld_item_ids"] == ["big"]
        assert shown_withholding["total"] == 2
        assert shown_withholding["by_reason"] == withholding["by_reason"]

    def test_a_hidden_id_is_dropped_under_whatever_reason_it_carries(self) -> None:
        """A sectioned report holds no sensitivity rows; the builder names the ids."""
        shown_report, _ = conceal_withheld_ids(
            _report(("secret", "content_floor")), None, ["secret"], "internal"
        )
        assert shown_report is not None
        assert shown_report["rejected_items"] == []

    def test_the_inputs_are_not_mutated(self) -> None:
        report = _report(("secret", "sensitivity"))
        withholding = _withholding("secret", "big")
        conceal_withheld_ids(report, withholding, ["secret"], "internal")
        assert len(report["rejected_items"]) == 1
        assert withholding["withheld_item_ids"] == ["secret", "big"]

    def test_none_passes_through(self) -> None:
        assert conceal_withheld_ids(None, None, [], "internal") == (None, None)

    def test_withheld_ids_reads_only_the_sensitivity_rows(self) -> None:
        item = _item("a")
        rows = [
            MagicMock(item_id="b", reason="sensitivity"),
            MagicMock(item_id="a", reason="sensitivity"),
            MagicMock(item_id="c", reason="noise"),
            MagicMock(item_id="a", reason="sensitivity"),
        ]
        assert withheld_ids(rows) == ["a", "b"]
        assert item.item_id == "a"


class TestRefuseOverwrite:
    def test_a_missing_document_is_no_verdict(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        refuse_overwrite(doc_store, "ghost", clearance="internal", metadata=None)

    def test_a_label_above_the_clearance_is_refused_without_naming_it(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("d", "classified body", label("restricted"))
        with pytest.raises(OverwriteRefusedError) as refused:
            refuse_overwrite(doc_store, "d", clearance="internal", metadata=None)
        assert "restricted" not in str(refused.value)
        assert "classified body" not in str(refused.value)

    def test_a_chunk_is_judged_by_its_parent(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        chunk_id = chunk_doc_id("parent", 0)
        doc_store.put(chunk_id, "classified body", {})
        with pytest.raises(OverwriteRefusedError):
            refuse_overwrite(doc_store, chunk_id, clearance="internal", metadata=None)

    def test_a_cleared_caller_may_not_write_a_lower_label(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("d", "body", label("confidential"))
        for metadata in (None, {}, label("internal"), label("public")):
            with pytest.raises(OverwriteRefusedError, match="lower"):
                refuse_overwrite(
                    doc_store, "d", clearance="restricted", metadata=metadata
                )

    @pytest.mark.parametrize("written", ["confidential", "restricted"])
    def test_a_cleared_caller_may_write_the_same_or_a_higher_label(
        self, doc_store: SQLiteDocumentStore, written: str
    ) -> None:
        doc_store.put("d", "body", label("confidential"))
        refuse_overwrite(
            doc_store, "d", clearance="restricted", metadata=label(written)
        )

    def test_an_unlabelled_document_may_be_replaced_by_an_unlabelled_write(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("d", "body", {})
        refuse_overwrite(doc_store, "d", clearance="internal", metadata=None)
