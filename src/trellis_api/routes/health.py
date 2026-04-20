"""Health probe routes.

Two endpoints for orchestrator probes (ECS, Kubernetes, ALB):

- ``/healthz`` — liveness. Returns 200 whenever the process is running.
  Never touches stores; must not flap under transient backend failure.
- ``/readyz`` — readiness. Returns 200 only when the StoreRegistry is
  initialized and a cheap round-trip against the event log succeeds.
  Returns 503 if the registry isn't ready or the round-trip fails.

Deliberately outside the ``/api/v1`` prefix — probes are deployment
plumbing, not versioned API surface.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response, status

from trellis_api.app import get_registry

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(response: Response) -> dict[str, str]:
    try:
        registry = get_registry()
    except RuntimeError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "initializing"}

    try:
        registry.operational.event_log.count()
    except Exception as exc:
        logger.warning("readyz_event_log_probe_failed", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded"}

    return {"status": "ready"}
