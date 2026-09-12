"""Rule-based tuner — deterministic parameter proposals from OutcomeEvents.

Split into three layers so each is testable in isolation:

1. :func:`aggregate_outcomes` — pure: ``list[OutcomeEvent]`` →
   ``list[AggregatedOutcomes]``, grouped by learning-axis cell.
2. :class:`TuningRule` + :func:`apply_rules` — pure: aggregates +
   rules → ``list[ParameterProposal]``, each with a deterministic
   ``proposal_id`` derived from rule name + scope.
3. :class:`RuleTuner` — orchestrator: a trailing-window read from
   :class:`OutcomeStore`, aggregation, rule application, and proposal
   persistence to :class:`TunerStateStore`.

Rules are data, not code. The built-in :data:`DEFAULT_RULES` covers the
most common retrieval-tuning triggers; callers can extend with custom
:class:`TuningRule` instances. This mirrors the
``ExtractionDispatcher``'s pattern — the dispatcher is generic; tier
definitions are data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from trellis.schemas.outcome import (
    GRAPH_SEARCH_COMPONENT_ID,
    OutcomeEvent,
)
from trellis.schemas.parameters import ParameterProposal, ParameterScope

if TYPE_CHECKING:
    from trellis.stores.base.outcome import OutcomeStore
    from trellis.stores.base.tuner_state import TunerStateStore

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AggregatedOutcomes:
    """Per-cell aggregate computed from a batch of :class:`OutcomeEvent`.

    ``scope`` identifies the learning-axis cell
    ``(component_id, domain, intent_family, tool_name)`` — rules key
    off this. Counters are totals; :attr:`success_rate` and
    :attr:`mean_latency_ms` are derived.

    ``metric_sums`` / ``metric_counts`` track the freeform
    ``OutcomeEvent.outcome.metrics`` dict so rules can consult
    component-specific numeric signals (e.g. ``precision``, ``recall``)
    without forcing every rule to crunch raw events.
    """

    scope: ParameterScope
    count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0.0
    items_served_total: int = 0
    items_referenced_total: int = 0
    metric_sums: dict[str, float] = field(default_factory=dict)
    metric_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.success_count / self.count if self.count else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.count if self.count else 0.0

    @property
    def reference_rate(self) -> float | None:
        """Ratio of items_referenced to items_served across the cell.

        Returns ``None`` when no sample reported a serving count, for the
        same reason :meth:`mean_metric` does — a rule must be able to
        tell "no signal" from "signal = 0.0". The ``0.0`` this used to
        return was indistinguishable from a real cell where nothing was
        cited, and every producer of these rows leaves ``items_served``
        unset (an agent cites what helped, it does not enumerate what it
        was shown), so ``reference_rate lt 0.2`` fired on a constant.
        """
        if self.items_served_total <= 0:
            return None
        return self.items_referenced_total / self.items_served_total

    def mean_metric(self, key: str) -> float | None:
        """Return the mean of a metric key across samples that reported it.

        Returns ``None`` when no samples reported the key so rules can
        distinguish "no signal" from "signal = 0.0".
        """
        n = self.metric_counts.get(key, 0)
        if n <= 0:
            return None
        return self.metric_sums.get(key, 0.0) / n


def aggregate_outcomes(
    outcomes: Sequence[OutcomeEvent],
) -> list[AggregatedOutcomes]:
    """Group outcomes by ``(component_id, domain, intent_family, tool_name)``.

    Unknown axes (``None``) are preserved — they form their own cells.
    Rules that want to back off to wider scopes re-aggregate with the
    narrowed axes set to ``None``.
    """
    buckets: dict[
        tuple[str, str | None, str | None, str | None], AggregatedOutcomes
    ] = {}

    for event in outcomes:
        key = (
            event.component_id,
            event.domain,
            event.intent_family,
            event.tool_name,
        )
        agg = buckets.get(key)
        if agg is None:
            agg = AggregatedOutcomes(
                scope=ParameterScope(
                    component_id=event.component_id,
                    domain=event.domain,
                    intent_family=event.intent_family,
                    tool_name=event.tool_name,
                ),
            )
            buckets[key] = agg

        agg.count += 1
        if event.outcome.success:
            agg.success_count += 1
        agg.total_latency_ms += event.outcome.latency_ms
        if event.outcome.items_served is not None:
            agg.items_served_total += event.outcome.items_served
        if event.outcome.items_referenced is not None:
            agg.items_referenced_total += event.outcome.items_referenced
        for metric_key, metric_value in event.outcome.metrics.items():
            agg.metric_sums[metric_key] = (
                agg.metric_sums.get(metric_key, 0.0) + metric_value
            )
            agg.metric_counts[metric_key] = agg.metric_counts.get(metric_key, 0) + 1

    return list(buckets.values())


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


_CONDITION_OPS: Final = {"lt", "lte", "gt", "gte", "eq"}


@dataclass(frozen=True, slots=True)
class TuningRule:
    """A deterministic rule mapping aggregate stats to a parameter proposal.

    A rule fires for a cell when:

    * ``agg.scope.component_id == target_component_id``
    * ``agg.count >= min_sample_size``
    * ``agg.items_served_total >= min_items_served``
    * ``getattr(agg, condition_key) <condition_op> condition_value``

    **The two floors measure different things and neither subsumes the
    other.** ``min_sample_size`` counts *rows* — how many graded packs
    contributed to the cell — and guards against one unusual pack
    speaking for a whole cell. ``min_items_served`` counts the
    denominator a *rate* is actually computed over: a cell built from
    three packs that served 26 items between them supports a rate
    estimate that one built from ten packs serving 14 does not. A rule
    keyed on ``reference_rate`` that floors only on row count is
    floored on the wrong number, and on the reference deployment that
    admitted a 0-of-4 cell whose zero is what the base rate predicts
    57% of the time.

    When a rule fires it emits a proposal setting ``proposed_param``
    to ``proposed_value`` for the cell's full scope.  Rules do not
    know the current value — the promotion step reads the active
    snapshot (if any) and records the delta as ``effect_size`` for the
    policy gate.

    ``condition_key`` may reference any property on
    :class:`AggregatedOutcomes` (including computed properties like
    ``success_rate`` and ``reference_rate``) or a metric key via the
    ``metric:<key>`` prefix (e.g. ``metric:precision``).
    """

    name: str
    target_component_id: str
    min_sample_size: int
    condition_key: str
    condition_op: str
    condition_value: float
    proposed_param: str
    proposed_value: float | int | str | bool
    description: str = ""
    #: Floor on ``items_served_total`` — the denominator of a rate
    #: statistic. Defaults to ``0`` (no floor), so a rule keyed on a
    #: non-rate statistic, and every rule written before this field
    #: existed, behaves exactly as before.
    min_items_served: int = 0

    def __post_init__(self) -> None:
        if self.condition_op not in _CONDITION_OPS:
            msg = (
                f"Unknown condition_op {self.condition_op!r}; "
                f"must be one of {sorted(_CONDITION_OPS)}"
            )
            raise ValueError(msg)
        if self.min_sample_size < 1:
            msg = f"min_sample_size must be >= 1, got {self.min_sample_size}"
            raise ValueError(msg)
        if self.min_items_served < 0:
            msg = f"min_items_served must be >= 0, got {self.min_items_served}"
            raise ValueError(msg)

    def _stat(self, agg: AggregatedOutcomes) -> float | None:
        if self.condition_key.startswith("metric:"):
            metric_key = self.condition_key.removeprefix("metric:")
            return agg.mean_metric(metric_key)
        value = getattr(agg, self.condition_key, None)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        return None

    def applies(self, agg: AggregatedOutcomes) -> bool:
        """Does this rule fire for the given aggregate?"""
        if agg.scope.component_id != self.target_component_id:
            return False
        if agg.count < self.min_sample_size:
            return False
        if agg.items_served_total < self.min_items_served:
            return False
        stat = self._stat(agg)
        if stat is None:
            return False
        return _compare(stat, self.condition_op, self.condition_value)


def _compare(stat: float, op: str, value: float) -> bool:
    if op == "lt":
        return stat < value
    if op == "lte":
        return stat <= value
    if op == "gt":
        return stat > value
    if op == "gte":
        return stat >= value
    if op == "eq":
        return stat == value
    return False  # unreachable; guarded by TuningRule.__post_init__


def _deterministic_proposal_id(rule: TuningRule, scope: ParameterScope) -> str:
    """Build a deterministic proposal_id from rule + scope.

    Same rule + same scope → same id, regardless of when the tuner
    runs.  This is the idempotency key: re-running the tuner on the
    same window never produces duplicate proposals.
    """
    payload = "|".join(
        [
            rule.name,
            rule.target_component_id,
            rule.condition_key,
            rule.condition_op,
            str(rule.condition_value),
            rule.proposed_param,
            repr(rule.proposed_value),
            scope.component_id,
            scope.domain or "",
            scope.intent_family or "",
            scope.tool_name or "",
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:26]
    return f"prop_{digest}"


def apply_rules(
    aggregates: Sequence[AggregatedOutcomes],
    rules: Sequence[TuningRule],
    *,
    tuner: str = "rule_tuner",
) -> list[ParameterProposal]:
    """Apply ``rules`` to every aggregate, return one proposal per firing.

    Each proposal carries:

    * ``tuner`` — caller-supplied name (default ``"rule_tuner"``).
    * ``scope`` — full scope from the aggregate.
    * ``proposed_values`` — single-key dict ``{rule.proposed_param:
      rule.proposed_value}``.
    * ``sample_size`` — ``agg.count`` at proposal time.
    * ``proposal_id`` — deterministic (see
      :func:`_deterministic_proposal_id`); re-running the tuner over
      the same data gives the same id.

    ``effect_size`` is left unset here; the promotion step computes it
    against the active snapshot.
    """
    proposals: list[ParameterProposal] = []
    for agg in aggregates:
        for rule in rules:
            if not rule.applies(agg):
                continue
            proposals.append(
                ParameterProposal(
                    proposal_id=_deterministic_proposal_id(rule, agg.scope),
                    scope=agg.scope,
                    proposed_values={rule.proposed_param: rule.proposed_value},
                    tuner=tuner,
                    sample_size=agg.count,
                    notes=rule.description or rule.name,
                    metadata={"rule_name": rule.name},
                )
            )
    return proposals


# ---------------------------------------------------------------------------
# Default rule set
# ---------------------------------------------------------------------------


#: Conservative starter rules — **one**, because one rule that can
#: discriminate is worth more than three that cannot.
#:
#: Every threshold here is calibrated against a *measured* base rate
#: rather than chosen as a round number, and the floors are set so the
#: rule cannot fire on a cell whose deviation is what chance predicts.
#: The measurement is the 30 days to 2026-09-12 on the reference
#: deployment: 53 pack-targeted feedback events fanned out to 152
#: per-strategy outcome rows over 70 learning-axis cells.
#:
#: **Two rules that shipped here were retired rather than retuned**,
#: and the reasons are different:
#:
#: ``rrf_low_success_reduce_smoothing`` was **unreachable by
#: construction**. It targeted
#: :data:`~trellis.schemas.outcome.RRF_RERANKER_COMPONENT_ID`, and
#: :data:`~trellis.schemas.outcome.COMPONENT_ID_BY_SOURCE_STRATEGY`
#: deliberately excludes rerankers — a reranker reorders every
#: candidate and serves none under its own name, so no outcome row can
#: ever carry that component id and no aggregate can ever match.  (RRF
#: still *reads* parameters under that id; nothing writes outcomes
#: under it.  A tuner proposal for RRF needs a producer first.)
#:
#: ``keyword_low_success_halve_half_life`` was **unmeasurable at this
#: volume**, twice over.  It keyed on ``success_rate``, which on this
#: path is the *pack's* success bit repeated onto every strategy row
#: (see ``_emit_strategy_outcomes``) — measured 0.1915 keyword /
#: 0.1923 graph / 0.1887 semantic against a pack-wide 0.1908, a spread
#: of 0.0036, so it separates nothing.  Re-sourcing it to
#: ``reference_rate`` does not rescue it: keyword's base rate is
#: 0.0346 (15 referenced / 433 served) and its entire per-cell
#: distribution sits between 0.0000 and 0.0455, so a threshold at half
#: the base rate fires on 1 of 5 qualifying cells and that one is
#: 0-of-26 — which a 0.0346 base rate produces 40% of the time by
#: chance.  Any threshold high enough to fire on more fires on all
#: five.  Constant-true or noise-triggered, with nothing in between;
#: the axis is uniformly weakly-cited rather than weakly-cited in
#: particular cells, so a per-cell rule is the wrong instrument for it.
#:
#: Replace / extend via the ``rules`` argument on :class:`RuleTuner`.
#: A new default rule needs the same thing these thresholds have: a
#: measured base rate to be relative to, and a floor on the statistic's
#: own denominator.
DEFAULT_RULES: Final[tuple[TuningRule, ...]] = (
    TuningRule(
        name="graph_low_reference_rate_tighten_domain_boost",
        target_component_id=GRAPH_SEARCH_COMPONENT_ID,
        # >= 3 graded packs contributed, so one unusual pack cannot
        # speak for the cell.  On today's data the served floor is the
        # binding one for this component; this guards the case it
        # cannot see — a single pack that served 20+ graph items.
        min_sample_size=3,
        # The rate's own denominator.  The two measured cells this
        # excludes served 4 and 6 items; the 4-serving one reports zero
        # citations, which GraphSearch's own base rate produces 57% of
        # the time, so admitting it would propose a parameter change off
        # an absence of evidence.
        min_items_served=20,
        condition_key="reference_rate",
        condition_op="lt",
        # Half the measured base rate of 0.1317 (27 referenced / 205
        # served).  Absolute against an *unmeasured* base rate is the
        # shape that broke the demotion gate (#336); this one is
        # absolute against a measured one, and is restated whenever the
        # base rate is re-measured.  Over the 2 qualifying cells it
        # fires on 1 — the cell serving 42 graph items for a single
        # citation, which under a 0.1317 base rate has probability
        # 0.020.  The cell it spares cites at 0.1905, above base.
        condition_value=0.07,
        proposed_param="domain_match_boost",
        # Unchanged: the previous module-level default, already
        # reviewed.  Calibrating a threshold is not licence to also
        # introduce a value nobody has looked at.
        proposed_value=1.15,
        description=(
            "Graph results rarely referenced — pull domain_match_boost "
            "back toward neutral so domain-matched noise stops crowding "
            "the pack."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


#: Width of the trailing window :class:`RuleTuner` aggregates over when
#: the caller names no explicit ``since``.
#:
#: 30 days, matched to the window every other measurement of this
#: deployment is taken over (``analyze value``, ``analyze replay``, the
#: demotion gate) so a sample floor calibrated against one of those
#: means the same thing here.  It is emphatically **not** "everything
#: since the last pass": a rule's ``min_sample_size`` is checked against
#: one pass's aggregate, so an incremental read makes the floor a
#: statement about cron cadence — on the reference deployment a nightly
#: pass would aggregate ~5 strategy rows against a floor of 30, and no
#: cell on any cadence could satisfy it (#562).
DEFAULT_WINDOW_DAYS: Final = 30


#: Statuses that indicate a proposal has moved past the tuner's reach.
#: The tuner refuses to overwrite these on re-run — only ``"pending"``
#: proposals are eligible to be refreshed with new sample sizes.
_TERMINAL_STATUSES: Final = frozenset({"canary", "promoted", "rejected"})


class RuleTuner:
    """Drives one tuning pass: read outcomes, aggregate, propose, persist.

    Reads from an :class:`~trellis.stores.base.outcome.OutcomeStore`,
    persists proposals to a
    :class:`~trellis.stores.base.tuner_state.TunerStateStore`, and
    tracks a per-tuner cursor so future runs can start from the
    newest event seen.

    Idempotency comes from two layers:

    * ``proposal_id`` is deterministic (see
      :func:`_deterministic_proposal_id`) — re-running over the same
      window produces the same ids.
    * Proposals in a terminal status (``canary`` / ``promoted`` /
      ``rejected``) are **never overwritten** by ``run()`` even if the
      deterministic id matches, so a downstream promotion decision
      can't be silently reverted by a later tuner pass.

    Args:
        outcome_store: Signal source.
        tuner_state_store: Proposal + cursor storage.
        tuner_name: Logical name stored on proposals and cursor.
        rules: Rule set; defaults to :data:`DEFAULT_RULES`.
        batch_limit: Per-call ``OutcomeStore.query(limit=...)`` cap.
        window_days: Width of the trailing window each pass aggregates
            over; see :data:`DEFAULT_WINDOW_DAYS`.
    """

    def __init__(
        self,
        outcome_store: OutcomeStore,
        tuner_state_store: TunerStateStore,
        *,
        tuner_name: str = "rule_tuner",
        rules: Sequence[TuningRule] = DEFAULT_RULES,
        batch_limit: int = 5000,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        if window_days < 1:
            msg = f"window_days must be >= 1, got {window_days}"
            raise ValueError(msg)
        self._outcomes = outcome_store
        self._state = tuner_state_store
        self._tuner_name = tuner_name
        self._rules: tuple[TuningRule, ...] = tuple(rules)
        self._batch_limit = batch_limit
        self._window = timedelta(days=window_days)

    # -- accessors -----------------------------------------------------------

    @property
    def tuner_name(self) -> str:
        return self._tuner_name

    @property
    def rules(self) -> tuple[TuningRule, ...]:
        return self._rules

    @property
    def window(self) -> timedelta:
        """The trailing window each pass aggregates over."""
        return self._window

    # -- main pass -----------------------------------------------------------

    def run(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ParameterProposal]:
        """Run one tuning pass.

        Reads a **trailing window**, not "everything since the last
        pass". A rule's ``min_sample_size`` is checked against the
        aggregate built in *one* pass, so reading incrementally makes
        the floor mean "this many arrived since the job last ran" — a
        statement about cron cadence rather than about evidence, and one
        no cell on the reference deployment could satisfy (#562: ~5
        strategy rows a day against a floor of 30; the largest 30-day
        cell holds 11).

        Re-reading an overlapping window is safe by construction and was
        always meant to be: ``proposal_id`` is deterministic in ``(rule,
        scope)``, so an outcome seen twice yields the *same* proposal
        rather than a duplicate, terminal-status proposals are never
        overwritten, and a ``pending`` one is refreshed with the larger
        ``sample_size`` — which is exactly what a widening window should
        do to it.

        Args:
            since: Explicit lower bound (inclusive) on event time; wins
                over the window. When ``None``, the window is used.
            until: Optional upper bound (inclusive). Defaults to "now"
                at the store level when unset.

        Returns the list of proposals that were created or updated
        this run — excludes proposals that matched an existing
        terminal-status record.
        """
        effective_since = since
        if effective_since is None:
            anchor = until if until is not None else datetime.now(UTC)
            effective_since = anchor - self._window

        outcomes = self._outcomes.query(
            since=effective_since,
            until=until,
            limit=self._batch_limit,
        )
        if not outcomes:
            logger.debug(
                "rule_tuner.no_outcomes",
                tuner=self._tuner_name,
                since=effective_since.isoformat() if effective_since else None,
            )
            return []

        aggregates = aggregate_outcomes(outcomes)
        proposals = apply_rules(aggregates, self._rules, tuner=self._tuner_name)

        persisted: list[ParameterProposal] = []
        skipped_terminal = 0
        for proposal in proposals:
            existing = self._state.get_proposal(proposal.proposal_id)
            if existing is not None and existing.status in _TERMINAL_STATUSES:
                skipped_terminal += 1
                continue
            self._state.put_proposal(proposal)
            persisted.append(proposal)

        # Written as a record of the newest outcome this pass saw, not as
        # the next pass's lower bound — see the window note in the
        # docstring. Kept because "when did this tuner last see fresh
        # signal?" is the question an operator asks of a silent tuner.
        latest = max(o.occurred_at for o in outcomes)
        self._state.set_cursor(self._tuner_name, latest.isoformat())

        logger.info(
            "rule_tuner.run_complete",
            tuner=self._tuner_name,
            outcomes_scanned=len(outcomes),
            aggregates=len(aggregates),
            proposals_persisted=len(persisted),
            proposals_skipped_terminal=skipped_terminal,
            cursor=latest.isoformat(),
        )
        return persisted
