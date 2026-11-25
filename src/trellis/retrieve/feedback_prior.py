"""Optional post-rerank feedback prior for pack ranking. Off by default.

**What it does.** Agents grade pack items helpful or unhelpful
(``FEEDBACK_RECORDED``) and open the full body of items they want
(``PACK_ITEMS_FETCHED``). This module turns that evidence into a bounded
multiplier on an item's post-fusion ``relevance_score``. Per ``item_id``,
over a look-back window (:data:`DEFAULT_WINDOW_DAYS`):

* ``helpful`` -- distinct packs whose feedback cited the item helpful;
* ``unhelpful`` -- distinct packs whose feedback cited it unhelpful;
* ``fetched`` -- distinct packs the item was fetched from that did not
  also grade it, so one pack is never counted twice.

Each ``(pack_id, item_id)`` contributes once; on one pack a helpful verdict
wins over an unhelpful one. With ``pos = helpful + fetch_weight * fetched``::

    factor = 1 + strength * (pos - unhelpful) / (pos + unhelpful + k)

Defaults (:data:`DEFAULT_STRENGTH` ``0.5``, :data:`DEFAULT_K` ``2.0``,
:data:`DEFAULT_FETCH_WEIGHT` ``1.0``) keep the factor strictly inside
``(0.5, 1.5)``. They are code constants, not env-parsed: a weight is a code
change reviewed with its replay, the posture
:mod:`trellis.retrieve.authority_rank` takes.

**Demotion needs evidence of unhelpfulness.** An item with no evidence gets
exactly ``1.0`` and is returned untouched, as is any item whose factor comes
out exactly ``1.0`` (``pos == unhelpful``): no keys, not counted as adjusted.
A factor below ``1.0`` additionally needs :func:`demotion_allowed`:
``unhelpful`` at least
:data:`~trellis.classify.demotion_gate.MIN_UNHELPFUL_CITATIONS` and above
``pos``. That is stricter than the demotion gate
(:mod:`trellis.classify.demotion_gate`), not a mirror of it: ``pos`` adds
weighted fetches to helpful citations, and the floor is the code constant,
not the gate's env-tunable threshold. Short of it, a would-be demotion is
exactly ``1.0`` and the item is returned untouched; one unhelpful grade
never demotes. Promotion has no such floor. An item whose score is not above
``0`` is never scaled.

**Why after the reranker.** The RRF reranker rebuilds every score from
within-axis rank, so a multiplier applied at the collect seam is discarded
by fusion. This runs at the post-rerank seam beside
:func:`~trellis.retrieve.authority_rank.apply_authority_rank`, on the flat
and sectioned :class:`~trellis.retrieve.pack_builder.PackBuilder` paths.

**Never on a public-only pack.** The public-only path does not call this
step at all: no evidence read, no scaling, no reorder, no keys, and
``PUBLIC_PACK_SERVED`` carries no ``feedback_prior`` block. The evidence is
how non-public callers graded and fetched items, and a public-only caller
must not learn it; hiding the breakdown keys is not enough, since the served
order alone would disclose it.

**Modes.** ``off`` (default) returns the very list it was given and adds no
key to any item or event. ``shadow`` records the would-be factor under
:data:`FEEDBACK_PRIOR_SHADOW_KEY` and changes no score or order. ``on``
applies it and records :data:`FEEDBACK_PRIOR_KEY`. In both non-off modes an
item with evidence also records its three counts in ``score_breakdown``
(:data:`EVIDENCE_KEYS`), which ``PACK_ASSEMBLED.injected_items[]`` carries,
and the event gains a ``feedback_prior`` block.

**Where the evidence comes from.** :class:`EventLogFeedbackEvidence` reads
the EventLog with bounded scans and caches the per-item table for a TTL. It
writes nothing. Held-out packs and the feedback naming them are dropped
(:func:`~trellis.core.pack_holdout.drop_holdout`, through the shared join),
as is any verdict or fetch naming a pack the scan holds no served row for.
A failed read, or one the scan cap truncated, serves the pack unadjusted and
says so in the block's ``state`` (``evidence_unavailable`` /
``evidence_truncated``). :func:`feedback_prior_step` never raises.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog

from trellis.classify.demotion_gate import MIN_UNHELPFUL_CITATIONS
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event, EventLog

logger = structlog.get_logger(__name__)

FeedbackPriorMode = Literal["off", "shadow", "on"]

FEEDBACK_PRIOR_MODE_ENV = "TRELLIS_FEEDBACK_PRIOR_MODE"
FEEDBACK_PRIOR_MODE_SETTING = "feedback_prior_mode"
#: ``score_breakdown`` key an adjusted item carries under ``on``: the factor.
FEEDBACK_PRIOR_KEY = "feedback_prior"
#: The would-be factor under ``shadow``; the score is unchanged.
FEEDBACK_PRIOR_SHADOW_KEY = "feedback_prior_shadow"
HELPFUL_KEY = "feedback_prior_helpful"
UNHELPFUL_KEY = "feedback_prior_unhelpful"
FETCHED_KEY = "feedback_prior_fetched"
#: Every ``score_breakdown`` key this module can write.
EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        FEEDBACK_PRIOR_KEY,
        FEEDBACK_PRIOR_SHADOW_KEY,
        HELPFUL_KEY,
        UNHELPFUL_KEY,
        FETCHED_KEY,
    }
)

DEFAULT_STRENGTH = 0.5
DEFAULT_K = 2.0
DEFAULT_FETCH_WEIGHT = 1.0
DEFAULT_WINDOW_DAYS = 90
DEFAULT_TTL_SECONDS = 600.0
#: Per-event-type cap on each serve-time scan. A window holding more events
#: than this reads as ``evidence_truncated`` and the prior goes inert.
DEFAULT_SCAN_LIMIT = 20_000

STATE_APPLIED = "applied"
STATE_UNAVAILABLE = "evidence_unavailable"
STATE_TRUNCATED = "evidence_truncated"
STATE_ERROR = "error"

_VALID_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})


@dataclass(frozen=True)
class FeedbackPriorConfig:
    """Config for :func:`apply_feedback_prior`. The default is a no-op."""

    mode: FeedbackPriorMode = "off"
    strength: float = DEFAULT_STRENGTH
    k: float = DEFAULT_K
    fetch_weight: float = DEFAULT_FETCH_WEIGHT
    window_days: int = DEFAULT_WINDOW_DAYS

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            msg = f"mode must be one of {sorted(_VALID_MODES)}; got {self.mode!r}"
            raise ValueError(msg)
        # strength <= 1 keeps the factor above 0, so a score never flips sign.
        if not 0.0 <= self.strength <= 1.0:
            msg = f"strength must be in [0, 1]; got {self.strength!r}"
            raise ValueError(msg)
        if not self.k > 0.0:
            msg = f"k must be > 0; got {self.k!r}"
            raise ValueError(msg)
        if not self.fetch_weight >= 0.0:
            msg = f"fetch_weight must be >= 0; got {self.fetch_weight!r}"
            raise ValueError(msg)
        if self.window_days < 1:
            msg = f"window_days must be >= 1; got {self.window_days!r}"
            raise ValueError(msg)


#: The no-op config every call site falls back to.
OFF = FeedbackPriorConfig()


def resolve_feedback_prior_config(
    settings: dict[str, Any] | None = None,
) -> FeedbackPriorConfig:
    """Resolve ``mode`` env > settings > default; the rest are fixed defaults.

    An unrecognised value degrades to ``"off"`` with a warning naming the
    variable or setting, never the value.
    """
    raw = os.environ.get(FEEDBACK_PRIOR_MODE_ENV, "").strip().lower()
    if raw:
        if raw in _VALID_MODES:
            return FeedbackPriorConfig(mode=raw)  # type: ignore[arg-type]
        logger.warning(
            "feedback_prior_mode_env_unrecognised",
            var=FEEDBACK_PRIOR_MODE_ENV,
            effective="off",
        )
        return OFF
    raw_value = (settings or {}).get(FEEDBACK_PRIOR_MODE_SETTING)
    value = raw_value.strip().lower() if isinstance(raw_value, str) else ""
    if value in _VALID_MODES:
        return FeedbackPriorConfig(mode=value)  # type: ignore[arg-type]
    if value:
        logger.warning(
            "feedback_prior_mode_settings_unrecognised",
            setting=FEEDBACK_PRIOR_MODE_SETTING,
        )
    return OFF


@dataclass(frozen=True, slots=True)
class ItemEvidence:
    """One item's evidence: distinct packs per signal."""

    helpful: int = 0
    unhelpful: int = 0
    #: Fetches on packs that did not grade the item.
    fetched: int = 0

    @property
    def empty(self) -> bool:
        return not (self.helpful or self.unhelpful or self.fetched)


