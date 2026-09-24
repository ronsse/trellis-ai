"""Never serve both sides of a declared supersession in one pack (#613).

``docs/design/plan-memory-lifecycle.md`` §4 commits to a *pair*: SCD-2
supersede plus recency-wins-at-retrieval, with the losing version
retrievable on demand — and "never serve both sides of a contradiction in
one pack (a pack-assembly invariant, cheap to enforce at build time)". The
first half shipped (``mark_document_superseded`` + the #397 recency pin in
``tests/unit/mcp/test_reconcile.py``); the second did not, so a stamped
loser and the successor that replaced it could both be served in one pack.

The gate is **pairwise, not per-item**. A superseded item whose successor is
*not* among the candidates is served exactly as before (recency-demoted,
fetchable) — ``test_reconcile.py::test_superseded_is_not_excluded_from_retrieval``
pins that half and must stay green unchanged. Only when the successor is in
the same candidate pool is the loser withheld, and reported as
``superseded``.

Every pool here holds at least three items of at least two ``item_type``s
with distinct scores (#447's two fixture traps), and the loser is always
given a *higher* raw score than its successor, so a withheld loser cannot
be an accident of ranking or budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from structlog.testing import capture_logs

from trellis.mutate.retention import ARCHIVED_STATE
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import withholding_from_payload
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from pathlib import Path

#: The ``Lifecycle.state`` value ``mark_document_superseded`` writes, which
#: the gate reuses verbatim as its ``RejectedItem.reason``.
SUPERSEDED = "superseded"

#: Clears the content floor's five-substance-word demotion.
_BODY = "the deploy checklist requires draining the write queue first"

#: Big enough that no fixture here is cut by the item or token budget.
_SECTION = SectionRequest(name="tactical", max_items=20, max_tokens=20000)


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(
    item_id: str,
    score: float,
    *,
    axis: str = "keyword",
    item_type: str = "document",
    metadata: dict[str, Any] | None = None,
) -> PackItem:
    """An item stamped with its axis the way all three built-ins stamp it."""
    return PackItem(
        item_id=item_id,
        item_type=item_type,
        excerpt=_BODY,
        relevance_score=score,
        metadata={"source_strategy": axis, **(metadata or {})},
    )


def _stamp(successor: Any, *, state: str = SUPERSEDED) -> dict[str, Any]:
    return {LIFECYCLE_KEY: {"state": state, "superseded_by": successor}}


def _loser(item_id: str, successor: Any, score: float, **kw: Any) -> PackItem:
    return _item(item_id, score, metadata=_stamp(successor), **kw)


def _node(item_id: str, score: float, evidence_ref: str) -> PackItem:
    """A graph item as ``GraphSearch`` builds one for a ``save_knowledge`` node.

    Node properties are spread into ``metadata``, so the node's pointer at
    its evidence document arrives as ``metadata["evidence_ref"]``.
    """
    return _item(
        item_id,
        score,
        axis="graph",
        item_type="entity",
        metadata={"evidence_ref": evidence_ref},
    )


def _unrelated() -> list[PackItem]:
    return [
        _item("u-doc", 0.40),
        _item("u-ent", 0.30, item_type="entity"),
    ]


def _served(pack: Any) -> list[str]:
    return [i.item_id for i in pack.items]


def _served_sectioned(pack: Any) -> list[str]:
    return [i.item_id for s in pack.sections for i in s.items]


def _rows(pack: Any) -> list[tuple[str, str, str | None]]:
    return [
        (r.item_id, r.reason, r.strategy_source)
        for r in pack.retrieval_report.rejected_items
    ]


def _summary(pack: Any) -> dict[str, Any]:
    return pack.metadata["withholding"]


class TestTheLoserIsWithheldWhenItsSuccessorIsPresent:
    def test_one_axis(self, tmp_path: Path) -> None:
        """T1 — the invariant itself, end to end through the event."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("old", "new", 0.90),
                        _item("new", 0.60, item_type="entity"),
                        *_unrelated(),
                    ],
                )
            ],
            event_log=log,
        )
        pack = builder.build("deploy checklist")

        assert "new" in _served(pack)
        assert "old" not in _served(pack)
        assert sorted(_served(pack)) == ["new", "u-doc", "u-ent"]
        assert _rows(pack) == [("old", SUPERSEDED, "keyword")]
        row = pack.retrieval_report.rejected_items[0]
        assert (row.item_type, row.relevance_score) == ("document", 0.90)
        assert _summary(pack)["by_reason"] == {SUPERSEDED: 1}
        assert _summary(pack)["total"] == 1

        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
        assert payload["withholding"]["withheld_item_ids"] == ["old"]
        assert [(r["item_id"], r["reason"]) for r in payload["rejected_items"]] == [
            ("old", SUPERSEDED)
        ]

    def test_successor_absent_the_loser_is_served(self) -> None:
        """T2 — the pairwise half. The build-level twin of
        ``test_reconcile.py::test_superseded_is_not_excluded_from_retrieval``:
        with nothing to lose to, the loser stays retrievable (§4)."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "elsewhere", 0.90), *_unrelated()])
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["old", "u-doc", "u-ent"]
        assert _summary(pack)["total"] == 0
        assert _rows(pack) == []

    def test_successor_on_another_axis(self) -> None:
        """T3 — the successor may arrive on a different strategy, which is
        why the gate runs over the collected pool and not per strategy."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "new", 0.90), _item("u-doc", 0.4)]),
                _strategy(
                    "semantic",
                    [
                        _item("new", 0.60, axis="semantic"),
                        _item("u-ent", 0.3, axis="semantic", item_type="entity"),
                    ],
                ),
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["new", "u-doc", "u-ent"]
        assert _rows(pack) == [("old", SUPERSEDED, "keyword")]

    def test_successor_present_only_as_its_graph_node(self) -> None:
        """T4 — the node/document seam. ``save_knowledge`` writes a document
        and a node pointing at it; the graph axis serves the node, so a
        successor named by its document id is present as that node."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "new-doc", 0.90), _item("u", 0.4)]),
                _strategy("graph", [_node("new-node", 0.60, evidence_ref="new-doc")]),
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["new-node", "u"]
        assert _rows(pack) == [("old", SUPERSEDED, "keyword")]

    def test_a_stale_unstamped_copy_of_the_loser_is_withheld_too(self) -> None:
        """The #338 shape: a vector row's metadata is an embed-time snapshot,
        so one axis can return the loser stamped and another unstamped. The
        invariant is about the *item*, so every copy of the id is withheld —
        otherwise the unstamped copy is served beside the successor and both
        sides reach the pack after all."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "new", 0.70), _item("new", 0.6)]),
                _strategy(
                    "semantic",
                    [
                        _item("old", 0.90, axis="semantic"),
                        _item("u-ent", 0.3, axis="semantic", item_type="entity"),
                    ],
                ),
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["new", "u-ent"]
        assert sorted(_rows(pack)) == [
            ("old", SUPERSEDED, "keyword"),
            ("old", SUPERSEDED, "semantic"),
        ]
        assert _summary(pack)["by_reason"] == {SUPERSEDED: 1}


