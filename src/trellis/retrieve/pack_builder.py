"""Pack builder — orchestrates search strategies to assemble retrieval packs.

Failure semantics (C2 Phase 4):

* A single configured strategy that raises is treated as a hard
  failure: the build cannot meaningfully assemble a pack without it.
  :class:`PackAssemblyError` is raised. Required-strategy failures
  never produce a quiet empty pack.
* When **multiple** strategies are configured and one (or more) fails,
  the build continues with the survivors. Each failure is recorded in
  a :class:`StrategyFailure` and surfaced in the ``PACK_ASSEMBLED``
  event payload under ``strategy_failures``. If **all** strategies
  fail the build raises :class:`PackAssemblyError`.
* A configured reranker that raises is treated as a hard failure for
  the same reason — the caller asked for reranked relevance, and
  silently falling back to the unranked order would mask the misconfig.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.base import utc_now
from trellis.core.hashing import content_hash
from trellis.learning.scoring import normalize_intent_family
from trellis.meta.agents import META_AGENT_PREFIX
from trellis.retrieve.concentration import measure_parent_concentration
from trellis.retrieve.disclosure import (
    DEFAULT_DISCLOSURE,
    DISCLOSURE_OFF,
    DisclosureConfig,
    apply_disclosure,
)
from trellis.retrieve.evaluate import QualityReport
from trellis.retrieve.excerpts import (
    DEFAULT_CONTENT_FLOOR,
    ContentFloorConfig,
    apply_content_floor,
)
from trellis.retrieve.formatters import format_index_line
from trellis.retrieve.lifecycle import ARCHIVED_REJECTION_REASON, partition_archived
from trellis.retrieve.noise import (
    NOISE_REJECTION_REASON,
    partition_by_signal_quality,
    resolve_signal_quality_spec,
)
from trellis.retrieve.rerankers.base import Reranker
from trellis.retrieve.servable import strip_non_servable
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.tier_mapping import TierMapper
from trellis.retrieve.token_counting import DEFAULT_TOKEN_COUNTER, TokenCounter
from trellis.retrieve.withholding import summarize_withheld
from trellis.schemas.advisory import Advisory
from trellis.schemas.classification import facet_values
from trellis.schemas.pack import (
    BudgetStep,
    Pack,
    PackBudget,
    PackItem,
    PackSection,
    RejectedItem,
    RetrievalReport,
    SectionedPack,
    SectionRequest,
)
from trellis.schemas.well_known import ACTIVITY
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger()

#: Default window for session-aware dedup. When a ``session_id`` is
#: supplied, items served in prior packs within this window are excluded.
#: This is the *time* bound on the served-set scan: a pack assembled more
#: than this many minutes ago is not consulted, so its items become
#: eligible for re-serving. Bounded, documented staleness — not a leak.
DEFAULT_SESSION_DEDUP_WINDOW_MINUTES = 60

#: Maximum number of ``PACK_ASSEMBLED`` events scanned when deriving the
#: session served-set. This is the *count* bound, complementing the time
#: window above. The ``session_id`` predicate is pushed SQL-side (see
#: :meth:`PackBuilder._recently_served`) so the cap counts only *this*
#: session's own packs — a busy neighbouring session cannot crowd the
#: window — and the scan runs newest-first (``order="desc"``) so hitting
#: the cap drops the *oldest* packs, never the recent end. A session that
#: assembles more than this many packs inside the time window will re-serve
#: items last seen beyond the cap: bounded, documented staleness.
DEFAULT_SESSION_DEDUP_EVENT_LIMIT = 200

#: Signature for an optional assembly-time pack evaluator. Consumers own the
#: scenario-resolution logic (e.g., lookup by ``agent_id`` + ``intent``) and
#: return a :class:`QualityReport` when the pack should be scored, or ``None``
#: to skip. See :mod:`trellis.retrieve.evaluate` for scorer building blocks
#: and ``docs/agent-guide/pack-quality-evaluation.md`` for usage.
PackEvaluator = Callable[[Pack], "QualityReport | None"]


class PackAssemblyError(RuntimeError):
    """Raised when pack assembly cannot make progress.

    Two surfaces raise this:

    * **Required-strategy failure** — the single configured strategy
      raises. Returning an empty pack here would mask the bug; the
      caller asked for retrieval and the system could not perform any.
    * **All-strategies failure** — every strategy in a multi-strategy
      pipeline raised. The build has nothing to assemble.

    The collected :class:`StrategyFailure` entries are attached via
    :attr:`strategy_failures` so the caller can inspect which strategies
    failed and why, even though the original exceptions are chained
    via ``__cause__`` (only the last one — the others are listed in
    the failure records).
    """

    def __init__(self, message: str, strategy_failures: list[StrategyFailure]) -> None:
        super().__init__(message)
        self.strategy_failures = list(strategy_failures)


@dataclass(frozen=True)
class StrategyFailure:
    """One entry per failed :class:`SearchStrategy.search` call.

    Attached to :class:`PackAssemblyError` when raised, and included in
    the ``PACK_ASSEMBLED`` event payload under ``strategy_failures``
    when the build continues with survivors.
    """

    strategy: str
    error_class: str
    message: str

    def to_event_payload(self) -> dict[str, str]:
        """Serialize for inclusion in the ``PACK_ASSEMBLED`` event."""
        return {
            "strategy": self.strategy,
            "error_class": self.error_class,
            "message": self.message,
        }


#: Leading YAML frontmatter block: a ``---`` fence line at the very start,
#: arbitrary YAML, then a closing ``---`` or ``...`` fence. Mirrors the ingest
#: markdown handler's ``_FRONTMATTER`` pattern — the same shape that produced
#: these corpus copies — kept independent to avoid a cross-package import into
#: the corpus layer. Consumes the trailing newline so the stripped body starts
#: cleanly.
_DEDUP_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


def _strip_dedup_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block before MinHash comparison.

    F14: the same fact stored via ``save_memory`` (raw body) and via corpus
    ingestion (the identical body wrapped in a ``---`` YAML frontmatter block
    plus a heading) must collapse to one pack item. But the corpus copy's
    ~130-char frontmatter/heading prefix dilutes the shingle-set overlap —
    on the F14-shaped test fixture the 3-shingle Jaccard estimate drops to
    ~0.75, under the 0.85 threshold, and the near-duplicate survives. (For
    documents longer than the strategies' 500-char excerpt window the prefix
    compounds the problem by also displacing shared body out of the compared
    excerpt.)

    Stripping the frontmatter before shingling restores the estimate to
    ~0.95+ so the default threshold catches the pair, without loosening the
    global match sensitivity (which would risk over-suppressing genuinely-
    distinct items). This only normalizes the text used for the *similarity
    comparison*; the excerpt served in the pack is untouched.

    Text without a leading frontmatter fence — including a mid-document
    ``---`` horizontal rule — is returned unchanged (the ``\\A`` anchor and
    required closing fence guard against false strips).

    Known limitation (accepted): a document whose *first* line is a bare
    ``---`` markdown horizontal rule and which contains a later ``---`` (or
    ``...``) line is indistinguishable from frontmatter by shape, so its
    leading section is stripped as if it were frontmatter. This affects only
    the comparison text, and errs in the safe direction — at worst two such
    docs sharing a post-rule body compare as more similar, and the
    relevance-order winner rule keeps the best copy.
    """
    return _DEDUP_FRONTMATTER_RE.sub("", text, count=1)


