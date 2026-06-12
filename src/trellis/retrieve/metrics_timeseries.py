"""Improvement-metrics time series — read-only EventLog aggregations.

Backs the admin dashboard's trend charts (WP11). Every metric is
computed **server-side from the EventLog on read** — there is no new
storage and no caching layer (POC scale). The five metrics, in priority
order, are:

* ``pack_success_rate`` — share of graded packs with a positive outcome
  per bucket (PACK_ASSEMBLED ⋈ FEEDBACK_RECORDED on ``pack_id``, the
  same join semantics as :mod:`trellis.learning.pack_observations`).
* ``reference_rate`` — ``items_referenced / items_served`` per bucket;
  the single best "are packs getting better" proxy.
* ``advisory_fitness`` — mean advisory confidence (from the fitness
  loop's ADVISORY_SUPPRESSED / ADVISORY_RESTORED ``new_confidence``)
  plus the count of advisories suppressed per bucket.
* ``noise_tag_volume`` — items flipped to ``signal_quality="noise"``
  per bucket, counted from the ``TAGS_REFRESHED`` audit events whose
  ``after`` tags carry the noise label.
* ``parameter_promotions`` — promote / reject / degrade governance
  event counts per bucket (``PARAMS_UPDATED`` + ``TUNER_PROPOSAL_*`` +
  ``PARAMETERS_DEGRADED`` + ``PARAMS_AUTO_*``; the tier-1 auto events
  ride alongside the underlying promote/degrade events — see
  :class:`trellis.stores.base.event_log.EventType`).

**Definitional parity with the convergence scenario.** Where a metric
overlaps with what ``eval/scenarios/agent_loop_convergence`` computes,
the formula matches that scenario's helpers in
``eval/scenarios/_convergence_common.py``. Each shared formula is
cross-referenced at its call site below.

**Bucketing.** Buckets are UTC calendar days keyed by the event's
``occurred_at``. A bucket with no contributing events is **omitted**
from the series (never zero-filled) — clients infer gaps from missing
``bucket_start`` keys. This keeps sparse POC corpora honest: an empty
day is "no signal", not "zero success".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event, EventLog

logger = structlog.get_logger(__name__)

#: Per-event-type scan cap. Matches :mod:`trellis.retrieve.effectiveness`
#: and :mod:`trellis.learning.pack_observations` so all three read the
#: same window depth.
_DEFAULT_EVENT_LIMIT = 5000

#: The ``group_by`` axes the dashboard exposes. ``"none"`` collapses
#: every event into a single ``"all"`` series.
GROUP_BY_NONE = "none"
GROUP_BY_DOMAIN = "domain"
GROUP_BY_INTENT_FAMILY = "intent_family"
_VALID_GROUP_BY = frozenset({GROUP_BY_NONE, GROUP_BY_DOMAIN, GROUP_BY_INTENT_FAMILY})

#: The five named metrics, in priority order.
METRIC_PACK_SUCCESS_RATE = "pack_success_rate"
METRIC_REFERENCE_RATE = "reference_rate"
METRIC_ADVISORY_FITNESS = "advisory_fitness"
METRIC_NOISE_TAG_VOLUME = "noise_tag_volume"
METRIC_PARAMETER_PROMOTIONS = "parameter_promotions"
VALID_METRICS: tuple[str, ...] = (
    METRIC_PACK_SUCCESS_RATE,
    METRIC_REFERENCE_RATE,
    METRIC_ADVISORY_FITNESS,
    METRIC_NOISE_TAG_VOLUME,
    METRIC_PARAMETER_PROMOTIONS,
)

#: Group key used when ``group_by="none"`` or when an event lacks the
#: requested grouping dimension.
_UNGROUPED_KEY = "all"


@dataclass
class TimeseriesPoint:
    """One bucket's value for one series.

    ``value`` is the metric value for the bucket; ``sample_count`` is the
    number of underlying observations that produced it (packs, items,
    events — metric-dependent) so the UI can dim low-confidence points.
    """

    bucket_start: str  # ISO date (UTC midnight) — "YYYY-MM-DD"
    value: float
    sample_count: int


@dataclass
class TimeseriesSeries:
    """One group's ordered list of buckets.

    ``group_key`` is the resolved grouping value (a domain, an
    intent_family, or ``"all"`` when ungrouped). Points are sorted by
    ``bucket_start`` ascending; buckets with no data are omitted.
    """

    group_key: str
    points: list[TimeseriesPoint] = field(default_factory=list)


@dataclass
class TimeseriesResult:
    """A computed metric across one or more grouped series."""

    metric: str
    bucket: str
    group_by: str
    days: int
    series: list[TimeseriesSeries] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bucketing + grouping helpers
# ---------------------------------------------------------------------------


def _bucket_key(occurred_at: datetime) -> str:
    """Return the UTC calendar-day key for an event timestamp."""
    # Naive timestamps are assumed UTC (the stores persist UTC ISO).
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return occurred_at.astimezone(UTC).date().isoformat()


def _resolve_group_key(payload: dict[str, object], group_by: str) -> str:
    """Pick the grouping value from an event payload.

    ``group_by="none"`` always collapses to ``"all"``. When the event
    payload lacks the requested dimension, the event still contributes
    under ``"all"`` so it is never silently dropped.
    """
    if group_by == GROUP_BY_NONE:
        return _UNGROUPED_KEY
    raw = payload.get(group_by)
    if raw is None or not str(raw).strip():
        return _UNGROUPED_KEY
    return str(raw)


def _validate_args(*, metric: str, group_by: str, days: int) -> None:
    """Reject unknown metric / group_by / non-positive window at the boundary."""
    if metric not in VALID_METRICS:
        msg = f"Unknown metric {metric!r}; valid metrics: {list(VALID_METRICS)}"
        raise ValueError(msg)
    if group_by not in _VALID_GROUP_BY:
        msg = f"Unknown group_by {group_by!r}; valid: {sorted(_VALID_GROUP_BY)}"
        raise ValueError(msg)
    if days <= 0:
        msg = "days must be positive"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Bucket accumulators — one per metric shape
# ---------------------------------------------------------------------------


@dataclass
class _RatioBucket:
    """Numerator / denominator accumulator for ratio metrics."""

    numerator: float = 0.0
    denominator: float = 0.0
    samples: int = 0


@dataclass
class _MeanCountBucket:
    """Running sum for a mean plus an independent integer count.

    Used by ``advisory_fitness``: ``value`` is the mean confidence over
    ``mean_samples`` observations; ``count`` is the suppressed-advisory
    tally (reported as ``sample_count``).
    """

    confidence_sum: float = 0.0
    mean_samples: int = 0
    count: int = 0


@dataclass
class _CountBucket:
    """Plain integer count accumulator for volume metrics."""

    count: int = 0


def _join_pack_feedback(
    event_log: EventLog, *, since: datetime, limit: int
) -> tuple[list[Event], dict[str, dict[str, object]]]:
    """Return ``(feedback_events, pack_payload_by_pack_id)``.

    The join key is ``pack_id`` read from ``FEEDBACK_RECORDED.payload``
    (falling back to ``entity_id``), matched against ``PACK_ASSEMBLED``
    whose ``entity_id`` is the pack_id — identical to the join in
    :func:`trellis.learning.pack_observations.build_learning_observations_from_event_log`
    and :func:`trellis.retrieve.effectiveness.analyze_effectiveness`.
    """
    pack_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, since=since, limit=limit
    )
    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED, since=since, limit=limit
    )
    pack_payloads: dict[str, dict[str, object]] = {}
    for event in pack_events:
        if event.entity_id:
            pack_payloads[event.entity_id] = event.payload or {}
    return feedback_events, pack_payloads


def _feedback_pack_id(feedback: Event) -> str | None:
    payload = feedback.payload or {}
    pack_id = payload.get("pack_id") or feedback.entity_id
    if pack_id is None:
        return None
    pack_id = str(pack_id).strip()
    return pack_id or None


# ---------------------------------------------------------------------------
# Metric computations
# ---------------------------------------------------------------------------


def _compute_pack_success_rate(
    event_log: EventLog, *, since: datetime, group_by: str, limit: int
) -> dict[str, dict[str, _RatioBucket]]:
    """share of graded packs with a positive outcome, per (group, bucket).

    Parity: matches ``round_success_rate`` in
    ``eval/scenarios/_convergence_common.py::_base_round_metrics`` —
    ``successes / total_graded`` where a "success" is a pack-level
    positive outcome. Grouping resolves ``domain`` from the
    PACK_ASSEMBLED payload and ``intent_family`` from the
    FEEDBACK_RECORDED payload (the only event carrying it), matching the
    pack_observations join precedence.
    """
    feedback_events, pack_payloads = _join_pack_feedback(
        event_log, since=since, limit=limit
    )
    buckets: dict[str, dict[str, _RatioBucket]] = defaultdict(
        lambda: defaultdict(_RatioBucket)
    )
    for feedback in feedback_events:
        pack_id = _feedback_pack_id(feedback)
        if pack_id is None or pack_id not in pack_payloads:
            continue
        fb_payload = feedback.payload or {}
        merged = {**pack_payloads[pack_id], **fb_payload}
        group_key = _resolve_group_key(merged, group_by)
        bucket = buckets[group_key][_bucket_key(feedback.occurred_at)]
        bucket.denominator += 1
        bucket.samples += 1
        if _feedback_success(fb_payload):
            bucket.numerator += 1
    return buckets


def _feedback_success(payload: dict[str, object]) -> bool:
    """Resolve a pack-level success flag from a FEEDBACK_RECORDED payload.

    Prefers the explicit ``success`` bool (set by
    :meth:`PackFeedback.to_event_payload`); falls back to
    ``outcome in {"success", "completed"}`` for back-compat events.
    """
    if "success" in payload:
        return bool(payload["success"])
    outcome = str(payload.get("outcome") or "").strip().lower()
    return outcome in ("success", "completed")


def _compute_reference_rate(
    event_log: EventLog, *, since: datetime, group_by: str, limit: int
) -> dict[str, dict[str, _RatioBucket]]:
    """``items_referenced / items_served`` per (group, bucket).

    Parity: matches the useful-fraction formula in
    ``eval/scenarios/_convergence_common.py`` — both
    ``round_useful_fraction_overall`` (sum referenced / sum served) in
    ``_base_round_metrics`` and the per-round ratio in
    ``_convergence_stats``. We aggregate corpus-wide per bucket: sum of
    referenced over sum of served, so a bucket's rate is the pooled
    useful fraction, not a mean of per-pack ratios.

    ``items_served`` / ``helpful_item_ids`` are read from the
    FEEDBACK_RECORDED payload (``helpful_item_ids`` is
    ``PackFeedback.items_referenced``). When a feedback event omits
    ``items_served`` it falls back to the joined PACK_ASSEMBLED
    ``injected_item_ids`` so older feedback rows still count.
    """
    feedback_events, pack_payloads = _join_pack_feedback(
        event_log, since=since, limit=limit
    )
    buckets: dict[str, dict[str, _RatioBucket]] = defaultdict(
        lambda: defaultdict(_RatioBucket)
    )
    for feedback in feedback_events:
        pack_id = _feedback_pack_id(feedback)
        if pack_id is None or pack_id not in pack_payloads:
            continue
        fb_payload = feedback.payload or {}
        pack_payload = pack_payloads[pack_id]
        served = fb_payload.get("items_served") or pack_payload.get(
            "injected_item_ids"
        )
        served_count = len(served) if isinstance(served, list) else 0
        if served_count == 0:
            continue
        referenced = fb_payload.get("helpful_item_ids")
        referenced_count = len(referenced) if isinstance(referenced, list) else 0
        merged = {**pack_payload, **fb_payload}
        group_key = _resolve_group_key(merged, group_by)
        bucket = buckets[group_key][_bucket_key(feedback.occurred_at)]
        bucket.numerator += referenced_count
        bucket.denominator += served_count
        bucket.samples += 1
    return buckets


def _compute_advisory_fitness(
    event_log: EventLog, *, since: datetime, group_by: str, limit: int
) -> dict[str, dict[str, _MeanCountBucket]]:
    """Mean advisory confidence + suppressed count per (group, bucket).

    Reads the fitness loop's audit events
    (:attr:`EventType.ADVISORY_SUPPRESSED` /
    :attr:`EventType.ADVISORY_RESTORED`), each of which carries
    ``new_confidence`` (the blended confidence the loop computed) and is
    emitted by
    :func:`trellis.retrieve.effectiveness.run_advisory_fitness_loop`.
    The bucket ``value`` is the mean of ``new_confidence``; the
    ``sample_count`` is the number of ADVISORY_SUPPRESSED events in the
    bucket — the convergence scenario's ``advisories_suppressed_total``
    signal (``eval/scenarios/_convergence_common.py::_loop_metrics``),
    sliced per day.

    Advisory events carry no domain / intent_family, so they all land
    under ``"all"`` regardless of ``group_by``.
    """
    suppressed = event_log.get_events(
        event_type=EventType.ADVISORY_SUPPRESSED, since=since, limit=limit
    )
    restored = event_log.get_events(
        event_type=EventType.ADVISORY_RESTORED, since=since, limit=limit
    )
    buckets: dict[str, dict[str, _MeanCountBucket]] = defaultdict(
        lambda: defaultdict(_MeanCountBucket)
    )
    for event in (*suppressed, *restored):
        payload = event.payload or {}
        group_key = _resolve_group_key(payload, group_by)
        bucket = buckets[group_key][_bucket_key(event.occurred_at)]
        confidence = payload.get("new_confidence")
        if isinstance(confidence, int | float):
            bucket.confidence_sum += float(confidence)
            bucket.mean_samples += 1
        if event.event_type == EventType.ADVISORY_SUPPRESSED:
            bucket.count += 1
    return buckets


def _compute_noise_tag_volume(
    event_log: EventLog, *, since: datetime, group_by: str, limit: int
) -> dict[str, dict[str, _CountBucket]]:
    """Items flipped to ``signal_quality="noise"`` per (group, bucket).

    Counts :attr:`EventType.TAGS_REFRESHED` events whose ``after`` tags
    carry ``signal_quality="noise"`` — the audit substrate that records
    a document's reclassification to noise (one event per item). The
    effectiveness loop's :func:`~trellis.classify.feedback.apply_noise_tags`
    persists the tag; the per-item TAGS_REFRESHED event (emitted on the
    reclassification path) is what makes the volume observable here.
    """
    events = event_log.get_events(
        event_type=EventType.TAGS_REFRESHED, since=since, limit=limit
    )
    buckets: dict[str, dict[str, _CountBucket]] = defaultdict(
        lambda: defaultdict(_CountBucket)
    )
    for event in events:
        payload = event.payload or {}
        if not _after_is_noise(payload):
            continue
        group_key = _resolve_group_key(payload, group_by)
        buckets[group_key][_bucket_key(event.occurred_at)].count += 1
    return buckets


def _after_is_noise(payload: dict[str, object]) -> bool:
    """True when a TAGS_REFRESHED payload's ``after`` tags mark noise.

    Tolerates two shapes: a flat ``after.signal_quality`` and a nested
    ``after.content_tags.signal_quality`` (the document-metadata shape).
    """
    after = payload.get("after")
    if not isinstance(after, dict):
        return False
    if str(after.get("signal_quality") or "").lower() == "noise":
        return True
    content_tags = after.get("content_tags")
    return (
        isinstance(content_tags, dict)
        and str(content_tags.get("signal_quality") or "").lower() == "noise"
    )


#: Governance events counted by ``parameter_promotions``. The audit trail
#: distinguishes promote (``PARAMS_UPDATED``), proposal lifecycle
#: (``TUNER_PROPOSAL_CREATED`` / ``_REJECTED``), the post-promotion
#: degrade signal (``PARAMETERS_DEGRADED``), and the tier-1 autonomous
#: actions (``PARAMS_AUTO_PROMOTED`` / ``PARAMS_AUTO_ROLLED_BACK`` — see
#: ``docs/design/adr-autonomy-ladder.md``; emitted *in addition to* the
#: underlying ``PARAMS_UPDATED`` / degrade events, so the strip shows
#: both the action and its autonomous provenance).
_PROMOTION_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.PARAMS_UPDATED,
    EventType.TUNER_PROPOSAL_CREATED,
    EventType.TUNER_PROPOSAL_REJECTED,
    EventType.PARAMETERS_DEGRADED,
    EventType.PARAMS_AUTO_PROMOTED,
    EventType.PARAMS_AUTO_ROLLED_BACK,
)


def _compute_parameter_promotions(
    event_log: EventLog,
    *,
    since: datetime,
    group_by: str,  # noqa: ARG001 — strip groups by event type, not a payload axis
    limit: int,
) -> dict[str, dict[str, _CountBucket]]:
    """Governance event counts per (event-type group, bucket).

    Unlike the other metrics, ``group_by`` is ignored for the series
    key: the natural grouping for a governance-events strip is the event
    *type* (promote / created / rejected / degraded), so each event type
    becomes its own series. This is the annotated-events strip the
    dashboard renders for metric 5.
    """
    buckets: dict[str, dict[str, _CountBucket]] = defaultdict(
        lambda: defaultdict(_CountBucket)
    )
    for event_type in _PROMOTION_EVENT_TYPES:
        events = event_log.get_events(
            event_type=event_type, since=since, limit=limit
        )
        series_key = event_type.value
        for event in events:
            buckets[series_key][_bucket_key(event.occurred_at)].count += 1
    return buckets


# ---------------------------------------------------------------------------
# Series assembly — turn bucket accumulators into ordered points
# ---------------------------------------------------------------------------


def _ratio_series(
    buckets: dict[str, dict[str, _RatioBucket]],
) -> list[TimeseriesSeries]:
    series: list[TimeseriesSeries] = []
    for group_key, by_bucket in sorted(buckets.items()):
        points = [
            TimeseriesPoint(
                bucket_start=bucket_start,
                value=round(b.numerator / b.denominator, 4)
                if b.denominator
                else 0.0,
                sample_count=b.samples,
            )
            for bucket_start, b in sorted(by_bucket.items())
        ]
        series.append(TimeseriesSeries(group_key=group_key, points=points))
    return series


def _mean_count_series(
    buckets: dict[str, dict[str, _MeanCountBucket]],
) -> list[TimeseriesSeries]:
    series: list[TimeseriesSeries] = []
    for group_key, by_bucket in sorted(buckets.items()):
        points = [
            TimeseriesPoint(
                bucket_start=bucket_start,
                value=round(b.confidence_sum / b.mean_samples, 4)
                if b.mean_samples
                else 0.0,
                sample_count=b.count,
            )
            for bucket_start, b in sorted(by_bucket.items())
        ]
        series.append(TimeseriesSeries(group_key=group_key, points=points))
    return series


def _count_series(
    buckets: dict[str, dict[str, _CountBucket]],
) -> list[TimeseriesSeries]:
    series: list[TimeseriesSeries] = []
    for group_key, by_bucket in sorted(buckets.items()):
        points = [
            TimeseriesPoint(
                bucket_start=bucket_start,
                value=float(b.count),
                sample_count=b.count,
            )
            for bucket_start, b in sorted(by_bucket.items())
        ]
        series.append(TimeseriesSeries(group_key=group_key, points=points))
    return series


def compute_timeseries(
    event_log: EventLog,
    *,
    metric: str,
    days: int = 30,
    group_by: str = GROUP_BY_NONE,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> TimeseriesResult:
    """Compute one named improvement metric as a daily time series.

    Read-only — never mutates a store. The window is the last ``days``
    calendar days ending now (UTC); buckets with no contributing events
    are omitted from each series.

    Args:
        event_log: Source EventLog (Operational Plane).
        metric: One of :data:`VALID_METRICS`.
        days: Look-back window in days (must be positive).
        group_by: ``"domain"`` | ``"intent_family"`` | ``"none"``.
            ``parameter_promotions`` ignores this and groups by event
            type; ``advisory_fitness`` always groups under ``"all"``
            because advisory events carry no grouping dimension.
        limit: Per-event-type scan cap.

    Returns:
        A :class:`TimeseriesResult` with one :class:`TimeseriesSeries`
        per resolved group key, each holding bucket points sorted by
        ``bucket_start`` ascending.

    Raises:
        ValueError: on an unknown ``metric`` / ``group_by`` or a
            non-positive ``days`` (the route layer maps this to 422).
    """
    _validate_args(metric=metric, group_by=group_by, days=days)
    since = datetime.now(tz=UTC) - timedelta(days=days)

    series: list[TimeseriesSeries]
    if metric == METRIC_PACK_SUCCESS_RATE:
        series = _ratio_series(
            _compute_pack_success_rate(
                event_log, since=since, group_by=group_by, limit=limit
            )
        )
    elif metric == METRIC_REFERENCE_RATE:
        series = _ratio_series(
            _compute_reference_rate(
                event_log, since=since, group_by=group_by, limit=limit
            )
        )
    elif metric == METRIC_ADVISORY_FITNESS:
        series = _mean_count_series(
            _compute_advisory_fitness(
                event_log, since=since, group_by=group_by, limit=limit
            )
        )
    elif metric == METRIC_NOISE_TAG_VOLUME:
        series = _count_series(
            _compute_noise_tag_volume(
                event_log, since=since, group_by=group_by, limit=limit
            )
        )
    else:  # METRIC_PARAMETER_PROMOTIONS
        series = _count_series(
            _compute_parameter_promotions(
                event_log, since=since, group_by=group_by, limit=limit
            )
        )

    logger.debug(
        "timeseries_computed",
        metric=metric,
        days=days,
        group_by=group_by,
        series=len(series),
    )
    return TimeseriesResult(
        metric=metric,
        bucket="day",
        group_by=group_by,
        days=days,
        series=series,
    )


def list_metrics() -> Iterable[str]:
    """Return the supported metric names in priority order."""
    return VALID_METRICS


__all__ = [
    "GROUP_BY_DOMAIN",
    "GROUP_BY_INTENT_FAMILY",
    "GROUP_BY_NONE",
    "VALID_METRICS",
    "TimeseriesPoint",
    "TimeseriesResult",
    "TimeseriesSeries",
    "compute_timeseries",
    "list_metrics",
]
