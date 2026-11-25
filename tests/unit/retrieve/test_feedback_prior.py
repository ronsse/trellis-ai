"""The default-off feedback prior and its replay arm.

The prior multiplies an item's post-fusion score by
``1 + strength * (pos - unhelpful) / (pos + unhelpful + k)``, from per-item
verdicts and fetches counted per distinct pack. These tests pin: off is
absent and byte-identical, shadow moves nothing, on reorders, no evidence
leaves an item untouched, the factor's bounds, demotion needing unhelpful
evidence, held-out packs contributing nothing, a failed read serving
unadjusted, and the replay never reading a pack's own or later grades.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tests.unit.retrieve.test_builder_factory as builder_factory_tests
from tests.unit.retrieve.replay_seeds import (
    authority_rank_strategies,
    seed_feedback_prior,
)
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.feedback_prior import (
    EVIDENCE_KEYS,
    FEEDBACK_PRIOR_KEY,
    FEEDBACK_PRIOR_MODE_ENV,
    FEEDBACK_PRIOR_SHADOW_KEY,
    OFF,
    STATE_APPLIED,
    STATE_ERROR,
    STATE_TRUNCATED,
    STATE_UNAVAILABLE,
    EventLogFeedbackEvidence,
    EvidenceLedger,
    EvidenceResult,
    FeedbackEvidenceProvider,
    FeedbackPriorConfig,
    ItemEvidence,
    _Signal,
    apply_feedback_prior,
    factor_for,
    feedback_prior_step,
    resolve_feedback_prior_config,
    scan_ledger,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.pack_replay import replay_feedback_prior
from trellis.retrieve.public_pack import project_for_public
from trellis.retrieve.rerankers.rrf import RRFReranker
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

ON = FeedbackPriorConfig(mode="on")
SHADOW = FeedbackPriorConfig(mode="shadow")
SECTIONS = [SectionRequest(name="all", max_items=10, max_tokens=4000)]


class _Fixed(FeedbackEvidenceProvider):
    def __init__(self, table: dict[str, ItemEvidence], state: str = STATE_APPLIED):
        self.result = EvidenceResult(state=state, table=table)
        self.calls = 0

    def evidence(self) -> EvidenceResult:
        self.calls += 1
        return self.result


class _Raises(FeedbackEvidenceProvider):
    def evidence(self) -> EvidenceResult:
        msg = "boom"
        raise RuntimeError(msg)


#: ``s2`` helpful three times: factor 1.3 lifts it past both RRF leaders.
LIFT_S2 = {"s2": ItemEvidence(helpful=3, unhelpful=0, fetched=0)}


def _builder(
    config: FeedbackPriorConfig | None,
    provider: FeedbackEvidenceProvider | None = None,
    **kwargs: object,
) -> PackBuilder:
    return PackBuilder(
        strategies=authority_rank_strategies(),
        reranker=RRFReranker(),
        content_floor=ContentFloorConfig(mode="off"),
        feedback_prior_config=config,
        feedback_evidence=provider,
        **kwargs,  # type: ignore[arg-type]
    )


def _item(item_id: str, score: float) -> PackItem:
    return PackItem(
        item_id=item_id, item_type="document", excerpt="x", relevance_score=score
    )


class TestFactor:
    def test_no_evidence_is_exactly_one(self) -> None:
        assert factor_for(None, ON) == 1.0
        assert factor_for(ItemEvidence(helpful=0, unhelpful=0, fetched=0), ON) == 1.0

    def test_bounds_over_a_grid(self) -> None:
        for h, u, f in itertools.product(range(6), range(6), range(6)):
            factor = factor_for(ItemEvidence(helpful=h, unhelpful=u, fetched=f), ON)
            assert 1.0 - ON.strength < factor < 1.0 + ON.strength or factor == 1.0

    def test_demotion_needs_unhelpful_evidence(self) -> None:
        """A factor below 1 never arises from helpful or fetch evidence alone."""
        for h, u, f in itertools.product(range(6), range(6), range(6)):
            factor = factor_for(ItemEvidence(helpful=h, unhelpful=u, fetched=f), ON)
            if factor < 1.0:
                assert u > 0
        assert factor_for(ItemEvidence(helpful=0, unhelpful=0, fetched=4), ON) > 1.0

    def test_formula(self) -> None:
        evidence = ItemEvidence(helpful=2, unhelpful=1, fetched=1)
        # pos = 2 + 1*1 = 3; 1 + 0.5 * (3-1) / (3+1+2)
        assert factor_for(evidence, ON) == pytest.approx(1 + 0.5 * 2 / 6)

    def test_bad_config_is_refused(self) -> None:
        for kwargs in (
            {"mode": "demote"},
            {"strength": 1.5},
            {"k": 0.0},
            {"fetch_weight": -1.0},
            {"window_days": 0},
        ):
            with pytest.raises(ValueError):
                FeedbackPriorConfig(**kwargs)  # type: ignore[arg-type]


class TestLedger:
    T0 = datetime(2026, 1, 1, tzinfo=UTC)

    def _ledger(self) -> EvidenceLedger:
        t = self.T0
        return EvidenceLedger(
            signals=[
                # One pack graded and fetched it: counted once, as helpful.
                _Signal(t, "p1", "a", "helpful"),
                _Signal(t, "p1", "a", "fetched"),
                # Helpful wins over unhelpful on one pack.
                _Signal(t, "p2", "a", "unhelpful"),
                _Signal(t, "p2", "a", "helpful"),
                _Signal(t + timedelta(hours=1), "p3", "a", "unhelpful"),
                _Signal(t + timedelta(hours=2), "p4", "a", "fetched"),
            ]
        )

    def test_once_per_pack_and_helpful_wins(self) -> None:
        assert self._ledger().table()["a"] == ItemEvidence(
            helpful=2, unhelpful=1, fetched=1
        )

    def test_before_is_strict_and_own_pack_is_excluded(self) -> None:
        ledger = self._ledger()
        at_p3 = self.T0 + timedelta(hours=1)
        assert ledger.table(before=at_p3)["a"] == ItemEvidence(
            helpful=2, unhelpful=0, fetched=0
        )
        assert ledger.table(exclude_pack="p2")["a"] == ItemEvidence(
            helpful=1, unhelpful=1, fetched=1
        )
        assert ledger.table(before=self.T0) == {}

    def test_held_out_pack_contributes_nothing(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            held = _builder(OFF, event_log=log, holdout_rate=1.0).build(
                "q", clearance="restricted"
            )
            served = _builder(OFF, event_log=log).build("q", clearance="restricted")
            for pack in (held, served):
                log.emit(
                    EventType.FEEDBACK_RECORDED,
                    source="mcp",
                    entity_id=pack.pack_id,
                    payload={
                        "pack_id": pack.pack_id,
                        "helpful_item_ids": ["s2"],
                        "rating": 1.0,
                        "success": True,
                    },
                )
                log.emit(
                    EventType.PACK_ITEMS_FETCHED,
                    source="mcp",
                    entity_id=pack.pack_id,
                    payload={"pack_id": pack.pack_id, "served_item_ids": ["k1"]},
                )
            ledger, truncated = scan_ledger(
                log, since=datetime.now(tz=UTC) - timedelta(days=1), limit=100
            )
        finally:
            log.close()
        assert not truncated
        assert {s.pack_id for s in ledger.signals} == {served.pack_id}
        assert ledger.table() == {
            "s2": ItemEvidence(helpful=1, unhelpful=0, fetched=0),
            "k1": ItemEvidence(helpful=0, unhelpful=0, fetched=1),
        }


class TestApply:
    def test_off_returns_the_same_list_object(self) -> None:
        items = [_item("s2", 1.0)]
        assert apply_feedback_prior(items, OFF, LIFT_S2) is items

    def test_no_evidence_and_non_positive_scores_are_untouched(self) -> None:
        bad = {i: ItemEvidence(helpful=0, unhelpful=3, fetched=0) for i in "zn"}
        items = [_item("a", 1.0), _item("z", 0.0), _item("n", -1.0)]
        out = apply_feedback_prior(items, ON, bad)
        for before, after in zip(items, out, strict=True):
            assert after is before
            assert not EVIDENCE_KEYS & set(after.score_breakdown)

    def test_on_records_factor_and_counts(self) -> None:
        [out] = apply_feedback_prior([_item("s2", 1.0)], ON, LIFT_S2)
        assert out.score_breakdown[FEEDBACK_PRIOR_KEY] == pytest.approx(1.3)
        assert out.score_breakdown["feedback_prior_helpful"] == 3.0
        assert out.relevance_score == pytest.approx(1.3)

    def test_public_projection_strips_every_prior_key(self) -> None:
        [out] = apply_feedback_prior([_item("s2", 1.0)], ON, LIFT_S2)
        public = project_for_public(out)
        assert not EVIDENCE_KEYS & set(public.score_breakdown)


class TestServing:
    def test_default_construction_is_off_and_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FEEDBACK_PRIOR_MODE_ENV, raising=False)
        provider = _Fixed(LIFT_S2)

        def payload(config: FeedbackPriorConfig | None, name: str) -> str:
            log = SQLiteEventLog(tmp_path / f"{name}.db")
            try:
                pack = _builder(config, provider, event_log=log).build(
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
        assert "feedback_prior" not in default
        assert default == payload(OFF, "off")
        # Off never reads evidence.
        assert provider.calls == 0

    def test_shadow_changes_no_score_or_order(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            shadow = _builder(SHADOW, _Fixed(LIFT_S2), event_log=log).build(
                "q", clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        off = _builder(OFF).build("q", clearance="restricted")
        assert [(i.item_id, i.relevance_score) for i in shadow.items] == [
            (i.item_id, i.relevance_score) for i in off.items
        ]
        s2 = next(i for i in shadow.items if i.item_id == "s2")
        assert s2.score_breakdown[FEEDBACK_PRIOR_SHADOW_KEY] == pytest.approx(1.3)
        assert FEEDBACK_PRIOR_KEY not in s2.score_breakdown
        assert event.payload["feedback_prior"]["mode"] == "shadow"
        assert event.payload["feedback_prior"]["promoted_count"] == 1

    def test_on_reorders_after_fusion(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            on = _builder(ON, _Fixed(LIFT_S2), event_log=log).build(
                "q", clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        off = _builder(OFF).build("q", clearance="restricted")
        assert off.items[0].item_id != "s2"
        top = on.items[0]
        assert top.item_id == "s2"
        assert top.relevance_score == pytest.approx(
            top.score_breakdown["rrf_total"] * 1.3, rel=1e-9
        )
        block = event.payload["feedback_prior"]
        assert block["state"] == STATE_APPLIED
        assert block["adjusted_count"] == 1
        rows = {r["item_id"]: r for r in event.payload["injected_items"]}
        assert rows["s2"]["score_breakdown"]["feedback_prior_helpful"] == 3.0

    def test_sectioned_and_public_paths_are_adjusted(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            on = _builder(ON, _Fixed(LIFT_S2), event_log=log).build_sectioned(
                "q", sections=SECTIONS, clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert on.sections[0].items[0].item_id == "s2"
        assert event.payload["feedback_prior"]["mode"] == "on"
        pool = [
            *authority_rank_strategies()[0].search.return_value,  # type: ignore[attr-defined]
            *authority_rank_strategies()[1].search.return_value,  # type: ignore[attr-defined]
        ]
        public = _builder(ON, _Fixed(LIFT_S2))._rerank_and_project_public(
            "q", list(pool), []
        )
        by_id = {i.item_id: i for i in public}
        assert by_id["s2"].relevance_score > by_id["s1"].relevance_score

    def test_sectioned_off_event_has_no_block(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            _builder(OFF, _Fixed(LIFT_S2), event_log=log).build_sectioned(
                "q", sections=SECTIONS, clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        assert "feedback_prior" not in json.dumps(event.payload)

    @pytest.mark.parametrize(
        ("provider", "state"),
        [
            (_Raises(), STATE_ERROR),
            (_Fixed({}, state=STATE_UNAVAILABLE), STATE_UNAVAILABLE),
            (_Fixed({}, state=STATE_TRUNCATED), STATE_TRUNCATED),
            (None, STATE_UNAVAILABLE),
        ],
    )
    def test_a_failed_read_serves_unadjusted_and_says_so(
        self, tmp_path: Path, provider: FeedbackEvidenceProvider | None, state: str
    ) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            pack = _builder(ON, provider, event_log=log).build(
                "q", clearance="restricted"
            )
            [event] = log.get_events(limit=5)
        finally:
            log.close()
        off = _builder(OFF).build("q", clearance="restricted")
        assert [i.item_id for i in pack.items] == [i.item_id for i in off.items]
        assert event.payload["feedback_prior"]["state"] == state
        assert event.payload["feedback_prior"]["adjusted_count"] == 0

    def test_a_failing_event_log_reads_as_unavailable(self) -> None:
        log = MagicMock()
        log.get_events.side_effect = RuntimeError("down")
        log.scan.side_effect = RuntimeError("down")
        provider = EventLogFeedbackEvidence(log)
        items = [_item("s2", 1.0)]
        out, block = feedback_prior_step(items, ON, provider)
        assert out is items
        assert block is not None
        assert block["state"] == STATE_UNAVAILABLE

    def test_a_capped_scan_reads_as_truncated_and_is_cached(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            seed_feedback_prior(log)
            now = [0.0]
            provider = EventLogFeedbackEvidence(log, scan_limit=1, clock=lambda: now[0])
            assert provider.evidence().state == STATE_TRUNCATED
            full = EventLogFeedbackEvidence(log, clock=lambda: now[0])
            first = full.evidence()
            assert first.state == STATE_APPLIED
            assert first.table["s2"].helpful == 2
            assert full.evidence() is first
            now[0] = 601.0
            assert full.evidence() is not first
        finally:
            log.close()


class TestResolve:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEEDBACK_PRIOR_MODE_ENV, raising=False)
        assert resolve_feedback_prior_config().mode == "off"

    def test_settings_then_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEEDBACK_PRIOR_MODE_ENV, raising=False)
        settings = {"feedback_prior_mode": "shadow"}
        assert resolve_feedback_prior_config(settings).mode == "shadow"
        monkeypatch.setenv(FEEDBACK_PRIOR_MODE_ENV, "on")
        assert resolve_feedback_prior_config(settings).mode == "on"

    def test_unrecognised_degrades_to_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FEEDBACK_PRIOR_MODE_ENV, "demote")
        assert resolve_feedback_prior_config().mode == "off"
        monkeypatch.delenv(FEEDBACK_PRIOR_MODE_ENV)
        assert resolve_feedback_prior_config({"feedback_prior_mode": "x"}) == OFF

    def test_factory_wires_a_provider_only_when_not_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = builder_factory_tests.TestBuildPackBuilder._registry(tmp_path)
        monkeypatch.delenv(FEEDBACK_PRIOR_MODE_ENV, raising=False)
        off = build_pack_builder(registry, surface="test")
        assert off._feedback_prior_config.mode == "off"
        assert off._feedback_evidence is None
        monkeypatch.setenv(FEEDBACK_PRIOR_MODE_ENV, "shadow")
        shadow = build_pack_builder(registry, surface="test")
        assert shadow._feedback_prior_config.mode == "shadow"
        assert isinstance(shadow._feedback_evidence, EventLogFeedbackEvidence)
        assert shadow._feedback_evidence.event_log is registry.operational.event_log


class TestReplay:
    def test_no_leakage_only_the_later_pack_moves(self, tmp_path: Path) -> None:
        """Both packs carry identical grades. With strictly-earlier, never-own
        evidence only the second pack has any, so only it re-orders; reading
        its own grades, or the later pack's, would move the first too."""
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            seed_feedback_prior(log)
            report = replay_feedback_prior(log)
        finally:
            log.close()
        assert report.attributed_packs == 2
        assert report.replayed_packs == 2, report.unmodelled_reasons
        assert report.packs_with_evidence == 1
        assert report.packs_order_changed == 1
        assert report.servings == 8
        assert report.servings_with_evidence == 2
        assert report.servings_moved_up == 1
        assert report.helpful_gained_body == 1
        assert report.newly_served == 0
        assert report.token_delta == report.tokens_after - report.tokens_before

    def test_packs_built_with_the_prior_on_are_not_replayed(
        self, tmp_path: Path
    ) -> None:
        log = SQLiteEventLog(tmp_path / "e.db")
        try:
            pack = _builder(ON, _Fixed(LIFT_S2), event_log=log).build(
                "q", clearance="restricted"
            )
            log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp",
                entity_id=pack.pack_id,
                payload={
                    "pack_id": pack.pack_id,
                    "helpful_item_ids": ["s2"],
                    "rating": 1.0,
                    "success": True,
                },
            )
            report = replay_feedback_prior(log)
        finally:
            log.close()
        assert report.replayed_packs == 0
        assert report.unmodelled_reasons == {"already_adjusted": 1}
