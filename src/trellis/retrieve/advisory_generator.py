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
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.core.pack_holdout import drop_holdout
from trellis.core.settings_registry import get_setting, validate_value
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.retrieve.effectiveness import lift_vs_baseline
from trellis.schemas.advisory import (
    RETIRED_BY_KEY,
    RETIRED_BY_QUERY_FILTER,
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

#: Settings-registry wiring for the two thresholds above — env wins over a
#: stored override, which wins over the module default. Every surface
#: that builds an :class:`AdvisoryGenerator` without its own explicit
#: ``min_sample``/``min_effect`` override (the nightly curation worker)
#: or that wants a settings-store value to be visible as the Query/CLI
#: default (the REST route, the CLI command) resolves through
#: :func:`resolve_advisory_thresholds` rather than passing the bare
#: module constants. See ``trellis.core.settings_registry``'s
#: ``advisory_min_sample_size`` / ``advisory_min_effect_size`` entries.
MIN_SAMPLE_SIZE_ENV = "TRELLIS_ADVISORY_MIN_SAMPLE_SIZE"
MIN_EFFECT_SIZE_ENV = "TRELLIS_ADVISORY_MIN_EFFECT_SIZE"
MIN_SAMPLE_SIZE_SETTING = "advisory_min_sample_size"
MIN_EFFECT_SIZE_SETTING = "advisory_min_effect_size"
_SUCCESS_RATING_THRESHOLD = SUCCESS_RATING_THRESHOLD
_CONFIDENCE_SCALE = 0.1  # multiplied by sample_size to cap at 1.0
_MIN_WORD_LENGTH = 3
_SCOPE_SMALL = 5
_SCOPE_LARGE = 15
_MIN_BIN_COUNT = 2
#: Document-frequency ceiling for a QUERY keyword: a word in more than this
#: share of the analysed packs' intents is background vocabulary, not a choice
#: of query, and its "effect" is the task mix it happens to sit in. 0.25 is a
#: judgement, not a fit: it is the point past which the packs *without* the
#: word are under three quarters of the sample, and it was not tuned against a
#: measured intent distribution. The run reports how many rows it withheld
#: (:attr:`AdvisoryReport.query_keywords_withheld`) so an operator can move it
#: (``trellis analyze generate-advisories --max-keyword-df``); ``1.0`` turns
#: the rule off.
DEFAULT_MAX_KEYWORD_DOC_FREQ = 0.25
#: Rule names, shared by the report's two per-rule tallies and the audit event.
_RULE_STOP_LIST = "stop_list"
_RULE_DOC_FREQ = "document_frequency"
#: Retirement only: a stored keyword that is not what the tokenizer would now
#: produce from it (edge punctuation, e.g. ``"deploy,"``). The generator emits
#: the cleaned word under its own id, so the old row would sit beside it.
_RULE_UNNORMALISED = "unnormalised_keyword"
_RETIREMENT_SOURCE = "retrieve.advisory_generator.query_filter"
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$")

#: English function words, which cannot name a topic: determiners, pronouns,
#: prepositions, conjunctions, auxiliaries and a few particles, at least
#: ``_MIN_WORD_LENGTH`` long (shorter words never reach the filter). Deliberately
#: nothing a task could be *about*, so no verbs of work ("fix", "add") and no
#: nouns: a topic word that is merely common is the document-frequency
#: ceiling's job, where it can be measured rather than guessed.
QUERY_STOP_WORDS: frozenset[str] = frozenset(
    """
    about above across after again against all also although among and another
    any are because been before being below between both but can could did does
    doing during each either else ever every few for from further had has have
    having her here hers him his how into its itself just may might more most
    must much neither never nor not off once one only onto other our ours out
    over own per same shall she should since some such than that the their
    theirs them then there these they this those though through too toward
    towards under unless until upon very via was were what when where whether
    which while who whom whose why will with within without would yet you your
    yours yourself yourselves himself herself themselves ourselves myself
    """.split()  # noqa: SIM905 - a word block reads and diffs better than 120 quoted strings
)


def _normalise_word(token: str) -> str:
    """``token`` without leading or trailing punctuation.

    ``str.split()`` leaves it attached, so ``"deploy,"`` and ``"deploy"`` were
    two keywords with two advisory ids, each with half the evidence.
    """
    return _EDGE_PUNCTUATION.sub("", token)


def _intent_words(intent: str | None) -> set[str]:
    """The candidate QUERY keywords of one intent, before any rule is applied."""
    words = (_normalise_word(t) for t in (intent or "").lower().split())
    return {w for w in words if len(w) >= _MIN_WORD_LENGTH}


#: How many pack ids to carry as evidence on one advisory. Enough for a
#: reviewer to spot-check the claim, short enough that the row stays small.
_MAX_REPRESENTATIVE = 5


def resolve_advisory_thresholds(
    settings: Mapping[str, Any] | None = None,
) -> tuple[int, float]:
    """Resolve ``(min_sample_size, min_effect_size)``.

    Env beats a settings-store override beats the module default — the
    same precedence every other read-side knob in this codebase applies
    (see :mod:`trellis.retrieve.builder_factory`'s ``graph_seeding``). A
    caller that already received an explicit per-call override (a CLI
    flag, a query parameter) should use that instead of calling this at
    all; this is for the common case where nothing was explicitly passed.

    Both values are coerced and bounds-checked through the registry's own
    ``validate_value`` (the same coercion the governed ``PUT /settings``
    path uses) — a non-finite or out-of-range value (``nan``, ``inf``, a
    negative sample size, an effect size outside ``0..1``) falls back to
    the module default with a warning instead of reaching the generator,
    which would otherwise either crash (``int(float("inf"))`` raises
    ``OverflowError``, which a bare ``except ValueError`` does not catch)
    or silently fail open (a ``nan``/negative effect floor compares false
    against everything, admitting advisories a real floor would refuse).
    """
    sample_spec = get_setting(MIN_SAMPLE_SIZE_SETTING)
    sample_raw = os.environ.get(MIN_SAMPLE_SIZE_ENV, "").strip()
    if sample_raw:
        try:
            min_sample_size = (
                int(validate_value(sample_spec, sample_raw))
                if sample_spec is not None
                else int(sample_raw)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "advisory_generator.bad_env_int",
                var=MIN_SAMPLE_SIZE_ENV,
                error_type=type(exc).__name__,
            )
            min_sample_size = _MIN_SAMPLE_SIZE
    elif settings is not None and MIN_SAMPLE_SIZE_SETTING in settings:
        sample_stored = settings[MIN_SAMPLE_SIZE_SETTING]
        try:
            min_sample_size = (
                int(validate_value(sample_spec, sample_stored))
                if sample_spec is not None
                else int(sample_stored)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "advisory_generator.bad_settings_int",
                setting=MIN_SAMPLE_SIZE_SETTING,
                error_type=type(exc).__name__,
            )
            min_sample_size = _MIN_SAMPLE_SIZE
    else:
        min_sample_size = _MIN_SAMPLE_SIZE

    effect_spec = get_setting(MIN_EFFECT_SIZE_SETTING)
    effect_raw = os.environ.get(MIN_EFFECT_SIZE_ENV, "").strip()
    if effect_raw:
        try:
            min_effect_size = (
                float(validate_value(effect_spec, effect_raw))
                if effect_spec is not None
                else float(effect_raw)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "advisory_generator.bad_env_float",
                var=MIN_EFFECT_SIZE_ENV,
                error_type=type(exc).__name__,
            )
            min_effect_size = _MIN_EFFECT_SIZE
    elif settings is not None and MIN_EFFECT_SIZE_SETTING in settings:
        effect_stored = settings[MIN_EFFECT_SIZE_SETTING]
        try:
            min_effect_size = (
                float(validate_value(effect_spec, effect_stored))
                if effect_spec is not None
                else float(effect_stored)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "advisory_generator.bad_settings_float",
                setting=MIN_EFFECT_SIZE_SETTING,
                error_type=type(exc).__name__,
            )
            min_effect_size = _MIN_EFFECT_SIZE
    else:
        min_effect_size = _MIN_EFFECT_SIZE

    return min_sample_size, min_effect_size


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
    #: Candidate findings that cleared the sample floor on the arm that
    #: *carried* them but had fewer than ``min_sample_size`` packs to be
    #: compared against, and were therefore not emitted. Counted per
    #: ``(method, key)`` candidate, so one ubiquitous strategy inspected
    #: by two methods contributes two. A high number against a low
    #: ``advisories_generated`` is the deployment saying its packs are too
    #: alike to attribute anything to — which is what the reference
    #: deployment was saying, in silence, while emitting the claim anyway.
    findings_refused_no_comparison_arm: int = 0
    #: QUERY keywords this run declined to key an advisory on, by rule
    #: (``stop_list``, ``document_frequency``), counted as the rows the rule
    #: kept out: only a word that would otherwise have cleared the sample and
    #: effect floors counts, so the figure is "advisories not written", not
    #: "words skipped". A word is counted under the first rule that catches it.
    query_keywords_withheld: dict[str, int] = Field(
        default_factory=lambda: {_RULE_STOP_LIST: 0, _RULE_DOC_FREQ: 0}
    )
    #: Active QUERY rows this run suppressed because the generator no longer
    #: emits their keyword, by rule (``stop_list``, ``document_frequency``,
    #: ``unnormalised_keyword``). Suppressed, not deleted, and each carries an
    #: ``advisory.suppressed`` event.
    query_rows_retired: dict[str, int] = Field(
        default_factory=lambda: {
            _RULE_STOP_LIST: 0,
            _RULE_DOC_FREQ: 0,
            _RULE_UNNORMALISED: 0,
        }
    )
    #: Rows a previous run retired whose keyword is no longer excluded (the
    #: ceiling was raised, or the word's share fell), put back to ``active``.
    query_rows_unretired: int = 0
    #: The document-frequency ceiling this run enforced, or ``None`` when it
    #: was not enforced: the ceiling is skipped while the analysed packs are so
    #: few that it could never admit a word the sample floor would (fewer than
    #: ``min_sample_size / ceiling`` of them), rather than silently turning
    #: QUERY analysis off. The stop-list applies either way.
    query_keyword_df_ceiling: float | None = None
    #: Coverage of the two capped event reads. A run whose window held
    #: more than ``DEFAULT_SCAN_LIMIT`` events of either type analysed
    #: only the newest of them, and says so here rather than reporting a
    #: partial window as a whole one (#374).
    coverage: ScanCoverage = Field(default_factory=ScanCoverage)
    #: Set when the advisory store could not read its file in full, in
    #: which case **nothing was analysed and nothing was written** and every
    #: count above is zero for that reason rather than for want of
    #: evidence. Carries
    #: :meth:`~trellis.stores.degradable_json_store.LoadDegradation.to_dict`,
    #: including the ``recovery`` command. A run that reported an ordinary
    #: ``advisories_generated: N`` over a corrupt file is the thing #393 is
    #: about; a report that cannot say "degraded" leaves the operator with
    #: nothing but a log line to notice it by.
    store_degradation: dict[str, Any] | None = None


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
        max_keyword_doc_freq: float = DEFAULT_MAX_KEYWORD_DOC_FREQ,
    ) -> None:
        # ``not (0 < x <= 1)`` rather than ``x <= 0 or x > 1``: nan fails
        # every comparison and would otherwise fail *open* here.
        if not 0.0 < max_keyword_doc_freq <= 1.0:
            msg = f"max_keyword_doc_freq must be in (0, 1], got {max_keyword_doc_freq}"
            raise ValueError(msg)
        self._event_log = event_log
        self._advisory_store = advisory_store
        self._min_sample_size = min_sample_size
        self._min_effect_size = min_effect_size
        self._max_keyword_doc_freq = max_keyword_doc_freq
        #: Per-run refusal tally, reset at the top of :meth:`generate`. A
        #: refusal nobody can see is the quiet half of the defect this fix
        #: is about: an operator whose advisory count drops needs to be
        #: told the findings were declined for want of evidence, not left
        #: to guess that the analysis found nothing. Per-instance mutable
        #: state, so a generator is single-run and not thread-safe — which
        #: is how all three call sites already use it.
        self._refusals: Counter[str] = Counter()
        #: Per-run QUERY-filter tallies, reset with ``_refusals``: rows each
        #: rule kept out, the keywords the document-frequency rule excluded
        #: (what retirement matches stored rows against), and whether that
        #: rule was enforced at all.
        self._query_withheld: Counter[str] = Counter()
        self._df_excluded: set[str] = set()
        self._df_ceiling_applied = False
        self._unretired: list[str] = []

    def generate(self, *, days: int = 30) -> AdvisoryReport:
        """Run all analysis methods and store resulting advisories.

        Returns an :class:`AdvisoryReport` summarising what was generated.

        **Refuses to run at all against a degraded advisory store.** Not
        merely because the write would be refused anyway
        (:class:`~trellis.errors.DegradedStoreWriteError` is the backstop,
        and it holds for callers that never look at this method), but
        because the analysis would be *wrong* to trust if it did land:
        :meth:`_carry_forward_status` decides what survives a replacing
        write by reading each finding's prior row, and against a store that
        could not read its file every ``get`` returns ``None``. Every
        regenerated advisory would be written fresh — ``status=ACTIVE``,
        ``suppressed_at=None`` — silently reversing every suppression the
        fitness loop had made (#393).

        Returning early also keeps the *report* honest. Running the
        analysis and then discarding it would report
        ``advisories_generated: N, advisories_stored: 0``, which reads as a
        store problem worth shrugging at rather than as the curation
        decision that just failed to apply.
        """
        degradation = self._advisory_store.degradation
        if degradation is not None:
            logger.error(
                "advisory_generation_refused_degraded_store",
                **degradation.to_dict(),
                impact=(
                    "No advisories were generated or written. Regenerating "
                    "against a store that could not read its file would write "
                    "fresh ACTIVE rows over suppressed ones."
                ),
            )
            return AdvisoryReport(
                advisories_generated=0,
                advisories_stored=0,
                total_packs=0,
                total_feedback=0,
                analysis_window_days=days,
                store_degradation=degradation.to_dict(),
            )

        since = datetime.now(tz=UTC) - timedelta(days=days)
        self._refusals = Counter()
        self._query_withheld = Counter()
        self._df_excluded = set()
        self._df_ceiling_applied = False
        self._unretired = []

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
        # A withheld pack served nothing, and its row still names the
        # strategies that ran: mined, its feedback would grade them.
        pack_events, feedback_events = drop_holdout(
            pack_scan.events, feedback_scan.events
        )

        # Build joined dataset
        packs = self._join_packs_feedback(pack_events, feedback_events)

        if not packs:
            return AdvisoryReport(
                advisories_generated=0,
                advisories_stored=0,
                total_packs=len(pack_events),
                total_feedback=len(feedback_events),
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
        # It is also what hands `confidence` over to the loop once the loop
        # has scored the row — see that method for why anything less makes
        # demotion arithmetically unreachable.
        stored = 0
        if advisories:
            advisories = [self._carry_forward_status(a) for a in advisories]
            stored = self._advisory_store.put_many(advisories)
            # After the write, so an event never names a row the store does
            # not hold.
            for advisory_id in self._unretired:
                self._emit_query_filter_event(
                    EventType.ADVISORY_RESTORED,
                    advisory_id,
                    {"reason": "query keyword no longer excluded"},
                )
        unretired = len(self._unretired)

        retired = self._retire_excluded_query_rows()

        logger.info(
            "advisories_generated",
            count=len(advisories),
            stored=stored,
            packs_analyzed=len(packs),
            refused_no_comparison_arm=self._refusals["no_comparison_arm"],
            refused_too_few_observations=self._refusals["too_few_observations"],
            query_withheld=dict(self._query_withheld),
            query_retired=dict(retired),
            query_unretired=unretired,
            truncated=coverage.truncated,
        )

        return AdvisoryReport(
            advisories_generated=len(advisories),
            advisories_stored=stored,
            total_packs=len(pack_events),
            total_feedback=len(feedback_events),
            analysis_window_days=days,
            findings_refused_no_comparison_arm=self._refusals["no_comparison_arm"],
            query_keywords_withheld={
                _RULE_STOP_LIST: self._query_withheld[_RULE_STOP_LIST],
                _RULE_DOC_FREQ: self._query_withheld[_RULE_DOC_FREQ],
            },
            query_rows_retired={
                _RULE_STOP_LIST: retired[_RULE_STOP_LIST],
                _RULE_DOC_FREQ: retired[_RULE_DOC_FREQ],
                _RULE_UNNORMALISED: retired[_RULE_UNNORMALISED],
            },
            query_rows_unretired=unretired,
            query_keyword_df_ceiling=(
                self._max_keyword_doc_freq if self._df_ceiling_applied else None
            ),
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
                    # ``or "global"``, not ``.get(..., "global")``: the key is
                    # *present and null* on 36 of the reference deployment's 46
                    # packs, which the default never sees. An unnormalised None
                    # reaches ``Advisory.scope: str`` through ``_scope_of`` and
                    # raises out of ``generate()`` — losing the whole nightly
                    # run, not one advisory. It has not fired only because no
                    # surviving candidate has yet had *every* one of its packs
                    # undomained.
                    "domain": payload.get("domain") or "global",
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
                    advisory_id=self._stable_id(AdvisoryCategory.ENTITY, item_id),
                    category=AdvisoryCategory.ENTITY,
                    confidence=self._compute_confidence(outcomes.presentations, effect),
                    message=(
                        f"Entity {item_id} appears in"
                        f" {rate_with:.0%} of successful packs"
                        f" (n={outcomes.presentations}, effect=+{effect:.0%})."
                        f" Consider including it."
                    ),
                    evidence=self._evidence(
                        outcomes,
                        rate_with,
                        rate_without,
                        effect,
                        outcomes.success_packs,
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

            exemplars = outcomes.success_packs if effect > 0 else outcomes.failure_packs
            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(AdvisoryCategory.APPROACH, strategy),
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
                advisory_id=self._stable_id(AdvisoryCategory.SCOPE, ""),
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
                    evidence_confidence=confidence,
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
                    advisory_id=self._stable_id(AdvisoryCategory.ANTI_PATTERN, item_id),
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
                        outcomes,
                        rate_with,
                        rate_without,
                        effect,
                        outcomes.failure_packs,
                    ),
                    scope=scope,
                    entity_id=item_id,
                )
            )

        return advisories

    def _query_improvement(self, packs: list[dict[str, Any]]) -> list[Advisory]:
        """Find intent keywords that correlate with successful packs.

        A keyword is dropped before it is scored when it cannot be a query
        choice: a function word (:data:`QUERY_STOP_WORDS`), or a word in more
        than ``max_keyword_doc_freq`` of the analysed packs' intents. Both
        left a ``for`` or ``with`` "correlating with success" at n of a handful,
        which is the task mix of whatever packs carried it, not the query.
        """
        total_success = sum(1 for p in packs if p["success"])
        tally = self._tally(packs, lambda p: _intent_words(p.get("intent")))
        # The ceiling is only meaningful while it can admit something: with
        # fewer packs than ``min_sample_size / ceiling`` every word that has
        # enough carriers is already over it, and enforcing it would end QUERY
        # analysis outright instead of filtering it.
        self._df_ceiling_applied = (
            len(packs) * self._max_keyword_doc_freq >= self._min_sample_size
        )

        advisories: list[Advisory] = []
        for word, outcomes in tally.items():
            rule = self._exclusion_rule(word, outcomes, len(packs))
            if rule is not None:
                if rule == _RULE_DOC_FREQ:
                    self._df_excluded.add(word)
                # What the rule cost: the row that *would* have been written.
                # Refusals are not counted for it (``count_refusal=False``),
                # so ``findings_refused_no_comparison_arm`` keeps meaning
                # "refused among the words that were let through".
                supported = self._supported_effect(
                    outcomes, total_success, len(packs), count_refusal=False
                )
                if supported is not None and supported[2] >= self._min_effect_size:
                    self._query_withheld[rule] += 1
                continue

            supported = self._supported_effect(outcomes, total_success, len(packs))
            if supported is None:
                continue
            rate_with, rate_without, effect = supported
            if effect < self._min_effect_size:
                continue

            advisories.append(
                Advisory(
                    advisory_id=self._stable_id(AdvisoryCategory.QUERY, word),
                    category=AdvisoryCategory.QUERY,
                    confidence=self._compute_confidence(outcomes.presentations, effect),
                    message=(
                        f"Including '{word}' in your context query"
                        f" correlates with {rate_with:.0%} success"
                        f" (n={outcomes.presentations}, effect=+{effect:.0%})."
                    ),
                    evidence=self._evidence(
                        outcomes,
                        rate_with,
                        rate_without,
                        effect,
                        outcomes.success_packs,
                    ),
                    scope="global",
                    metadata={"keyword": word},
                )
            )

        return advisories

    def _exclusion_rule(
        self, word: str, outcomes: _KeyOutcomes, total_packs: int
    ) -> str | None:
        """The rule that keeps ``word`` from keying an advisory, if any."""
        if word in QUERY_STOP_WORDS:
            return _RULE_STOP_LIST
        if (
            self._df_ceiling_applied
            and outcomes.presentations / total_packs > self._max_keyword_doc_freq
        ):
            return _RULE_DOC_FREQ
        return None

    def _retire_excluded_query_rows(self) -> Counter[str]:
        """Suppress active QUERY rows whose keyword the generator now excludes.

        Without this the rules above only stop *new* rows: the 176 rows already
        written stay active, keep their slots in every pack, and wait for the
        fitness loop to score them down, which needs ``min_presentations``
        presentations each and a confidence that clears ``suppress_below``.

        It is a **suppression**, the store's own reversible mechanism, with the
        row marked (:attr:`Advisory.retired_by`) so the fitness loop leaves it
        alone, and an ``advisory.suppressed`` event naming the rule. Rows are
        matched on ``metadata["keyword"]``; other categories, and QUERY rows
        without one, are untouched. So are already-suppressed rows: a fitness
        suppression stays the fitness loop's.

        Run only after the generator has packs to analyse (the
        document-frequency rule needs a corpus), and a row whose keyword is
        absent from this run's window is never retired on frequency.
        """
        retired: Counter[str] = Counter()
        for advisory in self._advisory_store.list():
            if advisory.category != AdvisoryCategory.QUERY:
                continue
            keyword = advisory.metadata.get("keyword")
            if not isinstance(keyword, str):
                continue
            rule = self._retirement_rule(keyword)
            if rule is None:
                continue
            reason = f"query keyword excluded by {rule}: {keyword!r}"
            self._advisory_store.suppress(
                advisory.advisory_id,
                reason=reason,
                metadata={RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER},
            )
            self._emit_query_filter_event(
                EventType.ADVISORY_SUPPRESSED,
                advisory.advisory_id,
                {"reason": reason, "rule": rule, "keyword": keyword},
            )
            retired[rule] += 1
        return retired

    def _retirement_rule(self, keyword: str) -> str | None:
        """Why a *stored* keyword would no longer be emitted, or ``None``."""
        if _normalise_word(keyword) != keyword:
            return _RULE_UNNORMALISED
        if keyword in QUERY_STOP_WORDS:
            return _RULE_STOP_LIST
        if keyword in self._df_excluded:
            return _RULE_DOC_FREQ
        return None

    def _emit_query_filter_event(
        self, event_type: EventType, advisory_id: str, payload: dict[str, Any]
    ) -> None:
        """Audit event for a retirement or un-retirement, fail-soft.

        The store write is the source of truth (same posture as the fitness
        loop's events). ``new_confidence`` is deliberately absent: the
        dashboard averages it over fitness events, and this row's confidence did
        not move.
        """
        try:
            self._event_log.emit(
                event_type,
                source=_RETIREMENT_SOURCE,
                entity_id=advisory_id,
                entity_type="advisory",
                payload={
                    "advisory_id": advisory_id,
                    RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER,
                    **payload,
                },
            )
        # GRACEFUL-DEGRADATION: the suppression/restore already landed in the
        # store; the event is an audit side-effect and must not fail the run.
        except Exception:
            logger.exception(
                "advisory_query_filter_event_emit_failed",
                event_type=event_type.value,
                advisory_id=advisory_id,
            )

    # --- Helpers ---

    def _supported_effect(
        self,
        outcomes: _KeyOutcomes,
        total_successes: int,
        total_packs: int,
        *,
        count_refusal: bool = True,
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

        ``count_refusal=False`` asks the question without recording an
        answer: the QUERY filter uses it to price a word it already excluded,
        which must not read as a refusal of a word that was let through.
        """
        presentations = outcomes.presentations
        if presentations < self._min_sample_size:
            if count_refusal:
                self._refusals["too_few_observations"] += 1
            return None
        if total_packs - presentations < self._min_sample_size:
            if count_refusal:
                self._refusals["no_comparison_arm"] += 1
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
            # Same inputs and same pure function as the ``confidence`` this
            # row is created with, so the two agree on night one and diverge
            # only when the fitness loop takes ownership of ``confidence``.
            evidence_confidence=self._compute_confidence(
                outcomes.presentations, abs(effect)
            ),
            representative_trace_ids=self._representative(exemplars),
        )

    @staticmethod
    def _stable_id(category: AdvisoryCategory, subject: str) -> str:
        """Deterministic advisory id for one *finding*.

        Keyed on what the advisory is **about** — its category and its
        subject (the entity id, strategy name or keyword; the empty string
        for SCOPE, which yields at most one finding per run) — and
        deliberately **not** on the numbers, which move every night. Two
        runs that rediscover the same finding must produce the same id or
        the fitness loop's presentation counts fragment across ids and
        never clear ``min_presentations``.

        ``scope`` is deliberately **not** in the key, though it is a field
        on the row. It is derived from the evidence — :meth:`_scope_of`
        returns a domain only while every pack carrying the subject shares
        one — so it moves as evidence accrues, exactly like ``message`` and
        ``confidence``, which are rewritten in place. Keying on it would
        mint a second id the first time an entity turned up in a second
        domain and orphan the first, which is the unreplaceable-row defect
        this method exists to fix. It also cannot disambiguate: ``_tally``
        yields one entry per subject per category, so within a run no two
        findings differ only by scope. And nothing is lost by leaving it
        out, because the fitness loop re-derives presentations from
        ``PACK_ASSEMBLED.advisory_ids`` each run rather than reading a
        counter off the row.

        Hashed rather than concatenated because subjects are open text
        (keywords, ULIDs, future strategy names) and an id is not the
        place to worry about a separator appearing in one. The category
        stays in the clear so a row is greppable.
        """
        digest = hashlib.sha256(f"{category.value}\x00{subject}".encode()).hexdigest()
        return f"adv-{category.value}-{digest[:16]}"

    def _carry_forward_status(self, advisory: Advisory) -> Advisory:
        """Merge a regenerated finding onto the row it replaces.

        The generator owns the *finding* — message and evidence, including
        ``evidence.evidence_confidence``, the statistic those imply. The
        fitness loop
        (:func:`~trellis.retrieve.effectiveness.run_advisory_fitness_loop`)
        owns the *fitness* — status, suppression, and the outcome-blended
        ``confidence`` that drives them. Stable ids make the generator's
        nightly write land on the loop's row, so the loop's fields have to
        survive it.

        **Precondition: the store loaded cleanly.** Everything below reads
        ``prior``, and a degraded store answers ``None`` for rows it could
        not parse rather than for rows that do not exist — which would turn
        this method from "preserve the loop's decisions" into "discard
        them". :meth:`generate` refuses before reaching here, and
        :meth:`AdvisoryStore._save` refuses after, so the mistake cannot
        reach disk from either direction (#393).

        Always carried: ``created_at`` (a regenerated row is the same
        finding, not a new one), ``status`` / ``suppressed_at`` /
        ``suppression_reason`` (a fresh :class:`Advisory` defaults to
        ``ACTIVE``, so without this every nightly run would silently
        un-suppress everything the loop had suppressed — restoration is the
        loop's decision, with hysteresis, and the generator must not make it
        by accident), and ``fitness_scored_at`` itself, without which the
        handoff below would be forgotten every night.

        **``confidence`` is carried once — and only once — the loop has
        scored the row.** Not "while suppressed", which was this method's
        first cut and was wrong in a way worth recording, because it makes
        the *demote* half of the loop arithmetically unreachable:

        * curation runs ``generate()`` then the fitness loop in one cycle,
          so a generator write to ``confidence`` is immediately followed by
          the blend ``0.7 * C + 0.3 * rate``;
        * every *emitted* advisory has ``C >= 0.15`` by construction
          (``n >= min_sample_size = 5`` gives a sample factor ``>= 0.5``,
          ``|effect| >= min_effect_size = 0.15`` gives an effect factor
          ``>= 0.3``);
        * so ``0.7 * C >= 0.105 > 0.1 = suppress_below`` **even at
          ``rate = 0.0``** — one pass can never cross the threshold, and
          resetting ``C`` nightly stops passes from accumulating.

        That is general, not a tuning accident: any scheme where the
        generator repeatedly pushes ``confidence`` toward a value bounded
        below by 0.15 makes 0.1 unreachable. Blending the fresh statistic in
        rather than replacing it does not escape it — the fixed point of
        ``C = 0.7((1-w)C + w*C_fresh)`` sits at 0.41 for a strong-evidence
        advisory, so the advisories demotion exists for are exactly the ones
        it would still never demote. The only fix is for the generator to
        stop writing the field, which is what ``fitness_scored_at`` records.

        Before the loop has spoken the generator *does* keep ``confidence``
        current, and that matters: measured against the reference
        deployment, the ``keyword`` finding's statistic moved 0.4 -> 0.6 over
        two days of window roll as feedback accrued. Freezing it at creation
        would pin the delivery gate to a number already stale by night three
        — and since no advisory there has ever been served, *every* row
        would be frozen.

        A ``SUPPRESSED`` status carries ``confidence`` too, independently of
        the stamp. Today only the fitness loop suppresses, and it always
        stamps, so the clause is unreachable — but
        :meth:`AdvisoryStore.suppress` is public, and a hand-suppressed row
        left unstamped would have its confidence reset to the statistic,
        which the loop's suppressed branch restores from as soon as it
        clears ``suppress_below + hysteresis``. That is the same
        undoes-itself failure in a different doorway, and it costs one
        clause to close. It cannot revive the arithmetic problem: an
        already-suppressed advisory is not on the demote path.
        """
        prior = self._advisory_store.get(advisory.advisory_id)
        if prior is None:
            return advisory
        # A row *this generator* retired is the one suppression it owns, so
        # it is the one it may undo: the finding is being emitted again,
        # which means the rule that retired it no longer holds (ceiling
        # raised, the word's share fell). Carrying SUPPRESSED here would pin
        # it off forever, since the fitness loop is barred from restoring it.
        # Fresh ``metadata`` drops the marker.
        unretire = prior.retired_by == RETIRED_BY_QUERY_FILTER
        if unretire:
            self._unretired.append(advisory.advisory_id)
        carried: dict[str, Any] = {
            "created_at": prior.created_at,
            "updated_at": datetime.now(tz=UTC),
            "fitness_scored_at": prior.fitness_scored_at,
        }
        if not unretire:
            carried.update(
                status=prior.status,
                suppressed_at=prior.suppressed_at,
                suppression_reason=prior.suppression_reason,
            )
        if prior.fitness_scored_at is not None or (
            prior.status == AdvisoryStatus.SUPPRESSED and not unretire
        ):
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
