"""Cross-backend eval scenario for Item 1 Phase 2 — Observation retrieval.

Seeds a small graph of subjects + Observations + Measurements on every
backend the local environment can reach (SQLite always; Postgres / Neo4j
gated on env credentials per the
:mod:`eval._backends` convention), then asserts:

1. Observations attached to a subject are surfaced by
   :class:`~trellis.retrieve.ObservationSearch` in
   :class:`~trellis.retrieve.PackBuilder` output.
2. Freshness decay orders fresh observations above stale ones with the
   same confidence.
3. The ``confidence_threshold`` filter drops low-scoring rows.
4. The same seed produces the same retrieval shape on every backend the
   environment can reach (cross-backend equivalence).

The scenario is runnable two ways:

- ``pytest eval/scenarios/observation_retrieval.py -v`` — discovers the
  ``test_*`` functions defined below.  This is the contract the Phase 2
  brief targets and what CI / make pytest will exercise.
- ``from eval.scenarios.observation_retrieval import run`` — the eval
  runner's ``ScenarioReport`` shape, mirroring the other scenarios.  This
  isn't used by the brief but keeps the door open for scheduled multi-
  backend runs.

Per ``docs/design/plan-self-improvement-program.md`` §5.1 this is the
"Observation ingestion + retrieval" scenario required for Item 1.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog

from eval._backends import (
    BackendHandle,
    get_neo4j_config,
    get_postgres_dsn,
    register_handle,
)
from trellis.retrieve import PackBuilder
from trellis.retrieve.observation_strategy import ObservationSearch
from trellis.schemas.well_known import HAS_OBSERVATION, MEASUREMENT, OBSERVATION

logger = structlog.get_logger(__name__)

#: ``test_cross_backend_equivalence`` requires at least this many backends
#: to do anything meaningful. SQLite is always present; Postgres / Neo4j
#: are env-gated. Skipping below the threshold is fine — that's the same
#: pattern other multi-backend scenarios use.
_MIN_BACKENDS_FOR_CROSS_CHECK = 2


# Use a fixed "now" so freshness assertions are deterministic across
# wall-clock drift in CI.
SCENARIO_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seed model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeededObservation:
    """One observation we want present in every backend under test."""

    node_id: str
    subject_id: str
    confidence: float
    observed_at: datetime
    content: str = "test observation"
    node_type: str = OBSERVATION


def _seed_plan() -> tuple[list[dict[str, Any]], list[SeededObservation]]:
    """Build the seed nodes + observation specs.

    Two subjects, one with a mix of fresh / stale / high-conf / low-conf
    observations so every assertion has something to bite on.
    """
    subjects = [
        {
            "node_id": "dataset:users",
            "node_type": "Dataset",
            "properties": {"name": "users"},
        },
        {
            "node_id": "dataset:events",
            "node_type": "Dataset",
            "properties": {"name": "events"},
        },
    ]
    observations = [
        # Fresh, high-confidence — should be top of the pack.
        SeededObservation(
            node_id="obs:users:fresh-high",
            subject_id="dataset:users",
            confidence=0.9,
            observed_at=SCENARIO_NOW - timedelta(hours=1),
            content="users table queried 100 times last hour",
        ),
        # Same confidence, much older — must rank below the fresh one.
        SeededObservation(
            node_id="obs:users:stale-high",
            subject_id="dataset:users",
            confidence=0.9,
            observed_at=SCENARIO_NOW - timedelta(days=90),
            content="users table queried 90 days ago",
        ),
        # Below the 0.5 threshold — should be filtered when threshold applied.
        SeededObservation(
            node_id="obs:users:fresh-low",
            subject_id="dataset:users",
            confidence=0.3,
            observed_at=SCENARIO_NOW - timedelta(hours=2),
            content="users table low-signal observation",
        ),
        # A Measurement attached to the same subject — must come through too.
        SeededObservation(
            node_id="meas:users:query_count",
            subject_id="dataset:users",
            confidence=0.8,
            observed_at=SCENARIO_NOW - timedelta(hours=1),
            content="query_count=100",
            node_type=MEASUREMENT,
        ),
        # An observation on a different subject — must not appear when
        # we query for ``dataset:users``.
        SeededObservation(
            node_id="obs:events:noise",
            subject_id="dataset:events",
            confidence=0.7,
            observed_at=SCENARIO_NOW - timedelta(hours=1),
            content="events observation — should not appear in users query",
        ),
    ]
    return subjects, observations


def _seed_backend(handle: BackendHandle) -> None:
    """Write the seed plan into a backend's graph store."""
    graph_store = handle.registry.knowledge.graph_store
    subjects, observations = _seed_plan()

    # Subjects first (Observations FK on them via hasObservation edges).
    for subj in subjects:
        graph_store.upsert_node(
            node_id=subj["node_id"],
            node_type=subj["node_type"],
            properties=subj["properties"],
        )

    # Then observation/measurement nodes.
    for obs in observations:
        graph_store.upsert_node(
            node_id=obs.node_id,
            node_type=obs.node_type,
            properties={
                "subject_entity_id": obs.subject_id,
                "subject_entity_type": "Dataset",
                "observer_agent_id": "observation_retrieval_eval",
                "content": obs.content,
                "confidence": obs.confidence,
                "observed_at": obs.observed_at.isoformat(),
            },
        )
        graph_store.upsert_edge(
            source_id=obs.subject_id,
            target_id=obs.node_id,
            edge_type=HAS_OBSERVATION,
        )


