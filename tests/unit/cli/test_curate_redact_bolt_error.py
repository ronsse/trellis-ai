"""``trellis curate redact`` exits 5 when the Bolt purge cannot finish.

A purge the neo4j driver gives up on reaches the executor as ``StoreError``,
so the command is FAILED: exit ``5`` (``EXIT_STORE``) in both output formats,
and under ``--format json`` a payload whose ``command_id`` joins the
``MUTATION_REJECTED`` audit event.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("neo4j")

from neo4j.exceptions import ServiceUnavailable

from tests.cli_output import plain
from tests.fake_bolt_driver import FakeBoltDriver
from trellis.stores.base.event_log import EventType
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis_cli.main import app
from trellis_cli.stores import _get_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _unreachable_target(monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a node whose every purge loses its connection to the server."""
    graph = _get_registry().knowledge.graph_store
    node_id = graph.upsert_node(None, "person", {"name": "Purge Target"})
    bolt_store = BoltOpenCypherGraphStore(
        driver=FakeBoltDriver(ServiceUnavailable("synthetic server text")),
        database="neo4j",
        owns_driver=False,
        init_schema=False,
    )
    monkeypatch.setattr(graph, "delete_node", bolt_store.delete_node)
    return node_id


def _redact(node_id: str, output_format: str) -> list[str]:
    return [
        "curate",
        "redact",
        node_id,
        "--yes",
        "--reason",
        "synthetic purge failure",
        "--format",
        output_format,
    ]


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_a_purge_that_cannot_finish_exits_store(
    output_format: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_id = _unreachable_target(monkeypatch)

    result = runner.invoke(app, _redact(node_id, output_format))

    assert result.exit_code == 5, result.output
    assert "ServiceUnavailable" in plain(result.output)


def test_the_json_payload_joins_the_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = _unreachable_target(monkeypatch)

    result = runner.invoke(app, _redact(node_id, "json"))

    assert result.exit_code == 5, result.output
    data = json.loads(result.stdout.strip())
    assert data["status"] == "failed"
    assert node_id in data["message"]
    rejected = _get_registry().operational.event_log.get_events(
        event_type=EventType.MUTATION_REJECTED
    )
    assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
        (data["command_id"], "failed")
    ]
