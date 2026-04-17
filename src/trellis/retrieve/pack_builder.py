"""Pack builder — orchestrates search strategies to assemble retrieval packs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.hashing import estimate_tokens
from trellis.retrieve.rerankers.base import Reranker
from trellis.retrieve.strategies import SearchStrategy
from trellis.retrieve.tier_mapping import TierMapper
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
    ) -> None:
        self._strategies = strategies or []
        self._event_log = event_log
        self._advisory_store = advisory_store
        self._reranker = reranker

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
            },
        )

    def _emit_telemetry(self, pack: Pack) -> None:
        """Emit a ContextRetrievalEvent for observability."""
        report = pack.retrieval_report
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
            },
        )

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

    def _apply_token_budget(
        self, items: list[PackItem], max_tokens: int
    ) -> list[PackItem]:
        """Trim items to fit within token budget (estimated at ~4 chars per token)."""
        result: list[PackItem] = []
        total_tokens = 0
        for item in items:
            item_tokens = estimate_tokens(item.excerpt)
            if total_tokens + item_tokens > max_tokens:
                break
            result.append(item)
            total_tokens += item_tokens
        return result

    def _apply_token_budget_tracked(
        self, items: list[PackItem], max_tokens: int
    ) -> tuple[list[PackItem], list[RejectedItem], list[BudgetStep]]:
        """Trim items to fit token budget, tracking rejections."""
        result: list[PackItem] = []
        rejected: list[RejectedItem] = []
        budget_trace: list[BudgetStep] = []
        total_tokens = 0
        budget_exceeded = False

        for item in items:
            item_tokens = estimate_tokens(item.excerpt)
            if not budget_exceeded and total_tokens + item_tokens <= max_tokens:
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
            estimated_tokens = estimate_tokens(item.excerpt)
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
