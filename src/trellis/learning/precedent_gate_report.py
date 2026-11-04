"""Report layer for the two-stage precedent gate: windows in, counts out.

:mod:`trellis.learning.precedent_activation` is the pure stage-2 test. This module
turns a window of ``PACK_ASSEMBLED`` and ``FEEDBACK_RECORDED`` events into the
packs it judges, and sets that test beside the gate ``auto-promote-candidates``
runs today, over the same candidates. It reads no store and writes none: the
caller hands it events and the candidates file's rows, so the report is a pure
function of what it was given.

Two rules decide what reaches a count.

* **Held-out packs reach none.** :func:`~trellis.core.pack_holdout.drop_holdout`
  runs first, on the packs and on the feedback that names them, and the report
  says how many of each it dropped. A withheld pack is an empty pack the agent
  received because of a coin, not evidence about retrieval.
* **Only attributed packs are judged.** A pack is graded when a grader named
  items in it and the pack carries per-item rows
  (:func:`~trellis.feedback.pack_rows.has_served_rows`). A pack nobody graded has
  no helpful count to shuffle. Verdicts come from
  :func:`~trellis.retrieve.pack_value.collect_pack_verdicts` and rows from
  :func:`~trellis.feedback.pack_rows.served_rows_from_payload`, the readers
  every other report uses.

What the two gates read differs, and the report says so: the current gate reads
the ``citation_evidence`` already written into the candidates file (a feedback
event is a citation; an ungraded pack is a serving), while the new gate
recomputes from this window (a pack is a unit; only graded packs are servings).
The counts are two measurements of the same candidates, not one measurement
twice. Implicit signals (re-retrieval, fetches) feed neither.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from trellis.core.pack_holdout import drop_holdout
from trellis.feedback.pack_rows import has_served_rows, served_rows_from_payload
from trellis.learning.candidate_auto_promote import (
    LearningCandidateAutoPromotePolicy,
    _evaluate_candidate,
)
from trellis.learning.precedent_activation import (
    CRITERIA,
    ActivationThresholds,
    GradedPack,
    ServedItem,
    TargetActivation,
    evaluate_targets,
)
from trellis.learning.scoring import PROMOTE_RECOMMENDATIONS
from trellis.retrieve.pack_value import collect_pack_verdicts

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event

logger = structlog.get_logger(__name__)

#: The family a feedback row or candidate with none is filed under, as the
#: learning layer files it (``scoring`` maps an empty family to this).
GENERAL_FAMILY: Final = "general_context"
_NO_FAMILY_MARK: Final = "(none)"
_NOT_PROMOTABLE: Final = "ineligible:not_promotable"

#: Why the two gates' counts are not one measurement twice. Carried in the
#: report so a reader of the JSON need not find this module.
GATE_DIFFERENCES: Final = (
    (
        "The current gate reads citation_evidence from the candidates file; the "
        "new gate recomputes from this window."
    ),
    (
        "citation_evidence counts a helpful citation per feedback event and counts "
        "ungraded packs as servings; the new gate counts a pack once, whatever the "
        "number of graders, and counts only packs a grader named items in."
    ),
    "A pack's helpful verdict wins over an unhelpful one on the same item.",
    "A candidate is judged over the graded packs of its own intent family.",
    "Candidates already promoted are evaluated like any other.",
)


@dataclass(frozen=True, slots=True)
class GradedWindow:
    """The packs a window yields for stage 2, and what it set aside."""

    #: Packs a grader named items in, held-out packs excluded.
    packs: tuple[GradedPack, ...]
    #: Every item id any served pack in the window carried, graded or not.
    served_item_ids: frozenset[str]
    packs_scanned: int
    #: Packs dropped as withheld by the holdout draw.
    packs_held_out: int
    #: Served packs with per-item rows (the attributable ones).
    packs_with_rows: int
    feedback_scanned: int
    #: Feedback dropped because it names a withheld pack.
    feedback_held_out: int

    @property
    def packs_graded(self) -> int:
        return len(self.packs)


def _family_of(raw: object) -> str:
    text = str(raw or "").strip()
    return text if text and text != _NO_FAMILY_MARK else GENERAL_FAMILY


def _session_of(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def build_graded_window(
    pack_events: Iterable[Event], feedback_events: Iterable[Event]
) -> GradedWindow:
    """Join a window's feedback to the packs it graded, held-out packs dropped.

    ``pack_events`` is ``PACK_ASSEMBLED`` and ``feedback_events`` is
    ``FEEDBACK_RECORDED``, both from one scan window. Sectioned packs are
    included (their rows carry section-local ranks); a pack built before rows
    were recorded has none and is not attributable.
    """
    packs_in = list(pack_events)
    feedback_in = list(feedback_events)
    served_packs, served_feedback = drop_holdout(packs_in, feedback_in)

    payloads: dict[str, Mapping[str, Any]] = {
        event.entity_id: event.payload or {}
        for event in served_packs
        if event.entity_id
    }
    flat = {
        pack_id: payload
        for pack_id, payload in payloads.items()
        if has_served_rows(payload)
    }
    verdicts = collect_pack_verdicts(served_feedback, flat)

    served_item_ids: set[str] = set()
    graded: list[GradedPack] = []
    for pack_id, payload in flat.items():
        rows = [
            ServedItem(row.item_id, row.rank, row.section)
            for row in served_rows_from_payload(
                pack_id,
                payload,
                include_sectioned=True,
                # Dropped above; the one gate is drop_holdout.
                include_holdout=True,
            )
            if row.item_id
        ]
        served_item_ids.update(row.item_id for row in rows)
        helpful = frozenset(verdicts["helpful"].get(pack_id, ()))
        unhelpful = frozenset(verdicts["unhelpful"].get(pack_id, ()))
        if not rows or not (helpful or unhelpful):
            continue
        graded.append(
            GradedPack(
                pack_id=pack_id,
                session_id=_session_of(payload),
                served=tuple(rows),
                helpful=helpful,
                unhelpful=unhelpful,
                intent_family=_family_of(verdicts["families"].get(pack_id)),
            )
        )

    return GradedWindow(
        packs=tuple(graded),
        served_item_ids=frozenset(served_item_ids),
        packs_scanned=len(packs_in),
        packs_held_out=len(packs_in) - len(served_packs),
        packs_with_rows=len(flat),
        feedback_scanned=len(feedback_in),
        feedback_held_out=len(feedback_in) - len(served_feedback),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _target_row(
    candidate_id: str, current: bool, new: bool, result: TargetActivation
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "current_gate": current,
        "new_gate": new,
        "servings": result.servings,
        "helpful": result.helpful,
        "unhelpful": result.unhelpful,
        "distinct_helpful_sessions": result.distinct_helpful_sessions,
        "lift": _round(result.lift),
        "p_value": _round(result.p_value),
        "first_failed": result.first_failed,
    }


_Row = tuple[str, str, str, Mapping[str, Any]]


def _split_candidates(candidates: Sequence[object]) -> tuple[list[_Row], int]:
    """Usable rows as ``(candidate_id, family, item_id, row)``; the rest, counted."""
    usable: list[_Row] = []
    unusable = 0
    for row in candidates:
        candidate_id = (
            str(row.get("candidate_id", "")).strip() if isinstance(row, Mapping) else ""
        )
        if not isinstance(row, Mapping) or not candidate_id:
            unusable += 1
            continue
        item_id = str(row.get("item_id", "")).strip()
        usable.append(
            (candidate_id, _family_of(row.get("intent_family")), item_id, row)
        )
    return usable, unusable


def _is_promotable(row: Mapping[str, Any]) -> bool:
    return str(row.get("recommendation_type", "")).strip() in PROMOTE_RECOMMENDATIONS


def _activation_by_target(
    promotable: Sequence[_Row], window: GradedWindow, limits: ActivationThresholds
) -> dict[tuple[str, str], TargetActivation]:
    """Stage 2 for each (family, item): over that family's graded packs only."""
    packs_by_family: dict[str, list[GradedPack]] = {}
    for pack in window.packs:
        packs_by_family.setdefault(pack.intent_family, []).append(pack)
    wanted: dict[str, set[str]] = {}
    for _, family, item_id, _ in promotable:
        wanted.setdefault(family, set()).add(item_id)
    results: dict[tuple[str, str], TargetActivation] = {}
    for family, item_ids in wanted.items():
        evaluated = evaluate_targets(
            sorted(item_ids), packs_by_family.get(family, ()), limits
        )
        for item_id, result in evaluated.items():
            results[(family, item_id)] = result
    return results