def _build_pack(handle: BackendHandle, **filters: Any) -> Any:
    """Run a PackBuilder with only the ObservationSearch strategy."""
    strategy = ObservationSearch(handle.registry.knowledge.graph_store)
    builder = PackBuilder(strategies=[strategy])
    return builder.build(intent="observation retrieval eval", filters=filters)


# ---------------------------------------------------------------------------
# Backend wiring (mirrors eval/scenarios/multi_backend_equivalence shape)
# ---------------------------------------------------------------------------


_SQLITE_OPERATIONAL = {
    "trace": {"backend": "sqlite"},
    "event_log": {"backend": "sqlite"},
}


def _build_backends(stack: ExitStack, tmp_dir: Path) -> list[BackendHandle]:
    handles: list[BackendHandle] = []

    register_handle(
        stack,
        handles,
        name="sqlite",
        config={
            "knowledge": {
                "graph": {"backend": "sqlite"},
                "vector": {"backend": "sqlite"},
                "document": {"backend": "sqlite"},
                "blob": {"backend": "local"},
            },
            "operational": _SQLITE_OPERATIONAL,
        },
        stores_dir=tmp_dir,
    )

    pg_dsn = get_postgres_dsn()
    if pg_dsn:
        register_handle(
            stack,
            handles,
            name="postgres",
            config={
                "knowledge": {
                    "graph": {"backend": "postgres", "dsn": pg_dsn},
                    "vector": {"backend": "sqlite"},
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=tmp_dir / "pg",
        )

    neo4j_graph = get_neo4j_config()
    if neo4j_graph:
        register_handle(
            stack,
            handles,
            name="neo4j",
            config={
                "knowledge": {
                    "graph": neo4j_graph,
                    "vector": {"backend": "sqlite"},
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=tmp_dir / "neo4j",
        )

    return handles


# ---------------------------------------------------------------------------
# pytest entry points (the primary surface per the Phase 2 brief)
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_handle(tmp_path: Path) -> Any:
    """Seeded SQLite backend. Always available, no env gating."""
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        # The first handle is always sqlite; later handles depend on env.
        sqlite = next(h for h in handles if h.name == "sqlite")
        _seed_backend(sqlite)
        yield sqlite


def test_observations_surface_in_pack_sqlite(sqlite_handle: BackendHandle) -> None:
    pack = _build_pack(sqlite_handle, subject_entity_id="dataset:users")
    item_ids = {item.item_id for item in pack.items}

    # The three observations + the measurement attached to dataset:users.
    expected = {
        "obs:users:fresh-high",
        "obs:users:stale-high",
        "obs:users:fresh-low",
        "meas:users:query_count",
    }
    assert expected.issubset(item_ids), (
        f"missing items on sqlite: {expected - item_ids}"
    )
    # The unrelated subject's observation MUST NOT appear.
    assert "obs:events:noise" not in item_ids


def test_freshness_orders_pack_sqlite(sqlite_handle: BackendHandle) -> None:
    pack = _build_pack(sqlite_handle, subject_entity_id="dataset:users")
    # Find the relative rank of the two same-confidence observations.
    by_id = {item.item_id: item for item in pack.items}
    fresh = by_id["obs:users:fresh-high"]
    stale = by_id["obs:users:stale-high"]
    assert fresh.relevance_score > stale.relevance_score, (
        f"freshness decay failed: fresh={fresh.relevance_score} "
        f"stale={stale.relevance_score}"
    )


def test_confidence_threshold_filters_sqlite(sqlite_handle: BackendHandle) -> None:
    pack = _build_pack(
        sqlite_handle,
        subject_entity_id="dataset:users",
        confidence_threshold=0.5,
    )
    item_ids = {item.item_id for item in pack.items}
    # Below-threshold observation dropped, others retained.
    assert "obs:users:fresh-low" not in item_ids
    assert "obs:users:fresh-high" in item_ids


def test_cross_backend_equivalence(tmp_path: Path) -> None:
    """When multiple backends are available, every backend must return the
    same set of observation ids for the same query. Backends that aren't
    configured (no env vars) are skipped silently.
    """
    with ExitStack() as stack:
        handles = _build_backends(stack, tmp_path)
        if len(handles) < _MIN_BACKENDS_FOR_CROSS_CHECK:
            pytest.skip(
                "cross-backend assertions require at least "
                f"{_MIN_BACKENDS_FOR_CROSS_CHECK} backends; "
                f"got {[h.name for h in handles]}"
            )
        results: dict[str, set[str]] = {}
        for handle in handles:
            _seed_backend(handle)
            pack = _build_pack(handle, subject_entity_id="dataset:users")
            results[handle.name] = {item.item_id for item in pack.items}

        ids_by_backend = list(results.items())
        baseline_name, baseline_ids = ids_by_backend[0]
        for other_name, other_ids in ids_by_backend[1:]:
            diff = baseline_ids.symmetric_difference(other_ids)
            assert not diff, (
                f"backends disagree: {baseline_name} vs {other_name}: "
                f"only in {baseline_name}={baseline_ids - other_ids}, "
                f"only in {other_name}={other_ids - baseline_ids}"
            )


# ---------------------------------------------------------------------------
# Eval-runner entry point (parallel scenario shape — optional)
# ---------------------------------------------------------------------------


def run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Lightweight invocation for ad-hoc runs (``python -m`` style).

    Not the primary path — pytest test functions above are.  Returns a
    simple dict of per-backend hit counts so an operator running this
    by hand can eyeball the result without dragging in the full
    :class:`~eval.runner.ScenarioReport` machinery.
    """
    with tempfile.TemporaryDirectory() as tmp_dir, ExitStack() as stack:
        handles = _build_backends(stack, Path(tmp_dir))
        out: dict[str, Any] = {"backends": {}}
        for handle in handles:
            _seed_backend(handle)
            pack = _build_pack(handle, subject_entity_id="dataset:users")
            out["backends"][handle.name] = {
                "items": [item.item_id for item in pack.items],
                "items_count": len(pack.items),
            }
        return out


if __name__ == "__main__":  # pragma: no cover — operator convenience
    import json

    os.environ.setdefault("STRUCTLOG_DISABLE_CONFIG", "1")
    print(json.dumps(run(), indent=2))