class TestAMalformedStampNeverHides:
    """T5 — the ``is_archived`` rule: a bad record is a reason to keep
    serving, never to hide. Every case below has a genuine candidate in the
    pool the stamp could be (mis)read as pointing at."""

    def _pack(self, loser_metadata: dict[str, Any]) -> Any:
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _item("old", 0.90, metadata=loser_metadata),
                        _item("new", 0.60, item_type="entity"),
                        _item("u", 0.40),
                    ],
                )
            ]
        )
        return builder.build("deploy checklist")

    def test_malformed_stamps_are_served(self) -> None:
        cases: list[dict[str, Any]] = [
            {LIFECYCLE_KEY: {"state": SUPERSEDED}},  # superseded_by missing
            _stamp(""),
            _stamp(123),
            _stamp(None),
            _stamp("old"),  # self-reference
            _stamp("new", state="current"),  # not superseded at all
            {LIFECYCLE_KEY: "superseded"},  # record not a dict
        ]
        for metadata in cases:
            pack = self._pack(metadata)
            assert sorted(_served(pack)) == ["new", "old", "u"], metadata
            assert _summary(pack)["total"] == 0, metadata

    def test_a_self_reference_is_malformed_not_a_contradiction(self) -> None:
        """A record naming itself is served by either reading — a one-node
        loop is a cycle, and cycles are kept — so what separates "malformed"
        from "contradiction" is the operator signal: a self-reference must
        not raise the cycle warning, which asserts two memories disagree."""
        with capture_logs() as logs:
            pack = self._pack(_stamp("old"))
        assert sorted(_served(pack)) == ["new", "old", "u"]
        assert [e for e in logs if e["event"] == "supersession_cycle_kept"] == []

    def test_the_well_formed_control_is_withheld(self) -> None:
        """The same fixture with a well-formed stamp, so the cases above are
        served because they are malformed and not because the fixture can
        never withhold."""
        pack = self._pack(_stamp("new"))
        assert sorted(_served(pack)) == ["new", "u"]


