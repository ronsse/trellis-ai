"""A pack must state what it did not serve (trellis-ai#404).

Eleven gates remove candidates before a pack is returned. Two of them —
``exclude_archived`` and ``exclude_noise`` at the collect seam — recorded
their decision *nowhere*: their only observable was a ``logger.debug``
line, which is a no-op under the CLI's ``WARNING`` default and under the
MCP server's own configuration. And none of the ten reached the pack the
caller reads, so "this layer was empty" and "this layer was redacted"
rendered identically.

The tests here are grouped by the claim each one would falsify:

* the definition (withheld is *absence*, not rejection),
* the two newly-recorded gates actually firing in a real build,
* the summary reaching the pack and the event,
* the note reaching the **header** of the rendered surface, including
  when the renderer's item loop breaks on budget,
* the note never carrying ids or content,
* the reporter itself failing loudly rather than silently,
* the eleventh gate — section routing — being recorded at all, and being
  reported as the narrower claim it makes (#440),
* the **sectioned** path's rejection telemetry, every call of which could be
  deleted with the whole suite green (#447).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.mutate.retention import ARCHIVED_STATE
from trellis.retrieve.excerpts import (
    CONTENT_FLOOR_REJECTION_REASON,
    ContentFloorConfig,
)
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.lifecycle import ARCHIVED_REJECTION_REASON
from trellis.retrieve.noise import NOISE_REJECTION_REASON
from trellis.retrieve.pack_builder import PackBuilder, SemanticDedupConfig
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import (
    WithheldGroup,
    WithholdingSummary,
    format_withholding_note,
    summarize_withheld,
    withholding_from_payload,
)
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.schemas.pack import (
    Pack,
    PackBudget,
    PackItem,
    RejectedItem,
    SectionRequest,
)
from trellis.schemas.well_known import ACTIVITY
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from pathlib import Path

#: Long enough to clear the content floor's five-substance-word demotion,
#: and distinctive enough that a leak into another item's block is visible.
_BODY = "the deploy checklist requires draining the write queue first"

#: The body of an item that is withheld. Must appear in no rendering.
_WITHHELD_BODY = "kangaroo pouch telemetry never belongs in a served pack"


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(
    item_id: str,
    *,
    score: float = 0.5,
    excerpt: str = _BODY,
    metadata: dict[str, Any] | None = None,
) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=excerpt,
        relevance_score=score,
        metadata=metadata or {},
    )


def _typed(
    item_id: str,
    item_type: str,
    score: float,
    *,
    excerpt: str = _BODY,
) -> PackItem:
    """An item whose ``item_type`` and ``relevance_score`` are both its own.

    ``_item`` fixes ``item_type="document"``; a pool built only from it
    cannot distinguish a rejection row that copies the field from one that
    hard-codes it (#456).
    """
    return PackItem(
        item_id=item_id,
        item_type=item_type,
        excerpt=excerpt,
        relevance_score=score,
    )


def _archived(item_id: str, **kw: Any) -> PackItem:
    return _item(item_id, metadata={LIFECYCLE_KEY: {"state": ARCHIVED_STATE}}, **kw)


def _noisy(item_id: str, **kw: Any) -> PackItem:
    return _item(item_id, metadata={"content_tags": {"signal_quality": "noise"}}, **kw)


def _summary(pack: Any) -> dict[str, Any]:
    return pack.metadata["withholding"]


class TestWithheldMeansAbsent:
    """``withheld = {rejected ids} - {served ids}`` is the definition.

    A rejection is not an absence. Reporting one as the other would tell a
    caller a memory was kept from it while the memory is on screen — the
    single most damaging way this report could be wrong, because it makes
    the honest ones unbelievable.
    """

    def test_a_dedup_loser_whose_winner_is_served_is_not_withheld(self) -> None:
        """The case the set difference exists for.

        ``_deduplicate_tracked`` records a ``RejectedItem`` for the losing
        *copy* of an id two strategies both returned. The winner carries
        the same ``item_id`` and is served. Counting that rejection would
        report ``1 item withheld`` about an item in the pack.
        """
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_item("d1", score=0.7)]),
                _strategy("semantic", [_item("d1", score=0.9)]),
            ]
        )
        pack = builder.build("deploy checklist")

        rejected = pack.retrieval_report.rejected_items
        assert [r.reason for r in rejected] == ["dedup"], (
            "the fixture must actually produce a dedup rejection, or this "
            "test passes vacuously"
        )
        assert [i.item_id for i in pack.items] == ["d1"]
        assert _summary(pack)["total"] == 0
        assert _summary(pack)["by_reason"] == {}
        # Recorded, so a zero total reads as "nothing was withheld" rather
        # than "nothing was rejected".
        assert _summary(pack)["non_absence_reasons"] == ["dedup"]

    def test_an_id_one_axis_gated_and_another_axis_served_is_not_withheld(
        self,
    ) -> None:
        """The #338 shape: the vector row's tags are an embed-time snapshot.

        The same document can arrive noise-tagged from the document store
        and untagged from a stale vector row. One copy is gated, the other
        is served — and the item is *present*, so it is not withheld.
        """
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_noisy("d1", score=0.9)]),
                _strategy("semantic", [_item("d1", score=0.7)]),
            ]
        )
        pack = builder.build("deploy checklist")

        assert [i.item_id for i in pack.items] == ["d1"]
        assert _summary(pack)["total"] == 0
        assert _summary(pack)["non_absence_reasons"] == [NOISE_REJECTION_REASON]

    def test_an_id_rejected_twice_is_attributed_to_the_first_gate(self) -> None:
        """``rejected`` is appended in pipeline order; the first removal is
        the one that actually happened."""
        summary = summarize_withheld(
            [
                RejectedItem(item_id="d1", item_type="document", reason="archived"),
                RejectedItem(item_id="d1", item_type="document", reason="max_items"),
            ],
            served_item_ids=[],
        )
        assert summary.groups == (WithheldGroup(reason="archived", count=1),)
        assert summary.total == 1

    def test_a_dedup_loser_whose_winner_is_dropped_downstream_names_the_real_gate(
        self,
    ) -> None:
        """Why ``dedup`` is excluded *before* the subtraction, not by it.

        The set difference alone does not reach this case: the winning copy
        was itself dropped by ``max_items``, so no copy of the id is served
        and the subtraction keeps the row. First-gate attribution would then
        name ``dedup`` — telling the caller a *duplicate* was withheld about
        an item that was withheld for running out of room, which is the one
        reason it certainly was not. :data:`NON_ABSENCE_REASONS` is what
        makes the honest gate win; empty it and this reads ``dedup 1``.
        """
        summary = summarize_withheld(
            [
                RejectedItem(item_id="d1", item_type="document", reason="dedup"),
                RejectedItem(item_id="d1", item_type="document", reason="max_items"),
            ],
            served_item_ids=[],
        )
        assert summary.groups == (WithheldGroup(reason="max_items", count=1),)
        assert summary.total == 1
        assert summary.non_absence_reasons == ("dedup",)

    def test_groups_are_ordered_by_count_then_name(self) -> None:
        """Stable ordering, so two packs' notes are diffable."""
        summary = summarize_withheld(
            [
                RejectedItem(item_id="a", item_type="document", reason="zzz"),
                RejectedItem(item_id="b", item_type="document", reason="aaa"),
                RejectedItem(item_id="c", item_type="document", reason="aaa"),
            ],
            served_item_ids=[],
        )
        assert summary.groups == (
            WithheldGroup(reason="aaa", count=2),
            WithheldGroup(reason="zzz", count=1),
        )


class TestTheTwoSilentGatesAreRecorded:
    """The gates whose only observable was ``logger.debug``.

    Behaviour is unchanged — the same items are dropped — but a drop is now
    a row a caller and an analyzer can read.
    """

    def test_an_archived_item_is_absent_and_recorded(self) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep"), _archived("gone")])]
        )
        pack = builder.build("deploy checklist")

        assert [i.item_id for i in pack.items] == ["keep"]
        rejected = pack.retrieval_report.rejected_items
        assert [(r.item_id, r.reason) for r in rejected] == [
            ("gone", ARCHIVED_REJECTION_REASON)
        ]
        assert _summary(pack)["by_reason"] == {ARCHIVED_REJECTION_REASON: 1}

    def test_a_noise_item_is_absent_and_recorded(self) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep"), _noisy("gone")])]
        )
        pack = builder.build("deploy checklist")

        assert [i.item_id for i in pack.items] == ["keep"]
        rejected = pack.retrieval_report.rejected_items
        assert [(r.item_id, r.reason) for r in rejected] == [
            ("gone", NOISE_REJECTION_REASON)
        ]
        assert _summary(pack)["by_reason"] == {NOISE_REJECTION_REASON: 1}

    def test_a_caller_supplied_spec_still_inverts_the_noise_boundary(self) -> None:
        """The default is a default. Curation tooling asking *for* noise
        must not then be told the noise it asked for was withheld."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_noisy("n1")])],
        )
        pack = builder.build(
            "deploy checklist",
            tag_filters={"signal_quality": {"in": ["noise"]}},
        )
        assert [i.item_id for i in pack.items] == ["n1"]
        assert _summary(pack)["total"] == 0

    def test_the_gate_records_the_strategy_that_produced_the_item(self) -> None:
        """``_promote_strategy_source`` has not run at this seam.

        Without an explicit stamp every collect-gate rejection carries
        ``strategy_source=None`` and ``analyze_pack_telemetry`` buckets all
        of them under ``"unknown"`` — a per-axis report that cannot see the
        one axis a demotion came from.
        """
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_item("keep")]),
                _strategy("semantic", [_noisy("gone")]),
            ]
        )
        pack = builder.build("deploy checklist")
        rejected = pack.retrieval_report.rejected_items
        assert [(r.item_id, r.strategy_source) for r in rejected] == [
            ("gone", "semantic")
        ]

    def test_archived_is_evaluated_before_noise(self) -> None:
        """Order is load-bearing: attribution goes to the first gate, and
        the composition being replaced was ``exclude_noise(exclude_archived(
        ...))``."""
        both = _item(
            "both",
            metadata={
                LIFECYCLE_KEY: {"state": ARCHIVED_STATE},
                "content_tags": {"signal_quality": "noise"},
            },
        )
        builder = PackBuilder(strategies=[_strategy("keyword", [both])])
        pack = builder.build("deploy checklist")
        assert _summary(pack)["by_reason"] == {ARCHIVED_REJECTION_REASON: 1}


class TestTheSummaryReachesThePackAndTheEvent:
    def test_the_event_carries_the_summary_and_the_ids(self, tmp_path: Path) -> None:
        """Ids ride the event, never the note: a different access path and a
        different audience."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep"), _noisy("gone")])],
            event_log=log,
        )
        builder.build("deploy checklist")

        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
        assert payload["withholding"]["total"] == 1
        assert payload["withholding"]["by_reason"] == {NOISE_REJECTION_REASON: 1}
        assert payload["withholding"]["withheld_item_ids"] == ["gone"]

    def test_the_event_carries_the_summary_even_when_nothing_was_withheld(
        self, tmp_path: Path
    ) -> None:
        """ "The summary ran and found nothing" must be distinguishable from
        "the summary never ran" — the same posture ``content_floor`` takes.
        A key that only appears when it is interesting cannot answer
        "is this deployment running the fix?"."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep")])], event_log=log
        )
        builder.build("deploy checklist")

        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
        assert payload["withholding"] == {
            "total": 0,
            "by_reason": {},
            "withheld_item_ids": [],
            "non_absence_reasons": [],
            "section_filtered": 0,
            "served_count": 1,
        }

    def test_a_sectioned_pack_carries_the_summary_too(self, tmp_path: Path) -> None:
        """``SectionedPack`` has no top-level ``RetrievalReport``, which is
        why the summary lives on ``metadata`` for both pack kinds."""
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep"), _archived("gone")])],
            event_log=log,
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[SectionRequest(name="tactical", max_items=5, max_tokens=500)],
        )

        assert pack.metadata["withholding"]["by_reason"] == {
            ARCHIVED_REJECTION_REASON: 1
        }
        payload = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[
            0
        ].payload
        assert payload["withholding"]["withheld_item_ids"] == ["gone"]

    def test_a_sectioned_per_section_budget_cut_is_withheld(self) -> None:
        """The sectioned path's own budget is a gate like any other."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword", [_item(f"d{i}", score=1.0 - i / 10) for i in range(5)]
                )
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[SectionRequest(name="tactical", max_items=2, max_tokens=5000)],
        )
        assert pack.total_items == 2
        assert pack.metadata["withholding"]["by_reason"] == {"max_items": 3}

    def test_a_cross_section_duplicate_is_not_withheld(self) -> None:
        """Cross-section dedup drops an item from one section because
        another section serves it. Present is present."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("d0"), _item("d1")])]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[
                SectionRequest(name="a", max_items=5, max_tokens=5000),
                SectionRequest(name="b", max_items=5, max_tokens=5000),
            ],
        )
        assert pack.metadata["withholding"]["total"] == 0


class TestTheNoteIsRenderedInTheHeader:
    """The surface the caller reads, above the item blocks.

    "Honest in JSON alone" is the failure #404 was filed about; a note
    appended after the item loop reproduces it one layer down, because the
    loop ``break``\\ s the moment the budget runs out.
    """

    @staticmethod
    def _withholding() -> WithholdingSummary:
        return summarize_withheld(
            [
                RejectedItem(item_id="w1", item_type="document", reason="archived"),
                RejectedItem(item_id="w2", item_type="document", reason="noise"),
                RejectedItem(item_id="w3", item_type="document", reason="noise"),
            ],
            served_item_ids=[],
        )

    @staticmethod
    def _many_items(n: int = 40) -> list[dict[str, Any]]:
        return [
            {
                "item_id": f"doc-{i}",
                "item_type": "document",
                "excerpt": f"{_BODY} number {i}. " * 20,
                "relevance_score": 1.0 - i / 100,
                "estimated_tokens": 100,
            }
            for i in range(n)
        ]

    def test_the_note_precedes_the_items_in_an_overflowing_pack(self) -> None:
        """The regression this class exists for.

        A small pack cannot see it: everything fits, so a note appended
        after the loop prints anyway. Only a pack that overflows its budget
        — which is *most* production packs, zero of 37 in a 30-day window
        served every candidate they found (#359) — reaches the ``break``.
        """
        rendered = format_pack_as_markdown(
            self._many_items(),
            "deploy checklist",
            max_tokens=400,
            pack_id="pk1",
            withholding=self._withholding(),
        )

        assert "more items omitted" in rendered, (
            "the fixture must actually overflow the renderer's budget, or "
            "this test cannot see the bug it guards"
        )
        assert "**Withheld:** 3 items" in rendered
        assert rendered.index("**Withheld:**") < rendered.index("## [document]")

    def test_the_note_costs_no_item_its_rendering(self) -> None:
        """#305's invariant: an id the builder charged as served is an id
        the agent is shown.

        The builder sizes its walk before any rejection is summarized, so
        charging the note against the item budget silently drops the tail
        item — recorded as served, suppressed by session dedup, graded by
        the learning join, never seen. This is a *formatter*-level pin of
        the same property ``test_every_id_charged_as_served_is_an_id_the_
        agent_is_shown`` pins end to end.
        """
        items = self._many_items()
        without = format_pack_as_markdown(items, "deploy checklist", max_tokens=400)
        with_note = format_pack_as_markdown(
            items, "deploy checklist", max_tokens=400, withholding=self._withholding()
        )
        assert without.count("## [document]") == with_note.count("## [document]")

    def test_the_note_carries_counts_and_reasons_never_ids(self) -> None:
        rendered = format_pack_as_markdown(
            self._many_items(3),
            "deploy checklist",
            max_tokens=4000,
            withholding=self._withholding(),
        )
        assert "noise 2" in rendered
        assert "archived 1" in rendered
        for withheld_id in ("w1", "w2", "w3"):
            assert withheld_id not in rendered

    def test_a_pack_that_withheld_nothing_carries_no_note(self) -> None:
        """A line that always prints is a line the reader learns to skip."""
        rendered = format_pack_as_markdown(
            self._many_items(2),
            "deploy checklist",
            max_tokens=4000,
            withholding=summarize_withheld([], served_item_ids=[]),
        )
        assert "Withheld" not in rendered
        assert format_withholding_note(None) == ""

    def test_the_index_rendering_carries_the_note(self) -> None:
        rendered = format_pack_as_index_markdown(
            self._many_items(3),
            "deploy checklist",
            max_tokens=4000,
            pack_id="pk1",
            withholding=self._withholding(),
        )
        assert "**Withheld:** 3 items" in rendered
        assert rendered.index("**Withheld:**") < rendered.index("- `doc-0`")

    def test_the_sectioned_rendering_carries_the_note(self) -> None:
        rendered = format_sectioned_pack_as_markdown(
            [{"name": "tactical", "items": self._many_items(3)}],
            "deploy checklist",
            max_tokens=4000,
            pack_id="pk1",
            withholding=self._withholding(),
        )
        assert "**Withheld:** 3 items" in rendered
        assert rendered.index("**Withheld:**") < rendered.index("## tactical")

    def test_one_withheld_item_reads_as_singular(self) -> None:
        note = format_withholding_note(
            summarize_withheld(
                [RejectedItem(item_id="w", item_type="document", reason="noise")],
                served_item_ids=[],
            )
        )
        assert "1 item matched this intent but was not served" in note


class TestWithheldContentIsNotCopiedIntoAServedSurface:
    def test_no_rendered_surface_and_no_event_carries_a_withheld_excerpt(
        self, tmp_path: Path
    ) -> None:
        """A report about withheld items must not leak the items.

        Checked against the two places a leak would land: the rendered
        markdown a caller reads, and the ``PACK_ASSEMBLED`` payload. The
        item's *id* is expected in the payload (operator surface) and
        forbidden in the rendering (agent surface).
        """
        log = SQLiteEventLog(tmp_path / "events.db")
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _item("keep"),
                        _noisy("gone", excerpt=_WITHHELD_BODY),
                    ],
                )
            ],
            event_log=log,
        )
        pack = builder.build("deploy checklist")

        rendered = format_pack_as_markdown(
            [
                {
                    "item_id": i.item_id,
                    "item_type": i.item_type,
                    "excerpt": i.excerpt,
                    "relevance_score": i.relevance_score,
                }
                for i in pack.items
            ],
            "deploy checklist",
            max_tokens=4000,
            withholding=withholding_from_payload(_summary(pack)),
        )
        assert "kangaroo" not in rendered
        assert "gone" not in rendered

        event = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)[0]
        assert "kangaroo" not in str(event.payload)
        assert event.payload["withholding"]["withheld_item_ids"] == ["gone"]


class TestTheReporterFailsLoudly:
    """The worst outcome for this change is a withholding reporter that
    itself silently reports nothing."""

    def test_a_serialized_summary_renders_the_identical_note(self) -> None:
        original = summarize_withheld(
            [
                RejectedItem(item_id="w1", item_type="document", reason="archived"),
                RejectedItem(item_id="w2", item_type="document", reason="noise"),
                RejectedItem(item_id="w3", item_type="document", reason="noise"),
            ],
            served_item_ids=[],
        )
        restored = withholding_from_payload(original.as_telemetry())
        assert restored is not None
        assert format_withholding_note(restored) == format_withholding_note(original)
        assert restored == original

    def test_an_absent_payload_is_not_an_error(self) -> None:
        """A pack assembled by an older build carries no summary. That is a
        missing feature, not a malfunction, and must not shout."""
        with capture_logs() as logs:
            assert withholding_from_payload(None) is None
        assert not [e for e in logs if e["event"] == "withholding_payload_unreadable"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"by_reason": "not-a-dict"},
            {"by_reason": {"noise": "not-a-number"}},
        ],
    )
    def test_an_unreadable_payload_warns_at_warning_level(
        self, payload: dict[str, Any]
    ) -> None:
        """The level is pinned, not just the event.

        ``structlog.testing.capture_logs`` swaps the processor chain but
        leaves ``wrapper_class`` alone, so under pytest a ``debug`` call is
        recorded too — which is exactly how a ``debug`` regression slipped
        past its own guard test in #395. Every other assertion here would
        pass just as well against an invisible line.
        """
        with capture_logs() as logs:
            assert withholding_from_payload(payload) is None

        lines = [e for e in logs if e["event"] == "withholding_payload_unreadable"]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "warning"
        # Field names only — an unreadable payload's *values* are still
        # pack content and have no business in a log line.
        assert lines[0]["payload_keys"] == sorted(payload)

    def test_the_warning_survives_the_production_log_level(self) -> None:
        """``trellis_cli.main._root`` pins ``TRELLIS_LOG_LEVEL=WARNING``
        unless ``--verbose``. A line below that is the ``logger.debug``
        no-op this whole issue is about."""
        prior = structlog.get_config()
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
        )
        try:
            with capture_logs() as logs:
                withholding_from_payload({"by_reason": "not-a-dict"})
        finally:
            structlog.configure(**prior)
        assert [e["event"] for e in logs] == ["withholding_payload_unreadable"]


class TestExistingConsumersStillHold:
    def test_the_extra_rejection_rows_do_not_disturb_the_budget_trace(self) -> None:
        """``budget_trace`` records the walk, and the collect gates run long
        before it. #359's counterfactual replay re-walks that trace, so a
        row added to ``rejected_items`` must not appear in it."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("keep"), _noisy("gone")])]
        )
        pack = builder.build(
            "deploy checklist", budget=PackBudget(max_items=10, max_tokens=5000)
        )
        assert [b.item_id for b in pack.retrieval_report.budget_trace] == ["keep"]


