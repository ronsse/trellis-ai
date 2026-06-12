"""Tests for :func:`trellis.meta.recorder.record_meta_analysis`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from trellis.meta import (
    DEFAULT_META_AGENT_ID,
    META_TRACES_ENV_VAR,
    record_meta_analysis,
)
from trellis.schemas import well_known as wk
from trellis.stores.registry import StoreRegistry


@pytest.fixture(autouse=True)
def _clear_meta_traces_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with the env var unset (default on)."""
    monkeypatch.delenv(META_TRACES_ENV_VAR, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    """Fresh SQLite-backed registry per test."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _seed_target_nodes(registry: StoreRegistry, *ids: str) -> None:
    """Materialise placeholder nodes so the SCD-2 ``upsert_edge`` has
    something to point at. The recorder only writes the *edge*; the
    target node lifecycle belongs to the operational EventLog (for
    events) and the producing analyzer (for findings)."""
    for nid in ids:
        registry.knowledge.graph_store.upsert_node(
            node_id=nid,
            node_type="Placeholder",
            properties={"name": nid},
        )


def test_round_trip_writes_activity_and_provenance_edges(
    registry: StoreRegistry,
) -> None:
    """End-to-end: enter, attach inputs/outputs, query back."""
    _seed_target_nodes(
        registry,
        "evt-1",
        "obs-1",
        "finding-1",
    )

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec:
        assert rec.enabled is True
        assert rec.activity_id is not None
        assert rec.analyzer_name == "context-effectiveness"
        assert rec.agent_id == DEFAULT_META_AGENT_ID
        rec.consumed_event("evt-1")
        rec.consumed_observation("obs-1")
        rec.produced_finding("finding-1", finding_type=wk.OBSERVATION)
        activity_id = rec.activity_id

    graph = registry.knowledge.graph_store

    activity = graph.get_node(activity_id)
    assert activity is not None
    assert activity["node_type"] == wk.ACTIVITY
    props = activity["properties"]
    assert props["analyzer_name"] == "context-effectiveness"
    assert props["agent_id"] == DEFAULT_META_AGENT_ID
    assert "started_at" in props

    # Activity wasAssociatedWith the synthetic Agent.
    out_edges = graph.get_edges(activity_id, direction="outgoing")
    assoc = [e for e in out_edges if e["edge_type"] == wk.WAS_ASSOCIATED_WITH]
    assert len(assoc) == 1
    assert assoc[0]["target_id"] == DEFAULT_META_AGENT_ID

    # wasInformedBy edges to the consumed event + observation.
    informed = [e for e in out_edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    assert {e["target_id"] for e in informed} == {"evt-1", "obs-1"}

    # wasGeneratedBy edge runs from the finding back to the Activity.
    in_edges = graph.get_edges(activity_id, direction="incoming")
    generated = [e for e in in_edges if e["edge_type"] == wk.WAS_GENERATED_BY]
    assert len(generated) == 1
    assert generated[0]["source_id"] == "finding-1"
    assert generated[0]["properties"]["finding_type"] == wk.OBSERVATION


def test_provenance_columns_populated_on_every_edge(
    registry: StoreRegistry,
) -> None:
    """Item 2's five provenance columns must be set on every edge."""
    _seed_target_nodes(registry, "evt-1", "finding-1")

    with record_meta_analysis(
        analyzer_name="schema-evolution",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec:
        rec.consumed_event("evt-1")
        rec.produced_finding("finding-1", finding_type="WellKnownCandidate")
        activity_id = rec.activity_id

    graph = registry.knowledge.graph_store
    all_edges = list(graph.get_edges(activity_id, direction="both"))
    # 1 wasAssociatedWith + 1 wasInformedBy + 1 wasGeneratedBy = 3.
    assert len(all_edges) == 3

    for edge in all_edges:
        assert edge["agent_id"] == DEFAULT_META_AGENT_ID
        assert edge["source_trace_id"] is None
        assert edge["confidence"] == 1.0
        assert edge["evidence_ref"] == activity_id
        assert edge["extractor_tier"] == "DETERMINISTIC"


def test_merge_window_reuses_activity_within_5_min(
    registry: StoreRegistry,
) -> None:
    """Two invocations of the same ``(agent_id, analyzer_name)`` merge."""
    _seed_target_nodes(registry, "evt-1", "evt-2")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_a:
        rec_a.consumed_event("evt-1")
        first_id = rec_a.activity_id

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_b:
        rec_b.consumed_event("evt-2")
        second_id = rec_b.activity_id

    assert first_id == second_id

    # Exactly one Activity present after both invocations.
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert len(activities) == 1


def test_merge_window_expired_creates_new_activity(
    registry: StoreRegistry,
) -> None:
    """Outside the merge window each invocation creates its own Activity."""
    _seed_target_nodes(registry, "evt-1", "evt-2")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
        merge_window_seconds=0,
    ) as rec_a:
        rec_a.consumed_event("evt-1")
        first_id = rec_a.activity_id

    # Tiny sleep so the second invocation's "now" strictly exceeds the
    # first invocation's created_at (SQLite stores microsecond-precision
    # ISO timestamps).
    time.sleep(0.01)

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
        merge_window_seconds=0,
    ) as rec_b:
        rec_b.consumed_event("evt-2")
        second_id = rec_b.activity_id

    assert first_id != second_id
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert len(activities) == 2


