"""Workflow integration hooks — context in, traces out, results fed back.

These three hooks wrap a :class:`~trellis_sdk.client.TrellisClient` so any
workflow engine can plug into the Trellis experience graph at the natural
boundaries of a step:

* :class:`ContextInjector` — *pre-task*. Fetch a markdown context pack for
  an intent (and optional known entity IDs / domain) to prime an agent.
* :class:`TraceRecorder` — *post-task*. Build and ingest a well-formed
  :class:`Trace` from the step's outcome so the run becomes shared memory.
* :class:`ResultFeedback` — *post-task*. Record evidence (a ``DOCUMENT``
  entity + ``DESCRIBED_BY`` edge) linking a successful result to a graph
  entity, **and** grade the context pack that supported the work via the
  WP2 :meth:`TrellisClient.record_feedback` method.

Graceful degradation is the contract
-------------------------------------
A host agent's task must never fail because Trellis is unreachable. Every
hook method catches :class:`~trellis_sdk.exceptions.TrellisError` subclasses
(which already wrap transport-level ``httpx`` failures into
:class:`~trellis_sdk.exceptions.TrellisTransportError`), logs the failure via
``structlog`` with enough context to debug, and returns a sentinel:

* :class:`ContextInjector` methods return ``""`` (empty context).
* :class:`TraceRecorder.record` returns ``None`` (no trace id).
* :class:`ResultFeedback` methods return a small typed
  :class:`HookResult` with ``ok=False``.

Callers who *want* exceptions (e.g. a strict test harness) pass
``raise_errors=True`` to the hook constructor; degradation is then disabled
and the underlying :class:`TrellisError` propagates.

Deviations from the design brief
--------------------------------
The brief (``docs/plans/workflow-integration-hooks.md``) predates two
architectural decisions and is adapted minimally here:

1. **HTTP-only SDK.** The brief's "local ``TrellisClient(registry=...)``
   mode" no longer exists — the SDK is HTTP-only by deliberate design (an
   isolation test forbids ``trellis.*`` imports inside ``trellis_sdk``). The
   hooks therefore take a constructed :class:`TrellisClient` /
   :class:`AsyncTrellisClient`; the "no server" story for tests and examples
   is :func:`trellis.testing.in_memory_client`, not in-process registry
   access.
2. **ResultFeedback feedback path.** The brief lists only entity + edge
   evidence. WP2 added :meth:`TrellisClient.record_feedback`; this module
   routes pack grading through that method rather than hand-rolling an HTTP
   call, so feedback flows the authoritative EventLog path. The
   ``record_success`` / ``record_failure`` evidence surface from the brief
   is preserved, with an additional optional ``pack_id`` so a result can
   both create evidence and grade the pack that produced it.

The brief's ``ContextInjector.for_intent`` / ``for_entities`` and
``TraceRecorder.record`` signatures are honoured; ``ResultFeedback`` returns
a typed result instead of ``None`` so callers can branch on success without
re-reading logs (still never raises by default).

Sync first. An :class:`AsyncResultFeedback` variant is provided because it is
cheap over :class:`AsyncTrellisClient`; async context-injection and
trace-recording are tracked as a follow-up (the sync hooks cover the
documented workflow-wrapper use case).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis_sdk._format import format_pack_as_markdown
from trellis_sdk.exceptions import TrellisError

if TYPE_CHECKING:
    from trellis_sdk.async_client import AsyncTrellisClient
    from trellis_sdk.client import TrellisClient

logger = structlog.get_logger(__name__)

_VALID_STATUSES = frozenset({"success", "failure", "partial", "unknown"})
_DEFAULT_MAX_TOKENS = 4000
#: Cap on how many entity excerpts the per-entity fallback will stitch
#: together when no pack is available.
_FALLBACK_MAX_ENTITIES = 10


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class HookResult:
    """Outcome of a fire-and-forget hook write.

    ``ok`` is ``False`` whenever Trellis was unreachable or rejected the
    write and the hook degraded gracefully. ``detail`` carries a short
    human-readable reason (the stringified error or a status note);
    ``ids`` holds any IDs the write produced (e.g. the created
    ``DOCUMENT`` entity / edge, or the recorded ``feedback_id``).
    """

    ok: bool
    detail: str = ""
    ids: dict[str, str] | None = None


class ContextInjector:
    """Assembles graph context for a workflow step (pre-task hook).

    Construct once per workflow with a live :class:`TrellisClient`, then
    call :meth:`for_intent` or :meth:`for_entities` before each step to get
    a markdown context string to inject into the agent's prompt. Returns an
    empty string when Trellis is unavailable (unless ``raise_errors=True``).
    """

    def __init__(
        self,
        client: TrellisClient,
        *,
        default_max_tokens: int = _DEFAULT_MAX_TOKENS,
        raise_errors: bool = False,
    ) -> None:
        """Initialise the injector.

        Args:
            client: A constructed (HTTP-only) Trellis SDK client.
            default_max_tokens: Token budget applied when a call omits
                ``max_tokens``.
            raise_errors: When ``True``, propagate
                :class:`~trellis_sdk.exceptions.TrellisError` instead of
                degrading to an empty string.
        """
        self._client = client
        self._default_max_tokens = default_max_tokens
        self._raise_errors = raise_errors

    def for_intent(
        self,
        intent: str,
        *,
        domain: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return markdown context assembled from an intent alone.

        Args:
            intent: What the upcoming step is trying to do.
            domain: Optional domain scope to narrow retrieval.
            max_tokens: Token budget; falls back to ``default_max_tokens``.

        Returns:
            Markdown context string, or ``""`` if Trellis is unavailable.
        """
        budget = max_tokens if max_tokens is not None else self._default_max_tokens
        try:
            pack = self._client.assemble_pack(
                intent,
                domain=domain,
                max_items=20,
                max_tokens=budget,
            )
        except TrellisError as exc:
            return self._degrade("context_injection_failed", intent, domain, exc)
        items = pack.get("items", [])
        if not items:
            return ""
        return format_pack_as_markdown(
            items,
            intent,
            max_tokens=budget,
            pack_id=pack.get("pack_id"),
        )

    def for_entities(
        self,
        entity_ids: list[str],
        *,
        intent: str = "",
        domain: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return markdown context for a set of known entity IDs.

        Tries the pack builder first (richer, retrieval-shaped output). If
        the pack comes back empty, falls back to per-entity lookups and
        stitches their excerpts into markdown sections. The token budget is
        split across the included entities rather than handed wholesale to
        the pack builder.

        Args:
            entity_ids: Graph node IDs the step will operate on.
            intent: Optional intent to bias pack retrieval.
            domain: Optional domain scope.
            max_tokens: Token budget; falls back to ``default_max_tokens``.

        Returns:
            Markdown context string, or ``""`` if Trellis is unavailable
            or nothing matched.
        """
        budget = max_tokens if max_tokens is not None else self._default_max_tokens
        effective_intent = intent or (
            f"context for entities: {', '.join(entity_ids[:5])}"
        )
        try:
            pack = self._client.assemble_pack(
                effective_intent,
                domain=domain,
                max_items=max(len(entity_ids), 20),
                max_tokens=budget,
            )
            items = pack.get("items", [])
            if items:
                return format_pack_as_markdown(
                    items,
                    effective_intent,
                    max_tokens=budget,
                    pack_id=pack.get("pack_id"),
                )
            return self._entity_fallback(entity_ids, effective_intent, budget)
        except TrellisError as exc:
            return self._degrade(
                "entity_context_injection_failed", effective_intent, domain, exc
            )

    def _entity_fallback(
        self,
        entity_ids: list[str],
        intent: str,
        max_tokens: int,
    ) -> str:
        """Per-entity lookup fallback when the pack builder returns nothing.

        Splits ``max_tokens`` evenly across the (capped) entity set so a
        single verbose entity can't blow the whole budget. Any entity that
        404s or errors is skipped, not fatal.
        """
        included = entity_ids[:_FALLBACK_MAX_ENTITIES]
        if not included:
            return ""
        per_entity_chars = max(1, (max_tokens // len(included))) * 4
        lines = [f"# Context for: {intent}", ""]
        found = 0
        for entity_id in included:
            try:
                entity = self._client.get_entity(entity_id)
            except TrellisError as exc:
                logger.warning(
                    "entity_context_lookup_failed",
                    entity_id=entity_id,
                    error=str(exc),
                )
                continue
            if entity is None:
                continue
            props = entity.get("properties", {})
            name = entity_id
            if isinstance(props, dict):
                name = props.get("name", entity_id)
            excerpt = ""
            if isinstance(props, dict):
                excerpt = str(props.get("description", "") or props.get("summary", ""))
            lines.append(f"## {name} (`{entity_id}`)")
            lines.append(excerpt[:per_entity_chars])
            lines.append("")
            found += 1
        if found == 0:
            return ""
        return "\n".join(lines)

    def _degrade(
        self,
        event: str,
        intent: str,
        domain: str | None,
        exc: TrellisError,
    ) -> str:
        if self._raise_errors:
            raise exc
        logger.warning(
            event,
            intent=intent[:200],
            domain=domain,
            error=str(exc),
        )
        return ""


class TraceRecorder:
    """Records workflow step executions as immutable Traces (post-task hook).

    The ``workflow_id`` ties every step recorded by one instance together
    into a single pipeline run (it lands on the trace's
    ``context.workflow_id``). Both success and failure are recorded —
    failure traces are how the graph learns from mistakes.
    """

    def __init__(
        self,
        client: TrellisClient,
        workflow_id: str,
        *,
        agent_id: str = "workflow",
        domain: str | None = None,
        raise_errors: bool = False,
    ) -> None:
        """Initialise the recorder.

        Args:
            client: A constructed (HTTP-only) Trellis SDK client.
            workflow_id: Stable ID grouping all steps of one run.
            agent_id: Recorded as ``context.agent_id`` (who ran the step).
            domain: Optional default domain stamped on every trace's
                ``context.domain``.
            raise_errors: When ``True``, propagate
                :class:`~trellis_sdk.exceptions.TrellisError` instead of
                returning ``None``.
        """
        self._client = client
        self._workflow_id = workflow_id
        self._agent_id = agent_id
        self._domain = domain
        self._raise_errors = raise_errors

    def record(
        self,
        step_name: str,
        status: str,
        duration_ms: int,
        *,
        entity_ids: list[str] | None = None,
        summary: str = "",
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
        domain: str | None = None,
    ) -> str | None:
        """Build and ingest a Trace for one workflow step.

        Args:
            step_name: Name of the step / skill that ran.
            status: ``success`` | ``failure`` | ``partial`` | ``unknown``.
                Any other value is coerced to ``unknown`` (never raises).
            duration_ms: Wall-clock duration of the step.
            entity_ids: Graph entities the step touched; recorded in
                ``metadata.entity_ids`` for downstream linking.
            summary: Human-readable outcome summary.
            metrics: Optional numeric/string metrics for the outcome.
            error: Error detail when ``status != success``; recorded on the
                step so failures are debuggable.
            domain: Per-call domain override (else the instance default).

        Returns:
            The new ``trace_id``, or ``None`` if ingestion failed and the
            hook degraded gracefully.
        """
        normalized_status = status if status in _VALID_STATUSES else "unknown"
        if normalized_status != status:
            logger.warning(
                "trace_status_coerced",
                given=status,
                coerced_to=normalized_status,
                step_name=step_name,
            )
        now = _utc_now_iso()
        step: dict[str, Any] = {
            "step_type": "workflow_step",
            "name": step_name,
            "duration_ms": duration_ms,
            "started_at": now,
        }
        if error:
            step["error"] = error
        trace: dict[str, Any] = {
            "source": "workflow",
            "intent": step_name,
            "steps": [step],
            "outcome": {
                "status": normalized_status,
                "summary": summary,
                "metrics": metrics or {},
            },
            "context": {
                "agent_id": self._agent_id,
                "workflow_id": self._workflow_id,
                "domain": domain or self._domain,
                "started_at": now,
                "ended_at": now,
            },
            "metadata": {"entity_ids": entity_ids or []},
        }
        try:
            trace_id = self._client.ingest_trace(trace)
        except TrellisError as exc:
            if self._raise_errors:
                raise
            logger.warning(
                "trace_recording_failed",
                workflow_id=self._workflow_id,
                step_name=step_name,
                status=normalized_status,
                error=str(exc),
            )
            return None
        logger.debug(
            "trace_recorded",
            workflow_id=self._workflow_id,
            step_name=step_name,
            trace_id=trace_id,
        )
        return trace_id


class ResultFeedback:
    """Records evidence and pack feedback for workflow results (post-task hook).

    Two surfaces:

    * :meth:`record_success` — creates a ``DOCUMENT`` entity describing the
      result and a ``DESCRIBED_BY`` edge to the target entity, then
      optionally grades the supporting pack.
    * :meth:`record_failure` — failure is captured in traces, so this only
      grades the supporting pack (negative) and logs; it creates no
      evidence.

    Pack grading always routes through :meth:`TrellisClient.record_feedback`
    (the WP2 method) so the signal flows the authoritative EventLog path —
    never a hand-rolled HTTP call.
    """

    _DESCRIBED_BY = "described_by"

    def __init__(
        self,
        client: TrellisClient,
        *,
        raise_errors: bool = False,
    ) -> None:
        """Initialise the feedback hook.

        Args:
            client: A constructed (HTTP-only) Trellis SDK client.
            raise_errors: When ``True``, propagate
                :class:`~trellis_sdk.exceptions.TrellisError` instead of
                returning a ``HookResult`` with ``ok=False``.
        """
        self._client = client
        self._raise_errors = raise_errors

    def record_success(
        self,
        target_entity_id: str,
        result_name: str,
        summary: str,
        *,
        full_content: str | None = None,
        metadata: dict[str, Any] | None = None,
        pack_id: str | None = None,
        helpful_item_ids: list[str] | None = None,
    ) -> HookResult:
        """Record a successful result as graph evidence (+ optional feedback).

        Creates a ``DOCUMENT`` entity for the result and links it to the
        target entity with a ``DESCRIBED_BY`` edge. When ``pack_id`` is
        given, also records *positive* pack feedback via
        :meth:`TrellisClient.record_feedback`.

        Args:
            target_entity_id: Graph entity the result describes / produces.
            result_name: Short name for the result (entity name).
            summary: One-line description stored on the document entity.
            full_content: Optional full result body (stored in properties).
            metadata: Extra properties merged onto the document entity.
            pack_id: Pack that supported the work, to grade positively.
            helpful_item_ids: Pack item IDs that actually helped.

        Returns:
            :class:`HookResult`. ``ok`` is ``True`` only when the evidence
            write succeeded; ``ids`` carries ``document_id``, ``edge_id``,
            and ``feedback_id`` when present.
        """
        ids: dict[str, str] = {}
        try:
            properties: dict[str, Any] = {
                "name": result_name,
                "summary": summary,
            }
            if full_content is not None:
                properties["content"] = full_content
            if metadata:
                properties.update(metadata)
            document_id = self._client.create_entity(
                result_name,
                entity_type="document",
                properties=properties,
            )
            ids["document_id"] = document_id
            edge_id = self._client.create_link(
                document_id,
                target_entity_id,
                edge_kind=self._DESCRIBED_BY,
            )
            ids["edge_id"] = edge_id
        except TrellisError as exc:
            if self._raise_errors:
                raise
            logger.warning(
                "result_feedback_evidence_failed",
                target_entity_id=target_entity_id,
                result_name=result_name,
                error=str(exc),
            )
            return HookResult(ok=False, detail=str(exc), ids=ids or None)

        if pack_id is not None:
            feedback_id = self._grade_pack(
                pack_id,
                success=True,
                helpful_item_ids=helpful_item_ids,
                target_id=document_id,
            )
            if feedback_id is not None:
                ids["feedback_id"] = feedback_id

        logger.debug(
            "result_feedback_recorded",
            target_entity_id=target_entity_id,
            document_id=ids.get("document_id"),
        )
        return HookResult(ok=True, detail="evidence recorded", ids=ids)

    def record_failure(
        self,
        target_entity_id: str,
        error_summary: str,
        *,
        trace_id: str | None = None,
        pack_id: str | None = None,
        unhelpful_item_ids: list[str] | None = None,
    ) -> HookResult:
        """Record a failed result — feedback only, no evidence document.

        Failure detail lives in the trace (see :class:`TraceRecorder`), so
        this creates no ``DOCUMENT`` entity. When ``pack_id`` is given it
        records *negative* pack feedback so a pack that led to a failure is
        graded down.

        Args:
            target_entity_id: Graph entity the failed work targeted.
            error_summary: Short description of what went wrong.
            trace_id: Optional trace that captured the failure (logged).
            pack_id: Pack that supported the failed work, to grade down.
            unhelpful_item_ids: Pack item IDs that were noise / misleading.

        Returns:
            :class:`HookResult`. ``ok`` reflects whether any feedback write
            succeeded; a no-op (no ``pack_id``) returns ``ok=True``.
        """
        logger.info(
            "result_failure_noted",
            target_entity_id=target_entity_id,
            error_summary=error_summary[:200],
            trace_id=trace_id,
        )
        if pack_id is None:
            return HookResult(ok=True, detail="failure noted (no pack to grade)")
        feedback_id = self._grade_pack(
            pack_id,
            success=False,
            unhelpful_item_ids=unhelpful_item_ids,
            target_id=trace_id,
        )
        if feedback_id is None:
            return HookResult(ok=False, detail="pack feedback failed")
        return HookResult(
            ok=True,
            detail="negative feedback recorded",
            ids={"feedback_id": feedback_id},
        )

    def _grade_pack(
        self,
        pack_id: str,
        *,
        success: bool,
        helpful_item_ids: list[str] | None = None,
        unhelpful_item_ids: list[str] | None = None,
        target_id: str | None = None,
    ) -> str | None:
        """Grade a pack via the WP2 ``record_feedback`` method.

        Returns the ``feedback_id`` on success, or ``None`` on graceful
        degradation. Re-raises when ``raise_errors`` is set.
        """
        try:
            response = self._client.record_feedback(
                pack_id,
                success,
                helpful_item_ids=helpful_item_ids,
                unhelpful_item_ids=unhelpful_item_ids,
                target_id=target_id,
            )
        except TrellisError as exc:
            if self._raise_errors:
                raise
            logger.warning(
                "result_feedback_grade_failed",
                pack_id=pack_id,
                success=success,
                error=str(exc),
            )
            return None
        return response.feedback_id


class AsyncResultFeedback:
    """Async variant of :class:`ResultFeedback` over :class:`AsyncTrellisClient`.

    Only the feedback hook has an async variant for now — it is the cheapest
    to provide and the one most likely to run inside an async agent loop.
    Async context injection and trace recording are a follow-up.
    """

    _DESCRIBED_BY = "described_by"

    def __init__(
        self,
        client: AsyncTrellisClient,
        *,
        raise_errors: bool = False,
    ) -> None:
        """Initialise the async feedback hook.

        Args:
            client: A constructed async (HTTP-only) Trellis SDK client.
            raise_errors: When ``True``, propagate
                :class:`~trellis_sdk.exceptions.TrellisError` instead of
                returning a degraded :class:`HookResult`.
        """
        self._client = client
        self._raise_errors = raise_errors

    async def record_success(
        self,
        target_entity_id: str,
        result_name: str,
        summary: str,
        *,
        full_content: str | None = None,
        metadata: dict[str, Any] | None = None,
        pack_id: str | None = None,
        helpful_item_ids: list[str] | None = None,
    ) -> HookResult:
        """Async :meth:`ResultFeedback.record_success`."""
        ids: dict[str, str] = {}
        try:
            properties: dict[str, Any] = {
                "name": result_name,
                "summary": summary,
            }
            if full_content is not None:
                properties["content"] = full_content
            if metadata:
                properties.update(metadata)
            document_id = await self._client.create_entity(
                result_name,
                entity_type="document",
                properties=properties,
            )
            ids["document_id"] = document_id
            edge_id = await self._client.create_link(
                document_id,
                target_entity_id,
                edge_kind=self._DESCRIBED_BY,
            )
            ids["edge_id"] = edge_id
        except TrellisError as exc:
            if self._raise_errors:
                raise
            logger.warning(
                "result_feedback_evidence_failed",
                target_entity_id=target_entity_id,
                result_name=result_name,
                error=str(exc),
            )
            return HookResult(ok=False, detail=str(exc), ids=ids or None)

        if pack_id is not None:
            try:
                response = await self._client.record_feedback(
                    pack_id,
                    True,
                    helpful_item_ids=helpful_item_ids,
                    target_id=document_id,
                )
                ids["feedback_id"] = response.feedback_id
            except TrellisError as exc:
                if self._raise_errors:
                    raise
                logger.warning(
                    "result_feedback_grade_failed",
                    pack_id=pack_id,
                    success=True,
                    error=str(exc),
                )
        return HookResult(ok=True, detail="evidence recorded", ids=ids)


__all__ = [
    "AsyncResultFeedback",
    "ContextInjector",
    "HookResult",
    "ResultFeedback",
    "TraceRecorder",
]
