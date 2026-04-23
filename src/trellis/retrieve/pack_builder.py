"""Pack builder — orchestrates search strategies to assemble retrieval packs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.base import utc_now
from trellis.retrieve.evaluate import QualityReport
from trellis.retrieve.rerankers.base import Reranker
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.tier_mapping import TierMapper
from trellis.retrieve.token_counting import DEFAULT_TOKEN_COUNTER, TokenCounter
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
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger()

#: Default window for session-aware dedup. When a ``session_id`` is
#: supplied, items served in prior packs within this window are excluded.
DEFAULT_SESSION_DEDUP_WINDOW_MINUTES = 60

#: Signature for an optional assembly-time pack evaluator. Consumers own the
#: scenario-resolution logic (e.g., lookup by ``agent_id`` + ``intent``) and
#: return a :class:`QualityReport` when the pack should be scored, or ``None``
#: to skip. See :mod:`trellis.retrieve.evaluate` for scorer building blocks
#: and ``docs/agent-guide/pack-quality-evaluation.md`` for usage.
PackEvaluator = Callable[[Pack], "QualityReport | None"]


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
        budget: PackBudget | None = None,
        filters: dict[str, Any] | None = None,
        tag_filters: dict[str, Any] | None = None,
        limit_per_strategy: int = 20,
        include_structural: bool = False,
        session_dedup_window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    ) -> Pack:
        """Assemble a pack by running all strategies and applying budget.

        Steps:
            1. Run each strategy with the intent as query.
            2. Collect all PackItems.
            3. Deduplicate by item_id (keep highest score).
            4. Drop structural items unless ``include_structural=True``.
            5. Drop items already served in this session (session dedup).
            6. Sort by relevance_score descending.
            7. Apply budget limits (max_items, then max_tokens).
            8. Build RetrievalReport.
            9. Return Pack.

        When ``session_id`` is provided, any ``item_id`` that appears in a
        ``PACK_ASSEMBLED`` event for this session within the last
        ``session_dedup_window_minutes`` is excluded (reason:
        ``session_dedup``). This prevents the agent from receiving the
        same context repeatedly across multiple tool calls in one
        conversation.
        """
        budget = budget or PackBudget()
        all_items: list[PackItem] = []
        strategies_used: list[str] = []
        candidates_found = 0
        rejected: list[RejectedItem] = []

        merged_filters = self._build_filters(filters, tag_filters)
        # Propagate structural preference into the per-strategy filter so
        # GraphSearch can skip the client-side filter when requested.
        if include_structural:
            merged_filters = dict(merged_filters) if merged_filters else {}
            merged_filters["include_structural"] = True

        for strategy in self._strategies:
            try:
                items = strategy.search(
                    intent,
                    limit=limit_per_strategy,
                    filters=dict(merged_filters) if merged_filters else None,
                )
                candidates_found += len(items)
                all_items.extend(items)
                strategies_used.append(strategy.name)
                logger.debug(
                    "strategy_completed", strategy=strategy.name, items=len(items)
                )
            except Exception:
                logger.exception("strategy_failed", strategy=strategy.name)
                continue

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

        # Session dedup: drop items recently served in this session.
        if session_id:
            served = self._recently_served_item_ids(
                session_id, window_minutes=session_dedup_window_minutes
            )
            if served:
                kept = []
                for item in deduped:
                    if item.item_id in served:
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

        # Rerank if a reranker is configured (after dedup + filters, before budget)
        if self._reranker is not None:
            try:
                deduped = self._reranker.rerank(intent, deduped)
                logger.debug("reranker_applied", reranker=self._reranker.name)
            except Exception:
                logger.exception("reranker_failed", reranker=self._reranker.name)
                # Fall through with original ordering

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

        # Apply budget: max_tokens (estimate ~4 chars per token)
        selected, token_rejected, budget_trace = self._apply_token_budget_tracked(
            selected, budget.max_tokens
        )
        rejected.extend(token_rejected)

        selected = self._annotate_selected_items(selected)

        report = RetrievalReport(
            queries_run=len(strategies_used),
            candidates_found=candidates_found,
            items_selected=len(selected),
            duration_ms=0,
            strategies_used=strategies_used,
            rejected_items=rejected,
            budget_trace=budget_trace,
        )

        # Attach matching advisories
        advisories = self._get_matching_advisories(domain)

        pack = Pack(
            intent=intent,
            items=selected,
            retrieval_report=report,
            budget=budget,
            domain=domain,
            agent_id=agent_id,
            session_id=session_id,
            advisories=advisories,
            assembled_at=utc_now(),
        )

        # Optional assembly-time quality evaluation (fail-soft).
        self._attach_quality_report(pack)

        # Emit telemetry event
        if self._event_log is not None:
            self._emit_telemetry(pack)

        return pack

    def build_sectioned(  # noqa: PLR0912, PLR0915
        self,
        intent: str,
        *,
        sections: list[SectionRequest],
        domain: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        filters: dict[str, Any] | None = None,
        tag_filters: dict[str, Any] | None = None,
        limit_per_strategy: int = 20,
        tier_mapper: TierMapper | None = None,
        include_structural: bool = False,
        session_dedup_window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    ) -> SectionedPack:
        """Assemble a sectioned pack with independently budgeted sections.

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

        merged_filters = self._build_filters(filters, tag_filters)
        if include_structural:
            merged_filters = dict(merged_filters) if merged_filters else {}
            merged_filters["include_structural"] = True

        for strategy in self._strategies:
            try:
                items = strategy.search(
                    intent,
                    limit=limit_per_strategy,
                    filters=dict(merged_filters) if merged_filters else None,
                )
                candidates_found += len(items)
                all_items.extend(items)
                strategies_used.append(strategy.name)
            except Exception:
                logger.exception("strategy_failed", strategy=strategy.name)
                continue

        # 2. Deduplicate
        deduped = self._deduplicate(all_items)

        # 2a. Fuzzy/semantic dedup (Gap 3.2). Rejected items are discarded
        # for the sectioned path because each section builds its own report
        # later; the shared pool just needs to be collapsed.
        if self._semantic_dedup is not None:
            deduped, _ = self._semantic_dedup_tracked(deduped, self._semantic_dedup)

        # 3. Defense-in-depth structural filter.
        if not include_structural:
            deduped = [
                item
                for item in deduped
                if (item.metadata or {}).get("node_role") != "structural"
            ]

        # 3a. Session dedup: drop items recently served in this session.
        if session_id:
            served = self._recently_served_item_ids(
                session_id, window_minutes=session_dedup_window_minutes
            )
            if served:
                deduped = [i for i in deduped if i.item_id not in served]

        # 3b. Rerank the shared candidate pool before section filling.
        if self._reranker is not None:
            try:
                deduped = self._reranker.rerank(intent, deduped)
                logger.debug("reranker_applied_sectioned", reranker=self._reranker.name)
            except Exception:
                logger.exception(
                    "reranker_failed_sectioned", reranker=self._reranker.name
                )

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

            # Apply per-section budget
            selected = matched[: section_budget.max_items]
            selected = self._apply_token_budget(selected, section_budget.max_tokens)

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

        sectioned_pack = SectionedPack(
            intent=intent,
            sections=pack_sections,
            domain=domain,
            agent_id=agent_id,
            session_id=session_id,
            advisories=advisories,
            assembled_at=utc_now(),
        )

        # 5. Emit telemetry
        if self._event_log is not None:
            self._emit_sectioned_telemetry(sectioned_pack)

        return sectioned_pack

    def _emit_sectioned_telemetry(self, pack: SectionedPack) -> None:
        """Emit telemetry event for a sectioned pack."""
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
                "section_count": len(pack.sections),
                "total_items": pack.total_items,
                "sections": [
                    {
                        "name": s.name,
                        "items_count": len(s.items),
                        "item_ids": [i.item_id for i in s.items],
                    }
                    for s in pack.sections
                ],
                "advisory_ids": [a.advisory_id for a in pack.advisories],
                "reranker": self._reranker.name if self._reranker else None,
                "semantic_dedup_enabled": self._semantic_dedup is not None,
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
                logger.exception("pack_quality_event_emit_failed", pack_id=pack.pack_id)

    def _emit_telemetry(self, pack: Pack) -> None:
        """Emit a ContextRetrievalEvent for observability."""
        report = pack.retrieval_report
        token_budget_fields = self._build_token_budget_payload(
            pack.budget.max_tokens,
            excerpts=lambda: [item.excerpt for item in pack.items],
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
                "items_count": len(pack.items),
                "injected_item_ids": [item.item_id for item in pack.items],
                "injected_items": [
                    {
                        "item_id": item.item_id,
                        "item_type": item.item_type,
                        "rank": item.rank,
                        "selection_reason": item.selection_reason,
                        "score_breakdown": item.score_breakdown,
                        "estimated_tokens": item.estimated_tokens,
                        "strategy_source": item.strategy_source,
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
                logger.exception("token_budget_validator_failed")
        return payload

    def _recently_served_item_ids(
        self,
        session_id: str,
        *,
        window_minutes: int = DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
    ) -> set[str]:
        """Return item_ids served to this session within the window.

        Reads ``PACK_ASSEMBLED`` events for ``session_id`` in the last
        ``window_minutes`` and aggregates their ``injected_item_ids``
        (flat packs) and section ``item_ids`` (sectioned packs).
        Returns an empty set if no event log is configured or nothing
        matches — the caller should treat this as "no dedup applied".
        """
        if self._event_log is None:
            return set()
        try:
            since = datetime.now(UTC) - timedelta(minutes=window_minutes)
            events = self._event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED,
                since=since,
                limit=200,
            )
        except Exception:
            logger.exception("session_dedup_event_query_failed")
            return set()

        served: set[str] = set()
        for event in events:
            payload = event.payload or {}
            if payload.get("session_id") != session_id:
                continue
            # Flat pack payload
            for iid in payload.get("injected_item_ids", []) or []:
                served.add(iid)
            # Sectioned pack payload
            for section in payload.get("sections", []) or []:
                for iid in section.get("item_ids", []) or []:
                    served.add(iid)
        return served

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
            logger.exception("advisory_retrieval_failed")
            return []

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
            excerpt = item.excerpt or ""
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

    def _apply_token_budget_tracked(
        self, items: list[PackItem], max_tokens: int
    ) -> tuple[list[PackItem], list[RejectedItem], list[BudgetStep]]:
        """Trim items to fit token budget, tracking rejections."""
        effective = self._effective_token_budget(max_tokens)
        result: list[PackItem] = []
        rejected: list[RejectedItem] = []
        budget_trace: list[BudgetStep] = []
        total_tokens = 0
        budget_exceeded = False

        for item in items:
            item_tokens = self._token_counter.count(item.excerpt)
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
    def _build_filters(
        filters: dict[str, Any] | None,
        tag_filters: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Merge user filters and tag_filters into a single filters dict.

        When ``tag_filters`` is provided but does not include ``signal_quality``,
        the default ``["high", "standard", "low"]`` is applied (excludes noise).
        """
        if tag_filters is None:
            return filters

        effective_tags = dict(tag_filters)
        if "signal_quality" not in effective_tags:
            effective_tags["signal_quality"] = ["high", "standard", "low"]

        merged = dict(filters) if filters else {}
        merged["content_tags"] = effective_tags
        return merged

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
