"""Tests for native per-strategy scores and the relevance cutoff gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from trellis.retrieve.excerpts import ContentFloorConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.relevance_cutoff import (
    MIN_KEEP,
    RELEVANCE_CUTOFF_REJECTION_REASON,
    RelevanceCutoffConfig,
    apply_relevance_cutoff,
    resolve_relevance_cutoff_config,
)
from trellis.retrieve.rerankers.mmr import MMRReranker
from trellis.retrieve.rerankers.rrf import RRFReranker
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.withholding import summarize_withheld
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _strategy(name: str, items: list[PackItem]) -> SearchStrategy:
    strategy = MagicMock(spec=SearchStrategy)
    strategy.name = name
    strategy.search.return_value = items
    return strategy


def _item(item_id: str, score: float, excerpt: str = "some text") -> PackItem:
    return PackItem(
        item_id=item_id, item_type="document", excerpt=excerpt, relevance_score=score
    )


def _builder(
    strategies: list[SearchStrategy],
    *,
    config: RelevanceCutoffConfig | None = None,
    reranker: object | None = None,
    event_log: SQLiteEventLog | None = None,
) -> PackBuilder:
    return PackBuilder(
        strategies=strategies,
        content_floor=ContentFloorConfig(mode="off"),
        relevance_cutoff_config=config or RelevanceCutoffConfig(),
        reranker=reranker,  # type: ignore[arg-type]
        event_log=event_log,
    )


def _strategies() -> list[SearchStrategy]:
    return [
        _strategy(
            "semantic",
            [_item("a", 0.9), _item("b", 0.4), _item("c", 0.35), _item("d", 0.3)],
        ),
        _strategy("keyword", [_item("d", 7.5), _item("e", 6.0)]),
    ]


def _pack_assembled(log: SQLiteEventLog) -> dict:
    (event,) = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
    return event.payload


def _strip_native(breakdown: dict[str, float]) -> dict[str, float]:
    return {k: v for k, v in breakdown.items() if not k.startswith("native_")}


class TestPart1Identity:
    """Native scores are telemetry: the served pack is unchanged by them."""

    @pytest.mark.parametrize("reranker_cls", [None, RRFReranker, MMRReranker])
    def test_native_scores_change_nothing_served(
        self, reranker_cls: type | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trellis.retrieve.pack_builder as pb

        def build(*, stamp: bool) -> list[tuple[str, float, str, dict]]:
            if not stamp:
                monkeypatch.setattr(
                    pb, "stamp_native_scores", lambda items, _name: items
                )
            reranker = reranker_cls() if reranker_cls else None
            pack = _builder(_strategies(), reranker=reranker).build(
                "q", clearance="restricted"
            )
            monkeypatch.undo()
            return [
                (
                    i.item_id,
                    i.relevance_score,
                    i.excerpt,
                    _strip_native(i.score_breakdown),
                )
                for i in pack.items
            ]

        assert build(stamp=True) == build(stamp=False)


class TestNativeScores:
    def test_single_strategy_item_carries_its_axis(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        _builder(_strategies(), event_log=log).build("q", clearance="restricted")
        rows = {r["item_id"]: r for r in _pack_assembled(log)["injected_items"]}
        assert rows["a"]["score_breakdown"]["native_semantic"] == pytest.approx(0.9)
        assert "native_keyword" not in rows["a"]["score_breakdown"]
        # The no-reranker default key survives beside the native key.
        assert rows["a"]["score_breakdown"]["relevance_score"] == pytest.approx(0.9)

    @pytest.mark.parametrize("reranker_cls", [None, RRFReranker, MMRReranker])
    def test_multi_strategy_item_carries_both_axes(
        self, tmp_path: Path, reranker_cls: type | None
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        reranker = reranker_cls() if reranker_cls else None
        _builder(_strategies(), reranker=reranker, event_log=log).build(
            "q", clearance="restricted"
        )
        rows = {r["item_id"]: r for r in _pack_assembled(log)["injected_items"]}
        breakdown = rows["d"]["score_breakdown"]
        assert breakdown["native_semantic"] == pytest.approx(0.3)
        assert breakdown["native_keyword"] == pytest.approx(7.5)

    def test_rejected_rows_carry_native_scores(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        _builder(_strategies(), event_log=log).build("q", clearance="restricted")
        dedup = [
            r
            for r in _pack_assembled(log)["rejected_items"]
            if r["reason"] == "dedup" and r["item_id"] == "d"
        ]
        assert dedup
        assert dedup[0]["native_scores"] == {"native_semantic": pytest.approx(0.3)}


class TestGateModes:
    def test_off_is_absent_and_returns_the_same_list(self, tmp_path: Path) -> None:
        items = [_item("a", 0.1)]
        result = apply_relevance_cutoff(
            items, config=RelevanceCutoffConfig(), price=lambda _i: 1
        )
        assert result.kept is items
        assert result.telemetry is None
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(_strategies(), event_log=log).build("q", clearance="restricted")
        assert "relevance_cutoff" not in _pack_assembled(log)
        assert "relevance_cutoff" not in pack.metadata

    def test_unconfigured_reports_unconfigured(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        pack = _builder(
            _strategies(), config=RelevanceCutoffConfig(mode="on"), event_log=log
        ).build("q", clearance="restricted")
        assert _pack_assembled(log)["relevance_cutoff"]["status"] == "unconfigured"
        assert len(pack.items) == 5

    def test_shadow_cuts_nothing_and_records(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        config = RelevanceCutoffConfig(mode="shadow", semantic_min=0.5)
        pack = _builder(_strategies(), config=config, event_log=log).build(
            "q", clearance="restricted"
        )
        assert len(pack.items) == 5
        block = _pack_assembled(log)["relevance_cutoff"]
        assert block["mode"] == "shadow"
        # b and c fail; d also has a keyword hit, so it is kept.
        assert block["cut_count"] == 2
        assert block["cut_tokens"] > 0
        assert not any(
            r.reason == RELEVANCE_CUTOFF_REJECTION_REASON
            for r in pack.retrieval_report.rejected_items
        )

    def test_on_cuts_records_and_keeps_multi_axis(self, tmp_path: Path) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        config = RelevanceCutoffConfig(mode="on", semantic_min=0.5)
        pack = _builder(_strategies(), config=config, event_log=log).build(
            "q", clearance="restricted"
        )
        served = [i.item_id for i in pack.items]
        assert set(served) == {"a", "d", "e"}
        cut = [
            r
            for r in pack.retrieval_report.rejected_items
            if r.reason == RELEVANCE_CUTOFF_REJECTION_REASON
        ]
        assert {r.item_id for r in cut} == {"b", "c"}
        assert all(r.estimated_tokens for r in cut)
        # Cut items stay retrievable for a later "nearby" section.
        assert {
            row["item_id"] for row in pack.metadata["relevance_cutoff"]["cut_items"]
        } == {"b", "c"}
        summary = summarize_withheld(pack.retrieval_report.rejected_items, served)
        by_reason = {g.reason: g.count for g in summary.groups}
        assert by_reason[RELEVANCE_CUTOFF_REJECTION_REASON] == 2
        payload = _pack_assembled(log)
        assert payload["relevance_cutoff"]["cut_count"] == 2
        assert any(
            r["reason"] == RELEVANCE_CUTOFF_REJECTION_REASON
            for r in payload["rejected_items"]
        )

    def test_min_keep_restores_the_best(self) -> None:
        items = [_item(f"s{n}", 0.1 + n / 100) for n in range(5)]
        for i, it in enumerate(items):
            items[i] = it.model_copy(
                update={"score_breakdown": {"native_semantic": it.relevance_score}}
            )
        result = apply_relevance_cutoff(
            items,
            config=RelevanceCutoffConfig(mode="on", semantic_min=0.9),
            price=lambda _i: 10,
        )
        assert len(result.kept) == MIN_KEEP
        assert {i.item_id for i in result.kept} == {"s4", "s3", "s2"}
        assert result.telemetry is not None
        assert result.telemetry["kept_by_min_keep"] == MIN_KEEP

    def test_ratio_floor(self) -> None:
        items = [
            _item(f"s{n}", 0.0).model_copy(
                update={"score_breakdown": {"native_semantic": s}}
            )
            for n, s in enumerate([0.8, 0.7, 0.6, 0.3])
        ]
        result = apply_relevance_cutoff(
            items,
            config=RelevanceCutoffConfig(mode="on", semantic_ratio=0.5),
            price=lambda _i: 1,
        )
        assert [r.item_id for r in result.rejected] == ["s3"]

    def test_sectioned_path_cuts(self) -> None:
        config = RelevanceCutoffConfig(mode="on", semantic_min=0.5)
        pack = _builder(_strategies(), config=config).build_sectioned(
            intent="q", sections=[SectionRequest(name="all")], clearance="restricted"
        )
        served = {i.item_id for s in pack.sections for i in s.items}
        assert "b" not in served
        assert "c" not in served
        assert "a" in served


class TestResolve:
    def test_unrecognised_mode_warns_and_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_RELEVANCE_CUTOFF_MODE", "bogus")
        with capture_logs() as logs:
            config = resolve_relevance_cutoff_config()
        assert config.mode == "off"
        assert any(entry["log_level"] == "warning" for entry in logs)

    def test_env_thresholds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_RELEVANCE_CUTOFF_MODE", "shadow")
        monkeypatch.setenv("TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_MIN", "0.4")
        monkeypatch.setenv("TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_RATIO", "0.6")
        config = resolve_relevance_cutoff_config()
        assert (config.mode, config.semantic_min, config.semantic_ratio) == (
            "shadow",
            0.4,
            0.6,
        )

    def test_settings_mode_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_RELEVANCE_CUTOFF_MODE", raising=False)
        config = resolve_relevance_cutoff_config(
            settings={"relevance_cutoff_mode": "on"}
        )
        assert config.mode == "on"
        assert not config.configured
