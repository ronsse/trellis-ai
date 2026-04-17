"""Tests for trellis admin graph-health command."""

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from trellis_cli.admin import admin_app

runner = CliRunner()


def _make_graph_store(nodes=None, edges_map=None):
    """Create a mock GraphStore with configurable node/edge data."""
    store = MagicMock()
    nodes = nodes or []
    edges_map = edges_map or {}
    store.count_nodes.return_value = len(nodes)
    store.count_edges.return_value = sum(len(v) for v in edges_map.values())
    store.query.return_value = nodes
    store.get_edges.side_effect = lambda nid, direction="both": edges_map.get(nid, [])
    return store


class TestGraphHealthEmpty:
    @patch("trellis_cli.admin.get_graph_store")
    def test_empty_graph_json(self, mock_gs):
        mock_gs.return_value = _make_graph_store()
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "empty"

    @patch("trellis_cli.admin.get_graph_store")
    def test_empty_graph_text(self, mock_gs):
        mock_gs.return_value = _make_graph_store()
        result = runner.invoke(admin_app, ["graph-health"])
        assert result.exit_code == 0
        assert "empty" in result.stdout.lower()


class TestGraphHealthRoleDistribution:
    @patch("trellis_cli.admin.get_graph_store")
    def test_role_counts_in_json(self, mock_gs):
        nodes = [
            {"node_id": "1", "node_type": "service", "node_role": "semantic"},
            {"node_id": "2", "node_type": "service", "node_role": "semantic"},
            {"node_id": "3", "node_type": "column", "node_role": "structural"},
        ]
        mock_gs.return_value = _make_graph_store(nodes)
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        data = json.loads(result.stdout.strip())
        roles = {r["role"]: r["count"] for r in data["role_distribution"]}
        assert roles["semantic"] == 2
        assert roles["structural"] == 1

    @patch("trellis_cli.admin.get_graph_store")
    def test_structural_dominant_warning(self, mock_gs):
        """Structural > 70% should trigger a warning."""
        nodes = [
            {"node_id": str(i), "node_type": "col", "node_role": "structural"}
            for i in range(8)
        ] + [
            {"node_id": "s1", "node_type": "svc", "node_role": "semantic"},
            {"node_id": "s2", "node_type": "svc", "node_role": "semantic"},
        ]
        mock_gs.return_value = _make_graph_store(nodes)
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        data = json.loads(result.stdout.strip())
        signals = [w["signal"] for w in data["warnings"]]
        assert "structural_dominant" in signals
        assert result.exit_code == 1  # warnings present


class TestGraphHealthTypeBalance:
    @patch("trellis_cli.admin.get_graph_store")
    def test_type_imbalance_warning(self, mock_gs):
        nodes = [
            {"node_id": str(i), "node_type": "column", "node_role": "structural"}
            for i in range(8)
        ] + [
            {"node_id": "s1", "node_type": "service", "node_role": "semantic"},
            {"node_id": "s2", "node_type": "team", "node_role": "semantic"},
        ]
        mock_gs.return_value = _make_graph_store(nodes)
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        data = json.loads(result.stdout.strip())
        signals = [w["signal"] for w in data["warnings"]]
        assert "type_imbalance" in signals


class TestGraphHealthOrphans:
    @patch("trellis_cli.admin.get_graph_store")
    def test_orphan_detection(self, mock_gs):
        nodes = [
            {"node_id": "connected", "node_type": "svc", "node_role": "semantic"},
            {"node_id": "orphan", "node_type": "svc", "node_role": "semantic"},
        ]
        edges_map = {
            "connected": [
                {"edge_id": "e1", "source_id": "connected", "target_id": "other"}
            ],
            "orphan": [],
        }
        mock_gs.return_value = _make_graph_store(nodes, edges_map)
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        data = json.loads(result.stdout.strip())
        assert data["orphan_count"] == 1
        assert "orphan" in data["orphan_sample"]


class TestGraphHealthLeafAnalysis:
    @patch("trellis_cli.admin.get_graph_store")
    def test_semantic_mostly_leaves_warning(self, mock_gs):
        nodes = [
            {"node_id": f"n{i}", "node_type": "metric", "node_role": "semantic"}
            for i in range(10)
        ]
        # All nodes have zero outbound edges → 100% leaves
        mock_gs.return_value = _make_graph_store(nodes)
        result = runner.invoke(admin_app, ["graph-health", "--format", "json"])
        data = json.loads(result.stdout.strip())
        signals = [w["signal"] for w in data["warnings"]]
        assert "semantic_mostly_leaves" in signals


class TestGraphHealthFilters:
    @patch("trellis_cli.admin.get_graph_store")
    def test_entity_type_filter(self, mock_gs):
        store = _make_graph_store(
            [
                {"node_id": "1", "node_type": "service", "node_role": "semantic"},
            ]
        )
        mock_gs.return_value = store
        runner.invoke(
            admin_app, ["graph-health", "--entity-type", "service", "--format", "json"]
        )
        store.query.assert_called_once_with(
            node_type="service",
            properties=None,
            limit=10000,
        )

    @patch("trellis_cli.admin.get_graph_store")
    def test_role_filter(self, mock_gs):
        store = _make_graph_store(
            [
                {"node_id": "1", "node_type": "service", "node_role": "curated"},
            ]
        )
        mock_gs.return_value = store
        runner.invoke(
            admin_app, ["graph-health", "--role", "curated", "--format", "json"]
        )
        store.query.assert_called_once_with(
            node_type=None,
            properties={"node_role": "curated"},
            limit=10000,
        )
