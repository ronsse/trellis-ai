"""Tests for GraphNeighborClassifier."""

from __future__ import annotations

from unittest.mock import MagicMock

from trellis.classify.classifiers.graph_neighbor import GraphNeighborClassifier
from trellis.classify.protocol import ClassificationContext


def _make_node(
    node_id: str,
    content_tags: dict | None = None,
    classification_confidence: float = 0.9,
) -> dict:
    props: dict = {}
    if content_tags is not None:
        props["content_tags"] = content_tags
        props["classification_confidence"] = classification_confidence
    return {"node_id": node_id, "node_type": "entity", "properties": props}


def _make_edge(source_id: str, target_id: str) -> dict:
    return {"source_id": source_id, "target_id": target_id, "edge_type": "related"}


class TestGraphNeighborClassifier:
    """GraphNeighborClassifier infers tags from connected nodes."""

    def test_name(self) -> None:
        store = MagicMock()
        c = GraphNeighborClassifier(graph_store=store)
        assert c.name == "graph_neighbor"

    def test_no_node_id_returns_empty(self) -> None:
        store = MagicMock()
        c = GraphNeighborClassifier(graph_store=store)
        result = c.classify("content")
        assert result.tags == {}
        assert result.confidence == 0.0

    def test_no_edges_returns_empty(self) -> None:
        store = MagicMock()
        store.get_edges.return_value = []
        c = GraphNeighborClassifier(graph_store=store)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)
        assert result.tags == {}

    def test_propagates_majority_domain(self) -> None:
        """When 2/3 neighbors agree on domain, it propagates."""
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),
            _make_edge("node1", "n3"),
            _make_edge("node1", "n4"),
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"domain": ["data-pipeline"]}),
            _make_node("n3", {"domain": ["data-pipeline"]}),
            _make_node("n4", {"domain": ["infrastructure"]}),
        ]
        c = GraphNeighborClassifier(graph_store=store)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        assert "data-pipeline" in result.tags.get("domain", [])
        # infrastructure only has 1/3 votes, below 0.5 threshold
        assert "infrastructure" not in result.tags.get("domain", [])

    def test_confidence_decay_applied(self) -> None:
        store = MagicMock()
        store.get_edges.return_value = [_make_edge("node1", "n2")]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"domain": ["api"]}, classification_confidence=0.95),
        ]
        c = GraphNeighborClassifier(graph_store=store, confidence_decay=0.85)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        assert result.confidence == 0.85

    def test_skips_low_confidence_neighbors(self) -> None:
        """Neighbors below min_neighbor_confidence are ignored."""
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),
            _make_edge("node1", "n3"),
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"domain": ["api"]}, classification_confidence=0.3),
            _make_node("n3", {"domain": ["api"]}, classification_confidence=0.3),
        ]
        c = GraphNeighborClassifier(graph_store=store, min_neighbor_confidence=0.8)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        # Both neighbors are below threshold — no tags propagated
        assert result.tags == {}

    def test_propagates_content_type(self) -> None:
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),
            _make_edge("n3", "node1"),
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"content_type": "code"}),
            _make_node("n3", {"content_type": "code"}),
        ]
        c = GraphNeighborClassifier(graph_store=store)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        assert "code" in result.tags.get("content_type", [])

    def test_vote_fraction_threshold(self) -> None:
        """With min_vote_fraction=0.7, need 70% agreement."""
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),
            _make_edge("node1", "n3"),
            _make_edge("node1", "n4"),
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"domain": ["api"]}),
            _make_node("n3", {"domain": ["api"]}),
            _make_node("n4", {"domain": ["security"]}),
        ]
        # 2/3 = 0.67, below 0.7 threshold
        c = GraphNeighborClassifier(graph_store=store, min_vote_fraction=0.7)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        assert result.tags == {}

    def test_neighbors_without_tags_ignored(self) -> None:
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),
            _make_edge("node1", "n3"),
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2"),  # no tags
            _make_node("n3", {"domain": ["api"]}),
        ]
        c = GraphNeighborClassifier(graph_store=store)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        # n2 has no classification_confidence at all, so it's skipped
        # Only n3 votes, 1/2 = 0.5, meets default threshold
        assert "api" in result.tags.get("domain", [])

    def test_bidirectional_edge_detection(self) -> None:
        """Both incoming and outgoing edges are considered."""
        store = MagicMock()
        store.get_edges.return_value = [
            _make_edge("node1", "n2"),  # outgoing
            _make_edge("n3", "node1"),  # incoming
        ]
        store.get_nodes_bulk.return_value = [
            _make_node("n2", {"domain": ["data-pipeline"]}),
            _make_node("n3", {"domain": ["data-pipeline"]}),
        ]
        c = GraphNeighborClassifier(graph_store=store)
        ctx = ClassificationContext(node_id="node1")
        result = c.classify("", context=ctx)

        assert "data-pipeline" in result.tags.get("domain", [])
