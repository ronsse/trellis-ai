"""Advisory generator — deterministic analysis of outcome data.

Generates :class:`Advisory` objects by analyzing the correlation between
pack contents and task outcomes.  All analysis is statistical — no LLM
is used.  Advisory text is template-generated from findings.

The five analysis methods correspond to the ADR categories:

1. **Entity correlation** — entities disproportionately in successes
2. **Step-pattern mining** — trace step patterns in successes vs failures
3. **Scope analysis** — pack breadth correlation with outcome
4. **Anti-pattern detection** — patterns disproportionately in failures
5. **Query improvement** — query terms that lead to high-scoring packs

**Every advisory is a comparison, so it needs two arms.** ``effect_size``
is ``success_rate_with - success_rate_without``, which is only a
statement about the thing being advised when packs *without* it were
also observed. Four of the five methods previously computed
``rate_without`` inline with an ``else 0.0`` fallback, so a feature
present on **every** pack in the window scored
``effect_size == success_rate_with`` — the deployment's overall success
rate wearing a causal claim (#383). Both arms now go through
:meth:`AdvisoryGenerator._supported_effect`, which defers the arithmetic
to :func:`trellis.retrieve.effectiveness.lift_vs_baseline` (the
authoritative zero-sample policy) and returns **nothing** when the
comparison arm is too small to support a claim. ``_scope_analysis`` is
the exception and always was: its two arms are disjoint pack-count bins,
each already gated at ``min_sample_size``, so it never had the fallback.

Measured against the reference deployment's 30-day window (18 packs with
feedback, 3 successes) the gate is doing real work and is not a blanket
refusal:

===========  =======  ==========  ===============  ==============
finding      with     without     before           after
===========  =======  ==========  ===============  ==============
``semantic``  18       0          ``+0.167``       refused, lift 0
``graph``     17       1          ``+0.176``       refused, arm=1
``keyword``    9       9          ``-0.333``       ``-0.333``
4 entities     5      13          ``-0.231``       ``-0.231``
===========  =======  ==========  ===============  ==============

The two refusals are exactly the two findings with no usable comparison
arm; the five with an arm are unchanged. An ``effect_size`` that can only
read back the window's success rate is a constant, and the ``keyword``
row is the evidence that this one is not.

**Advisory ids are derived from the finding, not minted per run.** The
generator used the ``Advisory.advisory_id`` ULID default, so each nightly
pass wrote a *new* row for a finding it had already written — 51 rows
carrying 29 distinct messages and roughly 5 underlying findings on the
reference deployment, growing without bound. The append also split every
finding's presentation count across a fresh id each night, so no advisory
could accumulate the presentations
:func:`~trellis.retrieve.effectiveness.analyze_advisory_effectiveness`
scores on — a second, independent reason the fitness loop cannot
self-correct. :meth:`AdvisoryGenerator._stable_id` keys the id on the
finding's subject so ``put_many`` replaces in place, which is what the
call site's comment always claimed it did.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.retrieve.effectiveness import lift_vs_baseline
from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    AdvisoryStatus,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

logger = structlog.get_logger(__name__)

# --- Thresholds ---
#: Minimum observations required of *each* arm. Applied to the comparison
#: ("without") arm as well as the presented ("with") arm: the generator
#: already declares that fewer than this many observations is not
#: evidence, and there is no reason that judgement is weaker on the side
#: it is being compared against. This is deliberately the same number
#: rather than a new tunable — a second threshold would need its own base
#: rate before it meant anything.
_MIN_SAMPLE_SIZE = 5
_MIN_EFFECT_SIZE = 0.15
_SUCCESS_RATING_THRESHOLD = SUCCESS_RATING_THRESHOLD
_CONFIDENCE_SCALE = 0.1  # multiplied by sample_size to cap at 1.0
_MIN_WORD_LENGTH = 3
_SCOPE_SMALL = 5
_SCOPE_LARGE = 15
_MIN_BIN_COUNT = 2
#: How many pack ids to carry as evidence on one advisory. Enough for a
#: reviewer to spot-check the claim, short enough that the row stays small.
_MAX_REPRESENTATIVE = 5


@dataclass(slots=True)
class _KeyOutcomes:
    """Which packs carried one key, split by outcome.

    Replaces four near-identical ``defaultdict(int)`` pairs *and* the
    rescans that followed them (``[p for p in packs if key in ...]``, run
    once per surviving key) — the pack ids are already in hand at counting
    time, so the evidence pointers cost nothing extra to keep.
    """

    success_packs: list[str] = field(default_factory=list)
    failure_packs: list[str] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)

    @property
    def successes(self) -> int:
        return len(self.success_packs)

    @property
    def presentations(self) -> int:
        return len(self.success_packs) + len(self.failure_packs)


class AdvisoryReport(TrellisModel):
    """Summary of an advisory generation run."""

    advisories_generated: int
    advisories_stored: int
    total_packs: int
    total_feedback: int
    analysis_window_days: int
    #: Coverage of the two capped event reads. A run whose window held
    #: more than ``DEFAULT_SCAN_LIMIT`` events of either type analysed
    #: only the newest of them, and says so here rather than reporting a
    #: partial window as a whole one (#374).
    coverage: ScanCoverage = Field(default_factory=ScanCoverage)


class AdvisoryGenerator:
    """Generate advisories from outcome data.

    Usage::

        generator = AdvisoryGenerator(event_log, advisory_store)
        report = generator.generate(days=30)
    """

    def __init__(
        self,
        event_log: EventLog,
        advisory_store: AdvisoryStore,
        *,
        min_sample_size: int = _MIN_SAMPLE_SIZE,
        min_effect_size: float = _MIN_EFFECT_SIZE,
    ) -> None:
        self._event_log = event_log
        self._advisory_store = advisory_store
        self._min_sample_size = min_sample_size
        self._min_effect_size = min_effect_size

    def generate(self, *, days: int = 30) -> AdvisoryReport:
        """Run all analysis methods and store resulting advisories.

        Returns an :class:`AdvisoryReport` summarising what was generated.
        """
        since = datetime.now(tz=UTC) - timedelta(days=days)

        # Capped reads go through scan_events so a window with more than
        # DEFAULT_SCAN_LIMIT matches keeps the *newest* events. The
        # ascending default dropped them, which on a busy deployment means
        # advisories mined from the oldest slice of the window (#374).
        pack_scan = scan_events(
            self._event_log,
            event_type=EventType.PACK_ASSEMBLED,
            since=since,
            limit=DEFAULT_SCAN_LIMIT,
        )
        feedback_scan = scan_events(
            self._event_log,
            event_type=EventType.FEEDBACK_RECORDED,
            since=since,
            limit=DEFAULT_SCAN_LIMIT,
        )
        coverage = merge_coverage(pack_scan.coverage, feedback_scan.coverage)

        # Build joined dataset
        packs = self._join_packs_feedback(pack_scan.events, feedback_scan.events)

        if not packs:
            return AdvisoryReport(
                advisories_generated=0,
                advisories_stored=0,
                total_packs=len(pack_scan.events),
                total_feedback=len(feedback_scan.events),
                analysis_window_days=days,
                coverage=coverage,
            )

        # Run all five analysis methods
        advisories: list[Advisory] = []
        advisories.extend(self._entity_correlation(packs))
        advisories.extend(self._strategy_correlation(packs))
        advisories.extend(self._scope_analysis(packs))
        advisories.extend(self._anti_pattern_detection(packs))
        advisories.extend(self._query_improvement(packs))

        # Replaces the previous run's row for each finding: advisory ids
        # are derived from the finding's identity (:meth:`_stable_id`), so
        # ``put_many`` overwrites in place instead of minting a fresh ULID
        # nightly. That in-place overwrite is why _carry_forward_status has
        # to run first — a regenerated row defaults to ACTIVE, and writing
        # it over a row the fitness loop suppressed would revive it in
        # silence, which is worse than the unbounded append it replaces.
        stored = 0
        if advisories:
            advisories = [self._carry_forward_status(a) for a in advisories]
            stored = self._advisory_store.put_many(advisories)

        logger.info(
            "advisories_generated",
            count=len(advisories),
            stored=stored,
            packs_analyzed=len(packs),
            truncated=coverage.truncated,
        )

        return AdvisoryReport(
            advisories_generated=len(advisories),
            advisories_stored=stored,
            total_packs=len(pack_scan.events),
            total_feedback=len(feedback_scan.events),
            analysis_window_days=days,
            coverage=coverage,
        )

    # --- Data preparation ---

    @staticmethod
    def _join_packs_feedback(
        pack_events: list[Any],
        feedback_events: list[Any],
    ) -> list[dict[str, Any]]:
        """Join PACK_ASSEMBLED with FEEDBACK_RECORDED into analysis rows."""
        # Build pack_id → payload mapping
        pack_data: dict[str, dict[str, Any]] = {}
        for event in pack_events:
            pack_id = event.entity_id
            if pack_id:
                pack_data[pack_id] = event.payload

        # Build pack_id → success mapping
        pack_success: dict[str, bool] = {}
        for event in feedback_events:
            pack_id = event.payload.get("pack_id") or event.entity_id
            if pack_id and pack_id in pack_data:
                rating = event.payload.get("rating", 0.0)
                pack_success[pack_id] = event.payload.get(
                    "success", rating >= _SUCCESS_RATING_THRESHOLD
                )

        # Join into analysis rows
        rows: list[dict[str, Any]] = []
        for pack_id, payload in pack_data.items():
            if pack_id not in pack_success:
                continue
            rows.append(
                {
                    "pack_id": pack_id,
                    "success": pack_success[pack_id],
                    "item_ids": payload.get("injected_item_ids", []),
                    "items": payload.get("injected_items", []),
                    "strategies": payload.get("strategies_used", []),
                    "domain": payload.get("domain", "global"),
                    "intent": payload.get("intent", ""),
                    "rejected": payload.get("rejected_items", []),
                    "budget_trace": payload.get("budget_trace", []),
                }
            )
        return rows

    @staticmethod
    def _tally(
        packs: list[dict[str, Any]],
        keys_of: Callable[[dict[str, Any]], Iterable[str]],
    ) -> dict[str, _KeyOutcomes]:
        """Group packs by the keys ``keys_of`` extracts from each of them."""
        tally: dict[str, _KeyOutcomes] = defaultdict(_KeyOutcomes)
        for pack in packs:
            for key in keys_of(pack):
                outcomes = tally[key]
                if pack["success"]:
                    outcomes.success_packs.append(pack["pack_id"])
                else:
                    outcomes.failure_packs.append(pack["pack_id"])
                outcomes.domains.add(pack["domain"])
        return tally

    # --- Analysis methods ---

    def _entity_correlation(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find entities disproportionately present in successful packs."""
        total_success = sum(1 for p in packs if p["success"])

        advisories: list[Advisory] = []
        for item_id, outcomes in self._tally(packs, lambda p: p["item_ids"]).items():
            supported = self._supported_effect(outcomes, total_success, len(packs))
            if supported is None:
                continue
            rate_with, rate_without, effect = supported
            if effect < self._min_effect_size:
                continue

            scope = self._scope_of(outcomes)
            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(
                        AdvisoryCategory.ENTITY, scope, item_id
                    ),
                    category=AdvisoryCategory.ENTITY,
                    confidence=self._compute_confidence(
                        outcomes.presentations, effect
                    ),
                    message=(
                        f"Entity {item_id} appears in"
                        f" {rate_with:.0%} of successful packs"
                        f" (n={outcomes.presentations}, effect=+{effect:.0%})."
                        f" Consider including it."
                    ),
                    evidence=self._evidence(
                        outcomes, rate_with, rate_without, effect, outcomes.success_packs
                    ),
                    scope=scope,
                    entity_id=item_id,
                )
            )

        return advisories

    def _strategy_correlation(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find strategies disproportionately present in successful packs.

        This is a proxy for step-pattern mining (ADR method 2) using the
        strategy_source data from Phase 1's decision trail.
        """
        total_success = sum(1 for p in packs if p["success"])
        tally = self._tally(
            packs,
            lambda p: {
                item["strategy_source"]
                for item in p.get("items", [])
                if item.get("strategy_source")
            },
        )

        advisories: list[Advisory] = []
        for strategy, outcomes in tally.items():
            supported = self._supported_effect(outcomes, total_success, len(packs))
            if supported is None:
                continue
            rate_with, rate_without, effect = supported
            if abs(effect) < self._min_effect_size:
                continue

            exemplars = (
                outcomes.success_packs if effect > 0 else outcomes.failure_packs
            )
            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(
                        AdvisoryCategory.APPROACH, "global", strategy
                    ),
                    category=AdvisoryCategory.APPROACH,
                    confidence=self._compute_confidence(
                        outcomes.presentations, abs(effect)
                    ),
                    message=(
                        f"Packs using the '{strategy}' strategy"
                        f" succeeded {rate_with:.0%} of the time"
                        f" vs {rate_without:.0%} without"
                        f" (n={outcomes.presentations}, effect={effect:+.0%})."
                    ),
                    evidence=self._evidence(
                        outcomes, rate_with, rate_without, effect, exemplars
                    ),
                    scope="global",
                    metadata={"strategy": strategy},
                )
            )

        return advisories

    def _scope_analysis(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Analyze whether narrower or broader packs correlate with success.

        The one method that never needed :meth:`_supported_effect`: its two
        arms are disjoint pack-count bins and *both* are already gated at
        ``min_sample_size`` below, so there is no arm it can compare
        against nothing.
        """
        # Bin packs by item count: small, medium, large
        bins: dict[str, list[dict[str, Any]]] = {
            "small": [],
            "medium": [],
            "large": [],
        }
        for pack in packs:
            n_items = len(pack["item_ids"])
            if n_items <= _SCOPE_SMALL:
                bins["small"].append(pack)
            elif n_items <= _SCOPE_LARGE:
                bins["medium"].append(pack)
            else:
                bins["large"].append(pack)

        advisories: list[Advisory] = []
        bin_rates: dict[str, float] = {}

        for bin_name, members in bins.items():
            if len(members) >= self._min_sample_size:
                bin_rates[bin_name] = sum(1 for p in members if p["success"]) / len(
                    members
                )

        # Compare best vs worst bin
        if len(bin_rates) < _MIN_BIN_COUNT:
            return advisories

        best_bin = max(bin_rates, key=lambda k: bin_rates[k])
        worst_bin = min(bin_rates, key=lambda k: bin_rates[k])
        effect = bin_rates[best_bin] - bin_rates[worst_bin]

        if effect < self._min_effect_size:
            return advisories

        best_n = len(bins[best_bin])
        worst_n = len(bins[worst_bin])
        confidence = self._compute_confidence(best_n + worst_n, effect)

        scope_hint = {
            "small": "<=5 items",
            "medium": "6-15 items",
            "large": ">15 items",
        }

        advisories.append(
            Advisory(
                advisory_id=self._stable_id(AdvisoryCategory.SCOPE, "global", ""),
                category=AdvisoryCategory.SCOPE,
                confidence=confidence,
                message=(
                    f"Packs with {scope_hint[best_bin]} succeeded"
                    f" {bin_rates[best_bin]:.0%} vs"
                    f" {bin_rates[worst_bin]:.0%} for"
                    f" {scope_hint[worst_bin]}"
                    f" (effect=+{effect:.0%})."
                ),
                evidence=AdvisoryEvidence(
                    sample_size=best_n + worst_n,
                    success_rate_with=round(bin_rates[best_bin], 3),
                    success_rate_without=round(bin_rates[worst_bin], 3),
                    effect_size=round(effect, 3),
                    representative_trace_ids=self._representative(
                        [p["pack_id"] for p in bins[best_bin] if p["success"]]
                    ),
                ),
                scope="global",
                metadata={
                    "best_bin": best_bin,
                    "worst_bin": worst_bin,
                },
            )
        )
        return advisories

    def _anti_pattern_detection(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find entities disproportionately present in failed packs."""
        total_success = sum(1 for p in packs if p["success"])

        advisories: list[Advisory] = []
        for item_id, outcomes in self._tally(packs, lambda p: p["item_ids"]).items():
            supported = self._supported_effect(outcomes, total_success, len(packs))
            if supported is None:
                continue
            rate_with, rate_without, effect = supported
            # Anti-patterns have *negative* effect (presence hurts)
            if effect >= -self._min_effect_size:
                continue

            scope = self._scope_of(outcomes)
            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(
                        AdvisoryCategory.ANTI_PATTERN, scope, item_id
                    ),
                    category=AdvisoryCategory.ANTI_PATTERN,
                    confidence=self._compute_confidence(
                        outcomes.presentations, abs(effect)
                    ),
                    message=(
                        f"Entity {item_id} correlates with failure:"
                        f" {rate_with:.0%} success when present vs"
                        f" {rate_without:.0%} without"
                        f" (n={outcomes.presentations}, effect={effect:+.0%})."
                    ),
                    evidence=self._evidence(
                        outcomes, rate_with, rate_without, effect, outcomes.failure_packs
                    ),
                    scope=scope,
                    entity_id=item_id,
                )
            )

        return advisories

    def _query_improvement(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find intent keywords that correlate with successful packs."""
        total_success = sum(1 for p in packs if p["success"])
        tally = self._tally(
            packs,
            lambda p: {
                word
                for word in p.get("intent", "").lower().split()
                if len(word) >= _MIN_WORD_LENGTH
            },
        )

        advisories: list[Advisory] = []
        for word, outcomes in tally.items():
            supported = self._supported_effect(outcomes, total_success, len(packs))
            if supported is None:
                continue
            rate_with, rate_without, effect = supported
            if effect < self._min_effect_size:
                continue

            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(
                        AdvisoryCategory.QUERY, "global", word
                    ),
                    category=AdvisoryCategory.QUERY,
                    confidence=self._compute_confidence(
                        outcomes.presentations, effect
                    ),
                    message=(
                        f"Including '{word}' in your context query"
                        f" correlates with {rate_with:.0%} success"
                        f" (n={outcomes.presentations}, effect=+{effect:.0%})."
                    ),
                    evidence=self._evidence(
                        outcomes, rate_with, rate_without, effect, outcomes.success_packs
                    ),
                    scope="global",
                    metadata={"keyword": word},
                )
            )

        return advisories

    # --- Helpers ---

    def _supported_effect(
        self,
        outcomes: _KeyOutcomes,
        total_successes: int,
        total_packs: int,
    ) -> tuple[float, float, float] | None:
        """``(rate_with, rate_without, effect)``, or ``None`` if unsupported.

        ``None`` is the whole point: it is the generator declining to make
        a claim, and every caller drops the candidate on it. Two ways to
        earn it, and they are the same rule applied to each arm —

        * fewer than ``min_sample_size`` packs *carried* the key (the
          pre-existing check, unchanged), or
        * fewer than ``min_sample_size`` packs *did not* carry it, so
          there is no comparison arm to subtract.

        The arithmetic itself is
        :func:`~trellis.retrieve.effectiveness.lift_vs_baseline` — the one
        implementation, rather than the second, divergent copy that made
        this a bug. That function's own zero-sample fallback (baseline =
        the window rate, so lift = 0) is now unreachable from here, since
        ``without == 0`` is refused before it is called. That is
        deliberate belt-and-braces: an unsupported claim should not be
        emitted at all, and if it somehow were, the shared fallback would
        still report lift 0 rather than the old ``0.0`` literal's
        fabricated effect.
        """
        presentations = outcomes.presentations
        if presentations < self._min_sample_size:
            return None
        if total_packs - presentations < self._min_sample_size:
            return None
        return lift_vs_baseline(
            outcomes.successes, presentations, total_successes, total_packs
        )

    @staticmethod
    def _scope_of(outcomes: _KeyOutcomes) -> str:
        """Domain scope for an item-keyed advisory, or ``global`` if mixed."""
        if len(outcomes.domains) == 1:
            return next(iter(outcomes.domains))
        return "global"

    @staticmethod
    def _representative(pack_ids: Sequence[str]) -> list[str]:
        """Up to :data:`_MAX_REPRESENTATIVE` evidence pointers."""
        return list(pack_ids[:_MAX_REPRESENTATIVE])

    def _evidence(
        self,
        outcomes: _KeyOutcomes,
        rate_with: float,
        rate_without: float,
        effect: float,
        exemplars: Sequence[str],
    ) -> AdvisoryEvidence:
        """Assemble the evidence block, including its pointers."""
        return AdvisoryEvidence(
            sample_size=outcomes.presentations,
            success_rate_with=round(rate_with, 3),
            success_rate_without=round(rate_without, 3),
            effect_size=round(effect, 3),
            representative_trace_ids=self._representative(exemplars),
        )

    @staticmethod
    def _stable_id(category: AdvisoryCategory, scope: str, subject: str) -> str:
        """Deterministic advisory id for one *finding*.

        Keyed on what the advisory is **about** — its category, its scope
        and its subject (the entity id, strategy name or keyword; the
        empty string for SCOPE, which yields at most one finding per
        scope) — and deliberately **not** on the numbers, which move every
        night. Two runs that rediscover the same finding must produce the
        same id or the fitness loop's presentation counts fragment across
        ids and never clear ``min_presentations``.

        Hashed rather than concatenated because subjects are open text
        (keywords, ULIDs, future strategy names) and an id is not the
        place to worry about a separator appearing in one. The category
        stays in the clear so a row is greppable.
        """
        digest = hashlib.sha256(
            "\x00".join((category.value, scope, subject)).encode()
        ).hexdigest()
        return f"adv-{category.value}-{digest[:16]}"

    def _carry_forward_status(self, advisory: Advisory) -> Advisory:
        """Merge a regenerated finding onto the row it replaces.

        The generator owns the *finding* — message, evidence, and the
        confidence those imply. The fitness loop
        (:func:`~trellis.retrieve.effectiveness.run_advisory_fitness_loop`)
        owns the *fitness* — status, suppression, and the confidence blend
        that drives them. Stable ids make the generator's nightly write
        land on the loop's row, so the loop's fields have to survive it.

        Carried forward:

        * ``created_at`` — when the finding was *first* observed. A
          regenerated row is the same finding, not a new one.
        * ``status`` / ``suppressed_at`` / ``suppression_reason`` — a
          fresh :class:`Advisory` defaults to ``ACTIVE``, so without this
          every nightly run would silently un-suppress everything the
          fitness loop had suppressed. Restoration is the loop's decision
          (it applies hysteresis); the generator must not make it by
          accident.
        * ``confidence``, **but only while suppressed.** Otherwise the
          generator would reset a suppressed advisory's confidence to a
          fresh statistical value each night and the very next fitness
          pass — which runs immediately after generation in the same
          curation cycle — could blend it back above the restore
          threshold. Suppression that undoes itself nightly is
          suppression that does nothing.
        """
        prior = self._advisory_store.get(advisory.advisory_id)
        if prior is None:
            return advisory
        carried: dict[str, Any] = {
            "created_at": prior.created_at,
            "updated_at": datetime.now(tz=UTC),
            "status": prior.status,
            "suppressed_at": prior.suppressed_at,
            "suppression_reason": prior.suppression_reason,
        }
        if prior.status == AdvisoryStatus.SUPPRESSED:
            carried["confidence"] = prior.confidence
        return advisory.model_copy(update=carried)

    @staticmethod
    def _compute_confidence(sample_size: int, effect_size: float) -> float:
        """Compute advisory confidence from sample size and effect size.

        Confidence scales linearly with both sample_size and effect_size,
        capped at 1.0.  Small samples or weak effects yield low confidence.
        """
        # sample component: scales from 0 at n=0 to 1.0 at n≥10
        sample_factor = min(1.0, sample_size * _CONFIDENCE_SCALE)
        # effect component: 0.0 at zero effect, 1.0 at effect ≥ 0.5
        effect_factor = min(1.0, abs(effect_size) / 0.5)
        return round(min(1.0, sample_factor * effect_factor), 3)
