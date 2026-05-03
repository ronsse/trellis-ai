"""Admin routes."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Query

from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    analyze_effectiveness,
    run_effectiveness_feedback,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis_api.app import get_registry
from trellis_api.models import HealthResponse, StatsResponse

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Check API and store health."""
    return HealthResponse(status="ok", checks={"api": True, "stores": True})


@router.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    """Get store statistics."""
    registry = get_registry()
    return StatsResponse(
        traces=registry.trace_store.count(),
        documents=registry.document_store.count(),
        nodes=registry.graph_store.count_nodes(),
        edges=registry.graph_store.count_edges(),
        events=registry.event_log.count(),
    )


@router.get("/effectiveness")
def effectiveness(
    days: int = Query(30, description="Days of history to analyze"),
    min_appearances: int = Query(2, description="Minimum item appearances"),
) -> dict[str, Any]:
    """Analyze context pack effectiveness."""
    registry = get_registry()
    report = analyze_effectiveness(
        registry.event_log,
        days=days,
        min_appearances=min_appearances,
    )
    return {"status": "ok", **report.model_dump()}


@router.post("/effectiveness/apply-noise-tags")
def apply_noise_tags(
    days: int = Query(30, description="Days of history to analyze"),
    min_appearances: int = Query(2, description="Minimum item appearances"),
) -> dict[str, Any]:
    """Analyze effectiveness AND apply noise tags to low-value items.

    Runs the full feedback loop: analyze → tag noise items with
    signal_quality="noise" so PackBuilder excludes them by default.
    """
    registry = get_registry()
    report = run_effectiveness_feedback(
        registry.event_log,
        registry.document_store,
        days=days,
        min_appearances=min_appearances,
    )
    return {
        "status": "ok",
        "noise_candidates_tagged": len(report.noise_candidates),
        **report.model_dump(),
    }


# -- Advisories --


@router.post("/advisories/generate")
def generate_advisories(
    days: int = Query(30, description="Days of history to analyze"),
    min_sample: int = Query(5, description="Min sample size"),
    min_effect: float = Query(0.15, description="Min effect size"),
) -> dict[str, Any]:
    """Generate advisories from outcome data.

    Analyzes PACK_ASSEMBLED and FEEDBACK_RECORDED events to find patterns,
    then stores deterministic advisories for delivery alongside packs.
    """
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        return {"status": "error", "message": "stores_dir not configured"}
    store = AdvisoryStore(stores_dir / "advisories.json")
    generator = AdvisoryGenerator(
        registry.event_log,
        store,
        min_sample_size=min_sample,
        min_effect_size=min_effect,
    )
    report = generator.generate(days=days)
    return {"status": "ok", **report.model_dump()}


@router.get("/advisories")
def list_advisories(
    scope: str | None = Query(None, description="Filter by scope"),
    min_confidence: float = Query(0.0, description="Minimum confidence"),
) -> dict[str, Any]:
    """List stored advisories."""
    registry = get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        return {"status": "error", "message": "stores_dir not configured"}
    store = AdvisoryStore(stores_dir / "advisories.json")
    advisories = store.list(scope=scope, min_confidence=min_confidence)
    return {
        "count": len(advisories),
        "advisories": [a.model_dump(mode="json") for a in advisories],
    }


# -- Vector store management --


@router.post("/vectors/reset")
def reset_vectors() -> dict[str, Any]:
    """Drop and recreate the vectors table with current configured dimensions."""
    registry = get_registry()
    vector_store = getattr(registry, "vector_store", None)
    if vector_store is None:
        return {"status": "error", "message": "Vector store not configured"}

    try:
        # SQLite vector store exposes a plain sqlite3 connection at `_conn`
        # whose Cursor does not support the context-manager protocol; pgvector
        # exposes an auto-reconnecting `.conn` property with psycopg cursors.
        if hasattr(vector_store, "conn"):
            with vector_store.conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS vectors")
            vector_store.conn.commit()
        else:
            vector_store._conn.execute("DROP TABLE IF EXISTS vectors")
            vector_store._conn.commit()
        vector_store._init_schema()
        if hasattr(vector_store, "conn"):
            vector_store.conn.commit()
        else:
            vector_store._conn.commit()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    else:
        dims = vector_store._dimensions
        return {"status": "ok", "message": f"Recreated with {dims}D"}
