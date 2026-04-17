"""Learning worker — outcome analysis and precedent extraction from traces."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from trellis.core.ids import generate_ulid
from trellis.llm import Message
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.precedent import Precedent
from trellis.schemas.trace import Trace
from trellis.stores.event_log import EventType

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis.stores.event_log import EventLog
    from trellis.stores.trace import TraceStore

logger = structlog.get_logger(__name__)

# Heuristic confidence scores per outcome status.
_OUTCOME_CONFIDENCE: dict[OutcomeStatus, float] = {
    OutcomeStatus.SUCCESS: 0.7,
    OutcomeStatus.PARTIAL: 0.4,
    OutcomeStatus.FAILURE: 0.3,
    OutcomeStatus.UNKNOWN: 0.2,
}


class PrecedentMiner:
    """Extracts precedent candidates from traces.

    Analyzes traces — especially failure/partial outcomes — to identify
    patterns and generate precedent candidates using LLM analysis.
    """

    def __init__(
        self,
        trace_store: TraceStore,
        event_log: EventLog | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._event_log = event_log
        self._llm = llm

    # ------------------------------------------------------------------
    # Deterministic extraction (no LLM)
    # ------------------------------------------------------------------

    def extract_precedent_from_trace(self, trace_id: str) -> Precedent | None:
        """Create a precedent directly from a single trace.

        Deterministic — no LLM.  Extracts structure from the trace.
        Returns ``None`` if the trace is not found or has no outcome.
        """
        trace = self._trace_store.get(trace_id)
        if trace is None or trace.outcome is None:
            return None

        description = trace.outcome.summary or f"Trace {trace.source} execution"
        domain = trace.context.domain if trace.context else None
        applicability = [domain] if domain else []

        confidence = _OUTCOME_CONFIDENCE.get(trace.outcome.status, 0.2)

        precedent = Precedent(
            precedent_id=generate_ulid(),
            source_trace_ids=[trace.trace_id],
            title=f"{trace.intent[:80]}",
            description=description,
            pattern=None,
            applicability=applicability,
            confidence=confidence,
            promoted_by="precedent_miner",
            evidence_refs=[ref.evidence_id for ref in trace.evidence_used],
        )

        if self._event_log is not None:
            self._event_log.emit(
                EventType.PRECEDENT_PROMOTED,
                source="precedent_miner",
                entity_id=precedent.precedent_id,
                entity_type="precedent",
                payload={
                    "trace_id": trace_id,
                    "outcome_status": trace.outcome.status.value,
                    "action": "precedent_extracted",
                },
            )

        return precedent

    # ------------------------------------------------------------------
    # LLM-driven candidate generation
    # ------------------------------------------------------------------

    async def generate_precedent_candidates(
        self,
        *,
        domain: str | None = None,
        min_traces: int = 3,
        limit: int = 100,
    ) -> list[Precedent]:
        """Generate precedent candidates from failure/partial traces using LLM.

        Analyzes failure patterns across traces and generates generalized
        precedents (lessons learned).

        Args:
            domain: Optional domain filter.
            min_traces: Minimum failure traces required.
            limit: Max traces to analyze.

        Returns:
            List of generated Precedent objects.
        """
        if self._llm is None:
            return []

        traces = self._trace_store.query(domain=domain, limit=limit)

        # Focus on failure/partial traces
        failure_traces = [
            t
            for t in traces
            if t.outcome
            and t.outcome.status
            in (
                OutcomeStatus.FAILURE,
                OutcomeStatus.PARTIAL,
            )
        ]

        if len(failure_traces) < min_traces:
            return []

        # Build summaries for LLM analysis (cap at 20)
        analyzed = failure_traces[:20]
        summaries = "\n".join(
            f"- Intent: {t.intent}, "
            f"Outcome: {t.outcome.status.value}, "
            f"Domain: {t.context.domain or 'N/A'}, "
            f"Summary: {t.outcome.summary or 'N/A'}"
            for t in analyzed
            if t.outcome
        )

        prompt = (
            f"Analyze these {len(analyzed)} failed/partial traces "
            f"and identify common patterns.\n"
            f"For each pattern, provide:\n"
            f"- title: Short descriptive title\n"
            f"- description: What the pattern is\n"
            f"- pattern: Generalized pattern\n"
            f"- confidence: 0.0-1.0\n\n"
            f"Traces:\n{summaries}\n\n"
            f"Respond in JSON format:\n"
            f'[{{"title": "...", "description": "...", '
            f'"pattern": "...", "confidence": 0.8}}]'
        )

        try:
            response = await self._llm.generate(
                messages=[
                    Message(
                        role="system",
                        content=(
                            "You are an experience analyst. "
                            "Identify patterns in failed traces."
                        ),
                    ),
                    Message(role="user", content=prompt),
                ],
            )
        except Exception:
            logger.exception("precedent_generation_llm_error")
            return []

        if response.usage is not None:
            logger.info(
                "precedent_generation_llm_usage",
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                model=response.model,
            )

        return self._parse_candidates(response.content, analyzed, domain)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_candidates(
        self,
        response: str,
        analyzed: list[Trace],
        domain: str | None,
    ) -> list[Precedent]:
        """Parse LLM response into ``Precedent`` objects."""
        try:
            text = response.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("precedent_parse_error", response=response[:200])
            return []

        if not isinstance(parsed, list):
            return []

        source_ids = [t.trace_id for t in analyzed]
        precedents: list[Precedent] = []

        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            description = item.get("description", "")
            if not title or not description:
                continue

            confidence = float(item.get("confidence", 0.5))

            precedent = Precedent(
                precedent_id=generate_ulid(),
                source_trace_ids=source_ids,
                title=title,
                description=description,
                pattern=item.get("pattern"),
                applicability=[domain] if domain else [],
                confidence=min(max(confidence, 0.0), 1.0),
                promoted_by="precedent_miner",
            )
            precedents.append(precedent)

            if self._event_log is not None:
                self._event_log.emit(
                    EventType.PRECEDENT_PROMOTED,
                    source="precedent_miner",
                    entity_id=precedent.precedent_id,
                    entity_type="precedent",
                    payload={
                        "action": "precedent_candidate_generated",
                        "title": precedent.title,
                        "confidence": precedent.confidence,
                    },
                )

        logger.info(
            "precedent_candidates_generated",
            count=len(precedents),
            domain=domain,
        )

        return precedents
