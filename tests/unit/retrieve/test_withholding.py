"""A pack must state what it did not serve (trellis-ai#404).

Ten gates remove candidates before a pack is returned. Two of them —
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
* the reporter itself failing loudly rather than silently.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from trellis.mutate.retention import ARCHIVED_STATE
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
)
from trellis.retrieve.lifecycle import ARCHIVED_REJECTION_REASON
from trellis.retrieve.noise import NOISE_REJECTION_REASON
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import (
    WithheldGroup,
    WithholdingSummary,
    format_withholding_note,
    summarize_withheld,
    withholding_from_payload,
)
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.schemas.pack import PackBudget, PackItem, RejectedItem, SectionRequest
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
