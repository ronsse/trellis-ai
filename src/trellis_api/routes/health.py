"""Health probe routes.

Two endpoints for orchestrator probes (ECS, Kubernetes, ALB):

- ``/healthz`` — liveness. Returns 200 whenever the process is running.
  Never touches stores; must not flap under transient backend failure.
- ``/readyz`` — readiness. Returns 200 only when the StoreRegistry is
  initialized AND every cloud-backend probe (operational EventLog,
  knowledge GraphStore + VectorStore + DocumentStore) round-trips
  cleanly. Returns 503 with a per-backend status breakdown otherwise.

Deliberately outside the ``/api/v1`` prefix — probes are deployment
plumbing, not versioned API surface. Also explicitly **unauthenticated**
so orchestrator probes work without holding the API secret.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import APIRouter, Response, status

from trellis_api.app import get_registry

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _probe(name: str, fn: Callable[[], Any]) -> dict[str, Any]:
    """Run a single backend probe; capture latency + outcome.

    Each probe runs in-process so the latency reflects the round-trip
    against the real backend (Postgres / Neo4j Bolt) — no socket-level
    timeout is plumbed because psycopg-pool / neo4j-driver each have
    their own. A failed probe shows as ``status="degraded"`` with the
    exception message; a healthy one as ``status="ok"`` with millisec.
    """
    start_ns = time.monotonic_ns()
    try:
        fn()
    except Exception as exc:
        latency_ms = (time.monotonic_ns() - start_ns) / 1_000_000
        logger.warning(
            "readyz_probe_failed",
            backend=name,
            error=str(exc),
            latency_ms=round(latency_ms, 2),
        )
        return {
            "status": "degraded",
            "latency_ms": round(latency_ms, 2),
            "error": str(exc),
        }
    latency_ms = (time.monotonic_ns() - start_ns) / 1_000_000
    return {"status": "ok", "latency_ms": round(latency_ms, 2)}


@router.get("/readyz", include_in_schema=False)
def readyz(response: Response) -> dict[str, Any]:
    """Return 200 only when every cloud backend round-trips cleanly.

    Probes the 4 backends an agent's request actually depends on:

    * operational ``event_log`` — every governed mutation emits here
    * knowledge ``graph_store`` — every entity / link query reads here
    * knowledge ``vector_store`` — every pack assembly hits here
    * knowledge ``document_store`` — every retrieval reads here

    A single backend failing flips the whole probe to 503, but the
    response body carries per-backend status so the operator can tell
    which one is down without grepping logs.
    """
    try:
        registry = get_registry()
    except RuntimeError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "initializing"}

    backends: dict[str, Any] = {
        "event_log": _probe("event_log", registry.operational.event_log.count),
        "graph_store": _probe(
            "graph_store", registry.knowledge.graph_store.count_nodes
        ),
        "vector_store": _probe("vector_store", registry.knowledge.vector_store.count),
        "document_store": _probe(
            "document_store", registry.knowledge.document_store.count
        ),
    }
    overall = (
        "ready" if all(b["status"] == "ok" for b in backends.values()) else "degraded"
    )
    if overall != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": overall, "backends": backends}
