"""A pure mapper: one stored advisory in, at most one candidate precedent out.

No store reads or writes, no LLM, no clock, no randomness: the same advisory
gives the same record with the same id. The records are *candidates*
(:mod:`trellis.schemas.precedent_record`), recorded through the governed
``precedent.candidate`` operation and never served. Reading an advisory here
changes nothing the advisory generator produces, stores, suppresses or serves.

Only the two lesson-shaped categories map. Each rule states a *measured
association*, never a cause: the advisory generator compares the success rate of
the packs that carried a subject against the packs that did not, and that is all
a candidate says.

``advisory_anti_pattern``
    An ``ANTI_PATTERN`` advisory is an item whose presence goes with failure
    (``effect_size`` below zero). It gives an ``avoid`` on that item. The item
    is the situation's one ``entity_ids`` entry.

``advisory_approach``
    An ``APPROACH`` advisory is a retrieval *strategy source* (the axis an item
    reached a pack through: keyword, semantic, graph) whose packs succeeded more
    or less often than the packs without it. This is the generator's proxy for
    step-pattern mining; it names a retrieval strategy, not a step an agent took.
    A positive ``effect_size`` gives a ``do`` and a negative one an ``avoid``
    (the pattern is the failing side). No entity is named.

Evidence. ``evidence.advisory_ids`` carries the advisory id and ``pack_ids`` the
pack ids the advisory cites (``AdvisoryEvidence.representative_trace_ids``, which
despite its name holds pack ids). ``trace_ids`` stays empty: an advisory cites no
trace. ``because`` carries the counts and rates as an association. The
situation's ``intent_family`` is the frozen learning key for no intent, because
an advisory is not tied to one; its ``domain`` is the advisory's scope unless
that is ``global``.

Confidence is the evidence's own (``evidence_confidence``, else the advisory's
``confidence``) capped at :data:`_CONFIDENCE_CAP`: a correlation never claims
more than a one-trace recovery does.

Skips, each counted by reason and none carrying advisory text:

``suppressed`` / ``retired``
    The advisory is not active. A retired row was withdrawn by a rule, a
    suppressed one demoted on outcomes; neither is a finding to carry forward.
``category_not_lesson_shaped``
    Any category but the two above.
``missing_subject``
    No item id (``ANTI_PATTERN``) or no strategy name (``APPROACH``).
``missing_evidence``
    No pack id the record can cite, or no sample. Advisories stored before the
    generator filled the pack pointers land here.
``not_a_failure_association`` / ``no_effect``
    An ``ANTI_PATTERN`` whose effect is not negative, or an ``APPROACH`` whose
    effect is zero: the stored numbers do not support the rule.
``entity_id_overlong``
    The item id is longer than the schema's id bound. Unlike a trace's entity
    ids it cannot be left out: the item is the whole situation.
``invalid_record``
    The record failed the schema's bounds.

There is no ``restates_intent`` check here: an advisory carries no intent for an
action to restate, and its action text is built from the subject id alone.

The advisory generator's ids are keyed on the finding, not the numbers, so one
advisory is the same finding every night while its counts move; the record id
hashes the counts, so it moves with them. Telling a finding already recorded
from a new one is the caller's job (it holds the event log); see
``trellis_cli.worker_distill_advisory_precedents``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from pydantic import ValidationError

from trellis.learning.scoring import normalize_intent_family
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryStatus
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_ID_CHARS,
    MAX_LIST_ITEMS,
    MAX_TRIGGER_CHARS,
    ActionKind,
    PrecedentAction,
    PrecedentEvidence,
    PrecedentRecord,
    PrecedentSituation,
)

ADVISORY_DISTILLER_VERSION: Final = "advisory-deterministic-1"

RULE_ADVISORY_ANTI_PATTERN: Final = "advisory_anti_pattern"
RULE_ADVISORY_APPROACH: Final = "advisory_approach"
RULES: Final = (RULE_ADVISORY_ANTI_PATTERN, RULE_ADVISORY_APPROACH)

SKIP_SUPPRESSED: Final = "suppressed"
SKIP_RETIRED: Final = "retired"
SKIP_CATEGORY: Final = "category_not_lesson_shaped"
SKIP_MISSING_SUBJECT: Final = "missing_subject"
SKIP_MISSING_EVIDENCE: Final = "missing_evidence"
SKIP_NOT_A_FAILURE_ASSOCIATION: Final = "not_a_failure_association"
SKIP_NO_EFFECT: Final = "no_effect"
SKIP_ENTITY_ID_OVERLONG: Final = "entity_id_overlong"
SKIP_INVALID_RECORD: Final = "invalid_record"

#: A correlation over packs never claims more than a one-trace recovery does.
_CONFIDENCE_CAP: Final = 0.5
#: The advisory scope that means "no single domain".
_GLOBAL_SCOPE: Final = "global"


@dataclass(frozen=True)
class AdvisoryMapResult:
    """What one advisory mapped to: a record, or the reason it gave none."""

    record: PrecedentRecord | None = None
    skip: str | None = None


@dataclass
class AdvisoryTally:
    """What a batch of advisories mapped to, in counts plus the records."""

    advisories_read: int = 0
    records: list[PrecedentRecord] = field(default_factory=list)
    by_rule: Counter[str] = field(default_factory=Counter)
    skips_by_reason: Counter[str] = field(default_factory=Counter)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _pack_ids(advisory: Advisory) -> list[str]:
    """The pack ids the advisory cites that fit the schema, in order, capped."""
    ids = [
        pack_id
        for pack_id in advisory.evidence.representative_trace_ids
        if pack_id and len(pack_id) <= MAX_ID_CHARS
    ]
    return ids[:MAX_LIST_ITEMS]


def _confidence(advisory: Advisory) -> float:
    basis = advisory.evidence.evidence_confidence
    if basis is None:
        basis = advisory.confidence
    return round(min(max(basis, 0.0), _CONFIDENCE_CAP), 3)


def _association(advisory: Advisory, *, carried: str) -> str:
    """The measured association, worded as one and never as a cause."""
    evidence = advisory.evidence
    return (
        f"Association, not a cause: of {evidence.sample_size} packs that "
        f"{carried}, {evidence.success_rate_with:.0%} succeeded, against "
        f"{evidence.success_rate_without:.0%} of the packs without it "
        f"(effect {evidence.effect_size:+.0%})."
    )


def _build(
    advisory: Advisory,
    *,
    rule: str,
    kind: ActionKind,
    action_text: str,
    trigger: str,
    because: str,
    entity_ids: list[str],
) -> AdvisoryMapResult:
    scope = _clean(advisory.scope)
    domain = None if not scope or scope == _GLOBAL_SCOPE else scope
    try:
        record = PrecedentRecord.derive(
            rule=rule,
            situation=PrecedentSituation(
                intent_family=normalize_intent_family(),
                domain=(
                    None
                    if domain is None
                    else truncate_excerpt(domain, MAX_DOMAIN_CHARS)
                ),
                entity_ids=entity_ids,
                trigger=truncate_excerpt(_clean(trigger), MAX_TRIGGER_CHARS),
            ),
            action=PrecedentAction(
                kind=kind, text=truncate_excerpt(_clean(action_text), MAX_ACTION_CHARS)
            ),
            because=truncate_excerpt(_clean(because), MAX_BECAUSE_CHARS),
            evidence=PrecedentEvidence(
                pack_ids=_pack_ids(advisory), advisory_ids=[advisory.advisory_id]
            ),
            confidence=_confidence(advisory),
            provenance_tier="deterministic",
            distiller_version=ADVISORY_DISTILLER_VERSION,
        )
    except ValidationError:
        return AdvisoryMapResult(skip=SKIP_INVALID_RECORD)
    return AdvisoryMapResult(record=record)


def _anti_pattern(advisory: Advisory) -> AdvisoryMapResult:
    item_id = _clean(advisory.entity_id)
    if not item_id:
        return AdvisoryMapResult(skip=SKIP_MISSING_SUBJECT)
    if len(item_id) > MAX_ID_CHARS:
        return AdvisoryMapResult(skip=SKIP_ENTITY_ID_OVERLONG)
    if not _pack_ids(advisory) or advisory.evidence.sample_size <= 0:
        return AdvisoryMapResult(skip=SKIP_MISSING_EVIDENCE)
    if advisory.evidence.effect_size >= 0:
        return AdvisoryMapResult(skip=SKIP_NOT_A_FAILURE_ASSOCIATION)
    return _build(
        advisory,
        rule=RULE_ADVISORY_ANTI_PATTERN,
        kind="avoid",
        action_text=f"Avoid relying on item {item_id}",
        trigger=f"A context pack carries item {item_id}",
        because=_association(advisory, carried="carried this item"),
        entity_ids=[item_id],
    )


def _approach(advisory: Advisory) -> AdvisoryMapResult:
    raw = advisory.metadata.get("strategy")
    strategy = _clean(raw if isinstance(raw, str) else "")
    if not strategy:
        return AdvisoryMapResult(skip=SKIP_MISSING_SUBJECT)
    if not _pack_ids(advisory) or advisory.evidence.sample_size <= 0:
        return AdvisoryMapResult(skip=SKIP_MISSING_EVIDENCE)
    effect = advisory.evidence.effect_size
    if effect == 0:
        return AdvisoryMapResult(skip=SKIP_NO_EFFECT)
    kind: ActionKind = "do" if effect > 0 else "avoid"
    verb = "Prefer" if kind == "do" else "Avoid"
    return _build(
        advisory,
        rule=RULE_ADVISORY_APPROACH,
        kind=kind,
        action_text=f"{verb} the '{strategy}' retrieval strategy",
        trigger=f"A context pack's items come from the '{strategy}' strategy",
        because=_association(advisory, carried=f"used the '{strategy}' strategy"),
        entity_ids=[],
    )


def map_advisory(advisory: Advisory) -> AdvisoryMapResult:
    """Map one stored advisory to a candidate record, or say why it gives none."""
    if advisory.status == AdvisoryStatus.SUPPRESSED:
        return AdvisoryMapResult(
            skip=SKIP_RETIRED if advisory.retired_by else SKIP_SUPPRESSED
        )
    if advisory.category == AdvisoryCategory.ANTI_PATTERN:
        return _anti_pattern(advisory)
    if advisory.category == AdvisoryCategory.APPROACH:
        return _approach(advisory)
    return AdvisoryMapResult(skip=SKIP_CATEGORY)


def map_advisories(advisories: Iterable[Advisory]) -> AdvisoryTally:
    """Map a batch, in advisory-id order, and count what came of it."""
    tally = AdvisoryTally()
    for advisory in sorted(advisories, key=lambda a: a.advisory_id):
        tally.advisories_read += 1
        result = map_advisory(advisory)
        if result.record is not None:
            tally.records.append(result.record)
            tally.by_rule[result.record.rule] += 1
        elif result.skip is not None:
            tally.skips_by_reason[result.skip] += 1
    return tally


__all__ = [
    "ADVISORY_DISTILLER_VERSION",
    "RULES",
    "RULE_ADVISORY_ANTI_PATTERN",
    "RULE_ADVISORY_APPROACH",
    "SKIP_CATEGORY",
    "SKIP_ENTITY_ID_OVERLONG",
    "SKIP_INVALID_RECORD",
    "SKIP_MISSING_EVIDENCE",
    "SKIP_MISSING_SUBJECT",
    "SKIP_NOT_A_FAILURE_ASSOCIATION",
    "SKIP_NO_EFFECT",
    "SKIP_RETIRED",
    "SKIP_SUPPRESSED",
    "AdvisoryMapResult",
    "AdvisoryTally",
    "map_advisories",
    "map_advisory",
]
