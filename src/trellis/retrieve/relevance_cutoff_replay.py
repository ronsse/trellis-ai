"""Price :mod:`trellis.retrieve.relevance_cutoff` over packs already served.

Run it with ``trellis analyze replay --relevance-cutoff``. Kept out of
:mod:`trellis.retrieve.pack_replay` so the arm and its report live beside the
gate they replay.

The population is the servings an attributed flat pack actually served,
counted once per ``(pack_id, item_id)``, as the class-prior arm counts them.
Each pack's candidate pool is rebuilt from its ``PACK_ASSEMBLED`` row: the
served ``injected_items[]`` plus the ``rejected_items[]`` rows that carry
``native_scores`` *and* whose reason arises at or after the cutoff stage
(:data:`POOL_REJECTION_REASONS`). The live gate runs after noise, archived,
demotion, supersession, sensitivity (precedent source gate included) and
class-prior gates, so a row those rejected never reached it; admitting one
would move ``best_semantic`` and the candidate count away from what the live
gate saw. A row with a reason outside the allowlist is excluded and counted
in ``excluded_unknown_reason``.
:func:`~trellis.retrieve.relevance_cutoff.apply_relevance_cutoff` then runs
over that pool unchanged, so the floor, the every-axis rule and ``min_keep``
are the gate's own. Only a served id's cut is counted.

What the arm cannot see:

* A pack served before native scores were recorded has no ``native_*`` key on
  any row. Its servings are counted as ``unpriced_servings``, never as kept.
* The pool is what the event recorded. A rejected row from before native
  scores were recorded carries none and cannot join the pool, so
  ``best_semantic`` (and a ratio floor) can be lower than the live gate's,
  and the candidate count (which ``min_keep`` reads) smaller.
* With ``--evidence-exempt``: each pack's evidence is the feedback prior's
  ledger (:func:`~trellis.retrieve.feedback_prior.build_ledger`, held-out
  packs dropped by the same join) cut to what was recorded before that
  pack's own ``PACK_ASSEMBLED`` and within the prior's window, excluding the
  pack itself, so a pack's replay never sees its own grades or anything
  later. The arm reads its events once over the replay window plus that
  window, under the same scan limit; a truncated read is reported in
  ``evidence_state`` and the exemption counts are then left at zero.
* No refill is modelled. Like the class-prior arm, this does not re-run the
  token-budget walk, so ``tokens_removed`` is the gross cost of the cut
  servings and the arm cannot say what the freed budget would have admitted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.feedback.attribution import is_sectioned_payload
from trellis.feedback.pack_rows import has_served_rows, served_rows_from_payload
from trellis.learning.pack_observations import join_pack_events_with_coverage
from trellis.retrieve.class_prior import CLASS_PRIOR_REJECTION_REASON
from trellis.retrieve.excerpts import CONTENT_FLOOR_REJECTION_REASON
from trellis.retrieve.feedback_prior import (
    DEFAULT_WINDOW_DAYS,
    STATE_APPLIED,
    STATE_TRUNCATED,
    EvidenceLedger,
    EvidenceResult,
    build_ledger,
)
from trellis.retrieve.lifecycle import (
    ARCHIVED_REJECTION_REASON,
    DEMOTED_REJECTION_REASON,
    SUPERSEDED_REJECTION_REASON,
)
from trellis.retrieve.noise import NOISE_REJECTION_REASON
from trellis.retrieve.pack_value import collect_pack_verdicts
from trellis.retrieve.relevance_cutoff import (
    NATIVE_SCORE_PREFIX,
    RELEVANCE_CUTOFF_REJECTION_REASON,
    RelevanceCutoffConfig,
    apply_relevance_cutoff,
)
from trellis.retrieve.sensitivity import (
    SENSITIVITY_REJECTION_REASON,
    SENSITIVITY_UNREADABLE_REJECTION_REASON,
)
from trellis.retrieve.strategies import MALFORMED_METADATA_REJECTION_REASON
from trellis.schemas.pack import PackItem
from trellis.stores.base.event_log import EventType, scan_events

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_LIMIT = 5000

#: Rejection reasons that arise at or after the cutoff stage of the flat
#: ``PackBuilder.build`` collect seam, so the live gate saw the row as a
#: candidate. Enumerated from the code, in pipeline order: the cutoff itself,
#: exact-id dedup, semantic dedup, the structural and meta-activity filters,
#: session dedup, the content floor, then the ``max_items`` and token-budget
#: walks. Everything before the cutoff (noise, archived, demoted, superseded,
#: sensitivity and sensitivity_unreadable, which the precedent source gate
#: also uses, malformed_metadata, class_prior) is excluded on purpose; an
#: unknown reason is excluded too and counted, so a new gate added later
#: reads as a visible gap, not as a silent change to the pool.
POOL_REJECTION_REASONS: frozenset[str] = frozenset(
    {
        RELEVANCE_CUTOFF_REJECTION_REASON,
        "dedup",
        "semantic_dedup",
        "structural_filter",
        "meta_activity_filter",
        "session_dedup",
        CONTENT_FLOOR_REJECTION_REASON,
        "max_items",
        "token_budget",
    }
)

#: Reasons known to arise before the cutoff stage. Excluded, not counted as
#: unknown.
_PRE_CUTOFF_REASONS: frozenset[str] = frozenset(
    {
        NOISE_REJECTION_REASON,
        ARCHIVED_REJECTION_REASON,
        DEMOTED_REJECTION_REASON,
        SUPERSEDED_REJECTION_REASON,
        SENSITIVITY_REJECTION_REASON,
        SENSITIVITY_UNREADABLE_REJECTION_REASON,
        MALFORMED_METADATA_REJECTION_REASON,
        CLASS_PRIOR_REJECTION_REASON,
    }
)


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
    #: Servings the floor failed that ``min_keep`` restored (served ids
    #: only; a restored rejected row is not a serving).
    kept_by_min_keep: int = 0
    #: Rejected rows with native scores left out of the pool because their
    #: reason is neither in :data:`POOL_REJECTION_REASONS` nor a known
    #: pre-cutoff reason.
    excluded_unknown_reason: int = 0
    #: Whether the evidence exemption was priced (``--evidence-exempt``).
    evidence_exempt: bool = False
    #: ``applied``, ``evidence_truncated`` or ``not_requested``.
    evidence_state: str = "not_requested"
    #: Look-back of each pack's evidence, ending at that pack's assembly.
    evidence_window_days: int = 0
    #: Cut servings (above) the exemption keeps, by the pack's own verdict.
    exempt_restored_servings: int = 0
    exempt_restored_helpful: int = 0
    exempt_restored_unhelpful: int = 0
    exempt_restored_unjudged: int = 0
    exempt_restored_tokens: int = 0
    #: Servings kept without the exemption but cut with it: an exempted id
    #: counts toward ``min_keep`` first, so it can displace a restored one.
    exempt_newly_cut_servings: int = 0
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
        and math.isfinite(v)
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


def _rejected_pool(rows: Any) -> tuple[list[PackItem], int]:
    """The rejected rows the live gate saw, and how many had an unknown reason.

    A row joins only when it carries native scores and its reason is in
    :data:`POOL_REJECTION_REASONS`.
    """
    pool: list[PackItem] = []
    unknown = 0
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        natives = _native(r.get("native_scores"))
        if not natives:
            continue
        reason = r.get("reason")
        if reason in POOL_REJECTION_REASONS:
            pool.append(_pool_item(str(r["item_id"]), natives, 0))
        elif reason not in _PRE_CUTOFF_REASONS:
            unknown += 1
    return pool, unknown


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
            "carry native_scores and were rejected at or after the cutoff "
            "stage; rows the noise, lifecycle, sensitivity or class-prior "
            "gates removed never reached the live gate and are left out. A "
            "rejected row without native_scores cannot join, so a ratio floor "
            "can sit lower than the live gate's."
        ),
    ]
    if report.excluded_unknown_reason:
        notes.append(
            f"{report.excluded_unknown_reason} rejected row(s) carried a reason "
            "this arm does not know the stage of and were left out of the pool."
        )
    if report.evidence_exempt:
        if report.evidence_state == STATE_APPLIED:
            notes.append(
                "Evidence exemption priced with each pack's evidence as of its "
                f"own assembly ({report.evidence_window_days}-day look-back, "
                "the pack's own grades and fetches excluded). cut_servings and "
                "the removed counts are WITHOUT the exemption; "
                "exempt_restored_* is what it would keep of those."
            )
        else:
            notes.append(
                f"Evidence read was {report.evidence_state}: the exemption is "
                "not priced and its counts are zero."
            )
    if report.unpriced_servings:
        notes.append(
            f"{report.unpriced_servings} serving(s) come from packs with no "
            "native score on any row (served before native scores were "
            "recorded) and are counted as unpriced, not as kept."
        )
    return notes


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _price_exemption(
    report: RelevanceCutoffReplayReport,
    *,
    pool: list[PackItem],
    served: list[PackItem],
    cut_ids: set[str],
    gate: RelevanceCutoffConfig,
    ledger: EvidenceLedger,
    assembled: datetime,
    pack_id: str,
    helpful: set[str],
    unhelpful: set[str],
) -> None:
    """Re-run the gate with the exemption on, evidence as of ``assembled``."""
    table = ledger.table(
        before=assembled,
        since=assembled - timedelta(days=DEFAULT_WINDOW_DAYS),
        exclude_pack=pack_id,
    )
    exempt_gate = RelevanceCutoffConfig(
        mode="on",
        semantic_min=gate.semantic_min,
        semantic_ratio=gate.semantic_ratio,
        evidence_exempt="on",
    )
    result = apply_relevance_cutoff(
        pool,
        config=exempt_gate,
        price=lambda item: item.estimated_tokens or 0,
        evidence=lambda: EvidenceResult(state=STATE_APPLIED, table=table),
    )
    served_ids = {item.item_id for item in served}
    cut_with = {r.item_id for r in result.rejected} & served_ids
    report.exempt_newly_cut_servings += len(cut_with - cut_ids)
    for item in served:
        if item.item_id not in cut_ids or item.item_id in cut_with:
            continue
        report.exempt_restored_servings += 1
        report.exempt_restored_tokens += item.estimated_tokens or 0
        if item.item_id in helpful:
            report.exempt_restored_helpful += 1
        elif item.item_id in unhelpful:
            report.exempt_restored_unhelpful += 1
        else:
            report.exempt_restored_unjudged += 1


def replay_relevance_cutoff(  # noqa: PLR0912, PLR0915
    event_log: EventLog,
    *,
    config: RelevanceCutoffConfig,
    days: int = 30,
    limit: int = _DEFAULT_EVENT_LIMIT,
    evidence_exempt: bool = False,
) -> RelevanceCutoffReplayReport:
    """Price ``config``'s cut over the attributed flat packs in the window.

    The mode in ``config`` is ignored beyond the label: the arm prices the
    cut ``on`` would make, which is also what ``shadow`` records. With
    ``evidence_exempt`` the arm also reports how many cut servings the
    evidence exemption would restore, each pack judged on the evidence
    recorded before it.
    """
    applied = (
        f"mode={config.mode} semantic_min={config.semantic_min} "
        f"semantic_ratio={config.semantic_ratio}"
    )
    if evidence_exempt:
        applied += " evidence_exempt=on"
    report = RelevanceCutoffReplayReport(window_days=days, applied_config=applied)
    since = datetime.now(tz=UTC) - timedelta(days=days)
    # The exemption's evidence reaches back one prior window before the
    # first replayed pack; without it the read is the replay window alone.
    scan_since = (
        since - timedelta(days=DEFAULT_WINDOW_DAYS) if evidence_exempt else since
    )
    feedback_events, pack_events, _, coverage = join_pack_events_with_coverage(
        event_log, since=scan_since, limit=limit
    )
    ledger: EvidenceLedger | None = None
    if evidence_exempt:
        report.evidence_exempt = True
        report.evidence_window_days = DEFAULT_WINDOW_DAYS
        fetch_scan = scan_events(
            event_log,
            event_type=EventType.PACK_ITEMS_FETCHED,
            since=scan_since,
            limit=limit,
        )
        if coverage.truncated or fetch_scan.coverage.truncated:
            report.evidence_state = STATE_TRUNCATED
        else:
            report.evidence_state = STATE_APPLIED
            ledger = build_ledger(pack_events, feedback_events, fetch_scan.events)
    flat_packs = {
        pack_id: event.payload or {}
        for pack_id, event in pack_events.items()
        if _as_utc(event.occurred_at) >= since
        and has_served_rows(event.payload or {})
        and not is_sectioned_payload(event.payload or {})
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
        rejected_pool, unknown = _rejected_pool(payload.get("rejected_items"))
        report.excluded_unknown_reason += unknown
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
        report.kept_by_min_keep += len(set(result.restored) & served_ids)
        cut_ids = {r.item_id for r in result.rejected} & served_ids
        helpful = helpful_by_pack.get(pack_id, set())
        unhelpful = unhelpful_by_pack.get(pack_id, set())
        if ledger is not None:
            _price_exemption(
                report,
                pool=served_pool + rejected_pool,
                served=served_pool,
                cut_ids=cut_ids,
                gate=gate,
                ledger=ledger,
                assembled=_as_utc(pack_events[pack_id].occurred_at),
                pack_id=pack_id,
                helpful=helpful,
                unhelpful=unhelpful,
            )
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


__all__ = [
    "POOL_REJECTION_REASONS",
    "RelevanceCutoffReplayReport",
    "replay_relevance_cutoff",
]