def demotion_allowed(evidence: ItemEvidence, config: FeedbackPriorConfig) -> bool:
    """Enough unhelpful citations, and more of them than ``pos``.

    Stricter than :mod:`trellis.classify.demotion_gate`, not a mirror of it:
    ``unhelpful`` is compared against ``pos`` (helpful citations plus
    ``fetch_weight`` times fetches), so a fetched item resists demotion where
    the gate would not, and the floor is the code constant
    :data:`~trellis.classify.demotion_gate.MIN_UNHELPFUL_CITATIONS`, never the
    gate's env-tunable threshold (the prior's weights are code constants).
    """
    pos = evidence.helpful + config.fetch_weight * evidence.fetched
    return evidence.unhelpful >= MIN_UNHELPFUL_CITATIONS and evidence.unhelpful > pos


def factor_for(evidence: ItemEvidence | None, config: FeedbackPriorConfig) -> float:
    """The multiplier for one item; exactly ``1.0`` with no evidence.

    Also exactly ``1.0`` when the raw factor is below ``1.0`` but
    :func:`demotion_allowed` refuses: demotion needs evidence of
    unhelpfulness, by a rule stricter than the demotion gate's.
    """
    if evidence is None or evidence.empty or demotion_refused(evidence, config):
        return 1.0
    return _raw_factor(evidence, config)


