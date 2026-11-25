"""The post-rerank authority boost (``trellis.retrieve.authority_rank``).

Off is the shipped default and must be invisible: the same list object back,
no key on any item or event. On, it must reorder *after* fusion, which is
the property a collect-seam multiplier cannot have, and it must never lower
a score.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.core.pack_holdout import HOLDOUT_ITEMS_KEY, HOLDOUT_KEY
from trellis.retrieve.authority_rank import (
    AUTHORITY_BOOST_KEY,
    AUTHORITY_RANK_MODE_ENV,
    CURATED_NODE_SIGNAL,
    DEFAULT_WEIGHTS,
    HIGH_SIGNAL_SIGNAL,
    KNOWN_SIGNALS,
    OFF,
    PRECEDENT_DOCUMENT_FORM,
    PRECEDENT_FORM_SIGNAL,
    REFERENCE_FORM_SIGNAL,
    AuthorityRankConfig,
    apply_authority_rank,
    authority_rank_telemetry,
    item_signals,
    resolve_authority_rank_config,
)
from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.rerankers.rrf import RRFReranker
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.sqlite.event_log import SQLiteEventLog

ON = AuthorityRankConfig(mode="on")
PRECEDENT_ID = "precedent://learning/abc"


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(
    item_id: str,
    score: float,
    *,
    axis: str | None = None,
    signal_quality: str | None = None,
    form: str | None = None,
    node_role: str | None = None,
) -> PackItem:
    metadata: dict[str, object] = {}
    if signal_quality:
        metadata["content_tags"] = {"signal_quality": signal_quality}
    if form:
        metadata["document_form"] = form
    if node_role:
        metadata["node_role"] = node_role
    return PackItem(
        item_id=item_id,
        item_type="document",
        excerpt=f"distinct body text for {item_id} " * 3,
        relevance_score=score,
        strategy_source=axis,
        metadata=metadata,
    )


def _two_axis_pool() -> list[SearchStrategy]:
    """Two axes; ``s2`` is second on its axis and the only curated node.

    Under RRF ``k1``/``s1`` score 1/61 and ``k2``/``s2`` 1/62, whatever
    their native scores, so only a post-fusion boost can lift ``s2`` past
    the two axis leaders.
    """
    return [
        _strategy(
            "keyword",
            [_item("k1", 0.9, axis="keyword"), _item("k2", 0.8, axis="keyword")],
        ),
        _strategy(
            "semantic",
            [
                _item("s1", 0.99, axis="semantic"),
                _item("s2", 0.10, axis="semantic", node_role="curated"),
            ],
        ),
    ]


def _builder(config: AuthorityRankConfig | None, **kwargs: object) -> PackBuilder:
    return PackBuilder(
        strategies=_two_axis_pool(),
        reranker=RRFReranker(),
        content_floor=ContentFloorConfig(mode="off"),
        authority_rank_config=config,
        **kwargs,  # type: ignore[arg-type]
    )


class TestSignals:
    def test_precedent_form_needs_the_reserved_id(self) -> None:
        """The form is caller-writable metadata; only promotion mints the id."""
        forged = _item("mem-1", 1.0, form=PRECEDENT_DOCUMENT_FORM)
        real = _item(PRECEDENT_ID, 1.0, form=PRECEDENT_DOCUMENT_FORM)
        assert item_signals(forged) == frozenset()
        assert item_signals(real) == {PRECEDENT_FORM_SIGNAL}

    def test_each_other_signal(self) -> None:
        assert item_signals(_item("r", 1.0, form="reference")) == {
            REFERENCE_FORM_SIGNAL
        }
        assert item_signals(_item("c", 1.0, node_role="curated")) == {
            CURATED_NODE_SIGNAL
        }
        assert item_signals(_item("h", 1.0, signal_quality="high")) == {
            HIGH_SIGNAL_SIGNAL
        }
        assert item_signals(_item("n", 1.0, signal_quality="standard")) == set()

    def test_precedent_constant_matches_the_learning_one(self) -> None:
        from trellis.learning.scoring import (
            PRECEDENT_DOCUMENT_FORM as LEARNING_FORM,
        )

        assert PRECEDENT_DOCUMENT_FORM == LEARNING_FORM

    def test_multiplier_is_the_largest_weight_not_a_sum(self) -> None:
        config = AuthorityRankConfig(
            mode="on",
            weights={
                PRECEDENT_FORM_SIGNAL: 0.0,
                REFERENCE_FORM_SIGNAL: 0.2,
                CURATED_NODE_SIGNAL: 0.0,
                HIGH_SIGNAL_SIGNAL: 0.1,
            },
        )
        item = _item("r", 2.0, form="reference", signal_quality="high")
        [out] = apply_authority_rank([item], config)
        assert out.score_breakdown[AUTHORITY_BOOST_KEY] == pytest.approx(1.2)
        assert out.relevance_score == pytest.approx(2.4)

    def test_default_weights_only_precedent_and_curated(self) -> None:
        """reference_form and high_signal are recognised but weigh nothing."""
        assert dict(DEFAULT_WEIGHTS) == {
            PRECEDENT_FORM_SIGNAL: 0.15,
            CURATED_NODE_SIGNAL: 0.15,
        }
        assert {REFERENCE_FORM_SIGNAL, HIGH_SIGNAL_SIGNAL} <= KNOWN_SIGNALS
        # A config built in code may still weight them.
        AuthorityRankConfig(mode="on", weights={HIGH_SIGNAL_SIGNAL: 0.1})

    def test_zero_weight_signals_leave_the_item_untouched(self) -> None:
        items = [
            _item("r", 1.0, form="reference"),
            _item("h", 1.0, signal_quality="high"),
            _item("rh", 1.0, form="reference", signal_quality="high"),
            _item("c", 1.0, node_role="curated"),
        ]
        out = apply_authority_rank(items, ON)
        assert out[0] is items[0]
        assert out[1] is items[1]
        assert out[2] is items[2]
        assert out[3].score_breakdown[AUTHORITY_BOOST_KEY] == pytest.approx(1.15)
        telemetry = authority_rank_telemetry(ON, out)
        assert telemetry is not None
        assert telemetry["boosted_count"] == 1

    def test_negative_weight_is_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            AuthorityRankConfig(mode="on", weights={HIGH_SIGNAL_SIGNAL: -0.1})


class TestOffIsInvisible:
    def test_off_returns_the_same_list_object(self) -> None:
        items = [_item("h", 1.0, signal_quality="high")]
        assert apply_authority_rank(items, OFF) is items
        assert AUTHORITY_BOOST_KEY not in items[0].score_breakdown

    def test_default_construction_is_off_and_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config passed and no env: the event equals an explicit-off build.

        Pack ids, timestamps and durations differ per build, so those keys
        are normalised; every other byte of the payload must match.
        """
        monkeypatch.delenv(AUTHORITY_RANK_MODE_ENV, raising=False)

        def payload(config: AuthorityRankConfig | None, name: str) -> str:
            log = SQLiteEventLog(tmp_path / f"{name}.db")
            try:
                pack = _builder(config, event_log=log).build(
                    "q", clearance="restricted"
                )
                [event] = log.get_events(limit=5)
                body = dict(event.payload)
                for key in ("duration_ms", "pack_id"):
                    body.pop(key, None)
                return json.dumps(
                    {"payload": body, "items": [i.model_dump() for i in pack.items]},
                    sort_keys=True,
                    default=str,
                ).replace(pack.pack_id, "<pack>")
            finally:
                log.close()

        default = payload(None, "default")
        assert "authority" not in default
        assert default == payload(OFF, "off")
        # And the off order is plain RRF: s2 is not lifted.
        assert json.loads(default)["payload"]["injected_item_ids"][0] != "s2"