class TestChainsAndCycles:
    def test_a_chain_serves_only_its_head(self) -> None:
        """T6 — A→B→C: A and B are both superseded by something present."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("a", "b", 0.95),
                        _item("u", 0.50),
                        _loser("b", "c", 0.90, item_type="entity"),
                        _item("c", 0.60),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["c", "u"]
        # Order of rows follows the pool, so the served record is stable.
        assert _rows(pack) == [
            ("a", SUPERSEDED, "keyword"),
            ("b", SUPERSEDED, "keyword"),
        ]
        assert _summary(pack)["by_reason"] == {SUPERSEDED: 2}

    def test_a_chain_whose_head_is_absent_serves_its_newest_present_link(
        self,
    ) -> None:
        """A→B→(C absent): B has nothing present to lose to, so it is served
        and A is withheld behind it."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("a", "b", 0.95),
                        _loser("b", "c", 0.90, item_type="entity"),
                        _item("u", 0.50),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")
        assert sorted(_served(pack)) == ["b", "u"]
        assert _rows(pack) == [("a", SUPERSEDED, "keyword")]

    def test_a_two_cycle_is_contradictory_and_both_are_served(self) -> None:
        """A↔B names no winner. Withholding both would hide the whole claim —
        the ``No context found`` failure #404 exists to prevent — so both are
        kept and the contradiction is logged."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("a", "b", 0.95),
                        _loser("b", "a", 0.90, item_type="entity"),
                        _item("u", 0.50),
                    ],
                )
            ]
        )
        with capture_logs() as logs:
            pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["a", "b", "u"]
        assert _summary(pack)["total"] == 0
        cycle_logs = [e for e in logs if e["event"] == "supersession_cycle_kept"]
        assert len(cycle_logs) == 1
        assert cycle_logs[0]["log_level"] == "warning"
        assert sorted(cycle_logs[0]["item_ids"]) == ["a", "b"]

    def test_a_three_cycle_is_kept_whole(self) -> None:
        """A→B→C→A: every member has a present successor, so a rule that only
        checked presence would withhold all three and serve none of them."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("a", "b", 0.95),
                        _loser("b", "c", 0.90, item_type="entity"),
                        _loser("c", "a", 0.85),
                        _item("u", 0.50),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")
        assert sorted(_served(pack)) == ["a", "b", "c", "u"]

    def test_a_tail_into_a_cycle_is_withheld(self) -> None:
        """D→A with A↔B: D is superseded by something present and is not
        itself part of the contradiction."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("d", "a", 0.97),
                        _loser("a", "b", 0.95),
                        _loser("b", "a", 0.90, item_type="entity"),
                        _item("u", 0.50),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")
        assert sorted(_served(pack)) == ["a", "b", "u"]
        assert _rows(pack) == [("d", SUPERSEDED, "keyword")]


class TestTheSectionedPath:
    def test_sectioned_withholds_the_loser(self, tmp_path: Path) -> None:
        """T7 — ``build_sectioned`` collects its own pool; it needs the gate
        too."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "new", 0.90), _item("u-doc", 0.4)]),
                _strategy(
                    "semantic",
                    [
                        _item("new", 0.60, axis="semantic"),
                        _item("u-ent", 0.3, axis="semantic", item_type="entity"),
                    ],
                ),
            ],
            event_log=log,
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_SECTION])

        assert sorted(_served_sectioned(pack)) == ["new", "u-doc", "u-ent"]
        assert _summary(pack)["by_reason"] == {SUPERSEDED: 1}
        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
        assert payload["withholding"]["withheld_item_ids"] == ["old"]

    def test_sectioned_successor_absent_serves_the_loser(self) -> None:
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "elsewhere", 0.9), *_unrelated()])
            ]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_SECTION])
        assert sorted(_served_sectioned(pack)) == ["old", "u-doc", "u-ent"]
        assert _summary(pack)["total"] == 0