class TestPartitionOrdering:
    """``PackBuilder._partition`` must preserve order in both halves.

    The refactor that introduced ``_partition`` replaced three hand-rolled
    loops in :meth:`PackBuilder.build` and three more in
    :meth:`build_sectioned`. A reversed-``dropped`` mutant passed all 937
    retrieval tests, so the property the helper's docstring asserts was
    unpinned — this class is the pin.
    """

    @staticmethod
    def _item(item_id: str, *, structural: bool = False) -> PackItem:
        return PackItem(
            item_id=item_id,
            item_type="entity",
            excerpt=f"excerpt for {item_id}",
            relevance_score=1.0,
            metadata={"node_role": "structural" if structural else "semantic"},
        )

    def test_partition_preserves_order_in_both_halves(self) -> None:
        """Both halves come back in input order, not merely with the right members."""
        items = [
            self._item("a"),
            self._item("b", structural=True),
            self._item("c"),
            self._item("d", structural=True),
            self._item("e", structural=True),
        ]

        kept, dropped = PackBuilder._partition(items, PackBuilder._is_structural)

        assert [i.item_id for i in kept] == ["a", "c"]
        # Order, not just membership: ``sorted()`` here would pass under the
        # reversed-dropped mutant that motivated this test.
        assert [i.item_id for i in dropped] == ["b", "d", "e"]

    def test_withheld_item_ids_follow_rejection_order(self) -> None:
        """The served telemetry record is stable, which is what the order buys.

        ``withheld_item_ids`` is emitted into
        ``PACK_ASSEMBLED.payload["withholding"]`` and should be diffable
        across packs — the same commitment ``groups`` makes by sorting.
        """
        rejected = [
            RejectedItem(item_id="b", item_type="entity", reason="structural_filter"),
            RejectedItem(item_id="d", item_type="entity", reason="structural_filter"),
            RejectedItem(item_id="e", item_type="entity", reason="structural_filter"),
        ]

        summary = summarize_withheld(rejected, served_item_ids=["a", "c"])

        assert summary.withheld_item_ids == ("b", "d", "e")