def test_different_analyzer_names_dont_merge(
    registry: StoreRegistry,
) -> None:
    """Same agent, distinct analyzer names → distinct Activities."""
    _seed_target_nodes(registry, "evt-1", "evt-2")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_a:
        rec_a.consumed_event("evt-1")
        first_id = rec_a.activity_id

    with record_meta_analysis(
        analyzer_name="schema-evolution",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_b:
        rec_b.consumed_event("evt-2")
        second_id = rec_b.activity_id

    assert first_id != second_id
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert len(activities) == 2


def test_env_var_off_no_activity_written(
    registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TRELLIS_META_TRACES=off`` short-circuits — no Activity, no edges."""
    monkeypatch.setenv(META_TRACES_ENV_VAR, "off")
    _seed_target_nodes(registry, "evt-1", "finding-1")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec:
        assert rec.enabled is False
        assert rec.activity_id is None
        # Calls remain valid but write nothing.
        rec.consumed_event("evt-1")
        rec.consumed_observation("evt-1")
        rec.produced_finding("finding-1", finding_type="Observation")

    # No Activity / Agent / provenance edges materialised.
    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert activities == []
    assert registry.knowledge.graph_store.get_node(DEFAULT_META_AGENT_ID) is None


def test_env_var_invalid_raises(
    registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage values raise — no silent fallback (POC directive)."""
    monkeypatch.setenv(META_TRACES_ENV_VAR, "maybe")

    with (
        pytest.raises(ValueError, match="TRELLIS_META_TRACES"),
        record_meta_analysis(
            analyzer_name="context-effectiveness",
            agent_id=DEFAULT_META_AGENT_ID,
            registry=registry,
        ),
    ):
        pass  # pragma: no cover — recorder raises on entry


def test_env_var_on_explicit(
    registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``on`` behaves like the default (recording active)."""
    monkeypatch.setenv(META_TRACES_ENV_VAR, "on")
    _seed_target_nodes(registry, "evt-1")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec:
        assert rec.enabled is True
        rec.consumed_event("evt-1")

    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert len(activities) == 1


def test_agent_id_outside_namespace_raises(registry: StoreRegistry) -> None:
    """Non-``trellis_meta_`` agent IDs raise via ``ensure_meta_agent``."""
    with (
        pytest.raises(ValueError, match="trellis_meta_"),
        record_meta_analysis(
            analyzer_name="context-effectiveness",
            agent_id="some-human-agent",
            registry=registry,
        ),
    ):
        pass  # pragma: no cover


def test_synthetic_agent_node_created_on_first_use(
    registry: StoreRegistry,
) -> None:
    """A fresh registry has no Agent node until the recorder runs."""
    assert registry.knowledge.graph_store.get_node(DEFAULT_META_AGENT_ID) is None

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ):
        pass

    agent = registry.knowledge.graph_store.get_node(DEFAULT_META_AGENT_ID)
    assert agent is not None
    assert agent["node_type"] == wk.AGENT


def test_merge_appends_edges_to_existing_activity(
    registry: StoreRegistry,
) -> None:
    """Within the merge window, the second call adds *more* edges."""
    _seed_target_nodes(registry, "evt-1", "evt-2", "finding-1")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_a:
        rec_a.consumed_event("evt-1")
        activity_id = rec_a.activity_id

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as rec_b:
        assert rec_b.activity_id == activity_id
        rec_b.consumed_event("evt-2")
        rec_b.produced_finding("finding-1", finding_type=wk.OBSERVATION)

    out_edges = registry.knowledge.graph_store.get_edges(
        activity_id, direction="outgoing"
    )
    informed = [e for e in out_edges if e["edge_type"] == wk.WAS_INFORMED_BY]
    assert {e["target_id"] for e in informed} == {"evt-1", "evt-2"}

    in_edges = registry.knowledge.graph_store.get_edges(
        activity_id, direction="incoming"
    )
    generated = [e for e in in_edges if e["edge_type"] == wk.WAS_GENERATED_BY]
    assert len(generated) == 1


def test_include_meta_kwarg_currently_no_op(registry: StoreRegistry) -> None:
    """``include_meta`` is accepted for forward compat; behaviour unchanged."""
    _seed_target_nodes(registry, "evt-1")

    with record_meta_analysis(
        analyzer_name="context-effectiveness",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
        include_meta=True,
    ) as rec:
        rec.consumed_event("evt-1")

    activities = registry.knowledge.graph_store.query(node_type=wk.ACTIVITY, limit=10)
    assert len(activities) == 1
