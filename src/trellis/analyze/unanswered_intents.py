"""Unanswered intents: which requests keep getting packs nobody found useful.

The memory-yield report asks which *writes* ever earn a helpful serving; this
one asks the mirror question from the request side: what do agents keep
asking for that the memory does not hold? It is read-only and decides
nothing.

**Unit.** One pack (``PACK_ASSEMBLED``, first event per ``pack_id``) joined to
the ``FEEDBACK_RECORDED`` rows that name it. Held-out packs and the feedback
naming them are dropped with :func:`~trellis.core.pack_holdout.drop_holdout`
and counted. Feedback names its pack by the payload ``pack_id``; a governed
row (``trellis curate feedback``, ``POST /feedback``) without one is joined by
its ``target_id`` when that is a scanned pack id.

**Verdict.** Read per feedback row the way every other reader does:
``success`` when the payload carries a boolean (both families derive it from
``rating`` with :data:`~trellis.feedback.models.SUCCESS_RATING_THRESHOLD`),
else ``rating >= SUCCESS_RATING_THRESHOLD`` (a rating exactly at the
threshold is answered), else no verdict. A pack is *unanswered* when its
verdict is a failure, *answered* when a success, *ungraded* when no row with
a verdict names it; ungraded is never counted as unanswered.

**Several rows on one pack.** The latest row with a verdict (event time, then
scan order) decides, because a later grade is the grader's revised view and
taking one row keeps each pack counted once. Packs with more than one verdict
row, and those whose rows disagree, are counted in ``limits``.

**Grouping key.** Chosen by ``group_by``; no embedding or model is consulted
in any mode.

* ``intent`` (the default): :func:`intent_key`, the pack's ``intent`` text
  case-folded, punctuation replaced by spaces and whitespace collapsed. It
  merges spellings of one request (case, spacing, punctuation) and nothing
  more: two paraphrases of the same need are two groups, so on a corpus where
  agents never word a request twice almost every group holds one pack. A pack
  with no usable intent text lands under :data:`NO_INTENT_KEY`, counted and
  never dropped.
* ``domain``: the payload's ``domain`` as the pack recorded it (stripped). A
  pack with none lands under :data:`NO_DOMAIN_KEY`, counted and never dropped.
* ``family``: the intent family the deterministic rule set of
  :mod:`trellis.learning.intent_families` assigns to the pack's ``intent``
  text (the shipped rules, or a deployment's ``retrieval.intent_families``
  block, passed in as ``rule_set``). A pack with no intent text takes the rule
  set's fallback family, as any unmatched intent does.

The payload's legacy ``intent_family`` field is never a key: it is a keyword
bucket that defaults to ``general_context`` and has been found empty in
practice (see :func:`~trellis.feedback.attribution.lookup_pack_scope`), so it
would merge unrelated requests. In ``intent`` mode ``domain`` is reported per
group, not keyed on. In ``domain`` and ``family`` mode each group also carries
``distinct_intents``, the number of distinct :func:`intent_key` values in it
(the no-intent key counting as one), so a reader sees whether a bucket is one
need asked many times or many needs; in ``intent`` mode that field, and the
report's ``group_by``, are absent from a dump, so the default output is what
it was before the option existed.

**Intent text is user content.** A group's label is the most recent raw
intent of the group, whitespace-collapsed and cut with
:func:`~trellis.retrieve.excerpts.truncate_excerpt` to
:data:`LABEL_MAX_CHARS`; ``redact_intents`` replaces both key and label by a
short stable digest of the key so the counts can be shared. In ``domain``
and ``family`` mode the label is the key itself: a domain or a family name is
a configured vocabulary term, not user content, so ``redact_intents`` leaves
both as they are and no intent text is printed in those modes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field, model_serializer

from trellis.core.base import TrellisModel
from trellis.core.pack_holdout import drop_holdout, is_holdout
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.feedback.pack_rows import (
    FeedbackFamily,
    FeedbackRow,
    iter_feedback_rows,
    served_rows_from_payload,
)
from trellis.learning.intent_families import default_rule_set
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    merge_coverage,
    resolve_scan_window,
    scan_events,
)

if TYPE_CHECKING:
    from collections.abc import Container, Iterable

    from trellis.learning.intent_families import RuleSet
    from trellis.stores.base.event_log import Event, EventLog

logger = structlog.get_logger(__name__)

#: The key (and label) of packs whose intent text is absent or blank.
NO_INTENT_KEY = "(no intent)"

#: The key (and label) of packs whose payload names no domain (``group_by="domain"``).
NO_DOMAIN_KEY = "(no domain)"

#: Group by normalised intent text (the default).
GROUP_BY_INTENT = "intent"
#: Group by the pack's recorded ``domain``.
GROUP_BY_DOMAIN = "domain"
#: Group by the deterministic intent family of the pack's intent text.
GROUP_BY_FAMILY = "family"
#: Every accepted ``group_by`` value, default first.
GROUP_BY_CHOICES: tuple[str, ...] = (GROUP_BY_INTENT, GROUP_BY_DOMAIN, GROUP_BY_FAMILY)

#: Longest group label printed, in characters.
LABEL_MAX_CHARS = 80

#: Hex digits of the digest a redacted label carries.
DIGEST_CHARS = 12

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

#: The first limit statement, per ``group_by``: what the key merges and misses.
_KEY_STATEMENTS: dict[str, str] = {
    GROUP_BY_INTENT: (
        "The grouping key is the intent text case-folded with punctuation and "
        "whitespace collapsed; paraphrases of one request are separate groups."
    ),
    GROUP_BY_DOMAIN: (
        "The grouping key is the domain the pack recorded; one domain can hold "
        "many unrelated needs (see distinct_intents), and a pack with no domain "
        "is grouped under (no domain)."
    ),
    GROUP_BY_FAMILY: (
        "The grouping key is the intent family the deterministic keyword rule "
        "set assigns to the intent text; first matching rule wins, an unmatched "
        "or missing intent takes the fallback family, and one family can hold "
        "many unrelated needs (see distinct_intents)."
    ),
}

_LIMIT_STATEMENTS: tuple[str, ...] = (
    _KEY_STATEMENTS[GROUP_BY_INTENT],
    (
        "Ungraded packs are not unanswered: no feedback with a verdict named "
        "them, which says nothing about whether they helped."
    ),
    (
        "When several feedback rows grade one pack, the latest row with a "
        "verdict decides; conflicting rows are counted, not resolved."
    ),
    (
        "unanswered_not_named_unhelpful means no grader named a served item "
        "unhelpful; feedback without item ids (the CLI and POST /feedback "
        "family) names none, so it is not evidence the items were fine."
    ),
    (
        "A pack from a build that wrote no injected_items rows reads as "
        "having served nothing (unanswered_empty)."
    ),
)


class IntentGroup(TrellisModel):
    """Counts for one grouping key."""

    key: str
    label: str
    packs: int = 0
    graded: int = 0
    unanswered: int = 0
    answered: int = 0
    ungraded: int = 0
    unanswered_empty: int = 0
    unanswered_named_unhelpful: int = 0
    unanswered_not_named_unhelpful: int = 0
    mean_rating: float | None = None
    rated_packs: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    domains: list[str] = Field(default_factory=list)
    intent_families: list[str] = Field(default_factory=list)
    example_pack_ids: list[str] = Field(default_factory=list)
    #: Distinct intent keys in the group; set only in ``domain`` and ``family``
    #: mode and absent from a dump otherwise.
    distinct_intents: int | None = None

    @model_serializer(mode="wrap")
    def _omit_intent_mode_fields(self, handler: Any) -> dict[str, Any]:
        dumped: dict[str, Any] = handler(self)
        if self.distinct_intents is None:
            dumped.pop("distinct_intents", None)
        return dumped


class UnansweredTotals(TrellisModel):
    """Totals over every group, shown or hidden."""

    groups: int = 0
    packs: int = 0
    graded: int = 0
    unanswered: int = 0
    answered: int = 0
    ungraded: int = 0


class UnansweredLimits(TrellisModel):
    """What the report cannot see, stated beside what it counted."""

    holdout_packs_dropped: int = 0
    holdout_feedback_dropped: int = 0
    packs: int = 0
    ungraded_packs: int = 0
    packs_without_intent: int = 0
    feedback_events: int = 0
    feedback_without_pack: int = 0
    feedback_outside_scan: int = 0
    feedback_without_verdict: int = 0
    packs_with_multiple_verdicts: int = 0
    packs_with_conflicting_verdicts: int = 0
    groups_hidden_below_min: int = 0
    groups_beyond_top: int = 0
    statements: list[str] = Field(default_factory=lambda: list(_LIMIT_STATEMENTS))


class UnansweredIntentsReport(TrellisModel):
    """Groups of requests whose packs were graded as failures."""

    window_days: int
    since: str
    min_unanswered: int
    top: int
    redacted: bool
    groups: list[IntentGroup] = Field(default_factory=list)
    totals: UnansweredTotals = Field(default_factory=UnansweredTotals)
    limits: UnansweredLimits = Field(default_factory=UnansweredLimits)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    #: The grouping key; absent from a dump when it is the default ``intent``,
    #: so the default payload is unchanged.
    group_by: str = GROUP_BY_INTENT

    @model_serializer(mode="wrap")
    def _omit_default_group_by(self, handler: Any) -> dict[str, Any]:
        dumped: dict[str, Any] = handler(self)
        if self.group_by == GROUP_BY_INTENT:
            dumped.pop("group_by", None)
        return dumped


def intent_key(intent: object) -> str:
    """The grouping key of one pack's ``intent``.

    Case-folded, every run of non-word characters (punctuation, whitespace,
    underscores) replaced by one space, then stripped. Anything that leaves
    nothing, a non-string included, is :data:`NO_INTENT_KEY`.
    """
    if not isinstance(intent, str):
        return NO_INTENT_KEY
    key = _NON_WORD.sub(" ", intent.casefold()).strip()
    return key or NO_INTENT_KEY


def redact_key(key: str) -> str:
    """A short stable digest standing in for an intent key."""
    if key == NO_INTENT_KEY:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    return f"intent:{digest}"


def row_verdict(row: FeedbackRow) -> bool | None:
    """``True`` answered, ``False`` unanswered, ``None`` no verdict."""
    if row.success is not None:
        return row.success
    if row.rating is not None:
        return not row.rating < SUCCESS_RATING_THRESHOLD
    return None


def feedback_pack_id(row: FeedbackRow, known_pack_ids: Container[str]) -> str | None:
    """The pack one feedback row grades, or ``None`` when it names none.

    The payload ``pack_id`` when there is one; else, for a governed row
    (``trellis curate feedback``, ``POST /feedback``), its ``target_id`` when
    that is one of ``known_pack_ids``, since that family's ``target_id`` may
    equally be a trace.
    """
    if row.pack_id is not None:
        return row.pack_id
    if row.family is FeedbackFamily.GOVERNED and row.target_id in known_pack_ids:
        return row.target_id
    return None


#: One graded row on a pack: ``(event time, scan index, answered, rating)``.
Verdict = tuple[datetime, int, bool, float | None]


def latest_verdict(verdicts: Iterable[Verdict]) -> Verdict | None:
    """The row that decides a pack: the latest by event time, then scan order."""
    return max(verdicts, key=lambda v: (v[0], v[1]), default=None)


@dataclass
class _Pack:
    pack_id: str
    at: datetime
    key: str
    label: str
    intent: str
    domain: str | None
    family: str | None
    served: frozenset[str]
    verdicts: list[Verdict] = field(default_factory=list)
    unhelpful: set[str] = field(default_factory=set)


def summarize_unanswered_intents(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = DEFAULT_SCAN_LIMIT,
    min_unanswered: int = 2,
    top: int = 20,
    examples: int = 3,
    redact_intents: bool = False,
    group_by: str = GROUP_BY_INTENT,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
) -> UnansweredIntentsReport:
    """Group graded packs by request and count the unanswered ones.

    Args:
        event_log: Holds the pack and feedback events.
        days: Look-back window for both scans.
        limit: Per-event-type scan cap; truncation is reported in ``scan``.
        min_unanswered: Groups with fewer unanswered packs are hidden and
            counted in ``limits.groups_hidden_below_min``.
        top: Most groups listed; the rest are counted in
            ``limits.groups_beyond_top``.
        examples: Most unanswered pack ids listed per group.
        redact_intents: Replace each intent key and label by a digest; a
            domain or family key is not user content and is left as it is.
        group_by: One of :data:`GROUP_BY_CHOICES`; see the module docstring.
        rule_set: The intent-family rules for ``group_by="family"`` (load a
            deployment's with :func:`~trellis.learning.intent_families.load_rule_set`).
            Default: the shipped rules. Ignored in the other modes.
        now: Window end, for tests. Default: the current time.

    Returns:
        An :class:`UnansweredIntentsReport`. Reads only.

    Raises:
        ValueError: ``group_by`` is not one of :data:`GROUP_BY_CHOICES`.
    """
    if group_by not in GROUP_BY_CHOICES:
        msg = f"group_by must be one of {', '.join(GROUP_BY_CHOICES)}, not {group_by!r}"
        raise ValueError(msg)
    moment = now if now is not None else datetime.now(tz=UTC)
    since, until = resolve_scan_window(days, now=moment)
    pack_scan = scan_events(
        event_log,
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        until=until,
        limit=limit,
    )
    feedback_scan = scan_events(
        event_log,
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        until=until,
        limit=limit,
    )
    limits = UnansweredLimits()
    limits.statements[0] = _KEY_STATEMENTS[group_by]
    limits.holdout_packs_dropped = sum(
        1 for event in pack_scan.events if is_holdout(event.payload)
    )
    pack_events, feedback_events = drop_holdout(pack_scan.events, feedback_scan.events)
    limits.holdout_feedback_dropped = len(feedback_scan.events) - len(feedback_events)
    limits.feedback_events = len(feedback_events)

    packs = _read_packs(pack_events, limits, group_by, rule_set)
    _attach_feedback(packs, feedback_events, limits)
    groups = _group(
        packs.values(),
        examples,
        redact_intents and group_by == GROUP_BY_INTENT,
        limits,
        by_intent=group_by == GROUP_BY_INTENT,
    )

    totals = UnansweredTotals(groups=len(groups))
    for group in groups:
        totals.packs += group.packs
        totals.graded += group.graded
        totals.unanswered += group.unanswered
        totals.answered += group.answered
        totals.ungraded += group.ungraded

    groups.sort(key=lambda g: g.key)
    groups.sort(key=lambda g: g.last_seen or "", reverse=True)
    groups.sort(key=lambda g: g.unanswered, reverse=True)
    shown = [g for g in groups if g.unanswered >= min_unanswered]
    limits.groups_hidden_below_min = len(groups) - len(shown)
    cap = max(top, 0)
    limits.groups_beyond_top = max(len(shown) - cap, 0)

    report = UnansweredIntentsReport(
        window_days=days,
        since=since.isoformat(),
        min_unanswered=min_unanswered,
        top=top,
        redacted=redact_intents,
        groups=shown[:cap],
        totals=totals,
        limits=limits,
        scan=merge_coverage(pack_scan.coverage, feedback_scan.coverage),
        group_by=group_by,
    )
    logger.debug(
        "unanswered_intents_summarized",
        window_days=days,
        group_by=group_by,
        groups=totals.groups,
        unanswered=totals.unanswered,
    )
    return report


def _read_packs(
    events: Iterable[Event],
    limits: UnansweredLimits,
    group_by: str,
    rule_set: RuleSet | None,
) -> dict[str, _Pack]:
    rules = rule_set or default_rule_set()
    packs: dict[str, _Pack] = {}
    for event in events:
        pack_id = event.entity_id
        if not pack_id or pack_id in packs:
            continue
        payload = event.payload or {}
        raw_intent = payload.get("intent")
        intent = intent_key(raw_intent)
        if intent == NO_INTENT_KEY:
            limits.packs_without_intent += 1
            label = NO_INTENT_KEY
        else:
            label = " ".join(str(raw_intent).split())
        domain = _clean(payload.get("domain"))
        key = intent
        if group_by == GROUP_BY_DOMAIN:
            key = label = domain or NO_DOMAIN_KEY
        elif group_by == GROUP_BY_FAMILY:
            text = raw_intent if isinstance(raw_intent, str) else None
            key = label = rules.classify(text).family
        served = frozenset(
            row.item_id
            for row in served_rows_from_payload(
                pack_id, payload, include_sectioned=True, include_holdout=False
            )
            if row.item_id
        )
        packs[pack_id] = _Pack(
            pack_id=pack_id,
            at=_as_utc(event.occurred_at),
            key=key,
            label=label,
            intent=intent,
            domain=domain,
            family=_clean(payload.get("intent_family")),
            served=served,
        )
    limits.packs = len(packs)
    return packs


def _attach_feedback(
    packs: dict[str, _Pack], events: list[Event], limits: UnansweredLimits
) -> None:
    for index, (event, row) in enumerate(
        zip(events, iter_feedback_rows(events), strict=True)
    ):
        pack_id = feedback_pack_id(row, packs)
        if pack_id is None:
            limits.feedback_without_pack += 1
            continue
        pack = packs.get(pack_id)
        if pack is None:
            limits.feedback_outside_scan += 1
            continue
        pack.unhelpful.update(row.unhelpful_item_ids)
        verdict = row_verdict(row)
        if verdict is None:
            limits.feedback_without_verdict += 1
            continue
        pack.verdicts.append((_as_utc(event.occurred_at), index, verdict, row.rating))


def _group(
    packs: Iterable[_Pack],
    examples: int,
    redact: bool,
    limits: UnansweredLimits,
    *,
    by_intent: bool,
) -> list[IntentGroup]:
    groups: dict[str, IntentGroup] = {}
    rating_sums: dict[str, float] = {}
    intents: dict[str, set[str]] = {}
    for pack in sorted(packs, key=lambda p: p.at):
        group = groups.get(pack.key)
        if group is None:
            shown_key = redact_key(pack.key) if redact else pack.key
            group = groups[pack.key] = IntentGroup(key=shown_key, label=shown_key)
            group.first_seen = pack.at.isoformat()
        if not redact:
            # Packs arrive oldest first, so the label ends as the latest.
            group.label = truncate_excerpt(pack.label, LABEL_MAX_CHARS)
        group.last_seen = pack.at.isoformat()
        group.packs += 1
        if not by_intent:
            seen = intents.setdefault(pack.key, set())
            seen.add(pack.intent)
            group.distinct_intents = len(seen)
        if pack.domain and pack.domain not in group.domains:
            group.domains = sorted([*group.domains, pack.domain])
        if pack.family and pack.family not in group.intent_families:
            group.intent_families = sorted([*group.intent_families, pack.family])
        _count_verdict(group, pack, examples, rating_sums, limits)
    for key, group in groups.items():
        if group.rated_packs:
            group.mean_rating = rating_sums[key] / group.rated_packs
    return list(groups.values())


def _count_verdict(
    group: IntentGroup,
    pack: _Pack,
    examples: int,
    rating_sums: dict[str, float],
    limits: UnansweredLimits,
) -> None:
    decided = latest_verdict(pack.verdicts)
    if decided is None:
        group.ungraded += 1
        limits.ungraded_packs += 1
        return
    if len(pack.verdicts) > 1:
        limits.packs_with_multiple_verdicts += 1
        if len({verdict for _, _, verdict, _ in pack.verdicts}) > 1:
            limits.packs_with_conflicting_verdicts += 1
    _, _, answered, rating = decided
    group.graded += 1
    if rating is not None:
        group.rated_packs += 1
        rating_sums[pack.key] = rating_sums.get(pack.key, 0.0) + rating
    if answered:
        group.answered += 1
        return
    group.unanswered += 1
    if examples > 0:
        # Packs arrive oldest first: keep the most recent ``examples``.
        group.example_pack_ids = [*group.example_pack_ids, pack.pack_id][-examples:]
    if not pack.served:
        group.unanswered_empty += 1
    elif pack.served & pack.unhelpful:
        group.unanswered_named_unhelpful += 1
    else:
        group.unanswered_not_named_unhelpful += 1


def _clean(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "GROUP_BY_CHOICES",
    "GROUP_BY_DOMAIN",
    "GROUP_BY_FAMILY",
    "GROUP_BY_INTENT",
    "LABEL_MAX_CHARS",
    "NO_DOMAIN_KEY",
    "NO_INTENT_KEY",
    "IntentGroup",
    "UnansweredIntentsReport",
    "UnansweredLimits",
    "UnansweredTotals",
    "Verdict",
    "feedback_pack_id",
    "intent_key",
    "latest_verdict",
    "redact_key",
    "row_verdict",
    "summarize_unanswered_intents",
]