def _raw_factor(evidence: ItemEvidence, config: FeedbackPriorConfig) -> float:
    pos = evidence.helpful + config.fetch_weight * evidence.fetched
    neg = evidence.unhelpful
    return 1.0 + config.strength * (pos - neg) / (pos + neg + config.k)


def demotion_refused(evidence: ItemEvidence, config: FeedbackPriorConfig) -> bool:
    """The raw factor would demote but :func:`demotion_allowed` refuses."""
    return _raw_factor(evidence, config) < 1.0 and not demotion_allowed(
        evidence, config
    )


_HELPFUL, _UNHELPFUL, _FETCHED = "helpful", "unhelpful", "fetched"


@dataclass(frozen=True, slots=True)
class _Signal:
    occurred_at: datetime
    pack_id: str
    item_id: str
    kind: str


@dataclass
class EvidenceLedger:
    """Every verdict and fetch, stamped, so a table can be cut at any instant.

    Serving reads :meth:`table` with no bound; the replay reads it bounded
    before each pack's own assembly and excluding that pack, so a pack's
    replayed ranking never sees its own grades or anything recorded after it.
    """

    signals: list[_Signal] = field(default_factory=list)

    def table(
        self,
        *,
        before: datetime | None = None,
        since: datetime | None = None,
        exclude_pack: str | None = None,
    ) -> dict[str, ItemEvidence]:
        kinds: dict[tuple[str, str], set[str]] = {}
        for signal in self.signals:
            if before is not None and not signal.occurred_at < before:
                continue
            if since is not None and signal.occurred_at < since:
                continue
            if exclude_pack is not None and signal.pack_id == exclude_pack:
                continue
            kinds.setdefault((signal.pack_id, signal.item_id), set()).add(signal.kind)
        counts: dict[str, list[int]] = {}
        for (_pack, item_id), seen in kinds.items():
            slot = counts.setdefault(item_id, [0, 0, 0])
            if _HELPFUL in seen:
                slot[0] += 1
            elif _UNHELPFUL in seen:
                slot[1] += 1
            else:
                slot[2] += 1
        return {
            item_id: ItemEvidence(helpful=h, unhelpful=u, fetched=f)
            for item_id, (h, u, f) in counts.items()
        }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def build_ledger(
    pack_events: Mapping[str, Event],
    feedback_events: Iterable[Event],
    fetch_events: Iterable[Event],
) -> EvidenceLedger:
    """Fold the joined events into an :class:`EvidenceLedger`.

    ``pack_events`` and ``feedback_events`` are the outputs of
    :func:`~trellis.learning.pack_observations.join_pack_events_with_coverage`,
    which has already dropped held-out packs and the feedback naming them. A
    verdict or fetch counts only for an item the named pack actually served,
    so a pack the scan holds no served row for (a held-out pack among them)
    contributes nothing.
    """
    from trellis.feedback.pack_rows import (  # noqa: PLC0415 - import cycle
        iter_feedback_rows,
        served_rows_from_payload,
    )
    from trellis.retrieve.pack_value import fetch_row  # noqa: PLC0415

    served: dict[str, set[str]] = {}
    for pack_id, event in pack_events.items():
        served[pack_id] = {
            row.item_id
            for row in served_rows_from_payload(
                pack_id,
                event.payload or {},
                include_sectioned=True,
                include_holdout=False,
            )
            if row.item_id
        }
    ledger = EvidenceLedger()
    feedback_list = list(feedback_events)
    for event, row in zip(
        feedback_list, iter_feedback_rows(feedback_list), strict=True
    ):
        items = served.get(row.pack_id or "")
        if not items:
            continue
        at = _as_utc(event.occurred_at)
        for kind, ids in (
            (_HELPFUL, row.helpful_item_ids),
            (_UNHELPFUL, row.unhelpful_item_ids),
        ):
            for item_id in ids:
                if item_id in items:
                    ledger.signals.append(_Signal(at, row.pack_id or "", item_id, kind))
    for event in fetch_events:
        fetch = fetch_row(event)
        items = served.get(fetch.pack_id or "")
        if not items:
            continue
        at = _as_utc(event.occurred_at)
        for item_id in fetch.served_item_ids:
            if item_id in items:
                ledger.signals.append(
                    _Signal(at, fetch.pack_id or "", item_id, _FETCHED)
                )
    return ledger