class TestAttributionAndRows:
    def test_an_archived_loser_is_attributed_to_archived(self) -> None:
        """T8a — archived is a collect-seam gate and runs first; the item is
        attributed to the first gate that removed it."""
        both = _item(
            "old",
            0.90,
            metadata={LIFECYCLE_KEY: {"state": ARCHIVED_STATE, "superseded_by": "new"}},
        )
        stamped_elsewhere = _loser("old", "new", 0.85, axis="semantic")
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [both, _item("new", 0.6, item_type="entity")]),
                _strategy("semantic", [stamped_elsewhere, _item("u", 0.4)]),
            ]
        )
        pack = builder.build("deploy checklist")

        assert sorted(_served(pack)) == ["new", "u"]
        assert _summary(pack)["by_reason"] == {"archived": 1}

    def test_a_loser_returned_by_two_axes_is_two_rows_and_one_item(self) -> None:
        """T8b — rows are per (strategy, item) like every other gate, and the
        gate runs before exact dedup, so both copies are recorded; the summary
        counts distinct ids."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_loser("old", "new", 0.90), _item("new", 0.6)]),
                _strategy(
                    "semantic",
                    [
                        _loser("old", "new", 0.80, axis="semantic"),
                        _item("u", 0.4, axis="semantic", item_type="entity"),
                    ],
                ),
            ]
        )
        pack = builder.build("deploy checklist")

        assert _rows(pack) == [
            ("old", SUPERSEDED, "keyword"),
            ("old", SUPERSEDED, "semantic"),
        ]
        assert _summary(pack)["by_reason"] == {SUPERSEDED: 1}
        assert sorted(_served(pack)) == ["new", "u"]


class TestTheNote:
    def test_the_rendered_note_names_the_reason_and_no_id(self) -> None:
        """T9 — the caller-facing note carries counts and reasons, never ids."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _loser("old-zq7", "new-kx4", 0.90),
                        _item("new-kx4", 0.60, item_type="entity"),
                        _item("u", 0.40),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")
        rows = [
            {
                "item_id": i.item_id,
                "item_type": i.item_type,
                "excerpt": i.excerpt,
                "relevance_score": i.relevance_score,
            }
            for i in pack.items
        ]
        withholding = withholding_from_payload(_summary(pack))

        for rendered in (
            format_pack_as_markdown(
                rows, "deploy checklist", max_tokens=4000, withholding=withholding
            ),
            format_pack_as_index_markdown(
                rows,
                "deploy checklist",
                max_tokens=4000,
                pack_id="pk1",
                withholding=withholding,
            ),
        ):
            assert f"{SUPERSEDED} 1" in rendered
            # In the header, above the item blocks (#404's placement rule).
            assert rendered.index(f"{SUPERSEDED} 1") < rendered.index("new-kx4")
            assert "old-zq7" not in rendered
