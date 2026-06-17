"""Bolt-semantics regression tests for the meta-trace recorder.

The single-row ``upsert_edge`` on the SQLite backend does **not** validate
that an edge's endpoints have a current version — it silently tolerates a
dangling edge. The Bolt/openCypher backends (Neo4j, ArcadeDB) reject it with
``"... has no current version"``. These backends are not exercised by the
unit suite (they need a live database), so the recorder's
:meth:`~trellis.meta.recorder.MetaAnalysisRecord.consumed_event` /
:meth:`~trellis.meta.recorder.MetaAnalysisRecord.consumed_observation` paths
were green on SQLite while latently broken on the blessed substrate.

:class:`_StrictEndpointGraphStore` wraps the real SQLite store and replicates
the *single-row* Bolt endpoint check, so we can assert the recorder's
materialise-or-skip behaviour against the rejecting-backend semantics without
standing up a database. SQLite-tolerant behaviour stays covered by
``test_recorder.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from trellis.meta import (
    DEFAULT_META_AGENT_ID,
    META_TRACES_ENV_VAR,
    record_meta_analysis,
)
from trellis.schemas import well_known as wk
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from trellis.stores.base import GraphStore


class _StrictEndpointGraphStore:
    """Delegate to a real GraphStore but reject dangling single-row edges.

    Mirrors the Bolt/openCypher ``upsert_edge`` contract: both endpoints
    must have a current version (``get_node`` not ``None``) or the write
    raises ``ValueError`` with the canonical ``"has no current version"``
    message. Every other method delegates unchanged.
    """

    def __init__(self, inner: GraphStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        # Anything we don't override (get_node, upsert_node, query,
        # get_edges, ...) delegates to the wrapped store.
        return getattr(self._inner, name)

    def upsert_edge(
        self,
        *,
        source_id: str,
        target_id: str,
        edge_type: str,
        **kwargs: Any,
    ) -> str:
        if self._inner.get_node(source_id) is None:
            msg = (
                f"Cannot upsert edge: source {source_id!r} or target "
                f"{target_id!r} has no current version"
            )
            raise ValueError(msg)
        if self._inner.get_node(target_id) is None:
            msg = (
                f"Cannot upsert edge: source {source_id!r} or target "
                f"{target_id!r} has no current version"
            )
            raise ValueError(msg)
        return self._inner.upsert_edge(  # type: ignore[no-any-return]
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            **kwargs,
        )


@pytest.fixture(autouse=True)
def _clear_meta_traces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with the env var unset (default on)."""
    monkeypatch.delenv(META_TRACES_ENV_VAR, raising=False)


@pytest.fixture
def strict_registry(tmp_path: Path) -> StoreRegistry:
    """Registry whose graph store rejects dangling edges (Bolt semantics)."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    registry = StoreRegistry(stores_dir=stores_dir)
    # Prime the registry's lazy store cache with the strict wrapper so the
    # recorder (which reaches through ``registry.knowledge.graph_store``)
    # sees Bolt-style endpoint validation.
    registry._cache["graph"] = _StrictEndpointGraphStore(
        registry.knowledge.graph_store
    )
    return registry


def test_strict_store_rejects_dangling_edge_baseline(
    strict_registry: StoreRegistry,
) -> None:
    """Guard: the strict wrapper actually rejects an unmaterialised target.

    This is the semantics the recorder fix must accommodate — proves the
    test backend is rejecting, not tolerating, dangling edges. Without the
    recorder's materialise-or-skip guard, ``consumed_event`` would surface
    exactly this error on Neo4j/ArcadeDB.
    """
    graph = strict_registry.knowledge.graph_store
    graph.upsert_node(node_id="activity-x", node_type=wk.ACTIVITY, properties={})

    with pytest.raises(ValueError, match="has no current version"):
        graph.upsert_edge(
            source_id="activity-x",
            target_id="evt-never-materialised",
            edge_type=wk.WAS_INFORMED_BY,
        )


def test_consumed_event_unmaterialised_target_is_skipped(
    strict_registry: StoreRegistry,
) -> None:
    """``consumed_event`` to an absent event node skips, never raises.

    Before the fix this raised ``"... has no current version"`` on the
    rejecting backend (the recorder wrote the edge unconditionally). After
    the fix the recorder skips the edge — the EventLog stays authoritative
    for what was consumed.
    """
    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=strict_registry,
    ) as rec:
        # event_id is an EventLog correlation id, not a materialised node.
        rec.consumed_event("evt-correlation-only")
        activity_id = rec.activity_id

    graph = strict_registry.knowledge.graph_store
    out_edges = graph.get_edges(activity_id, direction="outgoing")
    informed = [e for e in out_edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    # No wasInformedBy edge written — the dangling pointer was skipped.
    assert informed == []
    # The Activity + wasAssociatedWith edge still landed fine.
    assert graph.get_node(activity_id) is not None


def test_consumed_observation_unmaterialised_target_is_skipped(
    strict_registry: StoreRegistry,
) -> None:
    """An absent Observation target is skipped too (same dangling shape)."""
    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=strict_registry,
    ) as rec:
        rec.consumed_observation("obs-missing")
        activity_id = rec.activity_id

    graph = strict_registry.knowledge.graph_store
    out_edges = graph.get_edges(activity_id, direction="outgoing")
    informed = [e for e in out_edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    assert informed == []


def test_consumed_observation_materialised_target_writes_edge(
    strict_registry: StoreRegistry,
) -> None:
    """A real (current) Observation node yields the wasInformedBy edge.

    The materialise-or-skip guard only suppresses the edge when the target
    is genuinely absent — the normal path (the producing analyzer already
    wrote the Observation) is unaffected on the rejecting backend.
    """
    graph = strict_registry.knowledge.graph_store
    graph.upsert_node(
        node_id="obs-real",
        node_type=wk.OBSERVATION,
        properties={"name": "obs-real"},
    )

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=strict_registry,
    ) as rec:
        rec.consumed_observation("obs-real")
        activity_id = rec.activity_id

    out_edges = graph.get_edges(activity_id, direction="outgoing")
    informed = [e for e in out_edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    assert {e["target_id"] for e in informed} == {"obs-real"}


def test_produced_finding_still_materialises_on_strict_store(
    strict_registry: StoreRegistry,
) -> None:
    """Regression guard: ``produced_finding`` keeps create-if-absent.

    The consumed-edge fix must not regress the sibling produced_finding
    behaviour — a synthetic finding id still gets a node + edge even on the
    rejecting backend.
    """
    with record_meta_analysis(
        analyzer_name="learning-candidates",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=strict_registry,
    ) as rec:
        rec.produced_finding("synthetic-finding-1", finding_type=wk.OBSERVATION)
        activity_id = rec.activity_id

    graph = strict_registry.knowledge.graph_store
    assert graph.get_node("synthetic-finding-1") is not None
    in_edges = graph.get_edges(activity_id, direction="incoming")
    generated = [e for e in in_edges if e["edge_type"] == wk.WAS_GENERATED_BY]
    assert len(generated) == 1
    assert generated[0]["source_id"] == "synthetic-finding-1"