def scan_ledger(
    event_log: EventLog, *, since: datetime, limit: int
) -> tuple[EvidenceLedger, bool]:
    """Read and fold the window; returns ``(ledger, truncated)``.

    Raises whatever the reads raise: the caller decides how to degrade.
    """
    from trellis.learning.pack_observations import (  # noqa: PLC0415 - import cycle
        join_pack_events_with_coverage,
    )
    from trellis.stores.base.event_log import (  # noqa: PLC0415
        EventType,
        scan_events,
    )

    feedback_events, pack_events, _count, coverage = join_pack_events_with_coverage(
        event_log, since=since, limit=limit
    )
    fetch_scan = scan_events(
        event_log,
        event_type=EventType.PACK_ITEMS_FETCHED,
        since=since,
        limit=limit,
    )
    truncated = bool(coverage.truncated or fetch_scan.coverage.truncated)
    return build_ledger(pack_events, feedback_events, fetch_scan.events), truncated


@dataclass(frozen=True)
class EvidenceResult:
    """What a provider served: a table, or the state saying why not."""

    state: str
    table: Mapping[str, ItemEvidence] = field(default_factory=dict)


class FeedbackEvidenceProvider:
    """Protocol-shaped base: anything with :meth:`evidence`."""

    def evidence(self) -> EvidenceResult:  # pragma: no cover - interface
        raise NotImplementedError