class TestOnAfterFusion:
    def test_boost_reorders_across_axes_after_rrf(self) -> None:
        off = _builder(OFF).build("q", clearance="restricted")
        on = _builder(ON).build("q", clearance="restricted")
        assert [i.item_id for i in off.items][-1] in {"k2", "s2"}
        assert on.items[0].item_id == "s2"
        top = on.items[0]
        assert top.score_breakdown[AUTHORITY_BOOST_KEY] == pytest.approx(1.15)
        assert top.score_breakdown["rrf_total"] == pytest.approx(1 / 62)
        # Applied exactly once: the served score is the fused score x 1.15.
        assert top.relevance_score == pytest.approx(
            top.score_breakdown["rrf_total"] * 1.15, rel=1e-9
        )

    def test_a_collect_seam_multiplier_would_not_have(self) -> None:
        """The control: scaling s2's native score is erased by fusion."""
        strategies = _two_axis_pool()
        strategies[1].search.return_value[1] = _item(  # type: ignore[attr-defined]
            "s2", 0.10 * 1.15, axis="semantic", node_role="curated"
        )
        pack = PackBuilder(
            strategies=strategies,
            reranker=RRFReranker(),
            content_floor=ContentFloorConfig(mode="off"),
            authority_rank_config=OFF,
        ).build("q", clearance="restricted")
        assert pack.items[0].item_id != "s2"

    def test_boost_only_no_score_ever_decreases(self) -> None:
        items = [
            _item("a", 0.5, node_role="curated"),
            _item("b", 0.4),
            _item("c", 0.3, form="reference", node_role="curated"),
            _item("d", 0.0, signal_quality="low"),
            # Weighted signals on a score not above 0: multiplying would lower
            # a negative score, so neither is touched.
            _item("neg", -0.5, node_role="curated"),
            _item("zero", 0.0, node_role="curated"),
        ]
        out = apply_authority_rank(items, ON)
        for before, after in zip(items, out, strict=True):
            assert after.relevance_score >= before.relevance_score
            boost = after.score_breakdown.get(AUTHORITY_BOOST_KEY, 1.0)
            assert boost >= 1.0
        assert out[1] is items[1]
        assert out[3] is items[3]
        assert out[4] is items[4]
        assert out[5] is items[5]
        telemetry = authority_rank_telemetry(ON, out)
        assert telemetry is not None
        assert telemetry["boosted_count"] == 2

    def test_on_event_carries_the_block_and_per_item_boost(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            _builder(ON, event_log=log).build("q", clearance="restricted")
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert event.payload["authority_rank"]["boosted_count"] == 1
        rows = {r["item_id"]: r for r in event.payload["injected_items"]}
        assert rows["s2"]["score_breakdown"][AUTHORITY_BOOST_KEY] == pytest.approx(1.15)
        assert AUTHORITY_BOOST_KEY not in rows["k1"]["score_breakdown"]

    def test_sectioned_path_is_boosted(self, tmp_path: Path) -> None:
        sections = [SectionRequest(name="all", max_items=10, max_tokens=4000)]
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            on = _builder(ON, event_log=log).build_sectioned(
                "q", sections=sections, clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        off = _builder(OFF).build_sectioned(
            "q", sections=sections, clearance="restricted"
        )
        assert on.sections[0].items[0].item_id == "s2"
        assert off.sections[0].items[0].item_id != "s2"
        top = on.sections[0].items[0]
        assert top.relevance_score == pytest.approx(
            top.score_breakdown["rrf_total"] * 1.15, rel=1e-9
        )
        assert event.payload["authority_rank"]["mode"] == "on"

    def test_sectioned_off_event_has_no_block(self, tmp_path: Path) -> None:
        sections = [SectionRequest(name="all", max_items=10, max_tokens=4000)]
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            _builder(OFF, event_log=log).build_sectioned(
                "q", sections=sections, clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert "authority" not in json.dumps(event.payload)

    def test_public_path_seam_is_boosted(self) -> None:
        pool = [
            *_two_axis_pool()[0].search.return_value,  # type: ignore[attr-defined]
            *_two_axis_pool()[1].search.return_value,  # type: ignore[attr-defined]
        ]
        on, _ = _builder(ON)._rerank_and_project_public("q", list(pool), [])
        off, _ = _builder(OFF)._rerank_and_project_public("q", list(pool), [])
        by_id_on = {i.item_id: i for i in on}
        by_id_off = {i.item_id: i for i in off}
        assert by_id_on["s2"].relevance_score > by_id_on["s1"].relevance_score
        assert by_id_off["s2"].relevance_score < by_id_off["s1"].relevance_score
        # Applied exactly once on this path too.
        assert by_id_on["s2"].relevance_score == pytest.approx(
            by_id_off["s2"].relevance_score * 1.15, rel=1e-9
        )
        # The public projection strips the multiplier with ``original_score``.
        for item in [*on, *off]:
            assert AUTHORITY_BOOST_KEY not in item.score_breakdown
            assert "original_score" not in item.score_breakdown
        # ...and the boosted item's ``rrf_*`` keys, or ``relevance_score /
        # rrf_total`` would name the multiplier. Unboosted items keep them.
        s2 = by_id_on["s2"]
        assert not any(k.startswith("rrf_") for k in s2.score_breakdown)
        for a, b in itertools.product(
            [s2.relevance_score, *s2.score_breakdown.values()], repeat=2
        ):
            if b:
                assert a / b != pytest.approx(1.15, rel=1e-6)
        assert "rrf_total" in by_id_on["s1"].score_breakdown
        assert all("rrf_total" in i.score_breakdown for i in off)

    def test_project_for_public_strips_the_boost_key(self) -> None:
        from trellis.retrieve.public_pack import project_for_public

        [boosted] = apply_authority_rank([_item("c", 0.5, node_role="curated")], ON)
        assert AUTHORITY_BOOST_KEY in boosted.score_breakdown
        public = project_for_public(boosted)
        assert AUTHORITY_BOOST_KEY not in public.score_breakdown
        assert public.relevance_score == boosted.relevance_score


class TestHeldOutWithFlagOn:
    """A held-out pack's telemetry is replaced wholesale, block included.

    The would-be pack it records is the boosted one, which is why turning the
    flag on during a holdout changes what the holdout records.
    """

    def test_flat_held_out_event_has_no_block(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            pack = _builder(ON, event_log=log, holdout_rate=1.0).build(
                "q", clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert pack.items == []
        assert event.payload[HOLDOUT_KEY] is True
        assert "authority_rank" not in event.payload
        would_be = event.payload[HOLDOUT_ITEMS_KEY]
        assert would_be[0]["item_id"] == "s2"

    def test_sectioned_held_out_event_has_no_block(self, tmp_path: Path) -> None:
        sections = [SectionRequest(name="all", max_items=10, max_tokens=4000)]
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            _builder(ON, event_log=log, holdout_rate=1.0).build_sectioned(
                "q", sections=sections, clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert event.payload[HOLDOUT_KEY] is True
        assert "authority_rank" not in event.payload


class TestResolve:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(AUTHORITY_RANK_MODE_ENV, raising=False)
        assert resolve_authority_rank_config().mode == "off"

    def test_settings_then_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(AUTHORITY_RANK_MODE_ENV, raising=False)
        settings = {"authority_rank_mode": "on"}
        assert resolve_authority_rank_config(settings=settings).mode == "on"
        monkeypatch.setenv(AUTHORITY_RANK_MODE_ENV, "off")
        assert resolve_authority_rank_config(settings=settings).mode == "off"

    def test_unrecognised_degrades_to_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AUTHORITY_RANK_MODE_ENV, "boost")
        assert resolve_authority_rank_config().mode == "off"
        monkeypatch.delenv(AUTHORITY_RANK_MODE_ENV)
        assert (
            resolve_authority_rank_config(settings={"authority_rank_mode": "x"}) is OFF
        )


class TestReplayArm:
    def test_replay_predicts_what_an_on_build_serves(self, tmp_path: Path) -> None:
        """The replay's re-order of a real off pack matches a real on build."""
        from tests.unit.retrieve.replay_seeds import (
            authority_rank_strategies,
            seed_authority_rank,
        )
        from trellis.retrieve.disclosure import DisclosureConfig
        from trellis.retrieve.pack_replay import replay_authority_rank

        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            seed_authority_rank(log)
            report = replay_authority_rank(log)
        finally:
            log.close()

        on = PackBuilder(
            strategies=authority_rank_strategies(),
            reranker=RRFReranker(),
            content_floor=ContentFloorConfig(mode="off"),
            disclosure=DisclosureConfig(mode="graduated", body_items=1),
            authority_rank_config=ON,
        ).build("q", clearance="restricted")
        assert on.items[0].item_id == "s2"

        assert report.attributed_packs == 1
        assert report.replayed_packs == 1, report.notes
        assert report.unmodelled_packs == 0
        assert report.packs_order_changed == 1
        assert report.packs_without_boost == 0
        assert report.boosted_servings == 1
        assert report.boosted_by_signal == {CURATED_NODE_SIGNAL: 1}
        # s2 rises from pointer to the one body slot; the old body falls.
        assert report.pointer_to_body == 1
        assert report.body_to_pointer == 1
        assert report.helpful_up == 1
        assert report.helpful_down == 0
        assert report.newly_served == 0
        assert report.served_now_dropped == 0
        text = " ".join(report.notes)
        assert "status-quo bias" in text
        assert "never shown" in text
        assert "0 of 1 replayed pack(s) had no boosted served row" in text

    def test_unbudgeted_packs_are_not_replayed(self, tmp_path: Path) -> None:
        """A hand-written seed with no budget_trace is unmodelled, not guessed."""
        from tests.unit.retrieve.replay_seeds import seed_class_prior_edges
        from trellis.retrieve.pack_replay import replay_authority_rank

        docs = MagicMock()
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            seed_class_prior_edges(log, docs)
            report = replay_authority_rank(log)
        finally:
            log.close()
        assert report.attributed_packs >= 1
        assert report.replayed_packs == 0
        assert report.unmodelled_reasons == {"no_budget_trace": report.attributed_packs}
