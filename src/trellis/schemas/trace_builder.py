"""Fluent builder for trace payloads submitted to Trellis."""

from __future__ import annotations

import datetime
from typing import Any

_VALID_STATUSES = {"success", "failure", "partial", "unknown"}


class TracePayloadBuilder:
    """Fluent builder that assembles a trace payload dict.

    Minimal required fields: ``source`` and ``intent``.  All other sections
    are optional and compose via chained method calls.

    Example::

        payload = (
            TracePayloadBuilder(source="workflow", intent="run nightly etl")
            .set_context(agent_id="my-agent", domain="data_engineering")
            .add_step(step_type="sql", name="create_view")
            .add_artifact(artifact_id="/tmp/out.sql")
            .set_outcome(status="success", summary="completed ok")
            .set_metadata(run_id="abc123")
            .build()
        )
    """

    def __init__(self, *, source: str, intent: str) -> None:
        source = str(source).strip()
        intent = str(intent).strip()
        if not source:
            msg = "source must not be blank"
            raise ValueError(msg)
        if not intent:
            msg = "intent must not be blank"
            raise ValueError(msg)

        self._source = source
        self._intent = intent
        self._steps: list[dict[str, Any]] = []
        self._artifacts: list[dict[str, str]] = []
        self._outcome: dict[str, Any] | None = None
        self._context: dict[str, Any] | None = None
        self._metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step accumulation
    # ------------------------------------------------------------------

    def add_step(
        self,
        *,
        step_type: str,
        name: str,
        args: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        started_at: str | datetime.datetime | None = None,
    ) -> TracePayloadBuilder:
        """Append a single step to the trace.

        Args:
            step_type: Category label for the step (e.g. ``"sql"``, ``"event"``).
            name: Human-readable step name.
            args: Optional key/value arguments that were passed to the step.
            result: Optional key/value output produced by the step.
            started_at: ISO-8601 string or ``datetime`` for when the step began.

        Returns:
            ``self`` for chaining.
        """
        step: dict[str, Any] = {
            "step_type": str(step_type).strip(),
            "name": str(name).strip(),
            "args": dict(args) if args else {},
            "result": dict(result) if result else {},
        }
        if started_at is not None:
            step["started_at"] = (
                started_at.isoformat()
                if isinstance(started_at, datetime.datetime)
                else str(started_at)
            )
        self._steps.append(step)
        return self

    # ------------------------------------------------------------------
    # Artifact accumulation
    # ------------------------------------------------------------------

    def add_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str = "file",
    ) -> TracePayloadBuilder:
        """Append an artifact reference produced by this trace.

        Args:
            artifact_id: Identifier or path for the artifact.
            artifact_type: Kind of artifact (default ``"file"``).

        Returns:
            ``self`` for chaining.
        """
        self._artifacts.append(
            {
                "artifact_id": str(artifact_id),
                "artifact_type": str(artifact_type),
            }
        )
        return self

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------

    def set_outcome(
        self,
        *,
        status: str,
        metrics: dict[str, Any] | None = None,
        summary: str = "",
    ) -> TracePayloadBuilder:
        """Set the outcome for this trace.

        Args:
            status: One of ``success``, ``failure``, ``partial``, ``unknown``.
            metrics: Optional dict of numeric or string metrics.
            summary: Optional human-readable outcome summary.

        Returns:
            ``self`` for chaining.

        Raises:
            ValueError: If ``status`` is not one of the accepted values.
        """
        normalized = str(status).strip().lower()
        if normalized not in _VALID_STATUSES:
            msg = f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}"
            raise ValueError(msg)
        self._outcome = {
            "status": normalized,
            "metrics": dict(metrics) if metrics else {},
            "summary": str(summary),
        }
        return self

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def set_context(
        self,
        *,
        agent_id: str,
        domain: str = "",
        workflow_id: str = "",
        started_at: str | datetime.datetime | None = None,
        ended_at: str | datetime.datetime | None = None,
    ) -> TracePayloadBuilder:
        """Set the execution context for this trace.

        Args:
            agent_id: Identifier for the agent that produced the trace.
            domain: Optional business or system domain label.
            workflow_id: Optional identifier for the parent workflow or run.
            started_at: ISO-8601 string or ``datetime`` for when execution began.
            ended_at: ISO-8601 string or ``datetime`` for when execution finished.

        Returns:
            ``self`` for chaining.
        """

        def _coerce_dt(value: str | datetime.datetime | None) -> str | None:
            if value is None:
                return None
            return (
                value.isoformat()
                if isinstance(value, datetime.datetime)
                else str(value)
            )

        self._context = {
            "agent_id": str(agent_id).strip(),
            "domain": str(domain),
            "workflow_id": str(workflow_id),
            "started_at": _coerce_dt(started_at),
            "ended_at": _coerce_dt(ended_at),
        }
        return self

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def set_metadata(self, **kwargs: Any) -> TracePayloadBuilder:
        """Merge arbitrary key/value pairs into the trace metadata dict.

        Existing keys are overwritten by new values.  Call multiple times to
        accumulate metadata incrementally.

        Returns:
            ``self`` for chaining.
        """
        self._metadata.update(kwargs)
        return self

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> dict[str, Any]:
        """Validate and return the assembled payload dict.

        Returns:
            A plain ``dict`` ready to be serialised and posted to the
            trellis-ai trace ingest endpoint.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        payload: dict[str, Any] = {
            "source": self._source,
            "intent": self._intent,
            "steps": list(self._steps),
            "artifacts_produced": list(self._artifacts),
        }
        if self._outcome is not None:
            payload["outcome"] = dict(self._outcome)
        if self._context is not None:
            payload["context"] = dict(self._context)
        if self._metadata:
            payload["metadata"] = dict(self._metadata)
        return payload
