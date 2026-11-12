"""``record_observation`` and ``query_observations``."""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.auth import SCOPE_MUTATE, SCOPE_READ
from trellis.core.error_sanitize import describe_validation_error
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_clearance, trellis_scope
from trellis.mcp.boundary import (
    _exception_detail,
    _refuse_public_caller,
    _result_message,
)
from trellis.mcp.wiring import _get_registry
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.retrieve.precedents import PrecedentClearance
from trellis.retrieve.sensitivity import conceal_node_pointers

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Macro Tool: record_observation / query_observations (Item 1 Phase 1)
# ---------------------------------------------------------------------------
#
# Measurement is intentionally *not* exposed as an MCP tool in Phase 1.
# Measurement rows are append-only by convention (see ADR
# ``adr-observation-entity-type.md`` §2.1 / §5.6) and are produced
# primarily by automated metric streams, where per-call MCP overhead is
# the wrong shape — those callers should use the REST endpoint
# (`POST /api/v1/measurements`) or the SDK (`record_measurement`) which
# both go through the same governed pipeline. Reconsider exposing
# Measurement on MCP if agent-driven scalar capture becomes a real
# workload.


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def record_observation(
    subject_entity_id: str,
    subject_entity_type: str,
    observer_agent_id: str,
    content: str,
    confidence: float,
    evidence_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Record an empirical Observation about a subject entity.

    See ``docs/design/adr-observation-entity-type.md`` for the data model
    rationale. The Observation lands as a graph node with a
    ``hasObservation`` edge from the subject entity. Missing required
    fields surface as a JSON error envelope — no silent defaults.

    Args:
        subject_entity_id: The entity the observation is *about*.
        subject_entity_type: Open-string entity type of the subject.
        observer_agent_id: Which agent (human or automated) produced this.
        content: Narrative description of the observation.
        confidence: Producer confidence in ``[0.0, 1.0]``.
        evidence_ref: Optional id of a stored document, readable with
            this key, that supports the observation. An id naming none is
            refused, and an observation id keeps the one it has.
        metadata: Optional bag for conventional keys (kind, window_start,
            window_end, sample_size, method, …). See ADR §2.3.

    Returns:
        A JSON object with ``status``, ``observation_id`` (on success),
        or ``message`` on failure. This tool never raises to MCP.
    """
    from trellis.schemas.observation import Observation  # noqa: PLC0415
    from trellis.schemas.well_known import OBSERVATION  # noqa: PLC0415

    try:
        obs = Observation(
            subject_entity_id=subject_entity_id,
            subject_entity_type=subject_entity_type,
            observer_agent_id=observer_agent_id,
            content=content,
            confidence=confidence,
            evidence_ref=evidence_ref,
            metadata=metadata or {},
        )
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client (see docstring). Returns structured {"status": "error"}
    # JSON so the caller can branch on the response.
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": f"Invalid observation: {describe_validation_error(exc)}",
            }
        )

    try:
        command = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": obs},
            target_id=obs.observation_id,
            target_type=OBSERVATION,
            requested_by="mcp:record_observation",
        )
        executor = build_curate_executor(_get_registry(), clearance=current_clearance())
        result = executor.execute(command)
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client; execution failure is logged + surfaced as a structured
    # error response.
    except Exception as exc:
        logger.exception("record_observation_failed")
        detail = _exception_detail(exc)
        return json.dumps({"status": "error", "message": f"Execution failed: {detail}"})

    if result.status != CommandStatus.SUCCESS:
        return json.dumps(
            {
                "status": result.status.value,
                "message": _result_message(result),
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "observation_id": result.created_id or obs.observation_id,
        }
    )


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def query_observations(
    subject_entity_id: str = "",
    observer_agent_id: str = "",
    limit: int = 100,
) -> str:
    """Query Observation nodes by subject and/or observer.

    Args:
        subject_entity_id: Filter by subject entity id (empty = no filter).
        observer_agent_id: Filter by observer agent id (empty = no filter).
        limit: Maximum results to return (default 100).

    Returns:
        A JSON object with ``observations``: a list of Observation
        property dicts. Each dict carries ``node_id`` plus the
        schema's payload fields.
    """
    _refuse_public_caller("query_observations")
    from trellis.schemas.well_known import OBSERVATION  # noqa: PLC0415

    registry = _get_registry()
    props: dict[str, Any] = {}
    if subject_entity_id.strip():
        props["subject_entity_id"] = subject_entity_id.strip()
    # observer_agent_id is an identifier, not free text: every producer
    # sets it to an opaque agent handle, and this filter matches it
    # exactly. observation.record and measurement.record both refuse a
    # hazardous value at write time (see NESTED_ID_ARG_PATHS in
    # mutate/commands.py); an observer draft minted through entity.create
    # instead (trellis/extract/commands.py) is not scanned for one.
    if observer_agent_id.strip():
        props["observer_agent_id"] = observer_agent_id.strip()

    try:
        rows = registry.knowledge.graph_store.query(
            node_type=OBSERVATION,
            properties=props or None,
            limit=limit,
        )
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client; backend errors are logged and surfaced as a structured
    # error response.
    except Exception as exc:
        logger.exception("query_observations_failed")
        detail = _exception_detail(exc)
        return json.dumps({"status": "error", "message": f"Query failed: {detail}"})

    # A node carries no label, so every match is served; what is dropped is an
    # ``evidence_ref`` naming a document this caller may not read.
    clearance = current_clearance()
    rows = conceal_node_pointers(
        rows,
        registry.knowledge.document_store,
        clearance,
        precedents=PrecedentClearance(
            registry.knowledge.document_store,
            clearance,
            registry.knowledge.graph_store,
        ),
    )
    projected: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row.get("properties", {}))
        item["node_id"] = row.get("node_id")
        item["node_type"] = row.get("node_type")
        projected.append(item)
    return json.dumps({"status": "ok", "observations": projected})
