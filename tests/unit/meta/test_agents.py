"""Tests for :func:`trellis.meta.agents.ensure_meta_agent`."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.meta.agents import (
    DEFAULT_META_AGENT_ID,
    META_AGENT_PREFIX,
    ensure_meta_agent,
)
from trellis.schemas import well_known as wk
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    """Fresh SQLite-backed registry per test."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def test_creates_node_when_missing(registry: StoreRegistry) -> None:
    """``ensure_meta_agent`` materialises the synthetic Agent on first call."""
    agent_id = ensure_meta_agent(registry)
    assert agent_id == DEFAULT_META_AGENT_ID

    node = registry.knowledge.graph_store.get_node(agent_id)
    assert node is not None
    assert node["node_type"] == wk.AGENT
    assert node["properties"]["name"] == DEFAULT_META_AGENT_ID
    assert node["properties"]["synthetic"] is True


def test_returns_existing_node_on_repeat_call(registry: StoreRegistry) -> None:
    """Repeat calls are idempotent — same ID, no second version."""
    first = ensure_meta_agent(registry)
    second = ensure_meta_agent(registry)
    assert first == second == DEFAULT_META_AGENT_ID

    # Idempotent: only one current version exists in the SCD-2 history.
    history = registry.knowledge.graph_store.get_node_history(first)
    assert len(history) == 1


def test_multiple_distinct_meta_agent_ids_supported(
    registry: StoreRegistry,
) -> None:
    """The namespace supports more than the default agent."""
    tuner_id = ensure_meta_agent(registry, agent_id="trellis_meta_tuner")
    promoter_id = ensure_meta_agent(
        registry, agent_id="trellis_meta_promoter"
    )
    assert tuner_id == "trellis_meta_tuner"
    assert promoter_id == "trellis_meta_promoter"

    tuner = registry.knowledge.graph_store.get_node(tuner_id)
    promoter = registry.knowledge.graph_store.get_node(promoter_id)
    assert tuner is not None
    assert promoter is not None
    assert tuner["node_type"] == wk.AGENT
    assert promoter["node_type"] == wk.AGENT


def test_rejects_non_namespace_agent_id(registry: StoreRegistry) -> None:
    """Non-namespaced IDs raise — POC discipline."""
    with pytest.raises(ValueError, match=META_AGENT_PREFIX):
        ensure_meta_agent(registry, agent_id="my_custom_agent")


def test_rejects_existing_non_agent_node(registry: StoreRegistry) -> None:
    """A non-Agent node sitting at the desired ID is refused, not overwritten."""
    # Plant a Person node at the ID we're about to claim.
    registry.knowledge.graph_store.upsert_node(
        node_id="trellis_meta_collision",
        node_type=wk.PERSON,
        properties={"name": "not actually a meta agent"},
    )

    with pytest.raises(ValueError, match="node_type"):
        ensure_meta_agent(registry, agent_id="trellis_meta_collision")
