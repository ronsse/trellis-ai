"""Price :mod:`trellis.retrieve.relevance_cutoff` over packs already served.

Run it with ``trellis analyze replay --relevance-cutoff``. Kept out of
:mod:`trellis.retrieve.pack_replay` so the arm and its report live beside the
gate they replay.

The population is the servings an attributed flat pack actually served,
counted once per ``(pack_id, item_id)``, as the class-prior arm counts them.
Each pack's candidate pool is rebuilt from its ``PACK_ASSEMBLED`` row: the
served ``injected_items[]`` plus every ``rejected_items[]`` row that carries
``native_scores``. :func:`~trellis.retrieve.relevance_cutoff.apply_relevance_cutoff`
then runs over that pool unchanged, so the floor, the every-axis rule and
``min_keep`` are the gate's own. Only a served id's cut is counted.

What the arm cannot see:

* A pack served before native scores were recorded has no ``native_*`` key on
  any row. Its servings are counted as ``unpriced_servings``, never as kept.
* The pool is what the event recorded, not every candidate the strategies
  returned, so ``best_semantic`` (and a ratio floor) can be lower than the
  live gate would compute.
* No refill is modelled. Like the class-prior arm, this does not re-run the
  token-budget walk, so ``tokens_removed`` is the gross cost of the cut
  servings and the arm cannot say what the freed budget would have admitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.feedback.attribution import is_sectioned_payload
from trellis.feedback.pack_rows import has_served_rows, served_rows_from_payload
from trellis.learning.pack_observations import join_pack_feedback
from trellis.retrieve.pack_value import collect_pack_verdicts
from trellis.retrieve.relevance_cutoff import (
    NATIVE_SCORE_PREFIX,
    RelevanceCutoffConfig,
    apply_relevance_cutoff,
)
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_LIMIT = 5000


class RelevanceCutoffReplayReport(TrellisModel):
    """What the relevance cutoff would have removed from packs already served."""

    window_days: int
    #: ``mode=... semantic_min=... semantic_ratio=...`` as replayed. The arm
    #: always prices the cut ``on`` would make; ``shadow`` prices the same.
    applied_config: str = ""
    #: ``unconfigured`` when no threshold is set (every count is zero),
    #: otherwise ``applied``.
    status: str = "applied"
    attributed_packs: int = 0
    #: Attributed packs with at least one native score on a row.
    priced_packs: int = 0
    priced_servings: int = 0
    #: Servings in packs that carry no native score at all (served before
    #: native scores were recorded). Never counted as kept or cut.
    unpriced_servings: int = 0
    cut_servings: int = 0
    helpful_removed: int = 0
    unhelpful_removed: int = 0
    unjudged_removed: int = 0
    tokens_removed: int = 0
    helpful_tokens_removed: int = 0
    unhelpful_tokens_removed: int = 0
    unjudged_tokens_removed: int = 0
    #: Servings the floor failed that ``min_keep`` restored.
    kept_by_min_keep: int = 0
    notes: list[str] = Field(default_factory=list)


def _native(breakdown: Any) -> dict[str, float]:
    if not isinstance(breakdown, Mapping):
        return {}
    return {
        k: float(v)
        for k, v in breakdown.items()
        if isinstance(k, str)
        and k.startswith(NATIVE_SCORE_PREFIX)
        and isinstance(v, int | float)
    }


def _pool_item(item_id: str, natives: dict[str, float], tokens: int) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="replay",
        excerpt="",
        relevance_score=0.0,
        score_breakdown=natives,
        estimated_tokens=tokens,
    )


def _tokens(row: Mapping[str, Any]) -> int:
    raw = row.get("estimated_tokens")
    return int(raw) if isinstance(raw, int | float) else 0


def _notes(report: RelevanceCutoffReplayReport) -> list[str]:
    if report.status == "unconfigured":
        return [
            (
                "Neither TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_MIN nor "
                "TRELLIS_RELEVANCE_CUTOFF_SEMANTIC_RATIO (nor --semantic-min / "
                "--semantic-ratio) is set, so the gate has no floor and every "
                "count is zero by construction."
            )
        ]
    notes = [
        (
            "Counterfactual over the SAME packs and citations. No refill is "
            "modelled: tokens_removed is the gross cost of the cut servings, "
            "not a repriced pack, and nothing the freed budget would admit is "
            "graded."
        ),
        (
            "Each pack's pool is its served rows plus the rejected rows that "
            "carry native_scores, so a ratio floor can sit lower than the live "
            "gate's."
        ),
    ]
    if report.unpriced_servings:
        notes.append(
            f"{report.unpriced_servings} serving(s) come from packs with no "
            "native score on any row (served before native scores were "
            "recorded) and are counted as unpriced, not as kept."
        )
    return notes


def replay_relevance_cutoff(
    event_log: EventLog,
    *,
    config: RelevanceCutoffConfig,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> RelevanceCutoffReplayReport:
    """Price ``config``'s cut over the attributed flat packs in the window.

    The mode in ``config`` is ignored beyond the label: the arm prices the
    cut ``on`` would make, which is also what ``shadow`` records.
    """
    applied = (
        f"mode={config.mode} semantic_min={config.semantic_min} "
        f"semantic_ratio={config.semantic_ratio}"
    )
    report = RelevanceCutoffReplayReport(window_days=days, applied_config=applied)
    since = datetime.now(tz=UTC) - timedelta(days=days)
    feedback_events, pack_payloads, _ = join_pack_feedback(
        event_log, since=since, limit=limit
    )
    flat_packs = {
        pack_id: payload
        for pack_id, payload in pack_payloads.items()
        if has_served_rows(payload) and not is_sectioned_payload(payload)
    }
    verdicts = collect_pack_verdicts(feedback_events, flat_packs)
    helpful_by_pack = verdicts["helpful"]
    unhelpful_by_pack = verdicts["unhelpful"]
    attributed = sorted(set(helpful_by_pack) | set(unhelpful_by_pack))
    report.attributed_packs = len(attributed)
    if not config.configured:
        report.status = "unconfigured"
        report.notes = _notes(report)
        return report
    gate = RelevanceCutoffConfig(
        mode="on",
        semantic_min=config.semantic_min,
        semantic_ratio=config.semantic_ratio,
    )
    for pack_id in attributed:
        payload = flat_packs[pack_id]
        served = [
            s.row
            for s in served_rows_from_payload(
                pack_id, payload, include_sectioned=False, include_holdout=True
            )
            if s.item_id is not None
        ]
        served_pool = [
            _pool_item(r["item_id"], _native(r.get("score_breakdown")), _tokens(r))
            for r in served
        ]
        rejected_pool = [
            _pool_item(str(r["item_id"]), _native(r.get("native_scores")), 0)
            for r in (payload.get("rejected_items") or [])
            if isinstance(r, Mapping) and _native(r.get("native_scores"))
        ]
        if not any(item.score_breakdown for item in served_pool + rejected_pool):
            report.unpriced_servings += len(served_pool)
            continue
        report.priced_packs += 1
        report.priced_servings += len(served_pool)
        result = apply_relevance_cutoff(
            served_pool + rejected_pool,
            config=gate,
            price=lambda item: item.estimated_tokens or 0,
        )
        served_ids = {item.item_id for item in served_pool}
        telemetry = result.telemetry or {}
        report.kept_by_min_keep += int(telemetry.get("kept_by_min_keep", 0))
        cut_ids = {r.item_id for r in result.rejected} & served_ids
        helpful = helpful_by_pack.get(pack_id, set())
        unhelpful = unhelpful_by_pack.get(pack_id, set())
        for item in served_pool:
            if item.item_id not in cut_ids:
                continue
            tokens = item.estimated_tokens or 0
            report.cut_servings += 1
            report.tokens_removed += tokens
            if item.item_id in helpful:
                report.helpful_removed += 1
                report.helpful_tokens_removed += tokens
            elif item.item_id in unhelpful:
                report.unhelpful_removed += 1
                report.unhelpful_tokens_removed += tokens
            else:
                report.unjudged_removed += 1
                report.unjudged_tokens_removed += tokens
    report.notes = _notes(report)
    logger.debug(
        "pack_value_relevance_cutoff_replayed",
        window_days=days,
        attributed_packs=report.attributed_packs,
        cut_servings=report.cut_servings,
    )
    return report


__all__ = ["RelevanceCutoffReplayReport", "replay_relevance_cutoff"]
