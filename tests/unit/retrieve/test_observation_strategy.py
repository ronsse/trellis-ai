"""Unit tests for :class:`ObservationSearch`.

Exercises the strategy against a small fake graph store so the
freshness-decay math, the confidence threshold, the subject filter, and
the missing-subject loud-fail path can all be observed without bringing
up a real backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from trellis.retrieve.observation_strategy import ObservationSearch
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
)

NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake graph store
# ---------------------------------------------------------------------------


def _make_store(
    edges_by_subject: dict[str, list[dict[str, Any]]],
    nodes_by_id: dict[str, dict[str, Any]],
) -> MagicMock:
    """Build a minimal fake graph store with the methods ObservationSearch needs.

    Implements only ``get_edges`` and ``get_node`` — that's enough for
    the strategy, and the fallback path (no ``get_nodes_bulk``) is what
    most ABC backends use for ad-hoc queries.
    """
    store = MagicMock()
    # No bulk method available — strategy will fall through to per-id
    # get_node calls (mirrors what the smaller backends ship).
    del store.get_nodes_bulk

    def _get_edges(
        node_id: str,
        direction: str = "both",
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        del direction, edge_type  # the strategy always asks for outgoing/hasObservation
        return edges_by_subject.get(node_id, [])

    def _get_node(node_id: str) -> dict[str, Any] | None:
        return nodes_by_id.get(node_id)

    store.get_edges.side_effect = _get_edges
    store.get_node.side_effect = _get_node
    return store


def _make_observation_node(
    node_id: str,
    *,
    subject_entity_id: str,
    confidence: float | None = 0.8,
    observed_at: datetime | None = None,
    content: str = "test observation",
    node_type: str = OBSERVATION,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "subject_entity_id": subject_entity_id,
        "subject_entity_type": "Dataset",
        "observer_agent_id": "test-agent",
        "content": content,
    }
    if confidence is not None:
        props["confidence"] = confidence
    if observed_at is not None:
        props["observed_at"] = observed_at.isoformat()
    return {
        "node_id": node_id,
        "node_type": node_type,
        "node_role": "semantic",
        "properties": props,
    }


# ---------------------------------------------------------------------------
# Strategy identity
# ---------------------------------------------------------------------------


def test_strategy_name_is_observation() -> None:
    strategy = ObservationSearch(graph_store=MagicMock())
    assert strategy.name == "observation"


# ---------------------------------------------------------------------------
# Subject filtering
# ---------------------------------------------------------------------------


def test_returns_empty_when_no_subject_supplied() -> None:
    """Missing subject must NOT silently return 'all observations' — that
    would be a footgun on a populated graph."""
    store = _make_store(
        edges_by_subject={"dataset:x": [{"target_id": "obs1"}]},
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("any", filters={})
    assert result == []
    # And no graph traversal was attempted.
    store.get_edges.assert_not_called()


def test_filters_by_subject_entity_id() -> None:
    store = _make_store(
        edges_by_subject={
            "dataset:x": [{"target_id": "obs1"}, {"target_id": "obs2"}],
            "dataset:y": [{"target_id": "obs3"}],
        },
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
            "obs2": _make_observation_node(
                "obs2",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
            "obs3": _make_observation_node(
                "obs3",
                subject_entity_id="dataset:y",
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"subject_entity_id": "dataset:x"})
    ids = sorted(r.item_id for r in result)
    assert ids == ["obs1", "obs2"]


def test_accepts_seed_ids_list() -> None:
    store = _make_store(
        edges_by_subject={
            "dataset:x": [{"target_id": "obs1"}],
            "dataset:y": [{"target_id": "obs2"}],
        },
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
            "obs2": _make_observation_node(
                "obs2",
                subject_entity_id="dataset:y",
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"seed_ids": ["dataset:x", "dataset:y"]})
    ids = sorted(r.item_id for r in result)
    assert ids == ["obs1", "obs2"]


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------


def test_confidence_threshold_drops_low_scoring_observations() -> None:
    store = _make_store(
        edges_by_subject={
            "dataset:x": [
                {"target_id": "obs_high"},
                {"target_id": "obs_low"},
            ],
        },
        nodes_by_id={
            "obs_high": _make_observation_node(
                "obs_high",
                subject_entity_id="dataset:x",
                confidence=0.9,
                observed_at=NOW,
            ),
            "obs_low": _make_observation_node(
                "obs_low",
                subject_entity_id="dataset:x",
                confidence=0.2,
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search(
        "",
        filters={
            "subject_entity_id": "dataset:x",
            "confidence_threshold": 0.5,
        },
    )
    assert [r.item_id for r in result] == ["obs_high"]


def test_missing_confidence_keeps_item_but_is_logged() -> None:
    """``confidence=None`` is not a silent fail — observation surfaces but
    with the documented 0.5 fallback base score."""
    store = _make_store(
        edges_by_subject={"dataset:x": [{"target_id": "obs1"}]},
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                confidence=None,
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"subject_entity_id": "dataset:x"})
    assert len(result) == 1
    assert result[0].item_id == "obs1"
    assert result[0].metadata["confidence"] is None


# ---------------------------------------------------------------------------
# Freshness decay
# ---------------------------------------------------------------------------


def test_freshness_ordering_newest_first() -> None:
    """Two observations with identical confidence — the newer one wins
    because freshness decay drops the older one's relevance score."""
    older = NOW - timedelta(days=60)
    newer = NOW - timedelta(days=1)
    store = _make_store(
        edges_by_subject={
            "dataset:x": [
                {"target_id": "obs_old"},
                {"target_id": "obs_new"},
            ],
        },
        nodes_by_id={
            "obs_old": _make_observation_node(
                "obs_old",
                subject_entity_id="dataset:x",
                confidence=0.8,
                observed_at=older,
            ),
            "obs_new": _make_observation_node(
                "obs_new",
                subject_entity_id="dataset:x",
                confidence=0.8,
                observed_at=newer,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"subject_entity_id": "dataset:x"})
    # Newer first.
    assert result[0].item_id == "obs_new"
    assert result[1].item_id == "obs_old"
    assert result[0].relevance_score > result[1].relevance_score


def test_observed_after_filter_drops_stale_rows() -> None:
    old = NOW - timedelta(days=400)
    fresh = NOW - timedelta(days=1)
    store = _make_store(
        edges_by_subject={
            "dataset:x": [
                {"target_id": "obs_stale"},
                {"target_id": "obs_fresh"},
            ],
        },
        nodes_by_id={
            "obs_stale": _make_observation_node(
                "obs_stale",
                subject_entity_id="dataset:x",
                observed_at=old,
            ),
            "obs_fresh": _make_observation_node(
                "obs_fresh",
                subject_entity_id="dataset:x",
                observed_at=fresh,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    cutoff = NOW - timedelta(days=30)
    result = strategy.search(
        "",
        filters={"subject_entity_id": "dataset:x", "observed_after": cutoff},
    )
    assert [r.item_id for r in result] == ["obs_fresh"]


# ---------------------------------------------------------------------------
# Measurement inclusion
# ---------------------------------------------------------------------------


def test_measurements_included_by_default() -> None:
    store = _make_store(
        edges_by_subject={
            "dataset:x": [
                {"target_id": "obs1"},
                {"target_id": "meas1"},
            ],
        },
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
            "meas1": _make_observation_node(
                "meas1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
                node_type=MEASUREMENT,
                content="query_count",
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"subject_entity_id": "dataset:x"})
    types = sorted({r.metadata["node_type_canonical"] for r in result})
    assert types == [MEASUREMENT, OBSERVATION]


def test_measurements_excluded_when_flag_disabled() -> None:
    store = _make_store(
        edges_by_subject={
            "dataset:x": [
                {"target_id": "obs1"},
                {"target_id": "meas1"},
            ],
        },
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
            "meas1": _make_observation_node(
                "meas1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
                node_type=MEASUREMENT,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search(
        "",
        filters={"subject_entity_id": "dataset:x", "include_measurements": False},
    )
    assert [r.item_id for r in result] == ["obs1"]


# ---------------------------------------------------------------------------
# Empty cases
# ---------------------------------------------------------------------------


def test_empty_result_when_subject_has_no_observations() -> None:
    store = _make_store(edges_by_subject={}, nodes_by_id={})
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search("", filters={"subject_entity_id": "dataset:nobody"})
    assert result == []


def test_limit_caps_result_size() -> None:
    edges = [{"target_id": f"obs{i}"} for i in range(10)]
    nodes = {
        f"obs{i}": _make_observation_node(
            f"obs{i}",
            subject_entity_id="dataset:x",
            observed_at=NOW,
        )
        for i in range(10)
    }
    store = _make_store(
        edges_by_subject={"dataset:x": edges},
        nodes_by_id=nodes,
    )
    strategy = ObservationSearch(graph_store=store)
    result = strategy.search(
        "",
        limit=3,
        filters={"subject_entity_id": "dataset:x"},
    )
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Edge kind canonicalization
# ---------------------------------------------------------------------------


def test_strategy_uses_canonical_edge_kinds() -> None:
    """Ensures the strategy queries the canonical ``hasObservation`` and
    ``hasMeasurement`` forms rather than legacy aliases (no aliases
    exist yet, but pinning the behaviour now keeps us honest if one is
    added later)."""
    store = _make_store(
        edges_by_subject={"dataset:x": [{"target_id": "obs1"}]},
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    strategy.search("", filters={"subject_entity_id": "dataset:x"})
    edge_kinds_queried = {
        c.kwargs.get("edge_type") for c in store.get_edges.call_args_list
    }
    assert HAS_OBSERVATION in edge_kinds_queried
    assert HAS_MEASUREMENT in edge_kinds_queried


def test_strategy_skips_has_measurement_when_measurements_disabled() -> None:
    """When ``include_measurements=False`` the strategy must not waste
    a graph round-trip on ``hasMeasurement``."""
    store = _make_store(
        edges_by_subject={"dataset:x": [{"target_id": "obs1"}]},
        nodes_by_id={
            "obs1": _make_observation_node(
                "obs1",
                subject_entity_id="dataset:x",
                observed_at=NOW,
            ),
        },
    )
    strategy = ObservationSearch(graph_store=store)
    strategy.search(
        "",
        filters={"subject_entity_id": "dataset:x", "include_measurements": False},
    )
    edge_kinds_queried = {
        c.kwargs.get("edge_type") for c in store.get_edges.call_args_list
    }
    assert HAS_OBSERVATION in edge_kinds_queried
    assert HAS_MEASUREMENT not in edge_kinds_queried
