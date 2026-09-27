"""Tests for ``trellis analyze graph-shape``.

The parity property is tested **behaviourally** here, not only by the AST
rule in ``tests/unit/test_format_exit_parity_rule.py``: the same store state
is run through both format arms and the two exit codes are compared. #437
was a command whose exit code differed by format, and the scan that would
have caught it is a static one — a second, dynamic witness is cheap and
fails for a different reason than the scan would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.analyze.graph_shape import READ_MARGIN
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point CLI stores at a temp directory and return the registry."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()

    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def populated(_temp_stores: StoreRegistry) -> StoreRegistry:
    """A graph carrying one of every shape the report measures."""
    graph = _temp_stores.knowledge.graph_store
    graph.upsert_node("agent", "Agent", {"name": "trellis_meta_cli"})
    for index in range(3):
        graph.upsert_node(f"act{index}", "Activity", {"name": f"act{index}"})
        graph.upsert_edge("agent", f"act{index}", "wasAssociatedWith")
    # An uncovered case split: 'system' aliases, 'System' does not.
    graph.upsert_node("s1", "system", {"name": "hermes"})
    graph.upsert_node("s2", "System", {"name": "skynet"})
    # A cross-type name collision on the canonical key.
    graph.upsert_node("p1", "Project", {"name": "hermes"})
    # An isolated node from the save_knowledge vocabulary.
    graph.upsert_node("g1", "gotcha", {"name": "a gotcha"})
    # An external referent, and a document link.
    graph.upsert_node(
        "d1",
        "Dataset",
        {"name": "orders", "physical_uri": "postgres://host/db"},
        document_ids=["doc1"],
    )
    return _temp_stores


class _UndercountingGraphStore:
    """Wraps the real store so its counts under-report, forcing truncation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def count_nodes(self) -> int:
        return -READ_MARGIN

    def count_edges(self) -> int:
        return -READ_MARGIN

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestRendersBothSurfaces:
    def test_text_output_names_every_section(self, populated) -> None:
        result = runner.invoke(app, ["analyze", "graph-shape"])

        assert result.exit_code == EXIT_OK, result.output
        # ``result.stdout``, not ``result.output``: since click 8.2 ``output``
        # interleaves stderr, so a report printed to stderr passed here too.
        # The report is stdout; stderr is where the CLI's logs and warnings go.
        out = plain(result.stdout)
        # The counts, not only the heading: without this, the empty-graph
        # test's header pin is satisfied by a header that always prints 0/0.
        assert "Graph Shape — 9 nodes, 3 edges" in out.splitlines()
        for heading in (
            "Graph Shape",
            "Node types",
            "Name collisions",
            "Degree",
            "Isolated",
            "External referents",
            "Document linkage",
        ):
            assert heading in out

    def test_json_output_is_parseable_and_complete(self, populated) -> None:
        result = runner.invoke(app, ["analyze", "graph-shape", "--format", "json"])

        assert result.exit_code == EXIT_OK, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["nodes"] == 9
        assert payload["edges"] == 3
        assert payload["scan"]["truncated"] is False
        # Every section the text arm prints has a machine counterpart.
        for key in (
            "type_buckets",
            "uncovered_splits",
            "collisions",
            "degree",
            "isolation",
            "referents",
            "documents",
        ):
            assert key in payload

    def test_json_carries_the_findings_the_text_arm_shows(self, populated) -> None:
        result = runner.invoke(app, ["analyze", "graph-shape", "--format", "json"])
        payload = json.loads(result.output)

        splits = payload["uncovered_splits"]
        assert len(splits) == 1
        assert splits[0]["buckets"] == {"SoftwareApplication": 1, "System": 1}

        assert payload["collisions"]["canonical"] == 1
        assert payload["collisions"]["groups"][0]["name"] == "hermes"

        assert payload["degree"]["degree_max"] == 3
        assert payload["degree"]["top_hubs"][0]["node_id"] == "agent"

        assert payload["documents"]["linked"] == 1
        assert payload["documents"]["max_per_node"] == 1

        assert payload["referents"]["any_referent"] == 1
        assert payload["referents"]["alias_table_read"] is False

    def test_an_empty_graph_renders_rather_than_dividing_by_zero(self) -> None:
        result = runner.invoke(app, ["analyze", "graph-shape"])

        assert result.exit_code == EXIT_OK, result.output
        # The whole header line, not "0 nodes": an empty report prints that
        # on three other lines too, so rewording or deleting the header left
        # this test green. ``stdout`` because ``output`` also carries stderr.
        assert "Graph Shape — 0 nodes, 0 edges" in plain(result.stdout).splitlines()


class TestFormatExitParity:
    """#437: a command's exit code must not depend on ``--format``."""

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_a_healthy_read_exits_zero_on_both_surfaces(
        self, populated, output_format: str
    ) -> None:
        result = runner.invoke(
            app, ["analyze", "graph-shape", "--format", output_format]
        )

        assert result.exit_code == EXIT_OK, result.output

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_a_truncated_read_exits_non_zero_on_both_surfaces(
        self, populated, monkeypatch: pytest.MonkeyPatch, output_format: str
    ) -> None:
        """The failure #437 produced: success reported on the machine surface.

        A truncated census is a partial answer. If only the text arm could
        end non-zero, a script parsing the JSON would read a prefix of the
        graph as the graph and see exit 0 confirming it.
        """
        from trellis_cli import analyze

        inner = analyze.get_graph_store()
        monkeypatch.setattr(
            analyze, "get_graph_store", lambda: _UndercountingGraphStore(inner)
        )

        result = runner.invoke(
            app, ["analyze", "graph-shape", "--format", output_format]
        )

        assert result.exit_code == EXIT_STORE, result.output

    def test_the_two_surfaces_agree_on_the_same_store_state(
        self, populated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Compared directly, rather than asserted arm by arm."""
        from trellis_cli import analyze

        inner = analyze.get_graph_store()
        monkeypatch.setattr(
            analyze, "get_graph_store", lambda: _UndercountingGraphStore(inner)
        )

        text = runner.invoke(app, ["analyze", "graph-shape"])
        machine = runner.invoke(app, ["analyze", "graph-shape", "--format", "json"])

        assert text.exit_code == machine.exit_code

    def test_a_truncated_json_payload_says_so_in_its_status(
        self, populated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exit code and the payload are derived from the same flag."""
        from trellis_cli import analyze

        inner = analyze.get_graph_store()
        monkeypatch.setattr(
            analyze, "get_graph_store", lambda: _UndercountingGraphStore(inner)
        )

        result = runner.invoke(app, ["analyze", "graph-shape", "--format", "json"])
        payload = json.loads(result.output)

        assert result.exit_code == EXIT_STORE
        assert payload["status"] == "truncated"
        assert payload["scan"]["truncated"] is True
        assert payload["scan"]["note"]
