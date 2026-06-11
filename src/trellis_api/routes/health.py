"""Health probe routes.

Two endpoints for orchestrator probes (ECS, Kubernetes, ALB):

- ``/healthz`` — liveness. Returns 200 whenever the process is running.
  Never touches stores; must not flap under transient backend failure.
- ``/readyz`` — readiness. Returns 200 only when the StoreRegistry is
  initialized AND every cloud-backend probe (operational EventLog,
  knowledge GraphStore + VectorStore + DocumentStore) round-trips
  cleanly. Returns 503 otherwise. The status line is always public;
  the per-backend breakdown (backend names, latencies, error strings)
  is returned only to authenticated callers, or to everyone when
  ``TRELLIS_OPS_DETAIL=public`` is set explicitly.

Deliberately outside the ``/api/v1`` prefix — probes are deployment
plumbing, not versioned API surface. The *status line* of both probes
stays unauthenticated so orchestrator probes (k8s, ALB, etc.) work
without holding an API secret.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Response, status

from trellis.errors import ConfigError
from trellis_api.app import get_registry
from trellis_api.auth import AuthContext, authenticate_optional

logger = structlog.get_logger(__name__)

router = APIRouter()

#: Controls who sees the per-backend ``/readyz`` breakdown:
#: ``authenticated`` (default) — authenticated callers only;
#: ``public`` — everyone (pre-gating behavior, opt-in).
OPS_DETAIL_ENV = "TRELLIS_OPS_DETAIL"

OPS_DETAIL_AUTHENTICATED = "authenticated"
OPS_DETAIL_PUBLIC = "public"
_VALID_OPS_DETAIL = frozenset({OPS_DETAIL_AUTHENTICATED, OPS_DETAIL_PUBLIC})


def resolve_ops_detail() -> str:
    """Return the effective ops-detail posture, raising loudly on a bad value.

    Unset / empty env var → ``authenticated`` (detail requires a valid
    credential when the auth mode demands one). Called from
    ``create_app`` so a typo crashes uvicorn at startup, never silently
    downgrades to either posture.
    """
    raw = os.environ.get(OPS_DETAIL_ENV)
    if raw is None or not raw.strip():
        return OPS_DETAIL_AUTHENTICATED
    value = raw.strip().lower()
    if value not in _VALID_OPS_DETAIL:
        msg = (
            f"Invalid {OPS_DETAIL_ENV}={raw!r}; expected one of"
            f" {sorted(_VALID_OPS_DETAIL)}. Refusing to guess an exposure"
            " posture."
        )
        raise ConfigError(msg, setting=OPS_DETAIL_ENV)
    return value


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
    # GRACEFUL-DEGRADATION: probe contract is "return per-backend
    # status, don't raise" — readyz aggregates probes and flips the
    # overall response to 503 when any backend is degraded.
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
def readyz(
    response: Response,
    ctx: AuthContext | None = Depends(authenticate_optional),  # noqa: B008 — FastAPI DI idiom
) -> dict[str, Any]:
    """Return 200 only when every cloud backend round-trips cleanly.

    Probes the 4 backends an agent's request actually depends on:

    * operational ``event_log`` — every governed mutation emits here
    * knowledge ``graph_store`` — every entity / link query reads here
    * knowledge ``vector_store`` — every pack assembly hits here
    * knowledge ``document_store`` — every retrieval reads here

    A single backend failing flips the whole probe to 503. The public
    response carries only the status line (all an orchestrator probe
    needs); the per-backend breakdown — backend names, latencies,
    raw error strings — goes to authenticated callers (any scope), or
    to everyone when ``TRELLIS_OPS_DETAIL=public`` is set. A presented
    but invalid credential still gets 401 via
    :func:`~trellis_api.auth.authenticate_optional`.
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
    if ctx is None and resolve_ops_detail() != OPS_DETAIL_PUBLIC:
        return {"status": overall}
    return {"status": overall, "backends": backends}