#: A section that matches everything — no affinities, content types, scopes
#: or entity ids. Used wherever the gate under test is *not* section routing,
#: so the two cannot be confused for one another.
def _wildcard(name: str = "tactical", **kw: Any) -> SectionRequest:
    kw.setdefault("max_items", 10)
    kw.setdefault("max_tokens", 5000)
    return SectionRequest(name=name, **kw)


def _patterns(name: str = "Technical Patterns", **kw: Any) -> SectionRequest:
    """``get_task_context``'s real first section, verbatim in shape."""
    kw.setdefault("max_items", 10)
    kw.setdefault("max_tokens", 5000)
    return SectionRequest(name=name, retrieval_affinities=["technical_pattern"], **kw)


def _pattern_item(item_id: str, **kw: Any) -> PackItem:
    """An item ``TierMapper`` infers into the ``technical_pattern`` tier."""
    return _item(item_id, metadata={"content_tags": {"content_type": "pattern"}}, **kw)


def _structural(item_id: str, **kw: Any) -> PackItem:
    return _item(item_id, metadata={"node_role": "structural"}, **kw)


def _meta_activity(item_id: str, **kw: Any) -> PackItem:
    return _item(
        item_id,
        metadata={"node_type": ACTIVITY, "agent_id": "trellis_meta_analyzer"},
        **kw,
    )


