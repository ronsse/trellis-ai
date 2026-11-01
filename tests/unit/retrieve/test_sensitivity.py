"""The sensitivity gate: a document above the caller's clearance is not served (#194).

The security property is the **transparent default**: a store with no labels
serves every caller identically. The rest is what a label does once present,
at the collect seam every strategy shares.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.mutate.retention import ARCHIVED_STATE
from trellis.retrieve.formatters import format_pack_as_markdown
from trellis.retrieve.lifecycle import partition_superseded
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.sensitivity import (
    SENSITIVITY_REJECTION_REASON,
    SENSITIVITY_WITHHELD_IDS_KEY,
    SUPERSESSION_POINTER_PATHS,
    DocumentClearance,
    OverwriteRefusedError,
    conceal_document_rows,
    conceal_document_successor,
    conceal_unreadable_successors,
    conceal_withheld_ids,
    concealed_ids_metadata,
    partition_by_clearance,
    public_withholding,
    readable_ids,
    reader_for,
    refuse_overwrite,
    withheld_ids,
    withheld_ids_metadata,
)
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import withholding_from_payload
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    LIFECYCLE_KEY,
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


_NOISE = {"content_tags": {"signal_quality": "noise"}}


def _lifecycle(state: str, successor: str | None = None) -> dict[str, Any]:
    return {LIFECYCLE_KEY: {"state": state, "superseded_by": successor}}


class TestConcealmentCoversEveryRejectedCandidate:
    """An earlier gate's rejection must not leak what the clearance gate hides (#1022).

    Noise, archived and superseded candidates are rejected *before* the
    sensitivity gate runs, so their rows never carry the ``sensitivity``
    reason. A response that strips only that reason still names a
    restricted document by id.
    """

    @pytest.fixture(
        params=[
            pytest.param(_NOISE, id="noise"),
            pytest.param(_lifecycle(ARCHIVED_STATE), id="archived"),
            pytest.param(_lifecycle("superseded", "successor-doc"), id="superseded"),
        ]
    )
    def earlier_gate(self, request: pytest.FixtureRequest) -> dict[str, Any]:
        return dict(request.param)

    def _pool(self, earlier_gate: dict[str, Any]) -> list[PackItem]:
        return [
            _item("open-doc"),
            _item("successor-doc"),
            _item("secret-doc", {**label("restricted"), **earlier_gate}),
        ]

    def test_a_flat_pack_conceals_a_restricted_row_an_earlier_gate_rejected(
        self, earlier_gate: dict[str, Any]
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", self._pool(earlier_gate))]
        )
        pack = builder.build("deploy checklist", clearance="internal")

        rejected = {r.item_id: r.reason for r in pack.retrieval_report.rejected_items}
        assert rejected["secret-doc"] != SENSITIVITY_REJECTION_REASON
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["secret-doc"]

    def test_a_sectioned_pack_conceals_it_too(
        self, earlier_gate: dict[str, Any]
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", self._pool(earlier_gate))]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            clearance="internal",
            sections=[SectionRequest(name="All")],
        )
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == ["secret-doc"]

    def test_a_restricted_clearance_records_nothing_to_conceal(
        self, earlier_gate: dict[str, Any]
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", self._pool(earlier_gate))]
        )
        pack = builder.build("deploy checklist", clearance="restricted")
        assert SENSITIVITY_WITHHELD_IDS_KEY not in pack.metadata

    def test_an_unlabelled_rejected_row_stays_visible(
        self, earlier_gate: dict[str, Any]
    ) -> None:
        """The transparent default: no label, no concealment."""
        pool = [
            _item("open-doc"),
            _item("successor-doc"),
            _item("plain-doc", dict(earlier_gate)),
        ]
        builder = PackBuilder(strategies=[_strategy("keyword", pool)])
        pack = builder.build("deploy checklist", clearance="internal")
        assert SENSITIVITY_WITHHELD_IDS_KEY not in pack.metadata
        assert "plain-doc" in {r.item_id for r in pack.retrieval_report.rejected_items}

    def test_a_noise_chunk_of_a_restricted_parent_is_concealed(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        chunk_id = chunk_doc_id("parent", 0)
        pool = [_item("open-doc"), _item(chunk_id, dict(_NOISE))]
        builder = PackBuilder(
            strategies=[_strategy("keyword", pool)], document_store=doc_store
        )
        pack = builder.build("deploy checklist", clearance="internal")
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == [chunk_id]

    def test_a_noise_chunk_whose_parent_cannot_be_read_is_concealed(self) -> None:
        """An outage reads as restricted, as it does at the gate."""

        class _Broken:
            def get(self, _doc_id: str) -> dict[str, Any]:
                msg = "store down"
                raise RuntimeError(msg)

        chunk_id = chunk_doc_id("parent", 0)
        pool = [_item("open-doc"), _item(chunk_id, dict(_NOISE))]
        builder = PackBuilder(
            strategies=[_strategy("keyword", pool)],
            document_store=_Broken(),  # type: ignore[arg-type]
        )
        pack = builder.build("deploy checklist", clearance="internal")
        assert pack.metadata[SENSITIVITY_WITHHELD_IDS_KEY] == [chunk_id]


def _served(pack: Any) -> list[str]:
    return [i.item_id for i in pack.items]


def _rows_without(pack: Any, hidden_id: str) -> list[tuple[str, str]]:
    """The pack's rejected rows, less those about ``hidden_id`` itself."""
    return [
        (r.item_id, r.reason)
        for r in pack.retrieval_report.rejected_items
        if r.item_id != hidden_id
    ]


