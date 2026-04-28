"""Post-promotion monitoring and optional auto-rollback.

Closes Gap 2.2. :func:`promote_proposal` turns a proposal into an
active :class:`ParameterSet`; nothing downstream watches what happens
next. A parameter that wins at ``n=10`` and degrades at ``n=100`` can
only be undone by a new manual proposal.

This module fills the missing half. Given a promoted
``params_version``:

1. Look up the :class:`EventType.PARAMS_UPDATED` audit event to recover
   the ``baseline_version`` it replaced and the promotion timestamp.
2. Load the promoted :class:`ParameterSet` from the
   :class:`ParameterStore` to get its :class:`ParameterScope`.
3. Query :class:`OutcomeStore` for post-promotion outcomes at that
   ``params_version`` (since promotion) and pre-promotion outcomes at
   ``baseline_version`` (an equivalent window before promotion).
4. Compare aggregate ``success_rate``. If the drop exceeds
   :attr:`PostPromotionPolicy.regression_threshold` and the post sample
   is at least :attr:`PostPromotionPolicy.min_samples_post_promote`,
   emit :class:`EventType.PARAMETERS_DEGRADED`.
5. When :attr:`PostPromotionPolicy.auto_demote` is ``True`` and a
   baseline exists, write a new :class:`ParameterSet` carrying the
   baseline's values (``source="tuner:rollback"``) and emit a matching
   :class:`EventType.PARAMS_UPDATED` with ``reverted_from`` pointing at
   the degraded version.

Signal-only is the default because automatic demotion on noisy traffic
can thrash real wins; operators opt in explicitly, matching the posture
taken for advisory drift in Gap 2.4. Shadow evaluation mode (serve both
snapshots, compare in situ) is deliberately deferred — a separate gap,
bigger design, no design partner pushing today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import utc_now
from trellis.schemas.parameters import ParameterSet
from trellis.stores.base.event_log import EventLog, EventType

if TYPE_CHECKING:
    from trellis.schemas.parameters import ParameterScope
    from trellis.stores.base.outcome import OutcomeStore
    from trellis.stores.base.parameter import ParameterStore

logger = structlog.get_logger(__name__)


#: Default post-promotion monitor posture — 20 samples, 10% absolute drop
#: in success rate, and a 7-day lookback window on either side. Signal
#: only; auto_demote is opt-in.
DEFAULT_MIN_SAMPLES_POST_PROMOTE = 20
DEFAULT_REGRESSION_THRESHOLD = 0.10
DEFAULT_LOOKBACK_DAYS = 7


@dataclass(frozen=True, slots=True)
class PostPromotionPolicy:
    """Thresholds for post-promotion monitoring.

    Attributes:
        min_samples_post_promote: Minimum post-promotion outcome count
            before a regression verdict is allowed. Below this the
            verdict is ``"insufficient_samples"``. Guards against
            demoting on n=2 noise.
        regression_threshold: Absolute drop in success rate
            (``baseline - post``) that flags a regression. ``0.10``
            means "10 percentage points" since ``success_rate`` is in
            ``[0, 1]``.
        auto_demote: When ``True``, a degraded verdict also writes a
            rollback :class:`ParameterSet` restoring the baseline's
            values. Default ``False`` — signal only. Operators opt in
            once they trust the signal on their traffic.
        lookback_window: Window size for both post-promotion and
            baseline evaluation. Keeps the comparison recent so a
            parameter that won six months ago against a long-dead
            baseline isn't demoted on ancient outcomes.
    """

    min_samples_post_promote: int = DEFAULT_MIN_SAMPLES_POST_PROMOTE
    regression_threshold: float = DEFAULT_REGRESSION_THRESHOLD
    auto_demote: bool = False
    lookback_window: timedelta = field(
        default_factory=lambda: timedelta(days=DEFAULT_LOOKBACK_DAYS)
    )


@dataclass(frozen=True, slots=True)
class PostPromotionReport:
    """Outcome of a single :func:`monitor_post_promotion` call."""

    params_version: str
    baseline_version: str | None
    scope: ParameterScope | None
    post_samples: int
    baseline_samples: int
    post_success_rate: float | None
    baseline_success_rate: float | None
    #: Positive value means the post-promotion success rate dropped
    #: vs. baseline (degradation). ``None`` when either side has zero
    #: samples so the subtraction would be undefined.
    degradation: float | None
    #: Verdict categories:
    #:
    #: * ``"ok"`` — sufficient samples, no regression beyond threshold.
    #: * ``"degraded"`` — sufficient samples, regression >= threshold.
    #: * ``"insufficient_samples"`` — too few post-promotion outcomes.
    #: * ``"no_baseline"`` — no baseline version recorded on the
    #:   promotion event (bootstrap case).
    #: * ``"no_promotion_event"`` — no ``PARAMS_UPDATED`` event found
    #:   for this ``params_version``; we have nothing to anchor against.
    #: * ``"unknown_version"`` — the ``params_version`` itself is not
    #:   present in the :class:`ParameterStore`.
    verdict: str
    #: Action taken: ``"none"`` (no regression / blocked / insufficient),
    #: ``"event_only"`` (regression signalled but ``auto_demote`` off),
    #: or ``"demoted"`` (rollback snapshot written).
    action: str
    demoted_version: str | None = None
    reason: str | None = None


def monitor_post_promotion(  # noqa: PLR0911 — verdict space is flat by design; each early return maps to one named verdict.
    params_version: str,
    *,
    parameter_store: ParameterStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: PostPromotionPolicy | None = None,
    source: str = "tuner.rollback",
    now: datetime | None = None,
) -> PostPromotionReport:
    """Check post-promotion effectiveness for one ``params_version``.

    See the module docstring for the full flow. Returns a
    :class:`PostPromotionReport` and emits zero or more audit events
    on the :class:`EventLog` as a side effect.

    Args:
        params_version: The promoted snapshot to check.
        parameter_store: Used to resolve ``params_version`` -> scope,
            and (when demoting) to write the rollback snapshot.
        outcome_store: Queried for post-promotion and baseline outcomes.
        event_log: Queried to find the original promotion event; also
            the destination for :attr:`EventType.PARAMETERS_DEGRADED`
            and (on demotion) :attr:`EventType.PARAMS_UPDATED`.
        policy: Thresholds + auto-demote toggle.
        source: Event source label.
        now: Override the reference timestamp (tests).
    """
    effective_policy = policy or PostPromotionPolicy()
    now = now or utc_now()

    promoted_set = parameter_store.get(params_version)
    if promoted_set is None:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=None,
            scope=None,
            post_samples=0,
            baseline_samples=0,
            post_success_rate=None,
            baseline_success_rate=None,
            degradation=None,
            verdict="unknown_version",
            action="none",
            reason="params_version not found in parameter_store",
        )

    promotion_event, baseline_version = _load_promotion_event(event_log, params_version)
    if promotion_event is None:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=None,
            scope=promoted_set.scope,
            post_samples=0,
            baseline_samples=0,
            post_success_rate=None,
            baseline_success_rate=None,
            degradation=None,
            verdict="no_promotion_event",
            action="none",
            reason="no PARAMS_UPDATED event anchors this params_version",
        )

    promoted_at = promotion_event.occurred_at
    window = effective_policy.lookback_window
    post_since = promoted_at
    post_until = min(now, promoted_at + window)
    baseline_until = promoted_at
    baseline_since = promoted_at - window

    post_samples, post_success_rate = _aggregate_success_rate(
        outcome_store,
        params_version=params_version,
        since=post_since,
        until=post_until,
    )

    if post_samples < effective_policy.min_samples_post_promote:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=baseline_version,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=0,
            post_success_rate=post_success_rate,
            baseline_success_rate=None,
            degradation=None,
            verdict="insufficient_samples",
            action="none",
            reason=(
                f"post_samples={post_samples} < "
                f"min_samples_post_promote={effective_policy.min_samples_post_promote}"
            ),
        )

    if baseline_version is None:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=None,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=0,
            post_success_rate=post_success_rate,
            baseline_success_rate=None,
            degradation=None,
            verdict="no_baseline",
            action="none",
            reason="no baseline_version on promotion event (bootstrap case)",
        )

    baseline_samples, baseline_success_rate = _aggregate_success_rate(
        outcome_store,
        params_version=baseline_version,
        since=baseline_since,
        until=baseline_until,
    )

    if baseline_samples == 0 or baseline_success_rate is None:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=baseline_version,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=baseline_samples,
            post_success_rate=post_success_rate,
            baseline_success_rate=None,
            degradation=None,
            verdict="no_baseline",
            action="none",
            reason=(
                "baseline has zero outcomes in the comparable window "
                "— cannot compute regression"
            ),
        )

    assert post_success_rate is not None  # covered by min_samples guard
    degradation = baseline_success_rate - post_success_rate
    degraded = degradation >= effective_policy.regression_threshold

    if not degraded:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=baseline_version,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=baseline_samples,
            post_success_rate=post_success_rate,
            baseline_success_rate=baseline_success_rate,
            degradation=degradation,
            verdict="ok",
            action="none",
            reason=None,
        )

    # Degradation confirmed. Emit the signal, then optionally demote.
    event_log.emit(
        EventType.PARAMETERS_DEGRADED,
        source=source,
        entity_id=params_version,
        entity_type="parameter_set",
        payload={
            "params_version": params_version,
            "baseline_version": baseline_version,
            "scope": list(promoted_set.scope.key()),
            "post_samples": post_samples,
            "baseline_samples": baseline_samples,
            "post_success_rate": post_success_rate,
            "baseline_success_rate": baseline_success_rate,
            "degradation": degradation,
            "regression_threshold": effective_policy.regression_threshold,
            "auto_demote": effective_policy.auto_demote,
        },
    )
    logger.info(
        "parameters_degraded_detected",
        params_version=params_version,
        baseline_version=baseline_version,
        degradation=degradation,
        auto_demote=effective_policy.auto_demote,
    )

    if not effective_policy.auto_demote:
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=baseline_version,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=baseline_samples,
            post_success_rate=post_success_rate,
            baseline_success_rate=baseline_success_rate,
            degradation=degradation,
            verdict="degraded",
            action="event_only",
            reason=f"degradation={degradation:.4f} >= threshold",
        )

    baseline_set = parameter_store.get(baseline_version)
    if baseline_set is None:
        logger.warning(
            "rollback_baseline_missing",
            params_version=params_version,
            baseline_version=baseline_version,
        )
        return PostPromotionReport(
            params_version=params_version,
            baseline_version=baseline_version,
            scope=promoted_set.scope,
            post_samples=post_samples,
            baseline_samples=baseline_samples,
            post_success_rate=post_success_rate,
            baseline_success_rate=baseline_success_rate,
            degradation=degradation,
            verdict="degraded",
            action="event_only",
            reason=(
                "auto_demote requested but baseline snapshot missing from "
                "parameter_store — emitted event only"
            ),
        )

    rollback_set = ParameterSet(
        scope=promoted_set.scope,
        values=dict(baseline_set.values),
        source="tuner:rollback",
        notes=(
            f"Auto-rollback of {params_version} to "
            f"{baseline_version} (degradation={degradation:.4f})"
        ),
        metadata={
            "reverted_from": params_version,
            "restored_from_version": baseline_version,
            "degradation": degradation,
        },
    )
    stored_rollback = parameter_store.put(rollback_set)

    event_log.emit(
        EventType.PARAMS_UPDATED,
        source=source,
        entity_id=stored_rollback.params_version,
        entity_type="parameter_set",
        payload={
            "params_version": stored_rollback.params_version,
            "baseline_version": params_version,
            "scope": list(promoted_set.scope.key()),
            "proposed_values": dict(baseline_set.values),
            "baseline_values": dict(promoted_set.values),
            "reverted_from": params_version,
            "restored_from_version": baseline_version,
            "degradation": degradation,
            "tuner": "rollback",
            "force": False,
        },
    )
    logger.info(
        "parameters_auto_demoted",
        params_version=params_version,
        rollback_version=stored_rollback.params_version,
        baseline_version=baseline_version,
    )

    return PostPromotionReport(
        params_version=params_version,
        baseline_version=baseline_version,
        scope=promoted_set.scope,
        post_samples=post_samples,
        baseline_samples=baseline_samples,
        post_success_rate=post_success_rate,
        baseline_success_rate=baseline_success_rate,
        degradation=degradation,
        verdict="degraded",
        action="demoted",
        demoted_version=stored_rollback.params_version,
        reason=f"degradation={degradation:.4f} >= threshold; rolled back",
    )


def run_post_promotion_sweep(
    *,
    parameter_store: ParameterStore,
    outcome_store: OutcomeStore,
    event_log: EventLog,
    policy: PostPromotionPolicy | None = None,
    source: str = "tuner.rollback",
    since: datetime | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> list[PostPromotionReport]:
    """Sweep recent promotions and monitor each one.

    Iterates :class:`EventType.PARAMS_UPDATED` events (most recent first)
    and runs :func:`monitor_post_promotion` per unique ``params_version``.
    Skips events that are themselves rollbacks (``reverted_from`` set in
    the payload) to prevent re-demoting a rollback.

    Args:
        since: Only consider promotion events recorded at or after this
            timestamp. Defaults to ``now - 30 days``.
        limit: Upper bound on events scanned. Returned report list may
            be shorter after de-duplication and rollback filtering.
    """
    effective_policy = policy or PostPromotionPolicy()
    reports: list[PostPromotionReport] = []
    now = now or utc_now()
    default_since = now - timedelta(days=30)
    effective_since = since or default_since

    events = event_log.get_events(
        event_type=EventType.PARAMS_UPDATED,
        since=effective_since,
        limit=limit,
    )
    seen: set[str] = set()
    for event in events:
        payload = event.payload or {}
        # Skip rollbacks so we don't chase our own tail.
        if payload.get("reverted_from") is not None:
            continue
        params_version = payload.get("params_version") or event.entity_id
        if not params_version or params_version in seen:
            continue
        seen.add(params_version)
        reports.append(
            monitor_post_promotion(
                params_version,
                parameter_store=parameter_store,
                outcome_store=outcome_store,
                event_log=event_log,
                policy=effective_policy,
                source=source,
                now=now,
            )
        )
    return reports


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_promotion_event(
    event_log: EventLog, params_version: str
) -> tuple[Any, str | None]:
    """Return the ``PARAMS_UPDATED`` event for ``params_version`` and its baseline.

    Returns ``(event, baseline_version)`` or ``(None, None)`` when no
    matching event exists. When multiple events share the entity_id
    (rollbacks, theoretical re-promotions) the most recent non-rollback
    event wins.
    """
    events = event_log.get_events(
        event_type=EventType.PARAMS_UPDATED,
        entity_id=params_version,
        limit=50,
    )
    # Prefer the original promotion (not a rollback).
    for event in sorted(events, key=lambda e: e.occurred_at, reverse=True):
        payload = event.payload or {}
        if payload.get("reverted_from") is not None:
            continue
        return event, payload.get("baseline_version")
    return None, None


#: Upper bound on rows scanned per success-rate aggregation. A count()
#: pre-check detects saturation so the caller sees a loud warning rather
#: than a silently-biased rate. Very high-volume components should wait
#: for a store-side aggregation API rather than raising this number.
_AGGREGATE_SUCCESS_RATE_LIMIT = 10_000


def _aggregate_success_rate(
    outcome_store: OutcomeStore,
    *,
    params_version: str,
    since: datetime,
    until: datetime,
) -> tuple[int, float | None]:
    """Return ``(sample_count, success_rate)`` for a version+window.

    ``params_version`` alone anchors the comparison. Adding scope
    filters would be redundant (only one scope can hold a given
    ``params_version``) and would mix scope semantics: the
    :class:`OutcomeStore.query` contract treats a ``None`` filter as
    a wildcard, whereas :class:`ParameterScope` treats a ``None`` axis
    as "unset at that level, not wildcard". Filtering by the version
    sidesteps that entirely.

    Uses a ``count()`` probe before the full scan: when the count
    exceeds :data:`_AGGREGATE_SUCCESS_RATE_LIMIT` we log a warning so
    operators know the returned rate is a sample, not the population.
    The actual query still caps at the limit — callers get a rate
    rather than nothing, but with a visible signal that it's partial.
    """
    total = outcome_store.count(
        params_version=params_version,
        since=since,
        until=until,
    )
    if total == 0:
        return 0, None
    if total > _AGGREGATE_SUCCESS_RATE_LIMIT:
        logger.warning(
            "post_promotion_sample_truncated",
            params_version=params_version,
            total=total,
            limit=_AGGREGATE_SUCCESS_RATE_LIMIT,
        )
    outcomes = outcome_store.query(
        params_version=params_version,
        since=since,
        until=until,
        limit=_AGGREGATE_SUCCESS_RATE_LIMIT,
    )
    if not outcomes:
        return 0, None
    successes = sum(1 for o in outcomes if o.outcome.success)
    return len(outcomes), successes / len(outcomes)
