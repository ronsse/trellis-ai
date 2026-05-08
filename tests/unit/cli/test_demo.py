"""Tests for `trellis demo` commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


class TestDemoLoad:
    def test_load_succeeds(self) -> None:
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0
        assert "entities" in result.stdout
        assert "traces" in result.stdout

    def test_seeds_local_aliases_for_entities(self) -> None:
        # Demo loader is the only path that ships with seeded aliases — the
        # README quickstart's `retrieve entity user-api` depends on it.
        result = runner.invoke(app, ["demo", "load"])
        assert result.exit_code == 0

        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        match = graph.resolve_alias(LOCAL_SOURCE_SYSTEM, "user-api")
        assert match is not None
        node = graph.get_node(match["entity_id"])
        assert node is not None
        assert node["node_type"] == "service"
        assert node["properties"]["name"] == "user-api"
