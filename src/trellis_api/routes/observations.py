"""Observation + Measurement routes — Item 1 Phase 1.

Exposes the empirical-observation entity types through the REST API.
Both POST endpoints push the payload through the governed mutation
pipeline (``ObservationRecordHandler`` / ``MeasurementRecordHandler``)
so validation, policy gates, idempotency, and audit events apply
uniformly with the rest of the curate surface.

See:

* ``docs/design/adr-observation-entity-type.md`` — entity-type rationale.
* ``docs/design/plan-self-improvement-program.md`` §Item 1 Phase 1 — the
  three-surface (SDK / MCP / REST) parity requirement that this router
  implements.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query

from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
)
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.schemas.well_known import MEASUREMENT, OBSERVATION
from trellis_api.app import get_registry

logger = structlog.get_logger(__name__)

router = APIRouter()


def _projected_node(node: dict[str, Any]) -> dict[str, Any]:
    """Strip SCD-2 plumbing from a graph node dict for wire response.

    Callers want the Observation / Measurement payload, not the
    SCD-2 ``version_id`` / ``valid_from`` / ``valid_to`` columns. The
    properties bag already carries the schema's payload verbatim, so we
    project that as the top-level body and keep ``node_id`` /
    ``node_type`` alongside for clarity.
    """
    props = dict(node.get("properties", {}))
    return {
        "node_id": node.get("node_id"),
        "node_type": node.get("node_type"),
        **props,
    }


@router.post("/observations", status_code=201)
def record_observation(body: dict[str, Any]) -> dict[str, Any]:
    """Record an :class:`Observation` through the governed pipeline.

    Body shape: the full Observation Pydantic payload (see
    ``src/trellis/schemas/observation.py``). Missing required fields
    surface as a 422 — the handler raises ``ValidationError`` and the
    executor turns that into a structured rejection.
    """
    try:
        obs = Observation.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid observation: {exc}"
        ) from exc

    executor = build_curate_executor(get_registry())
    result = executor.execute(
        Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": obs},
            target_id=obs.observation_id,
            target_type=OBSERVATION,
            requested_by="api:record-observation",
        )
    )
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(status_code=400, detail=result.message)

    return {
        "status": "ok",
        "observation_id": result.created_id or obs.observation_id,
    }


@router.get("/observations")
def list_observations(
    subject_entity_id: str | None = Query(None),
    observer_agent_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """Query Observation nodes by subject and/or observer.

    Filters apply on the graph store's property-level WHERE clause —
    matches against the same keys the Pydantic schema declares.
    """
    registry = get_registry()
    props: dict[str, Any] = {}
    if subject_entity_id is not None:
        props["subject_entity_id"] = subject_entity_id
    if observer_agent_id is not None:
        props["observer_agent_id"] = observer_agent_id

    rows = registry.knowledge.graph_store.query(
        node_type=OBSERVATION,
        properties=props or None,
        limit=limit,
    )
    return {"observations": [_projected_node(r) for r in rows]}


@router.post("/measurements", status_code=201)
def record_measurement(body: dict[str, Any]) -> dict[str, Any]:
    """Record a :class:`Measurement` through the governed pipeline."""
    try:
        meas = Measurement.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid measurement: {exc}"
        ) from exc

    executor = build_curate_executor(get_registry())
    result = executor.execute(
        Command(
            operation=Operation.MEASUREMENT_RECORD,
            args={"measurement": meas},
            target_id=meas.measurement_id,
            target_type=MEASUREMENT,
            requested_by="api:record-measurement",
        )
    )
    if result.status in (CommandStatus.FAILED, CommandStatus.REJECTED):
        raise HTTPException(status_code=400, detail=result.message)

    return {
        "status": "ok",
        "measurement_id": result.created_id or meas.measurement_id,
    }


@router.get("/measurements")
def list_measurements(
    subject_entity_id: str | None = Query(None),
    metric_name: str | None = Query(None),
    observer_agent_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """Query Measurement nodes by subject / metric_name / observer."""
    registry = get_registry()
    props: dict[str, Any] = {}
    if subject_entity_id is not None:
        props["subject_entity_id"] = subject_entity_id
    if metric_name is not None:
        props["metric_name"] = metric_name
    if observer_agent_id is not None:
        props["observer_agent_id"] = observer_agent_id

    rows = registry.knowledge.graph_store.query(
        node_type=MEASUREMENT,
        properties=props or None,
        limit=limit,
    )
    return {"measurements": [_projected_node(r) for r in rows]}