class TestSectionRoutingIsAGate:
    """#440. ``matches_section`` removes candidates and recorded nothing.

    A sectioned pack could route every candidate away, serve zero items and
    report ``total: 0`` — an affirmative *"nothing was withheld"*, which is
    a stronger and more misleading signal than the silence #404 replaced.
    """

    def test_an_all_routed_away_pack_reports_the_count_not_silence(self) -> None:
        """The case #440 was filed on: zero served, and it says why."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("d0"), _item("d1"), _item("d2")])]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_patterns()])

        assert pack.total_items == 0
        summary = _summary(pack)
        assert summary["section_filtered"] == 3
        assert summary["served_count"] == 0

        note = format_withholding_note(withholding_from_payload(summary))
        assert "Section routing" in note
        assert "3 items" in note
        # The whole point: an empty pack must not read as an empty corpus.
        assert "not because nothing was found" in note

    def test_the_rendered_sectioned_surface_carries_the_sentence(self) -> None:
        """Through the renderer the MCP path actually calls, not just
        through :func:`format_withholding_note` — "honest in JSON alone"
        is the failure #404 was filed about."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("d0"), _item("d1")])]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_patterns()])

        rendered = format_sectioned_pack_as_markdown(
            [{"name": s.name, "items": []} for s in pack.sections],
            "deploy checklist",
            max_tokens=2000,
            pack_id=pack.pack_id,
            withholding=withholding_from_payload(_summary(pack)),
        )
        assert "**Section routing:** this pack is empty because 2 items" in rendered
        # In the header, above everything the caller reads past.
        assert rendered.index("Section routing") < rendered.index("Cite feedback")
        assert _WITHHELD_BODY not in rendered

    def test_the_count_is_kept_out_of_the_headline_and_out_of_by_reason(self) -> None:
        """It is a narrower claim than the other ten gates make, so it does
        not join them — measured rationale in the module docstring."""
        builder = PackBuilder(
            strategies=[_strategy("keyword", [_item("d0"), _item("d1")])]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_patterns()])

        summary = _summary(pack)
        assert summary["by_reason"] == {}
        assert summary["total"] == 0
        assert summary["withheld_item_ids"] == []
        assert summary["section_filtered"] == 2

    def test_a_routed_away_item_served_by_another_section_is_not_reported(
        self,
    ) -> None:
        """Checked by execution, not assumed from the set difference.

        Because the routed set is "matched no section at all", an item some
        other section served is never rejected here — the ``{rejected} -
        {served}`` subtraction is a backstop, not the mechanism.
        """
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_pattern_item("p0"), _pattern_item("p1")])
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            # Both items are routed away from the wildcard-free "Reference
            # Data" shape and served by "Technical Patterns".
            sections=[
                SectionRequest(
                    name="Reference Data",
                    retrieval_affinities=["reference"],
                    max_items=10,
                    max_tokens=5000,
                ),
                _patterns(),
            ],
        )

        assert pack.total_items == 2
        assert _summary(pack)["section_filtered"] == 0
        assert _summary(pack)["total"] == 0

    def test_an_item_its_own_section_could_not_afford_is_a_budget_cut(self) -> None:
        """Attribution, not just counting.

        Per-section rejection rows — the obvious implementation — would let
        the section that *didn't* match an item claim it, moving a genuinely
        budget-withheld item out of ``by_reason`` (which renders) and into
        the section count (which renders only on an empty pack).
        """
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _pattern_item("p0", score=0.9),
                        _pattern_item("p1", score=0.8),
                        _pattern_item("p2", score=0.7),
                    ],
                )
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[
                SectionRequest(
                    name="Reference Data",
                    retrieval_affinities=["reference"],
                    max_items=10,
                    max_tokens=5000,
                ),
                _patterns(max_items=1),
            ],
        )

        assert pack.total_items == 1
        summary = _summary(pack)
        assert summary["by_reason"] == {"max_items": 2}
        assert summary["section_filtered"] == 0

    def test_no_row_is_recorded_for_an_item_some_section_matched(self) -> None:
        """The routed set is the union across sections, not the last one.

        Accumulating per-section instead still reports the right *count* —
        every extra row is either served (subtracted) or already attributed
        to a budget gate — but it books a rejection against an item that was
        served, which surfaces as a spurious ``non_absence_reasons`` entry.
        The matching section runs **first** here, which is the arrangement
        that separates the two.
        """
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _pattern_item("p0", score=0.9),
                        _pattern_item("p1", score=0.8),
                    ],
                )
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[
                _patterns(max_items=1),
                SectionRequest(
                    name="Reference Data",
                    retrieval_affinities=["reference"],
                    max_items=10,
                    max_tokens=5000,
                ),
            ],
        )

        summary = _summary(pack)
        assert summary["by_reason"] == {"max_items": 1}
        assert summary["section_filtered"] == 0
        assert summary["non_absence_reasons"] == []

    def test_a_served_pack_counts_the_routing_but_renders_no_note(self) -> None:
        """The judgement #440 asked for, pinned as behaviour.

        Replayed over the reference deployment's 47 flat packs, both shipped
        section presets route at least one served item away on 46 and 47 of
        them — so a ``by_reason`` entry would print on essentially every
        sectioned pack. The operator still gets the count.
        """
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [_pattern_item("p0"), _item("d0"), _item("d1"), _item("d2")],
                )
            ]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_patterns()])

        summary = _summary(pack)
        assert pack.total_items == 1
        assert summary["section_filtered"] == 3
        assert summary["served_count"] == 1
        assert format_withholding_note(withholding_from_payload(summary)) == ""

    def test_both_sentences_render_when_a_gate_also_fired(self) -> None:
        """An empty pack whose candidates were split between gates must
        report both halves, not whichever one is checked first."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [_item("d0"), _item("d1"), _archived("a0"), _archived("a1")],
                )
            ]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_patterns()])

        note = format_withholding_note(withholding_from_payload(_summary(pack)))
        assert "**Withheld:** 2 items" in note
        assert f"({ARCHIVED_REJECTION_REASON} 2)" in note
        assert "**Section routing:** this pack is empty because 2 items" in note

    def test_one_routed_item_reads_as_singular(self) -> None:
        summary = WithholdingSummary(section_filtered=1, served_count=0)
        assert "because 1 item matched" in format_withholding_note(summary)

    def test_the_counts_survive_the_trip_through_telemetry(self) -> None:
        """The renderers read a *serialized* summary, so the rendering rule
        has to round-trip — not just the count it gates."""
        summary = WithholdingSummary(section_filtered=4, served_count=0)
        restored = withholding_from_payload(summary.as_telemetry())
        assert restored is not None
        assert restored.section_filtered == 4
        assert restored.served_count == 0
        assert restored.section_note_applies is True
        assert format_withholding_note(restored) == format_withholding_note(summary)

    def test_a_payload_written_before_the_field_existed_reads_as_zero(self) -> None:
        """Production holds four sectioned packs, all pre-#404. A payload
        without the key must not raise and must not invent a note."""
        restored = withholding_from_payload(
            {"total": 0, "by_reason": {}, "withheld_item_ids": []}
        )
        assert restored is not None
        assert restored.section_filtered == 0
        assert restored.section_note_applies is False
        assert format_withholding_note(restored) == ""

    def test_a_junk_count_reads_as_absent_and_says_so_at_warning(self) -> None:
        """``isinstance(True, int)`` is ``True`` in Python, so an unguarded
        coercion would read a shape change as "one item was routed away" —
        and then render a note claiming it.

        Absence is silent (every pre-#440 payload is in that state); a key
        that is *present and unusable* warns, because a reporter built to
        stop a pack under-reporting must not under-report quietly.
        """
        with capture_logs() as logs:
            restored = withholding_from_payload(
                {"by_reason": {}, "section_filtered": True, "served_count": "12"}
            )
        assert restored is not None
        assert restored.section_filtered == 0
        assert restored.served_count == 0
        assert format_withholding_note(restored) == ""

        warned = [
            log
            for log in logs
            if log["event"] == "withholding_payload_unreadable"
            and log["log_level"] == "warning"
        ]
        assert {log["field"] for log in warned} == {"section_filtered", "served_count"}

    def test_an_absent_count_is_not_a_warning(self) -> None:
        """A payload from before the field existed is not a defect."""
        with capture_logs() as logs:
            restored = withholding_from_payload({"by_reason": {}})
        assert restored is not None
        assert restored.section_filtered == 0
        assert [log for log in logs if log["log_level"] == "warning"] == []

    def test_the_flat_path_never_reports_section_routing(self) -> None:
        """``build`` has no sections; the field must stay 0 rather than
        acquire a meaning it does not have."""
        builder = PackBuilder(strategies=[_strategy("keyword", [_item("d0")])])
        pack = builder.build("deploy checklist")
        assert pack.metadata["withholding"]["section_filtered"] == 0


class TestTheSectionedPathRecordsWhatItRemoved:
    """#447. Every ``_reject`` call in ``build_sectioned`` could be deleted
    with the whole suite green: the *filtering* half of each gate was
    covered, the *telemetry* half was not.

    Each test here fails when its gate's ``_reject`` call is a no-op, and
    each uses at least two kept and two dropped items — a population of one
    makes many different wrong answers agree, which is how this hid.
    """

    def test_a_sectioned_structural_drop_is_recorded(self) -> None:
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "graph",
                    [
                        _item("keep0"),
                        _item("keep1"),
                        _structural("col0"),
                        _structural("col1"),
                    ],
                )
            ]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_wildcard()])

        assert pack.total_items == 2
        assert _summary(pack)["by_reason"] == {"structural_filter": 2}
        assert sorted(_summary(pack)["withheld_item_ids"]) == ["col0", "col1"]

    def test_a_sectioned_meta_activity_drop_is_recorded(self) -> None:
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "graph",
                    [
                        _item("keep0"),
                        _item("keep1"),
                        _meta_activity("meta0"),
                        _meta_activity("meta1"),
                    ],
                )
            ]
        )
        pack = builder.build_sectioned("deploy checklist", sections=[_wildcard()])

        assert pack.total_items == 2
        assert _summary(pack)["by_reason"] == {"meta_activity_filter": 2}

    def test_a_sectioned_session_dedup_drop_is_recorded(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        try:
            items = [_item(f"d{i}", score=1.0 - i / 10) for i in range(4)]
            builder = PackBuilder(
                strategies=[_strategy("keyword", items)], event_log=log
            )
            # First call serves d0/d1 only, so the second call has two
            # suppressed candidates *and* two fresh ones.
            first = builder.build_sectioned(
                "deploy checklist",
                sections=[_wildcard(max_items=2)],
                session_id="sess-A",
            )
            assert first.total_items == 2

            second = builder.build_sectioned(
                "deploy checklist",
                sections=[_wildcard()],
                session_id="sess-A",
            )
            assert second.total_items == 2
            assert _summary(second)["by_reason"] == {"session_dedup": 2}
            assert sorted(_summary(second)["withheld_item_ids"]) == ["d0", "d1"]
        finally:
            log.close()

    def test_a_sectioned_max_items_cut_is_recorded(self) -> None:
        """The sectioned path's own budget is a gate like any other."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword", [_item(f"d{i}", score=1.0 - i / 10) for i in range(5)]
                )
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[_wildcard(max_items=2)],
        )
        assert pack.total_items == 2
        assert _summary(pack)["by_reason"] == {"max_items": 3}
        assert sorted(_summary(pack)["withheld_item_ids"]) == ["d2", "d3", "d4"]

    def test_a_sectioned_token_budget_cut_is_recorded(self) -> None:
        """The second of the two budget cuts, and the one the original
        fixture could not see: it pinned ``max_items`` only."""
        # ~25 tokens each at the 4-chars-per-token default; a 55-token
        # section affords exactly two, leaving two cut by tokens and none
        # by ``max_items``.
        excerpt = (
            "the deploy checklist requires draining the write queue before "
            "the failover promotes a replica xx"
        )
        assert len(excerpt) == 96
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _item(f"d{i}", score=1.0 - i / 10, excerpt=excerpt)
                        for i in range(4)
                    ],
                )
            ]
        )
        pack = builder.build_sectioned(
            "deploy checklist",
            sections=[_wildcard(max_items=10, max_tokens=55)],
        )
        assert pack.total_items == 2
        assert _summary(pack)["by_reason"] == {"token_budget": 2}
        assert sorted(_summary(pack)["withheld_item_ids"]) == ["d2", "d3"]

    def test_a_rejection_row_carries_the_items_own_type_and_score(self) -> None:
        """A pool where every item shares one ``item_type`` cannot tell a
        row that copies the field from one that hard-codes it — the same
        uniformity flaw as a fixture sized 1, one field down."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "graph",
                    [
                        _item("keep"),
                        PackItem(
                            item_id="ent",
                            item_type="entity",
                            excerpt=_BODY,
                            relevance_score=0.81,
                            metadata={"node_role": "structural"},
                        ),
                        PackItem(
                            item_id="doc",
                            item_type="document",
                            excerpt=_BODY,
                            relevance_score=0.42,
                            metadata={"node_role": "structural"},
                        ),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist")

        rows = {
            r.item_id: r
            for r in pack.retrieval_report.rejected_items
            if r.reason == "structural_filter"
        }
        assert rows["ent"].item_type == "entity"
        assert rows["doc"].item_type == "document"
        assert rows["ent"].relevance_score == pytest.approx(0.81)
        assert rows["doc"].relevance_score == pytest.approx(0.42)


class TestEveryRejectionRowCarriesTheRejectedItemsOwnFields:
    """One roster, six gates, two fields each (#456).

    ``RejectedItem.item_type`` and ``RejectedItem.relevance_score`` had
    **six** independent hand-written copies of the same four-line field
    copy, and **eleven of the twelve ``item_type`` / ``relevance_score``
    mutants across them survived the full default selection (6,468
    passing on ``a40b027``)** — not
    merely the 992-test retrieval subset #456 measured, so widening the
    selection caught nothing extra. Only ``dedup``'s
    ``existing.relevance_score`` died. Nothing *branches* on either field,
    which is why every one of the six could have been constant-folded,
    mistyped or copied off the wrong object with no test anywhere
    noticing — the exact fixture-uniformity flaw #455 closed one layer up,
    six times over. They
    are not unread, though: both ride
    ``PACK_ASSEMBLED.payload["rejected_items"]`` and the Memory Explorer
    renders them as the *Type* and *Relevance* columns of its "Rejected
    items" table, so a wrong value reached an operator as fact.

    The source fix is a single constructor
    (:meth:`~trellis.schemas.pack.RejectedItem.from_pack_item`), but that
    only removes today's six copies; it does not stop a seventh gate
    landing next month with its own. These tests are what makes the fields
    observable, so they are written **per gate** and asserted through a
    real ``build`` — the seam where the row reaches
    ``retrieval_report.rejected_items`` and a consumer could read it.

    Every pool below carries at least two distinct ``item_type`` values
    and two distinct ``relevance_score`` values. A pool where every item
    shares one type cannot tell a row that copies the field from one that
    hard-codes it, which is how this hid from two reviewers.
    """

    @staticmethod
    def _rows(pack: Pack, reason: str) -> dict[str, RejectedItem]:
        return {
            r.item_id: r
            for r in pack.retrieval_report.rejected_items
            if r.reason == reason
        }

    def test_a_max_items_cut_carries_the_items_own_type_and_score(self) -> None:
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _typed("keep", "document", 0.91),
                        _typed("ent", "entity", 0.62),
                        _typed("pre", "precedent", 0.33),
                    ],
                )
            ]
        )
        pack = builder.build("deploy checklist", budget=PackBudget(max_items=1))

        rows = self._rows(pack, "max_items")
        assert sorted(rows) == ["ent", "pre"], (
            "the fixture must actually reach the max_items gate, or this "
            "test passes vacuously"
        )
        assert rows["ent"].item_type == "entity"
        assert rows["pre"].item_type == "precedent"
        assert rows["ent"].relevance_score == pytest.approx(0.62)
        assert rows["pre"].relevance_score == pytest.approx(0.33)

    def test_a_token_budget_cut_carries_the_items_own_type_and_score(self) -> None:
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _typed("keep", "document", 0.91),
                        _typed("ent", "entity", 0.62),
                        _typed("pre", "precedent", 0.33),
                    ],
                )
            ]
        )
        pack = builder.build(
            "deploy checklist", budget=PackBudget(max_items=10, max_tokens=20)
        )

        rows = self._rows(pack, "token_budget")
        assert sorted(rows) == ["ent", "pre"]
        assert rows["ent"].item_type == "entity"
        assert rows["pre"].item_type == "precedent"
        assert rows["ent"].relevance_score == pytest.approx(0.62)
        assert rows["pre"].relevance_score == pytest.approx(0.33)

    def test_a_dedup_row_describes_the_loser_when_the_incoming_copy_wins(
        self,
    ) -> None:
        """The asymmetric branch — the row is built off ``existing``.

        ``_deduplicate_tracked`` keeps the higher-scoring copy of an id and
        rejects the other, and *which object the row is built from* differs
        between its two branches. Two copies of one id routinely disagree
        about type and score — the keyword axis serves a document and the
        graph axis an entity summary under the same id — so a row built off
        the winner would describe a candidate that is *in* the pack. Only
        the loser's score dies on ``main``; its type does not.
        """
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_typed("d1", "entity", 0.40)]),
                _strategy("semantic", [_typed("d1", "document", 0.90)]),
            ]
        )
        pack = builder.build("deploy checklist")

        rows = self._rows(pack, "dedup")
        assert list(rows) == ["d1"]
        assert rows["d1"].item_type == "entity"
        assert rows["d1"].relevance_score == pytest.approx(0.40)
        # The served copy carries the other type and the other score, so a
        # row copied off the winner reads differently from this.
        assert [(i.item_type, i.relevance_score) for i in pack.items] == [
            ("document", pytest.approx(0.90))
        ]

    def test_a_dedup_row_describes_the_loser_when_the_incoming_copy_loses(
        self,
    ) -> None:
        """The other branch — the row is built off ``item``. Same pool, the
        two copies swapped between the axes, so a constant or a
        wrong-object mutant that survives one branch cannot survive both."""
        builder = PackBuilder(
            strategies=[
                _strategy("keyword", [_typed("d1", "document", 0.90)]),
                _strategy("semantic", [_typed("d1", "entity", 0.40)]),
            ]
        )
        pack = builder.build("deploy checklist")

        rows = self._rows(pack, "dedup")
        assert list(rows) == ["d1"]
        assert rows["d1"].item_type == "entity"
        assert rows["d1"].relevance_score == pytest.approx(0.40)
        assert [(i.item_type, i.relevance_score) for i in pack.items] == [
            ("document", pytest.approx(0.90))
        ]

    def test_a_semantic_dedup_row_carries_the_items_own_type_and_score(self) -> None:
        base = (
            "Deploying to production requires running the full migration "
            "suite, validating the schema against the staging database, and "
            "confirming that all downstream consumers updated their clients. "
        )
        near = base.replace(". ", ".  ").replace(",", " ,")
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _typed("winner", "document", 0.95, excerpt=base),
                        _typed("loser", "entity", 0.44, excerpt=near),
                    ],
                )
            ],
            semantic_dedup=SemanticDedupConfig(),
        )
        pack = builder.build("deploy checklist")

        rows = self._rows(pack, "semantic_dedup")
        assert list(rows) == ["loser"]
        assert rows["loser"].item_type == "entity"
        assert rows["loser"].relevance_score == pytest.approx(0.44)
        # The winner is a *document* at 0.95 — a row copying the surviving
        # neighbour's fields, or a constant, reads differently from this.
        assert [i.item_id for i in pack.items] == ["winner"]

    def test_a_content_floor_drop_carries_the_items_own_type_and_score(self) -> None:
        """``exclude`` mode is opt-in, so this is the one gate whose fixture
        has to configure it — the default demotes rather than drops."""
        builder = PackBuilder(
            strategies=[
                _strategy(
                    "keyword",
                    [
                        _typed("keep", "document", 0.91),
                        _typed("thin", "entity", 0.62, excerpt="Postgres"),
                        _typed("bare", "precedent", 0.33, excerpt=""),
                    ],
                )
            ],
            content_floor=ContentFloorConfig(mode="exclude"),
        )
        pack = builder.build("deploy checklist")

        rows = self._rows(pack, CONTENT_FLOOR_REJECTION_REASON)
        assert sorted(rows) == ["bare", "thin"]
        assert rows["thin"].item_type == "entity"
        assert rows["bare"].item_type == "precedent"
        assert rows["thin"].relevance_score == pytest.approx(0.62)
        assert rows["bare"].relevance_score == pytest.approx(0.33)