class EventLogFeedbackEvidence(FeedbackEvidenceProvider):
    """Per-item evidence read from the EventLog, cached for ``ttl_seconds``.

    Read only: no store, no file, no write path. A failed or truncated read
    is cached like a good one, so a failing log is not re-scanned per pack.
    """

    def __init__(
        self,
        event_log: EventLog,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.event_log = event_log
        self._window_days = window_days
        self._ttl = ttl_seconds
        self._limit = scan_limit
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: EvidenceResult | None = None
        self._loaded_at = 0.0

    def evidence(self) -> EvidenceResult:
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._loaded_at < self._ttl:
                return self._cached
            self._cached = self._load()
            self._loaded_at = now
            return self._cached

    def _load(self) -> EvidenceResult:
        since = datetime.now(tz=UTC) - timedelta(days=self._window_days)
        try:
            ledger, truncated = scan_ledger(
                self.event_log, since=since, limit=self._limit
            )
        except Exception:
            # GRACEFUL-DEGRADATION: the prior is advisory; the pack serves
            # unadjusted and the block's state says so.
            logger.exception("feedback_prior_evidence_read_failed")
            return EvidenceResult(state=STATE_UNAVAILABLE)
        if truncated:
            logger.warning("feedback_prior_evidence_truncated", limit=self._limit)
            return EvidenceResult(state=STATE_TRUNCATED)
        return EvidenceResult(state=STATE_APPLIED, table=ledger.table())


_SHARED: dict[int, EventLogFeedbackEvidence] = {}
_SHARED_LOCK = threading.Lock()
_SHARED_MAX = 16


def shared_evidence_provider(event_log: EventLog) -> EventLogFeedbackEvidence:
    """One provider per event log per process, so the TTL cache is shared."""
    with _SHARED_LOCK:
        provider = _SHARED.get(id(event_log))
        if provider is None or provider.event_log is not event_log:
            if len(_SHARED) >= _SHARED_MAX:
                _SHARED.clear()
            provider = EventLogFeedbackEvidence(event_log)
            _SHARED[id(event_log)] = provider
        return provider


def apply_feedback_prior(
    items: list[PackItem],
    config: FeedbackPriorConfig,
    table: Mapping[str, ItemEvidence],
) -> list[PackItem]:
    """Scale (``on``) or annotate (``shadow``) items with evidence.

    Order is preserved; callers sort afterwards. ``items`` itself when off.
    Only an item with evidence, a score above ``0`` and a factor other than
    exactly ``1.0`` is copied; every other item is the same object.
    """
    if config.mode == "off":
        return items
    out: list[PackItem] = []
    for item in items:
        evidence = table.get(item.item_id)
        if evidence is None or evidence.empty or not item.relevance_score > 0:
            out.append(item)
            continue
        if demotion_refused(evidence, config):
            # A demotion the floor refused: untouched, not annotated.
            out.append(item)
            continue
        factor = factor_for(evidence, config)
        if factor == 1.0:
            # Balanced evidence (pos == unhelpful): nothing to apply.
            out.append(item)
            continue
        breakdown = dict(item.score_breakdown)
        breakdown[HELPFUL_KEY] = float(evidence.helpful)
        breakdown[UNHELPFUL_KEY] = float(evidence.unhelpful)
        breakdown[FETCHED_KEY] = float(evidence.fetched)
        update: dict[str, Any] = {"score_breakdown": breakdown}
        if config.mode == "on":
            breakdown[FEEDBACK_PRIOR_KEY] = factor
            update["relevance_score"] = item.relevance_score * factor
        else:
            breakdown[FEEDBACK_PRIOR_SHADOW_KEY] = factor
        out.append(item.model_copy(update=update))
    return out


def _block(
    config: FeedbackPriorConfig, state: str, items: list[PackItem], table_size: int
) -> dict[str, Any]:
    key = FEEDBACK_PRIOR_KEY if config.mode == "on" else FEEDBACK_PRIOR_SHADOW_KEY
    factors = [
        item.score_breakdown[key] for item in items if key in item.score_breakdown
    ]
    return {
        "mode": config.mode,
        "state": state,
        "window_days": config.window_days,
        "strength": config.strength,
        "k": config.k,
        "fetch_weight": config.fetch_weight,
        "evidence_items": table_size,
        "adjusted_count": len(factors),
        "promoted_count": sum(1 for f in factors if f > 1.0),
        "demoted_count": sum(1 for f in factors if f < 1.0),
    }


def feedback_prior_step(
    items: list[PackItem],
    config: FeedbackPriorConfig,
    provider: FeedbackEvidenceProvider | None,
) -> tuple[list[PackItem], dict[str, Any] | None]:
    """The whole advisory step: ``(items, PACK_ASSEMBLED block or None)``.

    Never raises. Off returns ``(items, None)`` without reading evidence.
    Any failure serves ``items`` unadjusted with the block's ``state``
    naming why.
    """
    if config.mode == "off":
        return items, None
    try:
        if provider is None:
            return items, _block(config, STATE_UNAVAILABLE, [], 0)
        result = provider.evidence()
        if result.state != STATE_APPLIED:
            return items, _block(config, result.state, [], 0)
        adjusted = apply_feedback_prior(items, config, result.table)
        return adjusted, _block(config, STATE_APPLIED, adjusted, len(result.table))
    except Exception:
        # GRACEFUL-DEGRADATION: one escaped exception must not fail a pack.
        logger.exception("feedback_prior_step_failed")
        return items, _block(config, STATE_ERROR, [], 0)


__all__ = [
    "DEFAULT_FETCH_WEIGHT",
    "DEFAULT_K",
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_STRENGTH",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_WINDOW_DAYS",
    "EVIDENCE_KEYS",
    "FEEDBACK_PRIOR_KEY",
    "FEEDBACK_PRIOR_MODE_ENV",
    "FEEDBACK_PRIOR_MODE_SETTING",
    "FEEDBACK_PRIOR_SHADOW_KEY",
    "FETCHED_KEY",
    "HELPFUL_KEY",
    "OFF",
    "STATE_APPLIED",
    "STATE_ERROR",
    "STATE_TRUNCATED",
    "STATE_UNAVAILABLE",
    "UNHELPFUL_KEY",
    "EventLogFeedbackEvidence",
    "EvidenceLedger",
    "EvidenceResult",
    "FeedbackEvidenceProvider",
    "FeedbackPriorConfig",
    "FeedbackPriorMode",
    "ItemEvidence",
    "apply_feedback_prior",
    "build_ledger",
    "demotion_allowed",
    "demotion_refused",
    "factor_for",
    "feedback_prior_step",
    "resolve_feedback_prior_config",
    "scan_ledger",
    "shared_evidence_provider",
]
