"""``trellis analyze replay --relevance-cutoff``. Counts are computed by hand."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    for var in (
        "TRELLIS_RELEVANCE_CUTOFF_MODE",
        "TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_MIN",
        "TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_RATIO",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _emit_pack(
    registry: StoreRegistry,
    pack_id: str,
    rows: list[tuple[str, dict[str, float], int]],
    *,
    helpful: list[str],
    unhelpful: list[str],
    rejected: list[dict[str, object]] | None = None,
) -> None:
    log = registry.operational.event_log
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "pack_id": pack_id,
            "injected_items": [
                {
                    "item_id": item_id,
                    "item_type": "document",
                    "rank": rank,
                    "estimated_tokens": tokens,
                    "strategy_source": "semantic",
                    "score_breakdown": {"relevance_score": 0.5, **natives},
                }
                for rank, (item_id, natives, tokens) in enumerate(rows, 1)
            ],
            "rejected_items": rejected or [],
        },
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mcp",
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "helpful_item_ids": helpful,
            "unhelpful_item_ids": unhelpful,
            "rating": 0.5,
            "success": True,
        },
    )


@pytest.fixture
def seeded(_temp_stores: StoreRegistry) -> StoreRegistry:
    """pack_new carries native scores; pack_old predates them (2 servings)."""
    _emit_pack(
        _temp_stores,
        "pack_new",
        [
            ("s1", {"native_semantic": 0.9}, 100),
            ("s2", {"native_semantic": 0.8}, 100),
            ("s3", {"native_semantic": 0.7}, 100),
            ("s4", {"native_semantic": 0.2}, 30),  # cut, unhelpful
            ("s5", {"native_semantic": 0.1}, 20),  # cut, unjudged
            ("s6", {"native_semantic": 0.15, "native_keyword": 4.0}, 50),  # kept
            ("s7", {"native_semantic": 0.1}, 10),  # cut, helpful
        ],
        helpful=["s1", "s7"],
        unhelpful=["s4"],
    )
    _emit_pack(
        _temp_stores,
        "pack_old",
        [("o1", {}, 40), ("o2", {}, 40)],
        helpful=["o1"],
        unhelpful=[],
    )
    return _temp_stores


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["analyze", "replay", *args])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        return result.exit_code, f"{result.stdout}\n{result.exception!r}"
    return result.exit_code, result.stdout


def test_counts_by_verdict_and_old_events_are_unpriced(seeded: StoreRegistry) -> None:
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-min", "0.5", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    assert data["status"] == "applied"
    assert data["attributed_packs"] == 2
    assert data["priced_packs"] == 1
    assert data["priced_servings"] == 7
    assert data["unpriced_servings"] == 2
    assert data["cut_servings"] == 3
    assert (data["helpful_removed"], data["helpful_tokens_removed"]) == (1, 10)
    assert (data["unhelpful_removed"], data["unhelpful_tokens_removed"]) == (1, 30)
    assert (data["unjudged_removed"], data["unjudged_tokens_removed"]) == (1, 20)
    assert data["tokens_removed"] == 60
    assert data["kept_by_min_keep"] == 0


def test_env_thresholds_are_read(
    seeded: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_MIN", "0.5")
    code, out = _invoke("--relevance-cutoff", "--format", "json")
    assert code == 0, out
    assert json.loads(out.strip())["cut_servings"] == 3


def test_unconfigured_reports_status_and_cuts_nothing(seeded: StoreRegistry) -> None:
    code, out = _invoke("--relevance-cutoff", "--format", "json")
    assert code == 0, out
    data = json.loads(out.strip())
    assert data["status"] == "unconfigured"
    assert data["cut_servings"] == 0


def test_text_output(seeded: StoreRegistry) -> None:
    code, out = _invoke("--relevance-cutoff", "--semantic-min", "0.5")
    assert code == 0, out
    text = plain(out)
    assert "Relevance-Cutoff Replay" in text
    assert "unpriced servings:  2" in text


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_refusals_exit_the_same_under_both_formats(
    seeded: StoreRegistry, fmt: str
) -> None:
    combined, _ = _invoke("--relevance-cutoff", "--class-prior", "--format", fmt)
    orphan, _ = _invoke("--semantic-min", "0.5", "--format", fmt)
    bad_ratio, _ = _invoke(
        "--relevance-cutoff", "--semantic-ratio", "1.5", "--format", fmt
    )
    nan_min, _ = _invoke("--relevance-cutoff", "--semantic-min", "nan", "--format", fmt)
    nan_ratio, _ = _invoke(
        "--relevance-cutoff", "--semantic-ratio", "nan", "--format", fmt
    )
    assert (combined, orphan, bad_ratio, nan_min, nan_ratio) == (2, 2, 2, 2, 2)


@pytest.fixture
def stores(_temp_stores: StoreRegistry) -> StoreRegistry:
    """The bare registry, for tests that emit their own packs."""
    return _temp_stores


def _ratio_pack(registry: StoreRegistry, rejected: list[dict[str, object]]) -> None:
    _emit_pack(
        registry,
        "pack_ratio",
        [
            ("r1", {"native_semantic": 0.5}, 10),
            ("r2", {"native_semantic": 0.45}, 10),
            ("r3", {"native_semantic": 0.4}, 10),
            ("r4", {"native_semantic": 0.35}, 10),
        ],
        helpful=["r1"],
        unhelpful=[],
        rejected=rejected,
    )


def _rejected(item_id: str, reason: str, score: float) -> dict[str, object]:
    return {
        "item_id": item_id,
        "item_type": "document",
        "relevance_score": score,
        "reason": reason,
        "native_scores": {"native_semantic": score},
    }


def test_pre_cutoff_rejection_does_not_raise_the_floor(stores: StoreRegistry) -> None:
    """A noise-rejected row never reached the live gate: its score is not best."""
    _ratio_pack(stores, [_rejected("n1", "noise", 0.99)])
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-ratio", "0.8", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    # Best is 0.5, floor 0.4: only r4 fails. With n1 admitted the floor
    # would be 0.792 and every served row would fail.
    assert data["cut_servings"] == 1
    assert data["excluded_unknown_reason"] == 0


def test_post_cutoff_rejection_joins_the_pool(stores: StoreRegistry) -> None:
    _ratio_pack(stores, [_rejected("t1", "token_budget", 0.99)])
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-ratio", "0.8", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    # Floor 0.792: t1 passes and all four served rows fail, so min_keep
    # restores the best two failing ids (r1, r2) and r3, r4 are cut.
    assert data["cut_servings"] == 2
    assert data["kept_by_min_keep"] == 2


def test_unknown_reason_is_excluded_and_counted(stores: StoreRegistry) -> None:
    _ratio_pack(stores, [_rejected("u1", "some_future_gate", 0.99)])
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-ratio", "0.8", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    assert data["cut_servings"] == 1
    assert data["excluded_unknown_reason"] == 1


def test_kept_by_min_keep_counts_servings_only(stores: StoreRegistry) -> None:
    """A restored rejected row is not a serving, so it is not counted."""
    _emit_pack(
        stores,
        "pack_mk",
        [("m1", {"native_semantic": 0.1}, 10), ("m2", {"native_semantic": 0.09}, 10)],
        helpful=["m1"],
        unhelpful=[],
        rejected=[_rejected("x1", "token_budget", 0.2)],
    )
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-min", "0.5", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    # Three candidates, all failing; min_keep restores all three, but only
    # m1 and m2 are servings.
    assert data["kept_by_min_keep"] == 2
    assert data["cut_servings"] == 0


def _append_pack(
    registry: StoreRegistry,
    pack_id: str,
    at: datetime,
    rows: list[tuple[str, float]],
    *,
    helpful: list[str],
) -> None:
    log = registry.operational.event_log
    log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            occurred_at=at,
            payload={
                "pack_id": pack_id,
                "injected_items": [
                    {
                        "item_id": item_id,
                        "item_type": "document",
                        "rank": rank,
                        "estimated_tokens": 10,
                        "strategy_source": "semantic",
                        "score_breakdown": {
                            "relevance_score": 0.5,
                            "native_semantic": score,
                        },
                    }
                    for rank, (item_id, score) in enumerate(rows, 1)
                ],
                "rejected_items": [],
            },
        )
    )
    log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            occurred_at=at + timedelta(minutes=1),
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": helpful,
                "unhelpful_item_ids": [],
                "rating": 0.9,
                "success": True,
            },
        )
    )


def test_evidence_exempt_uses_only_earlier_evidence(seeded: StoreRegistry) -> None:
    now = datetime.now(tz=UTC)
    # Before pack_new: s5 graded helpful in two other packs, so s5 is exempt.
    for days, pack_id in ((1, "pack_before"), (2, "pack_before_2")):
        _append_pack(
            seeded, pack_id, now - timedelta(days=days), [("s5", 0.9)], helpful=["s5"]
        )
    # After pack_new: s4 graded helpful, which pack_new must not see.
    _append_pack(
        seeded, "pack_after", now + timedelta(hours=1), [("s4", 0.9)], helpful=["s4"]
    )
    code, out = _invoke(
        "--relevance-cutoff",
        "--semantic-min",
        "0.5",
        "--evidence-exempt",
        "--format",
        "json",
    )
    assert code == 0, out
    data = json.loads(out.strip())
    # The cut is reported without the exemption, as before.
    assert data["cut_servings"] == 3
    assert data["evidence_exempt"] is True
    assert data["evidence_state"] == "applied"
    # s5 only: s7's helpful grade is pack_new's own, s4's is later (and from
    # one pack, which would not be enough anyway).
    assert data["exempt_restored_servings"] == 1
    assert data["exempt_restored_unjudged"] == 1
    assert data["exempt_restored_tokens"] == 20
    assert (data["exempt_restored_helpful"], data["exempt_restored_unhelpful"]) == (
        0,
        0,
    )
    assert data["exempt_newly_cut_servings"] == 0
    assert any("as of its own assembly" in note for note in data["notes"])


def test_evidence_exempt_off_reports_not_requested(seeded: StoreRegistry) -> None:
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-min", "0.5", "--format", "json"
    )
    assert code == 0, out
    data = json.loads(out.strip())
    assert data["evidence_exempt"] is False
    assert data["evidence_state"] == "not_requested"
    assert data["exempt_restored_servings"] == 0


def test_evidence_exempt_text_output(seeded: StoreRegistry) -> None:
    code, out = _invoke(
        "--relevance-cutoff", "--semantic-min", "0.5", "--evidence-exempt"
    )
    assert code == 0, out
    assert "exempt restored:" in plain(out)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_evidence_exempt_needs_relevance_cutoff(
    seeded: StoreRegistry, fmt: str
) -> None:
    code, _ = _invoke("--evidence-exempt", "--format", fmt)
    assert code == 2


def _grade_at(
    registry: StoreRegistry, pack_id: str, at: datetime, helpful: list[str]
) -> None:
    registry.operational.event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="mcp",
            entity_id=pack_id,
            occurred_at=at,
            payload={
                "pack_id": pack_id,
                "helpful_item_ids": helpful,
                "unhelpful_item_ids": [],
                "rating": 0.9,
                "success": True,
            },
        )
    )


@pytest.mark.parametrize(("assembled_days_ago", "restored"), [(100, 0), (80, 1)])
def test_evidence_needs_its_pack_inside_the_window(
    seeded: StoreRegistry, assembled_days_ago: int, restored: int
) -> None:
    """A grade at T-10d on a pack assembled at T-100d is outside the live window.

    s5 has one in-window helpful pack; the second positive pack is graded
    10 days ago but was assembled ``assembled_days_ago``. Live reads only
    ``PACK_ASSEMBLED`` rows inside 90 days, so at 100 days the grade does
    not count and s5 has one pack (not exempt); at 80 days it has two.
    """
    now = datetime.now(tz=UTC)
    _append_pack(
        seeded, "pack_before", now - timedelta(days=1), [("s5", 0.9)], helpful=[]
    )
    _grade_at(seeded, "pack_before", now - timedelta(days=1), ["s5"])
    _append_pack(
        seeded,
        "pack_ancient",
        now - timedelta(days=assembled_days_ago),
        [("s5", 0.9)],
        helpful=[],
    )
    _grade_at(seeded, "pack_ancient", now - timedelta(days=10), ["s5"])
    code, out = _invoke(
        "--relevance-cutoff",
        "--semantic-min",
        "0.5",
        "--evidence-exempt",
        "--format",
        "json",
    )
    assert code == 0, out
    data = json.loads(out.strip())
    assert data["evidence_state"] == "applied"
    assert data["exempt_restored_servings"] == restored


def test_evidence_read_uses_the_live_scan_limit(
    seeded: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence read is not capped by the replay's own --limit.

    With the replay's read at a tiny limit the evidence read still runs at
    the live provider's ``DEFAULT_SCAN_LIMIT``, and the replay's own read
    (and its numbers) is exactly what it is with the flag off.
    """
    from trellis.retrieve import relevance_cutoff_replay as mod
    from trellis.retrieve.feedback_prior import DEFAULT_SCAN_LIMIT
    from trellis.retrieve.relevance_cutoff import (
        RelevanceCutoffConfig,
    )

    joins: list[tuple[datetime, int]] = []
    evidence_limits: list[int] = []
    real_join = mod.join_pack_events_with_coverage
    real_scan = mod.scan_ledger

    def _join(event_log, *, since, limit):  # type: ignore[no-untyped-def]
        joins.append((since, limit))
        return real_join(event_log, since=since, limit=limit)

    def _scan(event_log, *, since, limit):  # type: ignore[no-untyped-def]
        evidence_limits.append(limit)
        return real_scan(event_log, since=since, limit=limit)

    monkeypatch.setattr(mod, "join_pack_events_with_coverage", _join)
    monkeypatch.setattr(mod, "scan_ledger", _scan)
    log = seeded.operational.event_log
    config = RelevanceCutoffConfig(mode="on", semantic_min=0.5)
    off = mod.replay_relevance_cutoff(log, config=config, limit=5000)
    on = mod.replay_relevance_cutoff(
        log, config=config, limit=5000, evidence_exempt=True
    )
    assert evidence_limits == [DEFAULT_SCAN_LIMIT]
    assert [limit for _, limit in joins] == [5000, 5000]
    assert abs((joins[0][0] - joins[1][0]).total_seconds()) < 60
    base_fields = set(mod.RelevanceCutoffReplayReport.model_fields) - {
        "applied_config",
        "notes",
        "evidence_exempt",
        "evidence_state",
        "evidence_window_days",
    }
    assert {k: getattr(off, k) for k in base_fields} == {
        k: getattr(on, k) for k in base_fields
    }
    assert off.cut_servings == 3
    assert on.evidence_state == "applied"