def _item_attribution(item: PackItem) -> dict[str, Any]:
    """Per-item attribution stamped onto ``PACK_ASSEMBLED.injected_items``.

    ``trellis.learning.pack_observations._join_one`` reads ``title`` /
    ``category`` / ``domain_system`` off the *pack* payload (not the
    feedback payload) to describe promotion candidates. Without them every
    candidate carried ``title=None, category=None``, so a human reviewing
    ``intent_learning_candidates.json`` saw only opaque item_ids.

    All three are derived from metadata the strategies already attach —
    nothing new is computed here:

    * ``title`` — ``title``, then ``capture_title`` (the key the Claude
      Code session-capture ingest writes,
      :mod:`trellis_workers.session_capture.capture`), then ``name`` for
      graph nodes — whose name the graph strategy folds into the excerpt
      rather than the metadata, so most entities legitimately have none.
    * ``category`` — the ``content_type`` facet of
      :class:`~trellis.schemas.classification.ContentTags`, and *only*
      that. The closed vocabulary (pattern / decision / error-resolution /
      …) already answers "what shape of information is this", which is
      exactly what a candidate's category means. A flat
      ``metadata["content_type"]`` is deliberately **not** read as a
      fallback: ingest handlers stamp their own vocabulary on that key
      (``"conversation"`` in :mod:`trellis.ingest_corpus.conversations`,
      ``"entity_summary"`` in :mod:`trellis.retrieve.semantic_seeds`), and
      mixing those in would make the column ambiguous across item kinds.
      An item the tagging pipeline never touched carries no category — a
      known-unknown beats a value drawn from a second taxonomy.
    * ``domain_system`` — the ``source_system`` the
      :class:`~trellis.classify.classifiers.source_system.SourceSystemClassifier`
      records (dbt, snowflake, …). Despite the name it is *provenance*, not
      the ``domain`` facet, which is why the facet is carried separately.
    * ``domain`` — the ``domain`` facet itself, as a list. Without it the
      outcome join was tag-blind: every graded row could say which item was
      served and none could say what it was tagged, so no analysis could ask
      whether a tag predicts a good pack. That is the question the whole
      tagging ladder is eventually judged on.
    * ``signal_quality`` — the facet the noise filter acts on, so an
      effectiveness analysis can separate "served and unhelpful" from
      "served despite being marked low".
    * ``graph_selection`` — how the graph axis chose the candidate:
      ``"seeded"`` (the intent reached the store, via
      :class:`~trellis.retrieve.strategies.GraphSeedExtractor` or an
      explicit ``seed_ids``) or ``"recency_window"`` (it did not — the
      axis returned the newest rows and never consulted the query, #371).
      Only graph items carry it. Without it, whether a served entity was
      query-relevant at all is not a property of the record — it has to be
      inferred from which wiring was deployed that week, which §1.2 of
      `docs/design/swarm-handoff.md` shows is exactly the inference that
      goes wrong. It reveals nothing about content: two enum values
      describing the *mechanism*, next to a ``title`` that already names
      the thing.
    * ``node_type`` / ``node_role`` — the graph row's own type and role, as
      stamped by :class:`~trellis.retrieve.strategies.GraphSearch` and
      :class:`~trellis.retrieve.observation_strategy.ObservationSearch`
      (#375 gate 4). Only items sourced from a graph node carry either;
      **absence means "this item is not a graph node", and is recorded by
      omitting the key** — the same convention the five fields above use.
      Neither is ever defaulted: a ``node_role`` filled in as ``"semantic"``
      for a document would make the per-role split a statement about the
      filler rather than about the corpus, which is the failure mode #363 /
      #385 / #388 each shipped a version of.

      They are here because the per-type split is the axis's whole
      effectiveness story — `docs/design/plan-375-graph-candidates.md` §2.1
      shows the graph axis's citations concentrated in one ``node_type``
      and zero in eight others — and deriving it required joining every
      served id back to ``nodes WHERE valid_to IS NULL``. That join happens
      to have held (all 78 served ids resolved), but it reads a *mutable*
      table to describe a *past* serving: a node deleted, re-typed or
      re-minted since is either absent or answers for a different row.
      A standing measurement must not depend on it.

      Read them **beside** ``strategy_source``, not instead of it. A vector
      row whose metadata was written from a graph node — the Neo4j shape-#2
      layout, where the vector ``item_id`` *is* the ``node_id`` — carries
      them onto a ``semantic`` item legitimately. On the reference
      deployment no vector row carries either key today, so in practice
      they mark the ``graph`` and ``observation`` axes.

    **On disclosure.** ``domain`` is open-vocabulary and reveals subject
    matter, which is why :mod:`trellis.classify.shadow` deliberately keeps it
    *off* ``MEMORY_OP_JUDGED``. The rule differs here because the payloads
    differ: that event carries only a digest and a verdict, so a domain tag
    would be the sole content-revealing field in it, whereas
    ``injected_items`` has carried ``title`` since #285 — which reveals
    strictly more about a document than its domain does. Adding the facet
    changes what this event reveals by a smaller margin than the field
    already next to it.

    Empty values are omitted rather than emitted as ``None`` so thin items
    keep the pre-existing payload shape.
    """
    meta = item.metadata or {}
    raw_tags = meta.get("content_tags")
    tags: dict[str, Any] = raw_tags if isinstance(raw_tags, dict) else {}
    fields = {
        "title": meta.get("title") or meta.get("capture_title") or meta.get("name"),
        "category": tags.get("content_type"),
        "domain_system": meta.get("source_system"),
        "signal_quality": tags.get("signal_quality"),
        "graph_selection": meta.get("graph_selection"),
        "node_type": meta.get("node_type"),
        "node_role": meta.get("node_role"),
    }
    attribution: dict[str, Any] = {
        key: value.strip()
        for key, value in fields.items()
        if isinstance(value, str) and value.strip()
    }

    domain = [d.strip() for d in facet_values(tags.get("domain")) if d.strip()]
    if domain:
        attribution["domain"] = domain
    return attribution


@dataclass(frozen=True)
class SemanticDedupConfig:
    """Configuration for MinHash/LSH-based fuzzy dedup in :class:`PackBuilder`.

    Catches near-duplicate pack items that survived exact ``item_id`` dedup
    (e.g., the same excerpt indexed twice under mirrored entity ids, or
    almost-identical content from different source systems). Closes Gap 3.2.

    The infrastructure — :class:`~trellis.classify.dedup.minhash.MinHashIndex`
    — already exists and is wired into ``save_memory``; enabling it in
    PackBuilder is a wire-up, not new logic.

    Threshold selection guidance:

    * ``0.90+`` — very strict (typo / casing / punctuation only). Use for
      short excerpts where the risk of false positives is high.
    * ``0.80-0.90`` — standard. Good default for pack excerpts.
    * ``0.70-0.80`` — loose. Catches reworded content; higher false-positive
      risk on short text.

    ``min_shingles`` is an entropy filter — items with fewer shingles than
    this are never compared (protects against false matches on trivial
    text like "see above" or "TBD").
    """

    threshold: float = 0.85
    num_perm: int = 128
    num_bands: int = 16
    shingle_size: int = 3
    min_shingles: int = 5


@dataclass(frozen=True)
class _ServedSet:
    """The session dedup served-set, split into id- and content-knowledge.

    Session dedup is item-id based, but an item whose *content* changed
    since it was served (a superseded / updated doc reusing the same id)
    should be eligible for re-serving. So the served-set carries two
    things derived from prior ``PACK_ASSEMBLED`` events:

    * ``ids`` — every ``item_id`` served in the window, whether the event
      was rich (carried per-item hashes) or thin (historical, hash-less).
      This preserves the id-only suppression contract for old events.
    * ``hashes`` — ``item_id -> {content_hash, ...}`` for the ids whose
      served content we actually know (from the ``injected_item_hashes``
      payload field). An id can accumulate several hashes across the
      window if its content changed between serves.

    Suppression (:meth:`PackBuilder._is_suppressed`): an id not in ``ids``
    was never served → keep. An id in ``ids`` with *no* known hash (only
    thin events) → suppress by id, exactly as before content hashing
    existed. An id in ``ids`` *with* known hashes → suppress only when the
    candidate's current content hash matches one already served; a hash
    miss means the content changed → re-serve.
    """

    ids: frozenset[str]
    hashes: dict[str, set[str]]


def _raise_if_blocking_strategy_failures(
    strategy_failures: list[StrategyFailure],
    total_strategies: int,
    *,
    pack_kind: str,
) -> None:
    """Raise :class:`PackAssemblyError` for required-fail and all-fail (C2 Phase 4).

    ``pack_kind`` is interpolated into the all-failed message so
    sectioned vs. flat builds report which path tripped.
    """
    if not strategy_failures:
        return
    if total_strategies == 1:
        first = strategy_failures[0]
        msg = (
            f"Required strategy {first.strategy!r} failed: "
            f"{first.error_class}: {first.message}"
        )
        raise PackAssemblyError(msg, strategy_failures)
    if len(strategy_failures) == total_strategies:
        msg = (
            f"All {total_strategies} configured strategies failed; "
            f"no candidates available for {pack_kind} assembly"
        )
        raise PackAssemblyError(msg, strategy_failures)