def evaluate_gate(
    candidates: Sequence[object],
    window: GradedWindow,
    *,
    policy: LearningCandidateAutoPromotePolicy,
    thresholds: ActivationThresholds | None = None,
) -> dict[str, Any]:
    """Count what the current and the new gate would each pass, over ``candidates``.

    ``candidates`` are the learning-candidate rows ``auto-promote-candidates``
    reads. A row with no usable ``candidate_id`` is counted apart, as that
    command counts it, and judged by neither gate. A row whose
    ``recommendation_type`` is not promotable is counted and judged by neither.
    A candidate with no ``item_id`` was served nowhere, so it fails ``helpful``.
    Returns plain counts and candidate ids: no item text, intent or trace text.
    """
    limits = thresholds or ActivationThresholds()
    usable, unusable = _split_candidates(candidates)
    promotable = [entry for entry in usable if _is_promotable(entry[3])]
    activation = _activation_by_target(promotable, window, limits)

    current_dispositions: Counter[str] = Counter()
    failed_at: Counter[str] = Counter()
    current_pass = new_pass = both = without_servings = 0
    targets: list[dict[str, Any]] = []
    for candidate_id, family, item_id, row in promotable:
        qualifies, disposition = _evaluate_candidate(row, policy=policy)
        current_dispositions["qualifies" if qualifies else disposition] += 1
        result = activation[(family, item_id)]
        without_servings += result.servings == 0
        new_pass += result.passes
        if result.first_failed:
            failed_at[result.first_failed] += 1
        current_pass += qualifies
        both += qualifies and result.passes
        if qualifies or result.passes:
            targets.append(_target_row(candidate_id, qualifies, result.passes, result))

    return {
        "evaluated": len(usable),
        "unusable_candidates": unusable,
        "not_promotable": len(usable) - len(promotable),
        "promotable": len(promotable),
        "current_gate_pass": current_pass,
        "new_gate_pass": new_pass,
        "both": both,
        "only_new": new_pass - both,
        "only_current": current_pass - both,
        "failed_at": {name: failed_at.get(name, 0) for name in CRITERIA},
        "without_graded_servings": without_servings,
        "current_gate_dispositions": dict(sorted(current_dispositions.items())),
        "current_gate": {
            "min_helpful_count": policy.min_helpful_count,
            "min_distinct_sessions": policy.min_distinct_sessions,
            "require_zero_unhelpful": policy.require_zero_unhelpful,
        },
        "new_gate": {
            "min_helpful": limits.min_helpful,
            "u_below_h": True,
            "min_distinct_sessions": limits.min_distinct_sessions,
            "max_p_value": limits.max_p_value,
            "min_lift": limits.min_lift,
            "resamples": limits.resamples,
            "seed": limits.seed,
        },
        "targets": sorted(targets, key=lambda row: row["candidate_id"]),
        "differences": list(GATE_DIFFERENCES),
    }


def recorded_candidates_in_packs(
    recorded_ids: Iterable[str], window: GradedWindow
) -> int:
    """How many recorded precedent candidates any served pack in the window carried.

    A recorded candidate is event-only and never served, so this is expected to
    be zero until serving reads candidates; the report says it plainly rather
    than letting an empty gate read as one that rejects everything.
    """
    return len(set(recorded_ids) & window.served_item_ids)


__all__ = [
    "GATE_DIFFERENCES",
    "GENERAL_FAMILY",
    "GradedWindow",
    "build_graded_window",
    "evaluate_gate",
    "recorded_candidates_in_packs",
]