def _by_reason_without_sensitivity(pack: Any) -> dict[str, int]:
    return {
        reason: count
        for reason, count in pack.metadata["withholding"]["by_reason"].items()
        if reason != SENSITIVITY_REJECTION_REASON
    }


class TestSupersessionIgnoresAnUnreadableSuccessor:
    """A successor the caller cannot read is no successor (#1029).

    Otherwise a loser the caller may read is withheld on account of a document
    it may not, which tells it that document exists and matched, and costs it
    the newest version it is allowed to see. The pack must be the one built
    without the successor.
    """

    def _loser(self, successor: str = "succ-doc") -> PackItem:
        return _item("loser-doc", _lifecycle("superseded", successor))

    def _secret_pool(self) -> list[PackItem]:
        return [
            self._loser(),
            _item("succ-doc", label("restricted")),
            _item("other-doc"),
        ]

    def _without_pool(self) -> list[PackItem]:
        return [self._loser(), _item("other-doc")]

    def test_a_flat_pack_is_the_pack_built_without_the_successor(self) -> None:
        with_succ = PackBuilder(
            strategies=[_strategy("keyword", self._secret_pool())]
        ).build("deploy checklist", clearance="internal")
        without = PackBuilder(
            strategies=[_strategy("keyword", self._without_pool())]
        ).build("deploy checklist", clearance="internal")

        assert _served(with_succ) == _served(without) == ["loser-doc", "other-doc"]
        assert _rows_without(with_succ, "succ-doc") == _rows_without(
            without, "succ-doc"
        )
        assert _by_reason_without_sensitivity(with_succ) == (
            _by_reason_without_sensitivity(without)
        )
        assert "superseded" not in with_succ.metadata["withholding"]["by_reason"]

    def test_a_sectioned_pack_is_the_pack_built_without_the_successor(self) -> None:
        sections = [SectionRequest(name="All")]
        with_succ = PackBuilder(
            strategies=[_strategy("keyword", self._secret_pool())]
        ).build_sectioned("deploy checklist", clearance="internal", sections=sections)
        without = PackBuilder(
            strategies=[_strategy("keyword", self._without_pool())]
        ).build_sectioned("deploy checklist", clearance="internal", sections=sections)

        def served(pack: Any) -> list[str]:
            return [i.item_id for s in pack.sections for i in s.items]

        assert served(with_succ) == served(without) == ["loser-doc", "other-doc"]

    def test_a_trusted_caller_still_sees_the_loser_withheld(self) -> None:
        """Clearance covers the successor, so §4 holds: one side only."""
        pack = PackBuilder(
            strategies=[_strategy("keyword", self._secret_pool())]
        ).build("deploy checklist", clearance="restricted")
        assert _served(pack) == ["succ-doc", "other-doc"]
        assert ("loser-doc", "superseded") in _rows_without(pack, "succ-doc")

    def test_a_successor_the_caller_can_read_still_supersedes(self) -> None:
        pool = [
            self._loser(),
            _item("succ-doc", label("confidential")),
            _item("other-doc"),
        ]
        pack = PackBuilder(strategies=[_strategy("keyword", pool)]).build(
            "deploy checklist", clearance="confidential"
        )
        assert _served(pack) == ["succ-doc", "other-doc"]

    def test_an_unlabelled_pool_is_superseded_at_every_clearance(self) -> None:
        """The transparent default: the new rule changes nothing without labels."""
        served = {}
        for clearance in ("internal", "confidential", "restricted"):
            pool = [self._loser(), _item("succ-doc"), _item("other-doc")]
            pack = PackBuilder(strategies=[_strategy("keyword", pool)]).build(
                "deploy checklist",
                clearance=clearance,  # type: ignore[arg-type]
            )
            served[clearance] = (
                _served(pack),
                _rows_without(pack, ""),
                pack.metadata["withholding"],
            )
        assert served["internal"] == served["confidential"] == served["restricted"]
        assert _served_ids(served["internal"]) == ["succ-doc", "other-doc"]

    def test_a_successor_named_by_a_hidden_evidence_pointer_is_ignored(self) -> None:
        """The ``save_knowledge`` seam: the graph node points at the document."""
        node = _item("node-1", {"evidence_ref": "evidence-doc", **label("restricted")})
        pool = [self._loser("evidence-doc"), node, _item("other-doc")]
        pack = PackBuilder(strategies=[_strategy("keyword", pool)]).build(
            "deploy checklist", clearance="internal"
        )
        assert _served(pack) == ["loser-doc", "other-doc"]

    def test_a_readable_evidence_pointer_still_supersedes(self) -> None:
        node = _item("node-1", {"evidence_ref": "evidence-doc"})
        pool = [self._loser("evidence-doc"), node]
        pack = PackBuilder(strategies=[_strategy("keyword", pool)]).build(
            "deploy checklist", clearance="internal"
        )
        assert _served(pack) == ["node-1"]

    def test_a_chunk_of_a_restricted_parent_is_no_successor(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        chunk_id = chunk_doc_id("parent", 0)
        pool = [self._loser(chunk_id), _item(chunk_id), _item("other-doc")]
        pack = PackBuilder(
            strategies=[_strategy("keyword", pool)], document_store=doc_store
        ).build("deploy checklist", clearance="internal")
        assert _served(pack) == ["loser-doc", "other-doc"]

    def test_a_cycle_through_an_unreadable_member_keeps_the_readable_one(self) -> None:
        pool = [
            self._loser("succ-doc"),
            _item(
                "succ-doc",
                {**label("restricted"), **_lifecycle("superseded", "loser-doc")},
            ),
        ]
        pack = PackBuilder(strategies=[_strategy("keyword", pool)]).build(
            "deploy checklist", clearance="internal"
        )
        assert _served(pack) == ["loser-doc"]

    def test_partition_superseded_defaults_to_every_successor_counting(self) -> None:
        kept, withheld = partition_superseded(self._secret_pool())
        assert [i.item_id for i in withheld] == ["loser-doc"]
        assert [i.item_id for i in kept] == ["succ-doc", "other-doc"]

    def test_partition_superseded_skips_a_successor_the_predicate_refuses(
        self,
    ) -> None:
        kept, withheld = partition_superseded(
            self._secret_pool(),
            successor_visible=lambda item: item.item_id != "succ-doc",
        )
        assert withheld == []
        assert [i.item_id for i in kept] == ["loser-doc", "succ-doc", "other-doc"]


def _served_ids(row: tuple[list[str], Any, Any]) -> list[str]:
    return row[0]


class TestReaderFor:
    def test_restricted_reads_everything_so_no_predicate_is_built(self) -> None:
        assert reader_for("restricted") is None

    def test_a_lower_clearance_reads_an_item_by_its_effective_label(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        reader = reader_for("internal", ParentVerdictCache(doc_store))
        assert reader is not None
        assert reader(_item("plain"))
        assert not reader(_item("secret", label("confidential")))
        assert not reader(_item(chunk_doc_id("parent", 0)))


class TestReadableIds:
    """The ids a caller may read, from a list it did not earn (#1029)."""

    def test_an_unlabelled_store_gives_every_clearance_the_same_ids(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("a", "body", {})
        ids = ["a", "graph-node", chunk_doc_id("a", 0)]
        for clearance in ("internal", "confidential", "restricted"):
            assert readable_ids(doc_store, ids, clearance) == ids  # type: ignore[arg-type]

    def test_a_labelled_document_and_a_chunk_of_it_are_dropped_in_order(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("open", "body", {})
        doc_store.put("secret", "body", label("restricted"))
        doc_store.put("conf", "body", label("confidential"))
        chunk_id = chunk_doc_id("secret", 0)
        doc_store.put(chunk_id, "body", {})
        ids = ["secret", "open", chunk_id, "conf", "ghost"]
        assert readable_ids(doc_store, ids, "internal") == ["open", "ghost"]
        assert readable_ids(doc_store, ids, "confidential") == ["open", "conf", "ghost"]
        assert readable_ids(doc_store, ids, "restricted") == ids

    def test_a_chunk_row_that_is_gone_is_still_judged_by_its_parent(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("secret", "body", label("restricted"))
        assert readable_ids(doc_store, [chunk_doc_id("secret", 3)], "internal") == []

    def test_a_malformed_label_is_dropped(self, doc_store: SQLiteDocumentStore) -> None:
        doc_store.put("bad", "body", {DATA_CLASSIFICATION_KEY: "oops"})
        assert readable_ids(doc_store, ["bad"], "confidential") == []

    def test_a_store_that_raises_drops_the_id_and_restricted_never_asks(self) -> None:
        class _Broken:
            def get(self, _doc_id: str) -> dict[str, Any]:
                msg = "store down"
                raise RuntimeError(msg)

        broken: Any = _Broken()
        assert readable_ids(broken, ["a", chunk_doc_id("p", 0)], "internal") == []
        assert readable_ids(broken, ["a"], "restricted") == ["a"]


class TestConcealUnreadableSuccessors:
    """A served loser's stored ``superseded_by`` is dropped when unreadable (#1029).

    The store judges the successor, as a by-id read does, so the answer does not
    depend on whether the successor matched the query.
    """

    @staticmethod
    def _loser(successor: Any = "succ-doc", state: str = "superseded") -> PackItem:
        return _item("loser-doc", _lifecycle(state, successor))

    @staticmethod
    def _successor_of(item: PackItem) -> Any:
        return (item.metadata or {}).get(LIFECYCLE_KEY, {}).get("superseded_by")

    def test_an_unreadable_successor_is_dropped_and_the_state_kept(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        loser = self._loser()
        (out,) = conceal_unreadable_successors([loser], doc_store, "internal")
        assert out.metadata[LIFECYCLE_KEY] == {"state": "superseded"}
        assert out.item_id == "loser-doc"
        assert out.excerpt == loser.excerpt

    def test_it_is_copy_on_write(self, doc_store: SQLiteDocumentStore) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        shared = _lifecycle("superseded", "succ-doc")
        loser = _item("loser-doc", shared)
        other = _item("other-doc", shared)
        out = conceal_unreadable_successors([loser, other], doc_store, "internal")
        assert shared[LIFECYCLE_KEY]["superseded_by"] == "succ-doc"
        assert self._successor_of(loser) == self._successor_of(other) == "succ-doc"
        assert [self._successor_of(i) for i in out] == [None, None]

    def test_the_store_judges_not_the_pool(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """The successor is not among the items at all, and is still judged."""
        doc_store.put("succ-doc", "body", label("confidential"))
        pool = [self._loser(), _item("other-doc")]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert [self._successor_of(i) for i in out] == [None, None]
        kept = conceal_unreadable_successors(pool, doc_store, "confidential")
        assert [self._successor_of(i) for i in kept] == ["succ-doc", None]

    def test_a_chunk_successor_is_judged_by_its_parent(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        chunk_id = chunk_doc_id("parent", 0)
        doc_store.put(chunk_id, "slice", {})
        (out,) = conceal_unreadable_successors(
            [self._loser(chunk_id)], doc_store, "internal"
        )
        assert self._successor_of(out) is None

    @pytest.mark.parametrize("state", ["superseded", "archived", "current"])
    def test_the_id_is_dropped_whatever_the_state(
        self, doc_store: SQLiteDocumentStore, state: str
    ) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        (out,) = conceal_unreadable_successors(
            [self._loser(state=state)], doc_store, "internal"
        )
        assert out.metadata[LIFECYCLE_KEY] == {"state": state}

    def test_a_store_error_reads_as_unreadable(self) -> None:
        class _Broken:
            def get(self, _doc_id: str) -> dict[str, Any]:
                msg = "store down"
                raise RuntimeError(msg)

        broken: Any = _Broken()
        (out,) = conceal_unreadable_successors([self._loser()], broken, "internal")
        assert self._successor_of(out) is None

    def test_restricted_reads_nothing_and_changes_nothing(self) -> None:
        store = MagicMock()
        pool = [self._loser(), _item("other-doc")]
        out = conceal_unreadable_successors(pool, store, "restricted")
        assert out == pool
        assert all(a is b for a, b in zip(out, pool, strict=True))
        store.get.assert_not_called()

    def test_an_unlabelled_store_changes_nothing_at_any_clearance(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("succ-doc", "body", {})
        pool = [self._loser(), _item("other-doc")]
        for clearance in ("internal", "confidential", "restricted"):
            out = conceal_unreadable_successors(pool, doc_store, clearance)  # type: ignore[arg-type]
            assert all(a is b for a, b in zip(out, pool, strict=True))

    def test_a_successor_with_no_row_is_no_verdict(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        (out,) = conceal_unreadable_successors([self._loser()], doc_store, "internal")
        assert self._successor_of(out) == "succ-doc"

    def test_no_store_means_no_verdict(self) -> None:
        pool = [self._loser()]
        out = conceal_unreadable_successors(pool, None, "internal")
        assert out[0] is pool[0]

    @pytest.mark.parametrize("value", [None, "", 7, ["succ-doc"], {"id": "succ-doc"}])
    def test_a_value_that_names_no_id_is_left_as_stored(
        self, doc_store: SQLiteDocumentStore, value: Any
    ) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        pool = [self._loser(value)]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert out[0] is pool[0]

    def test_an_item_with_no_lifecycle_record_is_passed_through(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        pool = [_item("plain"), _item("bad", {LIFECYCLE_KEY: "oops"})]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert all(a is b for a, b in zip(out, pool, strict=True))

    def test_a_document_read_drops_an_unreadable_successor(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        doc_store.put("loser-doc", "old", _lifecycle("superseded", "succ-doc"))
        stored = doc_store.get("loser-doc")
        assert stored is not None
        out = conceal_document_successor(stored, doc_store, "internal")
        assert out["metadata"][LIFECYCLE_KEY] == {"state": "superseded"}
        assert out["content"] == "old"
        assert stored["metadata"][LIFECYCLE_KEY]["superseded_by"] == "succ-doc"
        assert conceal_document_successor(stored, doc_store, "restricted") is stored

    def test_a_document_read_keeps_a_readable_successor(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("succ-doc", "body", {})
        doc_store.put("loser-doc", "old", _lifecycle("superseded", "succ-doc"))
        stored = doc_store.get("loser-doc")
        assert stored is not None
        assert conceal_document_successor(stored, doc_store, "internal") is stored

    def test_a_flat_and_a_sectioned_build_drop_it_end_to_end(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("succ-doc", "body", label("restricted"))
        pool = [self._loser(), _item("other-doc")]
        flat = PackBuilder(
            strategies=[_strategy("keyword", pool)], document_store=doc_store
        ).build("deploy checklist", clearance="internal")
        sectioned = PackBuilder(
            strategies=[_strategy("keyword", pool)], document_store=doc_store
        ).build_sectioned(
            "deploy checklist",
            clearance="internal",
            sections=[SectionRequest(name="All")],
        )
        served = [*flat.items, *(i for s in sectioned.sections for i in s.items)]
        assert [i.item_id for i in served] == ["loser-doc", "other-doc"] * 2
        assert [self._successor_of(i) for i in served] == [None] * 4
        assert self._successor_of(pool[0]) == "succ-doc", "the pool's item is unchanged"


def _reverse(loser: Any = "old-doc", **extra: Any) -> dict[str, Any]:
    """A successor's metadata: ``supersedes_doc_id`` naming its loser."""
    return {"supersedes_doc_id": loser, "reconciliation": "supersede", **extra}


class TestConcealTheReversePointer:
    """A successor's ``supersedes_doc_id`` may name a loser the caller cannot read.

    Every metadata key that stores a supersession pointer is one path in
    :data:`SUPERSESSION_POINTER_PATHS`; each gets the same drop, judged by the
    store, so a successor served without its restricted loser still reads as
    one that replaced something.
    """

    @staticmethod
    def _loser_of(item: PackItem) -> Any:
        return (item.metadata or {}).get("supersedes_doc_id")

    def test_the_paths_are_every_pointer_key_the_writers_emit(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Enumerate the writers' pointer keys explicitly; a new one fails here.

        Each side is pinned to a closed list, not found by a name pattern: the
        reconcile module's ``*_DOC_KEY`` constants, the key set the session
        capture writer emits, the id-valued fields of its candidate model and
        the fields of ``Lifecycle``. A key a writer adds is then a failure to
        decide on, not a leak that no scan happened to name.
        """
        from trellis.mcp import reconcile
        from trellis.mcp.reconcile import (
            LIFECYCLE_KEY as RECONCILE_LIFECYCLE_KEY,
        )
        from trellis.mcp.reconcile import (
            RECONCILIATION_KEY,
            SUPERSEDES_DOC_KEY,
            UPDATES_DOC_KEY,
            mark_document_superseded,
        )
        from trellis.schemas.classification import Lifecycle
        from trellis_workers.session_capture.capture import _candidate_metadata
        from trellis_workers.session_capture.models import CandidateMemory

        # The literals in sensitivity.py equal the writers' constants.
        assert SUPERSEDES_DOC_KEY == "supersedes_doc_id"
        assert UPDATES_DOC_KEY == "updates_doc_id"
        assert RECONCILE_LIFECYCLE_KEY == LIFECYCLE_KEY
        assert {n for n in dir(reconcile) if n.endswith("_DOC_KEY")} == {
            "SUPERSEDES_DOC_KEY",
            "UPDATES_DOC_KEY",
        }
        assert set(SUPERSESSION_POINTER_PATHS) == {
            (LIFECYCLE_KEY, "superseded_by"),
            (SUPERSEDES_DOC_KEY,),
            (UPDATES_DOC_KEY,),
        }

        # The loser side: ``Lifecycle`` has one id-valued field, and the stamp
        # writes it under the lifecycle key.
        assert set(Lifecycle.model_fields) - {
            "state",
            "valid_from",
            "valid_until",
            "deprecation_reason",
            "classification_version",
        } == {"superseded_by"}
        doc_store.put("old-doc", "old", {})
        doc_store.put("new-doc", "new", {})
        assert (
            mark_document_superseded(
                doc_store, old_doc_id="old-doc", new_doc_id="new-doc", vector_store=None
            )
            is None
        )
        stamped = doc_store.get("old-doc")
        assert stamped is not None
        assert set(stamped["metadata"]) == {LIFECYCLE_KEY}
        assert stamped["metadata"][LIFECYCLE_KEY]["superseded_by"] == "new-doc"

        # The successor and addendum side: the capture writer's candidate model
        # has two id-valued fields, and with both set the metadata it emits has
        # exactly two keys beyond its own bookkeeping.
        assert {
            f.name
            for f in dataclasses.fields(CandidateMemory)
            if f.name.endswith("_doc_id")
        } == {UPDATES_DOC_KEY, SUPERSEDES_DOC_KEY}
        candidate = CandidateMemory(
            title="t",
            memory="m",
            memory_type="semantic",
            signal="success",
            evidence="e",
            non_derivable=True,
            durable=True,
            actionable=True,
            confidence=0.9,
            updates_doc_id="addendum-target",
            supersedes_doc_id="old-doc",
        )
        bookkeeping = {
            "session_id",
            "signal",
            "memory_type",
            "capture_title",
            "distilled",
            "subagent",
            RECONCILIATION_KEY,
        }
        assert set(_candidate_metadata(candidate)) - bookkeeping == {
            UPDATES_DOC_KEY,
            SUPERSEDES_DOC_KEY,
        }

    def test_an_unreadable_loser_is_dropped_and_the_rest_kept(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("old-doc", "body", label("restricted"))
        successor = _item("new-doc", _reverse(title="Newer"))
        (out,) = conceal_unreadable_successors([successor], doc_store, "internal")
        assert out.metadata == {"reconciliation": "supersede", "title": "Newer"}
        assert out.item_id == "new-doc"
        assert successor.metadata["supersedes_doc_id"] == "old-doc"

    def test_a_readable_loser_is_still_named(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("old-doc", "body", {})
        pool = [_item("new-doc", _reverse())]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert out[0] is pool[0]

    def test_the_store_judges_not_the_pool(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("old-doc", "body", label("confidential"))
        pool = [_item("new-doc", _reverse())]
        assert (
            self._loser_of(
                conceal_unreadable_successors(pool, doc_store, "internal")[0]
            )
            is None
        )
        assert (
            self._loser_of(
                conceal_unreadable_successors(pool, doc_store, "confidential")[0]
            )
            == "old-doc"
        )

    def test_a_chunk_loser_is_judged_by_its_parent(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("parent", "classified body", label("restricted"))
        chunk_id = chunk_doc_id("parent", 0)
        doc_store.put(chunk_id, "slice", {})
        (out,) = conceal_unreadable_successors(
            [_item("new-doc", _reverse(chunk_id))], doc_store, "internal"
        )
        assert self._loser_of(out) is None

    def test_a_store_error_reads_as_unreadable(self) -> None:
        class _Broken:
            def get(self, _doc_id: str) -> dict[str, Any]:
                msg = "store down"
                raise RuntimeError(msg)

        broken: Any = _Broken()
        (out,) = conceal_unreadable_successors(
            [_item("new-doc", _reverse())], broken, "internal"
        )
        assert self._loser_of(out) is None

    def test_restricted_reads_nothing_and_changes_nothing(self) -> None:
        store = MagicMock()
        pool = [_item("new-doc", _reverse()), _item("loser-doc", _lifecycle("x", "s"))]
        out = conceal_unreadable_successors(pool, store, "restricted")
        assert all(a is b for a, b in zip(out, pool, strict=True))
        store.get.assert_not_called()

    def test_an_unlabelled_store_changes_nothing_at_any_clearance(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("old-doc", "body", {})
        doc_store.put("succ-doc", "body", {})
        pool = [
            _item("new-doc", _reverse()),
            _item("loser-doc", _lifecycle("superseded", "succ-doc")),
        ]
        for clearance in ("internal", "confidential", "restricted"):
            out = conceal_unreadable_successors(pool, doc_store, clearance)  # type: ignore[arg-type]
            assert all(a is b for a, b in zip(out, pool, strict=True))

    def test_a_document_in_a_chain_drops_only_the_unreadable_end(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """Middle of a chain: successor of ``old-doc``, loser to ``succ-doc``."""
        doc_store.put("old-doc", "body", label("restricted"))
        doc_store.put("succ-doc", "body", {})
        middle = _item(
            "mid-doc", {**_reverse(), **_lifecycle("superseded", "succ-doc")}
        )
        (out,) = conceal_unreadable_successors([middle], doc_store, "internal")
        assert "supersedes_doc_id" not in out.metadata
        assert out.metadata[LIFECYCLE_KEY] == {
            "state": "superseded",
            "superseded_by": "succ-doc",
        }
        doc_store.put("old-doc", "body", {})
        doc_store.put("succ-doc", "body", label("restricted"))
        (out,) = conceal_unreadable_successors([middle], doc_store, "internal")
        assert out.metadata["supersedes_doc_id"] == "old-doc"
        assert out.metadata[LIFECYCLE_KEY] == {"state": "superseded"}

    def test_both_pointers_dropped_when_both_are_unreadable(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("old-doc", "body", label("restricted"))
        doc_store.put("succ-doc", "body", label("restricted"))
        shared = {**_reverse(), **_lifecycle("superseded", "succ-doc")}
        (out,) = conceal_unreadable_successors(
            [_item("mid-doc", shared)], doc_store, "internal"
        )
        assert out.metadata == {
            "reconciliation": "supersede",
            LIFECYCLE_KEY: {"state": "superseded"},
        }
        assert shared["supersedes_doc_id"] == "old-doc", "the shared bag is untouched"
        assert shared[LIFECYCLE_KEY]["superseded_by"] == "succ-doc"

    @pytest.mark.parametrize("value", [None, "", 7, ["old-doc"], {"id": "old-doc"}])
    def test_a_value_that_names_no_id_is_left_as_stored(
        self, doc_store: SQLiteDocumentStore, value: Any
    ) -> None:
        """No writer stores a list or a mapping here; one is left, never a crash."""
        doc_store.put("old-doc", "body", label("restricted"))
        pool = [_item("new-doc", _reverse(value))]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert out[0] is pool[0]

    @pytest.mark.parametrize("value", [["old-doc"], {"id": "old-doc"}, 7])
    def test_a_malformed_value_beside_a_concealed_one_is_left_alone(
        self, doc_store: SQLiteDocumentStore, value: Any
    ) -> None:
        """The drop is reached (another item names a hidden id) and must not choke."""
        doc_store.put("old-doc", "body", label("restricted"))
        odd = _item("odd-doc", _reverse(value))
        named = _item("new-doc", _reverse())
        out = conceal_unreadable_successors([odd, named], doc_store, "internal")
        assert out[0] is odd
        assert self._loser_of(out[1]) is None


def _update(target: Any = "base-doc", **extra: Any) -> dict[str, Any]:
    """An UPDATE addendum's metadata: ``updates_doc_id`` naming the annotated doc."""
    return {"updates_doc_id": target, "reconciliation": "update", **extra}


class TestConcealTheUpdatePointer:
    """An addendum's ``updates_doc_id`` names the candidate it annotates (#1029)."""

    @staticmethod
    def _target_of(item: PackItem) -> Any:
        return (item.metadata or {}).get("updates_doc_id")

    def test_an_unreadable_target_is_dropped_and_the_marker_kept(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("base-doc", "body", label("restricted"))
        addendum = _item("add-doc", _update(title="Addendum"))
        (out,) = conceal_unreadable_successors([addendum], doc_store, "internal")
        assert out.metadata == {"reconciliation": "update", "title": "Addendum"}
        assert addendum.metadata["updates_doc_id"] == "base-doc"

    def test_a_readable_target_is_still_named(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("base-doc", "body", {})
        pool = [_item("add-doc", _update())]
        out = conceal_unreadable_successors(pool, doc_store, "internal")
        assert out[0] is pool[0]

    def test_the_store_judges_not_the_pool(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("base-doc", "body", label("confidential"))
        pool = [_item("add-doc", _update())]
        assert (
            self._target_of(
                conceal_unreadable_successors(pool, doc_store, "internal")[0]
            )
            is None
        )
        assert (
            self._target_of(
                conceal_unreadable_successors(pool, doc_store, "confidential")[0]
            )
            == "base-doc"
        )

    def test_the_three_pointers_are_judged_in_one_call(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        for doc_id in ("base-doc", "old-doc", "succ-doc"):
            doc_store.put(doc_id, "body", label("restricted"))
        spied = MagicMock(wraps=doc_store)
        row = {
            "doc_id": "mid-doc",
            "metadata": {
                **_update(),
                **_reverse(),
                **_lifecycle("superseded", "succ-doc"),
            },
        }
        (out,) = conceal_document_rows([row], spied, "internal")
        assert out["metadata"] == {
            "reconciliation": "supersede",
            LIFECYCLE_KEY: {"state": "superseded"},
        }
        assert sorted(c.args[0] for c in spied.get.call_args_list) == [
            "base-doc",
            "old-doc",
            "succ-doc",
        ]


class TestConcealDocumentRows:
    """The batched REST variant: one store judgement per response (#1029)."""

    @staticmethod
    def _row(doc_id: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
        return {"doc_id": doc_id, "content": "body " + doc_id, "metadata": metadata}

    def test_each_pointer_is_dropped_from_a_copy_and_the_stored_row_is_intact(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("hidden-doc", "body", label("restricted"))
        doc_store.put("loser-doc", "x", _lifecycle("superseded", "hidden-doc"))
        doc_store.put("new-doc", "y", _reverse("hidden-doc"))
        stored = [doc_store.get("loser-doc"), doc_store.get("new-doc")]
        assert all(stored)
        rows: Any = stored
        out = conceal_document_rows(rows, doc_store, "internal")
        assert out[0]["metadata"][LIFECYCLE_KEY] == {"state": "superseded"}
        assert "supersedes_doc_id" not in out[1]["metadata"]
        assert [r["content"] for r in out] == ["x", "y"]
        assert doc_store.get("loser-doc") == stored[0]
        assert doc_store.get("new-doc") == stored[1]
        assert "hidden-doc" in str(stored)

    def test_a_row_with_nothing_to_drop_is_the_same_object(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("hidden-doc", "body", label("restricted"))
        doc_store.put("open-doc", "body", {})
        rows = [
            self._row("a", None),
            self._row("b", {}),
            self._row("c", _reverse("open-doc")),
            self._row("d", _reverse("hidden-doc")),
        ]
        out = conceal_document_rows(rows, doc_store, "internal")
        assert [a is b for a, b in zip(out, rows, strict=True)] == [
            True,
            True,
            True,
            False,
        ]

    def test_a_restricted_caller_reads_nothing_and_gets_the_rows_back(self) -> None:
        store = MagicMock()
        rows = [self._row("a", _reverse("hidden-doc"))]
        (out,) = conceal_document_rows(rows, store, "restricted")
        assert out is rows[0]
        store.get.assert_not_called()

    def test_one_read_per_distinct_id_however_many_rows_name_it(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        doc_store.put("hidden-doc", "body", label("restricted"))
        doc_store.put("open-doc", "body", {})
        counting = MagicMock(wraps=doc_store)
        rows = [
            *(
                self._row(f"l{i}", _lifecycle("superseded", "hidden-doc"))
                for i in range(5)
            ),
            *(self._row(f"n{i}", _reverse("hidden-doc")) for i in range(5)),
            *(self._row(f"o{i}", _reverse("open-doc")) for i in range(5)),
            self._row("plain", {}),
        ]
        out = conceal_document_rows(rows, counting, "internal")
        asked = [c.args[0] for c in counting.get.call_args_list]
        assert sorted(asked) == ["hidden-doc", "open-doc"]
        assert sum("hidden-doc" in str(r) for r in out) == 0

    def test_a_pool_batches_the_same_way(self, doc_store: SQLiteDocumentStore) -> None:
        doc_store.put("hidden-doc", "body", label("restricted"))
        counting = MagicMock(wraps=doc_store)
        pool = [_item(f"i{i}", _reverse("hidden-doc")) for i in range(6)]
        conceal_unreadable_successors(pool, counting, "internal")
        assert [c.args[0] for c in counting.get.call_args_list] == ["hidden-doc"]

    def test_no_pointer_anywhere_means_no_read(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        counting = MagicMock(wraps=doc_store)
        conceal_document_rows([self._row("a", {"title": "t"})], counting, "internal")
        conceal_unreadable_successors(
            [_item("b", {"title": "t"})], counting, "internal"
        )
        counting.get.assert_not_called()


class TestConcealedIdsMetadata:
    def test_a_row_the_collect_seam_never_saw_is_not_judged(self) -> None:
        """No collected copy, no label to read: the sensitivity ids alone."""
        from trellis.retrieve.sensitivity import CandidateIndex
        from trellis.schemas.pack import RejectedItem

        rejected = [
            RejectedItem(item_id="ghost", item_type="document", reason="noise"),
        ]
        out = concealed_ids_metadata(
            rejected, CandidateIndex("internal"), "internal", ParentVerdictCache(None)
        )
        assert out == {}


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

    @pytest.mark.parametrize("clearance", ["internal", "confidential"])
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

    @pytest.mark.parametrize("clearance", ["public", "not-a-label"])
    def test_a_public_only_caller_is_shown_no_count_at_all(
        self, clearance: Sensitivity
    ) -> None:
        """Every key stays, every number that sees past the filter is zero."""
        report = _report(("secret", "sensitivity"), ("big", "token_budget"))
        withholding = {**_withholding("secret", "big"), "served_count": 3}
        shown_report, shown_withholding = conceal_withheld_ids(
            report,
            withholding,
            ["secret"],
            clearance,  # type: ignore[arg-type]
        )
        assert shown_report is not None
        assert shown_withholding is not None
        assert shown_report["rejected_items"] == []
        assert shown_report["other"] == "kept"
        assert shown_withholding == {**public_withholding(3)}
        assert shown_withholding["total"] == 0
        assert shown_withholding["by_reason"] == {}
        assert shown_withholding["withheld_item_ids"] == []
        assert shown_withholding["served_count"] == 3

    def test_a_public_only_caller_with_no_block_is_shown_none(self) -> None:
        assert conceal_withheld_ids(None, None, [], "public") == (None, None)

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