class PackBuilder:
    """Assembles retrieval packs by running search strategies and applying budgets.

    Usage::

        builder = PackBuilder(strategies=[keyword, semantic, graph])
        pack = builder.build(intent="deploy checklist", domain="platform")
    """

    def __init__(
        self,
        strategies: list[SearchStrategy] | None = None,
        event_log: EventLog | None = None,
        advisory_store: AdvisoryStore | None = None,
        reranker: Reranker | None = None,
        semantic_dedup: SemanticDedupConfig | None = None,
        evaluator: PackEvaluator | None = None,
        token_counter: TokenCounter | None = None,
        token_budget_safety_margin: float = 0.0,
        token_budget_validator: TokenCounter | None = None,
        content_floor: ContentFloorConfig | None = None,
        disclosure: DisclosureConfig | None = None,
    ) -> None:
        self._strategies = strategies or []
        self._event_log = event_log
        self._advisory_store = advisory_store
        self._reranker = reranker
        #: Fuzzy-dedup config. ``None`` disables (exact ``item_id`` dedup only).
        self._semantic_dedup = semantic_dedup
        #: Optional assembly-time evaluator. When set, :meth:`build` runs the
        #: callable after pack assembly and, if it returns a
        #: :class:`QualityReport`, attaches it under
        #: ``pack.metadata["quality_report"]``. Exceptions are logged and
        #: swallowed — evaluation must never fail pack assembly.
        self._evaluator = evaluator
        #: Counter used to estimate tokens for budget enforcement and
        #: per-item annotation. Defaults to the 4-chars-per-token heuristic
        #: — plug in an accurate tokenizer (tiktoken, anthropic) to close
        #: boundary drift (Gap 3.1).
        self._token_counter: TokenCounter = token_counter or DEFAULT_TOKEN_COUNTER
        if not 0.0 <= token_budget_safety_margin < 1.0:
            msg = (
                "token_budget_safety_margin must be in [0.0, 1.0); "
                f"got {token_budget_safety_margin!r}"
            )
            raise ValueError(msg)
        #: Fractional headroom subtracted from ``max_tokens`` before the
        #: greedy budget walk. Guards against under-counting estimators
        #: overflowing the real context window. ``0.0`` preserves prior
        #: behavior. Recommended: ``0.05-0.10`` when using the heuristic
        #: counter against a real LLM window.
        self._token_budget_safety_margin = token_budget_safety_margin
        #: Optional second-pass counter invoked after pack assembly for
        #: post-hoc validation. When set, the real token total plus the
        #: delta vs. the estimator is included in ``PACK_ASSEMBLED``
        #: telemetry so drift is observable even when the estimator is
        #: the heuristic.
        self._token_budget_validator = token_budget_validator
        #: Substance floor for pack items. Defaults to
        #: :data:`~trellis.retrieve.excerpts.DEFAULT_CONTENT_FLOOR` —
        #: demote name-only stubs, never drop them. Pass an explicit
        #: config for ``mode="exclude"`` or ``mode="off"``.
        self._content_floor = content_floor or DEFAULT_CONTENT_FLOOR
        #: Graduated-disclosure policy for flat packs. Defaults to
        #: :data:`~trellis.retrieve.disclosure.DEFAULT_DISCLOSURE` — the
        #: first ``body_items`` items keep their excerpts, the tail is
        #: served as pointers. Pass
        #: :data:`~trellis.retrieve.disclosure.DISCLOSURE_OFF` for the
        #: every-item-gets-a-body behaviour that preceded it.
        self._disclosure = disclosure or DEFAULT_DISCLOSURE

    def add_strategy(self, strategy: SearchStrategy) -> None:
        """Add a search strategy."""
        self._strategies.append(strategy)

    def build(  # noqa: PLR0912, PLR0915
        self,
        intent: str,
        *,
        domain: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        intent_family: str | None = None,
        budget: PackBudget | None = None,
        filters: dict[str, Any] | None = None,
        tag_filters: dict[str, Any] | None = None,
        limit_per_strategy: int = 20,
        include_structural: bool = False,
        include_meta: bool = False,
        session_dedup_window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
        refresh: bool = False,
        index_mode: bool = False,
    ) -> Pack:
        """Assemble a pack by running all strategies and applying budget.

        Steps:
            1. Run each strategy with the intent as query.
            2. Collect all PackItems.
            3. Deduplicate by item_id (keep highest score).
            4. Drop structural items unless ``include_structural=True``.
            5. Drop Trellis-internal meta-Activities unless
               ``include_meta=True``. Since #375 a *newly written*
               meta-Activity is also ``node_role="structural"``, so the
               graph axis drops it one step earlier and ``include_meta=True``
               alone will not surface it — pass ``include_structural=True``
               as well. Rows written before #375 are still ``semantic`` and
               ``include_meta=True`` alone reaches them.
            6. Drop items already served in this session (session dedup).
            7. Apply the content floor (demote, or drop, substance-free
               excerpts — see :mod:`trellis.retrieve.excerpts`), then sort
               by relevance_score descending.
            8. Apply budget limits (max_items, then max_tokens).
            9. Build RetrievalReport.
            10. Return Pack.

        When ``session_id`` is provided, any ``item_id`` that appears in a
        ``PACK_ASSEMBLED`` event for this session within the last
        ``session_dedup_window_minutes`` is excluded (reason:
        ``session_dedup``). This prevents the agent from receiving the
        same context repeatedly across multiple tool calls in one
        conversation. The scan is bounded on two axes — the time window
        above and :data:`DEFAULT_SESSION_DEDUP_EVENT_LIMIT` events — both
        documented as bounded staleness rather than leaks.

        Session dedup is content-aware: an item is only suppressed when
        the content served earlier still matches. If the same ``item_id``
        is re-retrieved with *changed* content (a superseded / updated
        doc), its content hash no longer matches the served one and it is
        re-served. Events predating content hashing carry no per-item
        hashes and are suppressed by ``item_id`` alone, as before.

        ``refresh`` (default ``False``) bypasses session dedup for this
        call only: the served-set subtraction is skipped entirely and
        previously-served items are eligible again. This is the
        client-compaction signal — only the caller knows its context
        window was truncated and it needs the earlier items re-injected.

        ``include_meta`` (default ``False``) filters out graph nodes
        produced by :func:`trellis.meta.record_meta_analysis` — i.e.,
        ``Activity`` nodes whose ``agent_id`` starts with
        ``trellis_meta_``. Without this default, every agent-facing pack
        would surface Trellis's own analysis traces (see Item 6 Phase 2
        of ``docs/design/plan-self-improvement-program.md``). Set
        ``include_meta=True`` to surface them — useful for the
        ``meta_trace_round_trip`` eval scenario and for operators
        debugging the self-improvement loop.

        ``run_id`` and ``intent_family`` are request-scoped attribution
        the item layer cannot know. Both land in the ``PACK_ASSEMBLED``
        payload so :mod:`trellis.learning.pack_observations` can bucket
        the pack's outcome by intent family and credit the supporting
        run. ``intent_family`` defaults to
        :func:`~trellis.learning.scoring.normalize_intent_family` over
        ``intent`` — the same canonical normalizer the learning half
        uses, so an unmatched intent still lands in ``general_context``
        rather than a made-up bucket. ``run_id`` has no derivation:
        omitted when the caller has none, and the join keeps its
        ``"unknown-run"`` bucket.

        ``index_mode`` (default ``False``, #305) assembles the pack for an
        *index* rendering — one compact line per item instead of excerpt
        bodies. Only the token-budget walk changes: each item is charged
        its :func:`~trellis.retrieve.formatters.format_index_line` cost
        rather than its excerpt cost, so the same ``max_tokens`` admits
        many more items (``max_items`` still caps first, unchanged). The
        *response*-level overhead is the caller's to reserve — subtract
        :func:`~trellis.retrieve.formatters.index_render_overhead_tokens`
        from the response budget before passing it here, so every item
        charged as served is an item the rendering shows. The
        assembled :class:`Pack` is otherwise identical — items keep their
        excerpts, ``pack_id`` and the full ``PACK_ASSEMBLED`` telemetry
        (per-item hashes, ``injected_items``, session-dedup visibility)
        are intact, so ``record_feedback`` attribution and the learning
        join are unchanged. An index serve *counts as a serve* for session
        dedup; ``refresh=True`` remains the re-injection escape hatch.
        The payload carries ``index_mode`` so analyzers can tell the two
        serve shapes apart.
        """
        budget = budget or PackBudget()
        all_items: list[PackItem] = []
        strategies_used: list[str] = []
        candidates_found = 0
        rejected: list[RejectedItem] = []
        strategy_failures: list[StrategyFailure] = []
        meta_filtered_count = 0

        scoped_filters, scoped_tag_filters = self._apply_domain_scope(
            domain, filters, tag_filters
        )
        merged_filters = self._build_filters(scoped_filters, scoped_tag_filters)
        signal_quality_spec = resolve_signal_quality_spec(scoped_tag_filters)
        # Propagate structural preference into the per-strategy filter so
        # GraphSearch can skip the client-side filter when requested.
        if include_structural:
            merged_filters = dict(merged_filters) if merged_filters else {}
            merged_filters["include_structural"] = True

        for strategy in self._strategies:
            try:
                items, gate_rejected = self._apply_collect_gates(
                    strip_non_servable(
                        strategy.search(
                            intent,
                            limit=limit_per_strategy,
                            filters=(dict(merged_filters) if merged_filters else None),
                        )
                    ),
                    signal_quality_spec=signal_quality_spec,
                    strategy_name=strategy.name,
                )
                rejected.extend(gate_rejected)
                candidates_found += len(items)
                all_items.extend(items)
                strategies_used.append(strategy.name)
                logger.debug(
                    "strategy_completed", strategy=strategy.name, items=len(items)
                )
            # AGGREGATE: collected and re-raised post-loop by
            # _raise_if_blocking_strategy_failures (required-strategy +
            # all-failed cases) or surfaced in PACK_ASSEMBLED payload
            # under strategy_failures (partial-failure case).
            except Exception as exc:
                logger.exception("strategy_failed", strategy=strategy.name)
                strategy_failures.append(
                    StrategyFailure(
                        strategy=strategy.name,
                        error_class=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue

        # Loud-by-default failure surfaces (C2 Phase 4).
        # 1. Single configured strategy that failed: required-strategy
        #    failure — never return an empty pack here.
        # 2. Multiple configured strategies and *all* of them failed:
        #    we cannot make progress, raise.
        _raise_if_blocking_strategy_failures(
            strategy_failures,
            len(self._strategies),
            pack_kind="pack",
        )

        # Promote metadata["source_strategy"] → strategy_source field
        all_items = self._promote_strategy_source(all_items)

        # Deduplicate by item_id (keep highest relevance_score)
        deduped, dedup_rejected = self._deduplicate_tracked(all_items)
        rejected.extend(dedup_rejected)

        # Fuzzy/semantic dedup (Gap 3.2): near-duplicates that survived exact
        # item_id dedup (mirrored schemas, cross-system clones) are collapsed
        # here via MinHash/LSH. Skipped when config is None.
        if self._semantic_dedup is not None:
            deduped, semantic_rejected = self._semantic_dedup_tracked(
                deduped, self._semantic_dedup
            )
            rejected.extend(semantic_rejected)

        # Defense-in-depth: drop any item whose metadata marks it structural,
        # even if it slipped past a strategy-level filter (e.g., a keyword
        # hit against a document whose parent entity is structural).
        if not include_structural:
            kept: list[PackItem] = []
            for item in deduped:
                if (item.metadata or {}).get("node_role") == "structural":
                    rejected.append(
                        RejectedItem(
                            item_id=item.item_id,
                            item_type=item.item_type,
                            relevance_score=item.relevance_score,
                            reason="structural_filter",
                            strategy_source=item.strategy_source,
                        )
                    )
                else:
                    kept.append(item)
            deduped = kept

        # Meta-Activity filter (Item 6 Phase 2): drop graph nodes that
        # represent Trellis's own analyzer runs. Without this default,
        # every agent-facing pack would pollute itself with the
        # ``Activity`` nodes emitted by ``record_meta_analysis``. Opt-in
        # via ``include_meta=True``.
        if not include_meta:
            kept_meta: list[PackItem] = []
            for item in deduped:
                if self._is_meta_activity(item):
                    meta_filtered_count += 1
                    rejected.append(
                        RejectedItem(
                            item_id=item.item_id,
                            item_type=item.item_type,
                            relevance_score=item.relevance_score,
                            reason="meta_activity_filter",
                            strategy_source=item.strategy_source,
                        )
                    )
                else:
                    kept_meta.append(item)
            deduped = kept_meta

        # Session dedup: drop items recently served in this session.
        # ``refresh`` bypasses the subtraction entirely (client-compaction
        # signal — the caller lost its window and needs served items back).
        if session_id and not refresh:
            served = self._recently_served(
                session_id, window_minutes=session_dedup_window_minutes
            )
            if served.ids:
                kept = []
                for item in deduped:
                    if self._is_suppressed(item, served):
                        rejected.append(
                            RejectedItem(
                                item_id=item.item_id,
                                item_type=item.item_type,
                                relevance_score=item.relevance_score,
                                reason="session_dedup",
                                strategy_source=item.strategy_source,
                            )
                        )
                    else:
                        kept.append(item)
                deduped = kept

        # Rerank if a reranker is configured (after dedup + filters, before budget).
        # Loud-by-default (C2 Phase 4): a configured reranker that fails
        # is a misconfiguration the caller needs to see — silently
        # falling back to unranked order would mask it.
        if self._reranker is not None:
            try:
                deduped = self._reranker.rerank(intent, deduped)
                logger.debug("reranker_applied", reranker=self._reranker.name)
            except Exception as exc:
                logger.exception("reranker_failed", reranker=self._reranker.name)
                msg = (
                    f"Reranker {self._reranker.name!r} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise PackAssemblyError(msg, strategy_failures) from exc

        # Content floor: demote (or, when configured, drop) items whose
        # excerpt carries no substance — the logic lives in
        # ``trellis.retrieve.excerpts``. Applied after rerank so the
        # penalty is not overwritten, and before the sort so it actually
        # moves the item down the ranking.
        floor_result = apply_content_floor(deduped, self._content_floor)
        deduped = floor_result.items
        rejected.extend(floor_result.rejected)

        # Sort by relevance_score descending
        deduped.sort(key=lambda x: x.relevance_score, reverse=True)

        # Apply budget: max_items first
        if len(deduped) > budget.max_items:
            rejected.extend(
                RejectedItem(
                    item_id=item.item_id,
                    item_type=item.item_type,
                    relevance_score=item.relevance_score,
                    reason="max_items",
                    strategy_source=item.strategy_source,
                )
                for item in deduped[budget.max_items :]
            )
        selected = deduped[: budget.max_items]

        # Apply budget: max_tokens (estimate ~4 chars per token). In index
        # mode each item is charged its index-line cost, not its excerpt.
        selected, token_rejected, budget_trace = self._apply_token_budget_tracked(
            selected, budget.max_tokens, index_mode=index_mode
        )
        rejected.extend(token_rejected)

        # Graduated disclosure runs *after* the walk, on the item set the
        # walk chose, so the tokens it frees are not spent (see
        # :mod:`trellis.retrieve.disclosure`). An index pack is already all
        # pointers, so it is exempt rather than cut twice.
        disclosure_result = apply_disclosure(
            selected,
            DISCLOSURE_OFF if index_mode else self._disclosure,
        )
        selected = disclosure_result.items
        budget_trace = self._recharge_budget_trace(
            budget_trace, selected, index_mode=index_mode
        )

        selected = self._annotate_selected_items(selected)

        # Repeat-source measurement (B2). Read-only, and taken *after*
        # disclosure so an extra serving already reduced to a pointer is
        # not counted as a body a rollup could reclaim. See
        # :mod:`trellis.retrieve.concentration` for the production numbers
        # that refused the rollup this measures.
        concentration = measure_parent_concentration(
            selected,
            pointer_item_ids=frozenset(disclosure_result.pointer_item_ids),
        )

        report = RetrievalReport(
            queries_run=len(strategies_used),
            candidates_found=candidates_found,
            items_selected=len(selected),
            duration_ms=0,
            strategies_used=strategies_used,
            rejected_items=rejected,
            budget_trace=budget_trace,
        )

        # Attach matching advisories and stamp per-item provenance
        # (Unit C1, foundation for D1 axis C semantic tightening).
        advisories = self._get_matching_advisories(domain)
        selected = self._attach_advisory_provenance(selected, advisories)

        # What this build removed and did not serve (#404). Computed from
        # ``rejected`` against the *served* ids, so a rejection whose item
        # is on screen anyway (the losing copy of a dedup, or a row one
        # axis gated and another served) is not reported as withheld. See
        # :mod:`trellis.retrieve.withholding`.
        withholding = summarize_withheld(rejected, [item.item_id for item in selected])

        pack = Pack(
            intent=intent,
            items=selected,
            retrieval_report=report,
            budget=budget,
            domain=domain,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            intent_family=intent_family or normalize_intent_family(intent=intent),
            advisories=advisories,
            assembled_at=utc_now(),
            # One home, on both pack kinds: ``SectionedPack`` has no
            # top-level ``RetrievalReport`` to hold this, and the renderer
            # must read it the same way for both.
            metadata={"withholding": withholding.as_telemetry()},
        )

        # Optional assembly-time quality evaluation (fail-soft).
        self._attach_quality_report(pack)

        # Emit telemetry event
        if self._event_log is not None:
            self._emit_telemetry(
                pack,
                strategy_failures=strategy_failures,
                meta_filtered_count=meta_filtered_count,
                content_floor=floor_result.as_telemetry(),
                disclosure=disclosure_result.as_telemetry(),
                parent_concentration=concentration.as_telemetry(),
                withholding=withholding.as_telemetry(),
                index_mode=index_mode,
            )

        return pack

    def build_sectioned(  # noqa: PLR0912, PLR0915
        self,
        intent: str,
        *,
        sections: list[SectionRequest],
        domain: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        intent_family: str | None = None,
        filters: dict[str, Any] | None = None,
        tag_filters: dict[str, Any] | None = None,
        limit_per_strategy: int = 20,
        tier_mapper: TierMapper | None = None,
        include_structural: bool = False,
        include_meta: bool = False,
        session_dedup_window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
        refresh: bool = False,
    ) -> SectionedPack:
        """Assemble a sectioned pack with independently budgeted sections.

        Session dedup (step 3a) mirrors :meth:`build`: content-aware
        suppression bounded by the time window and event-count cap, and
        ``refresh=True`` bypasses it for this call only (client-compaction
        signal). ``run_id`` / ``intent_family`` mirror :meth:`build` too.
        See :meth:`build` for the full contract.

        Steps:
            1. Run all strategies once to collect a candidate pool.
            2. Deduplicate the pool.
            3. Drop structural items unless ``include_structural=True``.
            4. For each SectionRequest, filter candidates by section criteria,
               sort by relevance, apply per-section budget, annotate.
            5. Cross-section dedup: keep each item in its highest-scoring section.
            6. Emit telemetry and return SectionedPack.
        """
        mapper = tier_mapper or TierMapper()

        # 1. Collect candidate pool (same as build())
        all_items: list[PackItem] = []
        strategies_used: list[str] = []
        candidates_found = 0
        strategy_failures: list[StrategyFailure] = []
        # Pool-level rejections, for the withholding summary (#404). Kept
        # separate from the per-section ``RetrievalReport.rejected_items``,
        # which stays the content-floor attribution it already was: a
        # pool-level gate has no single section to be blamed on, and
        # copying it into every section would double-count.
        rejected: list[RejectedItem] = []

        scoped_filters, scoped_tag_filters = self._apply_domain_scope(
            domain, filters, tag_filters
        )
        merged_filters = self._build_filters(scoped_filters, scoped_tag_filters)
        signal_quality_spec = resolve_signal_quality_spec(scoped_tag_filters)
        if include_structural:
            merged_filters = dict(merged_filters) if merged_filters else {}
            merged_filters["include_structural"] = True

        for strategy in self._strategies:
            try:
                items, gate_rejected = self._apply_collect_gates(
                    strip_non_servable(
                        strategy.search(
                            intent,
                            limit=limit_per_strategy,
                            filters=(dict(merged_filters) if merged_filters else None),
                        )
                    ),
                    signal_quality_spec=signal_quality_spec,
                    strategy_name=strategy.name,
                )
                rejected.extend(gate_rejected)
                candidates_found += len(items)
                all_items.extend(items)
                strategies_used.append(strategy.name)
            # AGGREGATE: collected and re-raised post-loop by
            # _raise_if_blocking_strategy_failures (see matching note
            # in :meth:`build`); partial failures surface in the
            # PACK_ASSEMBLED event payload.
            except Exception as exc:
                logger.exception("strategy_failed", strategy=strategy.name)
                strategy_failures.append(
                    StrategyFailure(
                        strategy=strategy.name,
                        error_class=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue

        # Loud-by-default failure surfaces (C2 Phase 4) — see build().
        _raise_if_blocking_strategy_failures(
            strategy_failures,
            len(self._strategies),
            pack_kind="sectioned pack",
        )

        # 2. Deduplicate
        deduped = self._deduplicate(all_items)

        # 2a. Fuzzy/semantic dedup (Gap 3.2 / #259). Per-section reports are
        # built later from the collapsed pool, so the rejection *records*
        # aren't threaded into section RetrievalReports — but the count is
        # kept so the sectioned PACK_ASSEMBLED emit carries the same
        # ``semantic_dedup_rejected`` observability as the flat path.
        semantic_dedup_rejected_count = 0
        if self._semantic_dedup is not None:
            deduped, semantic_rejected = self._semantic_dedup_tracked(
                deduped, self._semantic_dedup
            )
            semantic_dedup_rejected_count = len(semantic_rejected)
            rejected.extend(semantic_rejected)

        # 3. Defense-in-depth structural filter.
        if not include_structural:
            kept_structural: list[PackItem] = []
            structural_dropped: list[PackItem] = []
            for item in deduped:
                if (item.metadata or {}).get("node_role") == "structural":
                    structural_dropped.append(item)
                else:
                    kept_structural.append(item)
            rejected.extend(self._reject(structural_dropped, "structural_filter"))
            deduped = kept_structural

        # 3a-meta. Meta-Activity filter (Item 6 Phase 2).
        meta_filtered_count = 0
        if not include_meta:
            kept_meta: list[PackItem] = []
            meta_dropped: list[PackItem] = []
            for item in deduped:
                if self._is_meta_activity(item):
                    meta_dropped.append(item)
                else:
                    kept_meta.append(item)
            meta_filtered_count = len(meta_dropped)
            rejected.extend(self._reject(meta_dropped, "meta_activity_filter"))
            deduped = kept_meta

        # 3a. Session dedup: drop items recently served in this session.
        # ``refresh`` bypasses it (client-compaction signal) — see build().
        if session_id and not refresh:
            served = self._recently_served(
                session_id, window_minutes=session_dedup_window_minutes
            )
            if served.ids:
                kept_session: list[PackItem] = []
                session_dropped: list[PackItem] = []
                for item in deduped:
                    if self._is_suppressed(item, served):
                        session_dropped.append(item)
                    else:
                        kept_session.append(item)
                rejected.extend(self._reject(session_dropped, "session_dedup"))
                deduped = kept_session

        # 3b. Rerank the shared candidate pool before section filling.
        # Loud-by-default (C2 Phase 4) — see build().
        if self._reranker is not None:
            try:
                deduped = self._reranker.rerank(intent, deduped)
                logger.debug("reranker_applied_sectioned", reranker=self._reranker.name)
            except Exception as exc:
                logger.exception(
                    "reranker_failed_sectioned", reranker=self._reranker.name
                )
                msg = (
                    f"Reranker {self._reranker.name!r} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise PackAssemblyError(msg, strategy_failures) from exc

        # 3c. Content floor over the shared pool — mirrors build(). The floor
        # runs before items are assigned to sections, so a dropped item has
        # no single "best" section to be attributed to; it is recorded on
        # every section whose filter it matched, i.e. every report where its
        # absence would otherwise be unexplained.
        floor_result = apply_content_floor(deduped, self._content_floor)
        floor_rejections = {r.item_id: r for r in floor_result.rejected}
        floor_dropped = [i for i in deduped if i.item_id in floor_rejections]
        deduped = floor_result.items
        rejected.extend(floor_result.rejected)

        # 4. Fill each section independently
        #    Track which section each item lands in (for cross-section dedup)
        item_best_section: dict[
            str, tuple[str, float]
        ] = {}  # item_id -> (section, score)
        raw_sections: dict[str, list[PackItem]] = {}

        for section_req in sections:
            section_budget = PackBudget(
                max_items=section_req.max_items,
                max_tokens=section_req.max_tokens,
            )

            # Filter candidates for this section
            matched = [
                item for item in deduped if mapper.matches_section(item, section_req)
            ]
            matched.sort(key=lambda x: x.relevance_score, reverse=True)

            # Apply per-section budget. Both cuts are recorded: an item
            # a section could not afford is absent for a reason the caller
            # can act on, and the withholding summary drops it again if
            # another section served it (rejected minus served).
            within_items = matched[: section_budget.max_items]
            rejected.extend(
                self._reject(matched[section_budget.max_items :], "max_items")
            )
            selected = self._apply_token_budget(within_items, section_budget.max_tokens)
            rejected.extend(self._reject(within_items[len(selected) :], "token_budget"))

            raw_sections[section_req.name] = selected

            for item in selected:
                prev = item_best_section.get(item.item_id)
                if prev is None or item.relevance_score > prev[1]:
                    item_best_section[item.item_id] = (
                        section_req.name,
                        item.relevance_score,
                    )

        # 4. Cross-section dedup — keep item only in its best section
        pack_sections: list[PackSection] = []
        for section_req in sections:
            deduped_items = [
                item
                for item in raw_sections.get(section_req.name, [])
                if item_best_section.get(item.item_id, (None,))[0] == section_req.name
            ]

            annotated = self._annotate_selected_items(deduped_items)
            # Tag selection_reason with section name
            annotated = [
                item.model_copy(
                    update={
                        "selection_reason": f"section:{section_req.name}",
                    }
                )
                for item in annotated
            ]

            section_report = RetrievalReport(
                queries_run=len(strategies_used),
                candidates_found=len(raw_sections.get(section_req.name, [])),
                items_selected=len(annotated),
                strategies_used=strategies_used,
                rejected_items=[
                    floor_rejections[item.item_id]
                    for item in floor_dropped
                    if mapper.matches_section(item, section_req)
                ],
            )

            pack_sections.append(
                PackSection(
                    name=section_req.name,
                    items=annotated,
                    retrieval_report=section_report,
                    budget=PackBudget(
                        max_items=section_req.max_items,
                        max_tokens=section_req.max_tokens,
                    ),
                )
            )

        advisories = self._get_matching_advisories(domain)

        # Stamp per-item advisory provenance across all sections
        # (Unit C1, foundation for D1 axis C semantic tightening).
        if advisories:
            for section in pack_sections:
                section.items = self._attach_advisory_provenance(
                    section.items, advisories
                )

        withholding = summarize_withheld(
            rejected,
            [item.item_id for section in pack_sections for item in section.items],
        )

        sectioned_pack = SectionedPack(
            intent=intent,
            sections=pack_sections,
            domain=domain,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            intent_family=intent_family or normalize_intent_family(intent=intent),
            advisories=advisories,
            assembled_at=utc_now(),
            metadata={"withholding": withholding.as_telemetry()},
        )

        # 5. Emit telemetry
        if self._event_log is not None:
            self._emit_sectioned_telemetry(
                sectioned_pack,
                strategy_failures=strategy_failures,
                meta_filtered_count=meta_filtered_count,
                semantic_dedup_rejected_count=semantic_dedup_rejected_count,
                content_floor=floor_result.as_telemetry(),
                withholding=withholding.as_telemetry(),
            )

        return sectioned_pack

    def _emit_sectioned_telemetry(
        self,
        pack: SectionedPack,
        *,
        strategy_failures: list[StrategyFailure] | None = None,
        meta_filtered_count: int = 0,
        semantic_dedup_rejected_count: int = 0,
        content_floor: dict[str, Any] | None = None,
        withholding: dict[str, Any] | None = None,
    ) -> None:
        """Emit telemetry event for a sectioned pack.

        ``semantic_dedup_rejected_count`` (#259) mirrors the flat path's
        ``semantic_dedup_rejected`` payload field so near-duplicate
        suppression is observable on both pack kinds. Additive field —
        consumers ``payload.get(...)`` with a default. ``content_floor``
        mirrors the flat path's field of the same name, and so does
        ``withholding`` (#404) — emitted even when nothing was withheld, so
        a consumer can tell an empty summary from a missing one.
        """
        per_item_estimates = [
            item.estimated_tokens or self._token_counter.count(item.excerpt)
            for section in pack.sections
            for item in section.items
        ]
        total_budget = sum(section.budget.max_tokens for section in pack.sections)
        token_budget_fields = self._build_token_budget_payload(
            total_budget,
            excerpts=lambda: [
                item.excerpt for section in pack.sections for item in section.items
            ],
            per_item_estimates=per_item_estimates,
        )
        self._event_log.emit(  # type: ignore[union-attr]
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack.pack_id,
            entity_type="sectioned_pack",
            payload={
                "intent": pack.intent,
                "domain": pack.domain,
                "agent_id": pack.agent_id,
                "session_id": pack.session_id,
                # Symmetric with the flat payload (see _emit_telemetry).
                # Known gap: this payload carries no ``injected_items``, so
                # ``pack_observations._join_one`` yields zero per-item rows
                # for a sectioned pack and these two fields stay inert until
                # that is fixed. Emitted now so the sectioned path does not
                # need a second change once it is.
                "run_id": pack.run_id,
                "intent_family": pack.intent_family,
                "section_count": len(pack.sections),
                "total_items": pack.total_items,
                # Per-item content hashes (issue #258), flattened across
                # sections. Symmetric with the flat pack payload so the
                # served-set reader consults one field for both pack kinds.
                "injected_item_hashes": {
                    item.item_id: content_hash(item.excerpt or "")
                    for section in pack.sections
                    for item in section.items
                },
                "sections": [
                    {
                        "name": s.name,
                        "items_count": len(s.items),
                        "item_ids": [i.item_id for i in s.items],
                        "injected_advisory_ids": [
                            list(i.injected_advisory_ids) for i in s.items
                        ],
                    }
                    for s in pack.sections
                ],
                "advisory_ids": [a.advisory_id for a in pack.advisories],
                "reranker": self._reranker.name if self._reranker else None,
                "semantic_dedup_enabled": self._semantic_dedup is not None,
                "semantic_dedup_rejected": semantic_dedup_rejected_count,
                "content_floor": content_floor or {},
                "withholding": withholding or {},
                "strategy_failures": [
                    sf.to_event_payload() for sf in (strategy_failures or [])
                ],
                "meta_filtered_count": meta_filtered_count,
                **token_budget_fields,
            },
        )

    def _attach_quality_report(self, pack: Pack) -> None:
        """Run the optional evaluator and attach its report to the pack.

        Fail-soft: exceptions are logged and swallowed. An evaluator must
        never block pack assembly. When the evaluator returns ``None`` the
        pack is left untouched — consumers decide per-pack whether to score.

        When an ``event_log`` is configured and the evaluator returned a
        report, a :attr:`~EventType.PACK_QUALITY_SCORED` event is emitted
        with ``pack_id`` as the join key to ``PACK_ASSEMBLED`` and
        ``FEEDBACK_RECORDED``.
        """
        if self._evaluator is None:
            return
        try:
            report = self._evaluator(pack)
        except Exception:
            # GRACEFUL-DEGRADATION: assembly-time evaluation is a
            # diagnostics hook, not part of the retrieval contract. A
            # broken evaluator must not prevent the agent from receiving
            # a pack. The exception is logged in full and the pack is
            # returned without a quality report.
            logger.exception("pack_evaluator_failed", pack_id=pack.pack_id)
            return
        if report is None:
            return
        pack.metadata["quality_report"] = report.model_dump(mode="json")
        logger.debug(
            "pack_quality_attached",
            pack_id=pack.pack_id,
            weighted_score=report.weighted_score,
            profile=report.profile_name,
        )
        if self._event_log is not None:
            try:
                self._event_log.emit(
                    EventType.PACK_QUALITY_SCORED,
                    source="pack_builder",
                    entity_id=pack.pack_id,
                    entity_type="pack",
                    payload={
                        "pack_id": pack.pack_id,
                        "intent": pack.intent,
                        "domain": pack.domain,
                        "agent_id": pack.agent_id,
                        "session_id": pack.session_id,
                        "scenario_name": report.scenario_name,
                        "profile_name": report.profile_name,
                        "dimensions": report.dimensions,
                        "weighted_score": report.weighted_score,
                        "missing_coverage_count": len(report.missing_coverage),
                        "findings_count": len(report.findings),
                    },
                )
            except Exception:
                # GRACEFUL-DEGRADATION: a failed telemetry emit must not
                # invalidate a successfully assembled pack. The pack
                # itself is already attached to ``pack.metadata``; the
                # event is best-effort observability and is logged on
                # failure so an operator can investigate.
                logger.exception("pack_quality_event_emit_failed", pack_id=pack.pack_id)

    def _emit_telemetry(
        self,
        pack: Pack,
        *,
        strategy_failures: list[StrategyFailure] | None = None,
        meta_filtered_count: int = 0,
        content_floor: dict[str, Any] | None = None,
        disclosure: dict[str, Any] | None = None,
        parent_concentration: dict[str, Any] | None = None,
        withholding: dict[str, Any] | None = None,
        index_mode: bool = False,
    ) -> None:
        """Emit a ContextRetrievalEvent for observability.

        ``strategy_failures`` (C2 Phase 4) records each strategy that
        raised during this build but did not block assembly because a
        sibling strategy succeeded. Empty list when all strategies
        succeeded — kept in the payload for schema consistency so
        downstream consumers can ``payload.get("strategy_failures", [])``
        without a key check.

        ``meta_filtered_count`` (Item 6 Phase 2) records how many graph
        nodes were dropped by the default meta-Activity filter. ``0``
        when the build either had no candidates of that shape or was
        called with ``include_meta=True``.

        ``content_floor`` carries the substance-floor decision summary
        (mode, threshold, and the item ids penalised or excluded) so a
        demoted item is attributable in telemetry rather than just
        appearing lower in the ranking. Per-item detail also rides
        ``injected_items[].score_breakdown``.

        ``disclosure`` carries the graduated-disclosure summary (mode,
        body-item cut, the ids demoted to pointers, and the pack's excerpt
        cost either side of the pass) so the saving is attributable per
        pack. A demoted item is still a served item: it keeps its id, its
        rank and its row in ``injected_items[]``, and its
        ``estimated_tokens`` is the pointer's cost, not the withheld
        body's.

        ``parent_concentration`` (B2) records how many servings this pack
        drew from repeat source documents — the quantity a chunk rollup
        would have merged away. Measurement only: production numbers
        refused the rollup (see
        :mod:`trellis.retrieve.concentration`), and this field is what
        lets that refusal be re-checked at a larger ``n`` instead of being
        re-derived by string-matching ``item_id``.

        ``withholding`` (#404) records what this build removed and did not
        serve, grouped by reason, with the withheld ids. Emitted **even
        when nothing was withheld**, so a consumer can tell "the summary
        ran and found nothing" from "the summary never ran" — the same
        posture ``content_floor`` takes. Unlike the rendered pack note it
        carries the ids: the event log is a different access path with a
        different audience.

        ``index_mode`` (#305) marks a pack assembled for the one-line-per-
        item index rendering. Additive — consumers ``payload.get(...)``
        with a ``False`` default. When set, ``budget_trace`` /
        ``token_total_estimated`` reflect index-line charges (what was
        actually served) while ``injected_items[].estimated_tokens``
        stays the excerpt read cost.
        """
        report = pack.retrieval_report
        token_budget_fields = self._build_token_budget_payload(
            pack.budget.max_tokens,
            # The validator's second pass must count the same text the
            # primary count charged, or its drift delta measures the two
            # renderings against each other instead of the two counters.
            excerpts=(
                (lambda: [self._index_line_text(item) for item in pack.items])
                if index_mode
                else (lambda: [item.excerpt for item in pack.items])
            ),
            per_item_estimates=[
                b.item_tokens for b in report.budget_trace if b.included
            ],
        )
        self._event_log.emit(  # type: ignore[union-attr]
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id=pack.pack_id,
            entity_type="pack",
            payload={
                "intent": pack.intent,
                "domain": pack.domain,
                "agent_id": pack.agent_id,
                "session_id": pack.session_id,
                # Request-scoped attribution for the learning join. Both
                # follow the ``domain`` idiom above: the key is always
                # present, its value is ``None`` when unknown.
                "run_id": pack.run_id,
                "intent_family": pack.intent_family,
                "items_count": len(pack.items),
                "injected_item_ids": [item.item_id for item in pack.items],
                # Per-item content hashes (issue #258): lets a later build in
                # the same session re-serve an item whose content changed
                # since it was served. Additive — thin/older events without
                # this field fall back to id-only suppression in the reader.
                "injected_item_hashes": {
                    item.item_id: content_hash(item.excerpt or "")
                    for item in pack.items
                },
                "injected_items": [
                    {
                        "item_id": item.item_id,
                        "item_type": item.item_type,
                        "rank": item.rank,
                        "selection_reason": item.selection_reason,
                        "score_breakdown": item.score_breakdown,
                        "estimated_tokens": item.estimated_tokens,
                        "strategy_source": item.strategy_source,
                        "injected_advisory_ids": list(item.injected_advisory_ids),
                        # title / category / domain_system for the learning
                        # join — see :func:`_item_attribution`.
                        **_item_attribution(item),
                    }
                    for item in pack.items
                ],
                "strategies_used": report.strategies_used,
                "candidates_found": report.candidates_found,
                "budget_max_items": pack.budget.max_items,
                "budget_max_tokens": pack.budget.max_tokens,
                "rejected_items": [
                    {
                        "item_id": r.item_id,
                        "item_type": r.item_type,
                        "relevance_score": r.relevance_score,
                        "reason": r.reason,
                        "strategy_source": r.strategy_source,
                    }
                    for r in report.rejected_items
                ],
                "content_floor": content_floor or {},
                "disclosure": disclosure or {},
                "parent_concentration": parent_concentration or {},
                "withholding": withholding or {},
                "budget_trace": [
                    {
                        "item_id": b.item_id,
                        "item_tokens": b.item_tokens,
                        "running_total": b.running_total,
                        "included": b.included,
                    }
                    for b in report.budget_trace
                ],
                "advisory_ids": [a.advisory_id for a in pack.advisories],
                "reranker": self._reranker.name if self._reranker else None,
                "semantic_dedup_enabled": self._semantic_dedup is not None,
                "semantic_dedup_rejected": sum(
                    1 for r in report.rejected_items if r.reason == "semantic_dedup"
                ),
                "strategy_failures": [
                    sf.to_event_payload() for sf in (strategy_failures or [])
                ],
                "meta_filtered_count": meta_filtered_count,
                "index_mode": index_mode,
                **token_budget_fields,
            },
        )

    def _build_token_budget_payload(
        self,
        max_tokens: int,
        *,
        excerpts: Callable[[], list[str]],
        per_item_estimates: list[int],
    ) -> dict[str, Any]:
        """Token-budget telemetry fields shared across flat/sectioned packs.

        Exposes the counter identity, the margin, the effective budget,
        and the pack's total estimated tokens. When a ``token_budget_validator``
        is configured, runs a second-pass count and adds the real total
        plus the delta (absolute + percent) so downstream analysis can
        track estimator drift — directly addressing the "no post-hoc
        validation" half of Gap 3.1.

        ``excerpts`` is passed as a thunk so callers don't materialize a
        potentially-large list on the pack-assembly hot path when no
        validator is configured (the default case).
        """
        payload: dict[str, Any] = {
            "token_counter": self._token_counter.name,
            "token_budget_safety_margin": self._token_budget_safety_margin,
            "token_budget_effective": self._effective_token_budget(max_tokens),
            "token_total_estimated": sum(per_item_estimates),
        }
        if self._token_budget_validator is not None:
            try:
                validated_per_item = [
                    self._token_budget_validator.count(text) for text in excerpts()
                ]
                validated_total = sum(validated_per_item)
                payload["token_counter_validator"] = self._token_budget_validator.name
                payload["token_total_validated"] = validated_total
                delta = validated_total - payload["token_total_estimated"]
                payload["token_count_delta"] = delta
                payload["token_count_delta_pct"] = (
                    delta / payload["token_total_estimated"]
                    if payload["token_total_estimated"] > 0
                    else 0.0
                )
                if validated_total > max_tokens:
                    logger.warning(
                        "token_budget_overrun_detected",
                        validator=self._token_budget_validator.name,
                        validated_total=validated_total,
                        budget_max_tokens=max_tokens,
                        delta=delta,
                    )
            except Exception:
                # GRACEFUL-DEGRADATION: the validator is an optional
                # second-pass tokenizer used for telemetry drift
                # detection. A failed validator should not block pack
                # delivery — the primary tokenizer already produced an
                # estimate and the pack is already assembled.
                logger.exception("token_budget_validator_failed")
        return payload

    def _recently_served(
        self,
        session_id: str,
        *,
        window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    ) -> _ServedSet:
        """Return the served-set for this session within the bounded window.

        Reads ``PACK_ASSEMBLED`` events for ``session_id`` and aggregates
        the item_ids they served — ``injected_item_ids`` (flat packs) and
        section ``item_ids`` (sectioned packs) — plus any per-item
        ``injected_item_hashes`` recorded (issue #258). See
        :class:`_ServedSet` for how the two feed the suppression rule.

        The scan is bounded on both axes documented at module scope:

        * **time** — only events newer than ``window_minutes`` (SQL
          ``since``);
        * **count** — at most :data:`DEFAULT_SESSION_DEDUP_EVENT_LIMIT`
          events. The ``session_id`` predicate is pushed SQL-side via
          ``payload_filters`` so that cap counts only this session's own
          packs (a neighbouring session cannot crowd the window), and
          ``order="desc"`` fetches newest-first so hitting the cap drops
          the oldest packs, never the recent end.

        Returns an empty served-set when no event log is configured or
        nothing matches — the caller treats this as "no dedup applied".
        """
        if self._event_log is None:
            return _ServedSet(ids=frozenset(), hashes={})
        try:
            since = datetime.now(UTC) - timedelta(minutes=window_minutes)
            events = self._event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED,
                since=since,
                limit=DEFAULT_SESSION_DEDUP_EVENT_LIMIT,
                order="desc",
                payload_filters={"session_id": session_id},
            )
        except Exception:
            # GRACEFUL-DEGRADATION: session dedup is a duplicate-
            # suppression optimization. A failed event-log query means
            # the agent may receive a previously-served item again,
            # which is undesirable but not incorrect. Raising here would
            # turn a transient observability outage into a hard
            # retrieval failure.
            logger.exception("session_dedup_event_query_failed")
            return _ServedSet(ids=frozenset(), hashes={})

        ids: set[str] = set()
        hashes: dict[str, set[str]] = {}
        for event in events:
            payload = event.payload or {}
            # Defense-in-depth: ``payload_filters`` already scopes the query
            # to this session SQL-side, but keep the guard so a backend that
            # ignored the predicate cannot leak another session's items.
            if payload.get("session_id") != session_id:
                continue
            # Item ids served — flat packs and sectioned packs.
            for iid in payload.get("injected_item_ids", []) or []:
                ids.add(iid)
            for section in payload.get("sections", []) or []:
                for iid in section.get("item_ids", []) or []:
                    ids.add(iid)
            # Per-item content hashes (issue #258). Absent on thin/older
            # events — those ids stay hash-less and suppress by id alone.
            for iid, chash in (payload.get("injected_item_hashes") or {}).items():
                ids.add(iid)
                if chash:
                    hashes.setdefault(iid, set()).add(chash)
        return _ServedSet(ids=frozenset(ids), hashes=hashes)

    @staticmethod
    def _is_suppressed(item: PackItem, served: _ServedSet) -> bool:
        """Return ``True`` when ``item`` should be dropped by session dedup.

        The content-hash re-serve rule (issue #258):

        * ``item_id`` never served in the window → keep (not suppressed).
        * ``item_id`` served but with no known content hash (only thin /
          pre-hashing events) → suppress by id, the historical behavior.
        * ``item_id`` served *with* known hashes → suppress only when the
          candidate's current content matches one already served; a hash
          miss means the content changed since serving → re-serve.
        """
        if item.item_id not in served.ids:
            return False
        known = served.hashes.get(item.item_id)
        if not known:
            # No content knowledge for this id (thin events only): treat a
            # missing hash as "unchanged" and suppress, exactly as before
            # content hashing existed. Never a KeyError.
            return True
        return content_hash(item.excerpt or "") in known

    # Advisories below this confidence are suppressed from delivery
    _ADVISORY_MIN_CONFIDENCE = 0.1

    def _get_matching_advisories(self, domain: str | None) -> list[Any]:
        """Retrieve advisories matching the pack's domain scope.

        Only advisories with confidence >= ``_ADVISORY_MIN_CONFIDENCE``
        are surfaced.  This ensures the fitness loop can suppress weak
        advisories by lowering their confidence below threshold.
        """
        if self._advisory_store is None:
            return []
        try:
            all_advisories = self._advisory_store.list(
                min_confidence=self._ADVISORY_MIN_CONFIDENCE,
            )
            return [a for a in all_advisories if a.scope in {"global", domain}]
        except Exception:
            # GRACEFUL-DEGRADATION: advisories are auxiliary guidance
            # attached to packs, not core retrieval payload. A failed
            # advisory store query must not block the primary pack from
            # being delivered; the agent gets the pack without
            # advisories and the failure is logged for follow-up.
            logger.exception("advisory_retrieval_failed")
            return []

    @staticmethod
    def _attach_advisory_provenance(
        items: list[PackItem],
        advisories: list[Advisory],
    ) -> list[PackItem]:
        """Stamp ``injected_advisory_ids`` on items influenced by an advisory.

        Foundation for D1 (axis C semantic tightening): records which
        advisory (by ``advisory_id``) influenced each item's presence in
        the pack so downstream analyzers can join
        ``advisory_id -> outcome`` per-item instead of relying on the
        coarser domain-scope proxy.

        Influence rule (Unit C1): an advisory influences ``PackItem`` X
        when ``advisory.entity_id == X.item_id``. This covers the two
        item-scoped advisory categories — :attr:`AdvisoryCategory.ENTITY`
        and :attr:`AdvisoryCategory.ANTI_PATTERN` — which carry an
        ``entity_id`` field. The remaining categories (APPROACH, SCOPE,
        QUERY) are pack-scoped and stay on ``pack.advisories``; they
        deliberately do not stamp individual items.

        Multiple advisories can target the same item; the IDs are
        appended in the order ``advisories`` was iterated (typically
        descending confidence from the store). The list is left empty
        when no advisory matched — the default state preserves prior
        behavior for callers that don't ship advisories at all.
        """
        if not advisories or not items:
            return items

        # Build item_id -> [advisory_id] index.
        per_item: dict[str, list[str]] = {}
        for advisory in advisories:
            entity_id = advisory.entity_id
            if entity_id is None:
                # Pack-scoped advisory; no per-item stamping.
                continue
            per_item.setdefault(entity_id, []).append(advisory.advisory_id)

        if not per_item:
            return items

        annotated: list[PackItem] = []
        for item in items:
            advisory_ids = per_item.get(item.item_id)
            if not advisory_ids:
                annotated.append(item)
                continue
            # Preserve any IDs the strategy stamped pre-build; dedup
            # while keeping insertion order so the audit trail is stable.
            existing = list(item.injected_advisory_ids)
            for aid in advisory_ids:
                if aid not in existing:
                    existing.append(aid)
            annotated.append(
                item.model_copy(update={"injected_advisory_ids": existing})
            )
        return annotated

    @staticmethod
    def _is_meta_activity(item: PackItem) -> bool:
        """Return ``True`` when this item is a Trellis-internal meta-Activity.

        Meta-Activities are recorded by
        :func:`trellis.meta.record_meta_analysis` and have two signatures:

        * ``metadata["node_type"]`` equals :data:`trellis.schemas.well_known.ACTIVITY`
          (GraphSearch stamps the raw node_type into metadata).
        * ``metadata["agent_id"]`` (the Activity's analyzer agent) starts with
          :data:`trellis.meta.agents.META_AGENT_PREFIX` (``trellis_meta_``).

        Both signals must be present — a user-authored Activity node with a
        non-synthetic ``agent_id`` is *not* a meta-Activity and stays in the
        pack. This is the post-strategy filter applied when
        ``include_meta=False`` (the default).
        """
        meta = item.metadata or {}
        node_type = meta.get("node_type")
        agent_id = meta.get("agent_id")
        if node_type != ACTIVITY:
            return False
        if not isinstance(agent_id, str):
            return False
        return agent_id.startswith(META_AGENT_PREFIX)

    @staticmethod
    def _promote_strategy_source(items: list[PackItem]) -> list[PackItem]:
        """Promote metadata source_strategy to the first-class field."""
        result: list[PackItem] = []
        for item in items:
            if item.strategy_source is None and "source_strategy" in (
                item.metadata or {}
            ):
                promoted = item.model_copy(
                    update={
                        "strategy_source": item.metadata["source_strategy"],
                    }
                )
                result.append(promoted)
            else:
                result.append(item)
        return result

    @staticmethod
    def _reject(
        items: Iterable[PackItem],
        reason: str,
        *,
        strategy_source: str | None = None,
    ) -> list[RejectedItem]:
        """Record ``items`` as rejected under ``reason``.

        Every gate in this class already builds ``RejectedItem`` rows by
        hand; this exists for the gates that build several at once, so a
        new gate cannot ship a *partial* row (the interesting fields here
        are ``item_id`` and ``reason`` — a row missing either is invisible
        to :func:`~trellis.retrieve.withholding.summarize_withheld`).
        """
        return [
            RejectedItem(
                item_id=item.item_id,
                item_type=item.item_type,
                relevance_score=item.relevance_score,
                reason=reason,
                strategy_source=strategy_source or item.strategy_source,
            )
            for item in items
        ]

    @classmethod
    def _apply_collect_gates(
        cls,
        items: list[PackItem],
        *,
        signal_quality_spec: dict[str, Any],
        strategy_name: str,
    ) -> tuple[list[PackItem], list[RejectedItem]]:
        """The two collect-seam gates, with what they removed handed back.

        Replaces ``exclude_noise(exclude_archived(...))``. Behaviour is
        unchanged — the same items are dropped, in the same order — but the
        drops are now *recorded*. Before #404 these two gates' only
        observable was a ``logger.debug`` line, a no-op under the CLI's
        ``WARNING`` default and under the MCP server's own configuration,
        so an archived or noise-demoted item vanished from the pack, from
        the ``PACK_ASSEMBLED`` payload and from the log at once.

        Order is load-bearing: archived is evaluated first, matching the
        call nesting it replaces, because
        :func:`~trellis.retrieve.withholding.summarize_withheld` attributes
        an item to the *first* gate that rejected it.

        ``strategy_source`` is stamped from the running strategy rather
        than read off the item: ``_promote_strategy_source`` has not run
        yet at this seam, so the field would otherwise be ``None`` on every
        collect-gate rejection and
        :func:`~trellis.retrieve.telemetry.analyze_pack_telemetry` would
        bucket all of them under ``"unknown"``.
        """
        kept, archived = partition_archived(items)
        kept, noisy = partition_by_signal_quality(kept, signal_quality_spec)
        rejected = cls._reject(
            archived, ARCHIVED_REJECTION_REASON, strategy_source=strategy_name
        )
        rejected.extend(
            cls._reject(noisy, NOISE_REJECTION_REASON, strategy_source=strategy_name)
        )
        return kept, rejected

    def _deduplicate(self, items: list[PackItem]) -> list[PackItem]:
        """Deduplicate by item_id, keeping the entry with highest relevance_score."""
        seen: dict[str, PackItem] = {}
        for item in items:
            existing = seen.get(item.item_id)
            if existing is None or item.relevance_score > existing.relevance_score:
                seen[item.item_id] = item
        return list(seen.values())

    @staticmethod
    def _semantic_dedup_tracked(
        items: list[PackItem],
        config: SemanticDedupConfig,
    ) -> tuple[list[PackItem], list[RejectedItem]]:
        """Collapse near-duplicates via MinHash/LSH.

        Processes items in descending ``relevance_score`` order so the
        winner of a duplicate cluster is always the highest-scoring one.
        Subsequent items that match an already-kept one above threshold
        are rejected with ``reason="semantic_dedup"``.

        Items below the entropy threshold (``min_shingles``) are kept
        unchanged — MinHash can't meaningfully compare them, and
        erroneously dropping short excerpts ("see README", citation
        stubs) would be worse than letting them through. Matches the
        same conservative posture used in ``save_memory``.
        """
        # A single item cannot duplicate itself; skip the index build.
        min_items_for_comparison = 2
        if len(items) < min_items_for_comparison:
            return list(items), []

        index = MinHashIndex(
            num_perm=config.num_perm,
            num_bands=config.num_bands,
            threshold=config.threshold,
            shingle_size=config.shingle_size,
            min_shingles=config.min_shingles,
        )

        ordered = sorted(items, key=lambda i: i.relevance_score, reverse=True)
        kept: list[PackItem] = []
        rejected: list[RejectedItem] = []

        for item in ordered:
            # Normalize the comparison text only (the served excerpt is
            # untouched): strip a leading YAML frontmatter block so a corpus
            # copy wrapped in frontmatter still matches its raw save_memory
            # twin at the default threshold (F14).
            excerpt = _strip_dedup_frontmatter(item.excerpt or "")
            match = index.find_duplicate(excerpt)
            if match is not None:
                matched_id, similarity = match
                rejected.append(
                    RejectedItem(
                        item_id=item.item_id,
                        item_type=item.item_type,
                        relevance_score=item.relevance_score,
                        reason="semantic_dedup",
                        strategy_source=item.strategy_source,
                    )
                )
                logger.debug(
                    "semantic_dedup_match",
                    rejected_id=item.item_id,
                    matched_id=matched_id,
                    similarity=round(similarity, 3),
                )
                continue
            # add() returns False when entropy-filtered; keep the item
            # either way — we just can't index it for future comparisons.
            index.add(item.item_id, excerpt)
            kept.append(item)

        return kept, rejected

    def _deduplicate_tracked(
        self, items: list[PackItem]
    ) -> tuple[list[PackItem], list[RejectedItem]]:
        """Deduplicate by item_id, tracking rejected duplicates."""
        seen: dict[str, PackItem] = {}
        rejected: list[RejectedItem] = []
        for item in items:
            existing = seen.get(item.item_id)
            if existing is None:
                seen[item.item_id] = item
            elif item.relevance_score > existing.relevance_score:
                # The existing one is the loser
                rejected.append(
                    RejectedItem(
                        item_id=existing.item_id,
                        item_type=existing.item_type,
                        relevance_score=existing.relevance_score,
                        reason="dedup",
                        strategy_source=existing.strategy_source,
                    )
                )
                seen[item.item_id] = item
            else:
                # The new one is the loser
                rejected.append(
                    RejectedItem(
                        item_id=item.item_id,
                        item_type=item.item_type,
                        relevance_score=item.relevance_score,
                        reason="dedup",
                        strategy_source=item.strategy_source,
                    )
                )
        return list(seen.values()), rejected

    def _effective_token_budget(self, max_tokens: int) -> int:
        """Apply the safety margin to ``max_tokens``.

        Subtracts ``ceil(max_tokens * safety_margin)`` so the greedy walk
        leaves headroom for tokenizer under-counting. Always returns at
        least 1 to avoid pathological zero-budget behavior on small
        budgets.
        """
        if self._token_budget_safety_margin <= 0.0:
            return max_tokens
        reserved = int(max_tokens * self._token_budget_safety_margin + 0.5)
        effective = max_tokens - reserved
        return max(effective, 1)

    def _apply_token_budget(
        self, items: list[PackItem], max_tokens: int
    ) -> list[PackItem]:
        """Trim items to fit within token budget.

        Uses :attr:`_token_counter` (default: 4-chars-per-token heuristic)
        and applies :attr:`_token_budget_safety_margin` to the budget.
        """
        effective = self._effective_token_budget(max_tokens)
        result: list[PackItem] = []
        total_tokens = 0
        for item in items:
            item_tokens = self._token_counter.count(item.excerpt)
            if total_tokens + item_tokens > effective:
                break
            result.append(item)
            total_tokens += item_tokens
        return result

    def _index_line_text(self, item: PackItem) -> str:
        """The index line this item will be rendered as (#305).

        Built from the same fields the response renderer reads, and with
        the same ``estimated_tokens`` expression
        :meth:`_annotate_selected_items` stamps on the item, so the line
        the builder charges for is byte-identical to the line the agent
        is shown.
        """
        return format_index_line(
            {
                "item_id": item.item_id,
                "item_type": item.item_type,
                "excerpt": item.excerpt,
                "metadata": item.metadata,
                "estimated_tokens": item.estimated_tokens
                or self._token_counter.count(item.excerpt),
            }
        )

    def _item_budget_tokens(self, item: PackItem, *, index_mode: bool) -> int:
        """Tokens this item charges against the pack budget.

        Excerpt cost normally; in index mode (#305) the cost of its
        :meth:`_index_line_text` rendering — the builder charges what the
        index response will actually serve, so the existing ``max_tokens``
        budget composes unchanged while admitting many more items. The
        caller owns the response-level overhead: an index renderer's
        heading is not free, so callers subtract
        :func:`~trellis.retrieve.formatters.index_render_overhead_tokens`
        before handing this budget over.
        """
        if not index_mode:
            return self._token_counter.count(item.excerpt)
        return self._token_counter.count(self._index_line_text(item))

    def _apply_token_budget_tracked(
        self, items: list[PackItem], max_tokens: int, *, index_mode: bool = False
    ) -> tuple[list[PackItem], list[RejectedItem], list[BudgetStep]]:
        """Trim items to fit token budget, tracking rejections.

        ``index_mode`` switches the per-item charge — see
        :meth:`_item_budget_tokens`. ``BudgetStep.item_tokens`` records
        whichever cost was charged, so the trace explains the walk that
        actually ran.
        """
        effective = self._effective_token_budget(max_tokens)
        result: list[PackItem] = []
        rejected: list[RejectedItem] = []
        budget_trace: list[BudgetStep] = []
        total_tokens = 0
        budget_exceeded = False

        for item in items:
            item_tokens = self._item_budget_tokens(item, index_mode=index_mode)
            if not budget_exceeded and total_tokens + item_tokens <= effective:
                result.append(item)
                total_tokens += item_tokens
                budget_trace.append(
                    BudgetStep(
                        item_id=item.item_id,
                        item_tokens=item_tokens,
                        running_total=total_tokens,
                        included=True,
                    )
                )
            else:
                budget_exceeded = True
                budget_trace.append(
                    BudgetStep(
                        item_id=item.item_id,
                        item_tokens=item_tokens,
                        running_total=total_tokens,
                        included=False,
                    )
                )
                rejected.append(
                    RejectedItem(
                        item_id=item.item_id,
                        item_type=item.item_type,
                        relevance_score=item.relevance_score,
                        reason="token_budget",
                        strategy_source=item.strategy_source,
                    )
                )

        return result, rejected, budget_trace

    @staticmethod
    def _apply_domain_scope(
        domain: str | None,
        filters: dict[str, Any] | None,
        tag_filters: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Translate the ``domain`` scope into per-store filter dialects (#262).

        A single ``domain=`` argument must scope every retrieval axis without
        hard-excluding domain-less items (the #254 default-pass contract). The
        stores speak different filter dialects, so ``domain`` is expressed
        twice and each strategy keeps only the form its store understands:

        * the document store speaks the ``content_tags.domain`` facet with
          default-pass semantics → injected into ``tag_filters``
          (:class:`KeywordSearch` forwards it and strips the scalar);
        * the graph match-boost and the semantic-axis default-pass post-filter
          read the scalar ``domain`` key
          (:class:`GraphSearch` / :class:`SemanticSearch` strip it from their
          store calls).

        Caller-supplied domain filters win (``setdefault``) so an explicit
        ``filters`` / ``tag_filters`` from the caller is never overridden.
        Returns ``(filters, tag_filters)`` unchanged when ``domain`` is falsy.
        """
        if not domain:
            return filters, tag_filters
        scoped_tags = dict(tag_filters or {})
        scoped_tags.setdefault("domain", {"in": [domain]})
        scoped_filters = dict(filters or {})
        scoped_filters.setdefault("domain", domain)
        return scoped_filters, scoped_tags

    @staticmethod
    def _build_filters(
        filters: dict[str, Any] | None,
        tag_filters: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Merge user filters and tag_filters into a single filters dict.

        Each facet under ``tag_filters`` is a single-key operator dict:
        ``{"in": [...]}``, ``{"not_in": [...]}``, ``{"eq": x}``, or
        ``{"ne": x}``. Bare lists and bare scalars are rejected
        downstream by the document store's filter parser — the
        operator must be explicit so silent no-ops can't happen.

        When ``tag_filters`` is provided but does not include
        ``signal_quality``, the default ``{"not_in": ["noise"]}`` is
        applied — express the negation directly instead of enumerating
        the inverse allowlist (which would silently miss any new
        ``signal_quality`` value added later).

        **This is the pushdown, not the guarantee.** It reaches only the
        axes whose store speaks the facet, and only when ``tag_filters``
        is not ``None`` (the early return below) — neither of which held
        on the semantic axis or on MCP's calling convention. The noise
        boundary is enforced at the collect seam by
        :func:`~trellis.retrieve.noise.exclude_noise`, which resolves the
        same default through
        :func:`~trellis.retrieve.noise.resolve_signal_quality_spec`. What
        survives here is the cheap half: a row filtered in SQL never
        spends a strategy's ``limit`` budget.
        """
        if tag_filters is None:
            return filters

        effective_tags = dict(tag_filters)
        if "signal_quality" not in effective_tags:
            effective_tags["signal_quality"] = {"not_in": ["noise"]}

        merged = dict(filters) if filters else {}
        merged["content_tags"] = effective_tags
        return merged

    def _recharge_budget_trace(
        self,
        budget_trace: list[BudgetStep],
        items: list[PackItem],
        *,
        index_mode: bool,
    ) -> list[BudgetStep]:
        """Re-price the included steps against the text actually served.

        Graduated disclosure rewrites tail excerpts after the walk has
        run, so the walk's charges describe bodies the pack no longer
        carries. Left alone, three consumers would read a cost that was
        never paid: ``budget_trace`` itself, the token-total validator
        (which counts the rendered excerpts and would report the
        difference as estimator drift), and any analysis joining the two.

        Only *included* steps are re-priced. An excluded step records why
        an item did not make the pack, and it did not make the pack on the
        pre-disclosure arithmetic — rewriting its charge would claim the
        walk had rejected it at a price it was never offered at.

        Returns the trace unchanged when nothing was demoted, so the
        common path allocates nothing.
        """
        served = {item.item_id: item for item in items}

        def repriced(step: BudgetStep) -> bool:
            item = served.get(step.item_id)
            if not step.included or item is None:
                return False
            charge = self._item_budget_tokens(item, index_mode=index_mode)
            return charge != step.item_tokens

        if not any(repriced(step) for step in budget_trace):
            return budget_trace

        recharged: list[BudgetStep] = []
        running = 0
        for step in budget_trace:
            item = served.get(step.item_id)
            if not step.included or item is None:
                recharged.append(step)
                continue
            tokens = self._item_budget_tokens(item, index_mode=index_mode)
            running += tokens
            recharged.append(
                BudgetStep(
                    item_id=step.item_id,
                    item_tokens=tokens,
                    running_total=running,
                    included=True,
                )
            )
        return recharged

    def _annotate_selected_items(self, items: list[PackItem]) -> list[PackItem]:
        """Attach deterministic observability fields to selected items."""
        annotated: list[PackItem] = []
        for index, item in enumerate(items, start=1):
            estimated_tokens = self._token_counter.count(item.excerpt)
            update: dict[str, Any] = {
                "included": True,
                "rank": index,
                "selection_reason": item.selection_reason or "selected_by_relevance",
                "score_breakdown": item.score_breakdown
                or {"relevance_score": item.relevance_score},
                "estimated_tokens": item.estimated_tokens or estimated_tokens,
            }
            # Promote strategy_source from metadata if not already set
            if item.strategy_source is None and "source_strategy" in (
                item.metadata or {}
            ):
                update["strategy_source"] = item.metadata["source_strategy"]
            annotated.append(item.model_copy(update=update))
        return annotated
