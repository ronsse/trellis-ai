"""``trellis curate redact`` exits 5 when a store is down before the purge.

A Neo4j graph store that cannot open, and an ArcadeDB vector store that
refuses connections or answers an error, reach the executor as
``StoreError``: the command is FAILED with exit ``5`` (``EXIT_STORE``) and
one ``MUTATION_REJECTED`` event, nothing is purged, and neither the result
nor the event carries the driver's or the server's text.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.stub_http_server import StubHttpServer, http_response
from trellis.stores.base.event_log import EventType
from trellis_cli.main import app
from trellis_cli.stores import _get_registry

runner = CliRunner()

MARKER = "preescape-server-reply-0002"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def refused_port() -> Iterator[int]:
    # A bound socket that never listens refuses every connection, and holding
    # it keeps the port from being reused while the test runs.
    with socket.socket() as unheard:
        unheard.bind(("127.0.0.1", 0))
        yield unheard.getsockname()[1]


def _configure(tmp_path: Path, knowledge: dict[str, Any]) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"knowledge": knowledge}))


def _redact(target_id: str) -> list[str]:
    return [
        "curate",
        "redact",
        target_id,
        "--yes",
        "--reason",
        "synthetic store outage",
        "--format",
        "json",
    ]


def _failed_and_audited(output: str, message: str) -> dict[str, Any]:
    """The JSON result is FAILED on ``message``, and one event records it."""
    data = json.loads(output)
    assert data["status"] == "failed"
    assert data["message"] == f"Execution failed: {message}"
    event_log = _get_registry().operational.event_log
    rejected = event_log.get_events(event_type=EventType.MUTATION_REJECTED)
    assert [
        (e.payload["command_id"], e.payload["status"], e.payload["message"])
        for e in rejected
    ] == [(data["command_id"], "failed", message)]
    assert event_log.get_events(event_type=EventType.REDACTION_APPLIED) == []
    return rejected[0].payload


def test_a_graph_store_that_cannot_open_fails_the_redaction(
    tmp_path: Path, refused_port: int
) -> None:
    pytest.importorskip("neo4j")
    _configure(
        tmp_path,
        {
            "graph": {
                "backend": "neo4j",
                "uri": f"bolt://127.0.0.1:{refused_port}",
                "password": "unused-secret",
            }
        },
    )

    result = runner.invoke(app, _redact("ent-preescape-0001"))

    assert result.exit_code == 5, result.output
    payload = _failed_and_audited(
        result.stdout, "Opening the graph store failed: ServiceUnavailable"
    )
    for text in (result.output, json.dumps(payload)):
        assert f"127.0.0.1:{refused_port}" not in text


def _arcadedb_vector(http_url: str) -> dict[str, Any]:
    return {
        "vector": {
            "backend": "arcadedb",
            "http_url": http_url,
            "password": "unused-secret",
            "database": "db_preescape",
            "dimensions": 3,
        }
    }


def _seed_target() -> str:
    graph = _get_registry().knowledge.graph_store
    return graph.upsert_node(None, "person", {"name": "Redaction Target"})


def test_a_vector_store_that_refuses_connections_fails_the_redaction(
    tmp_path: Path, refused_port: int
) -> None:
    _configure(tmp_path, _arcadedb_vector(f"http://127.0.0.1:{refused_port}"))
    target_id = _seed_target()

    result = runner.invoke(app, _redact(target_id))

    assert result.exit_code == 5, result.output
    payload = _failed_and_audited(
        result.stdout, "ArcadeDB SQL command failed: URLError(ConnectionRefusedError)"
    )
    for text in (result.output, json.dumps(payload)):
        assert "urlopen" not in text
        assert f"127.0.0.1:{refused_port}" not in text
    assert _get_registry().knowledge.graph_store.get_node(target_id) is not None


def _schema_ok_delete_fails(_path: str, body: dict[str, Any]) -> bytes:
    if str(body["command"]).startswith("UPDATE Node SET embedding = null"):
        return http_response(500, json.dumps({"error": "Internal", "detail": MARKER}))
    return http_response(200, json.dumps({"result": []}))


def test_a_vector_delete_the_server_refuses_fails_the_redaction(
    tmp_path: Path,
) -> None:
    with StubHttpServer(_schema_ok_delete_fails) as server:
        _configure(tmp_path, _arcadedb_vector(server.url))
        target_id = _seed_target()

        result = runner.invoke(app, _redact(target_id))

    assert result.exit_code == 5, result.output
    payload = _failed_and_audited(
        result.stdout, "ArcadeDB SQL command failed: HTTP 500"
    )
    for text in (result.output, json.dumps(payload)):
        assert MARKER not in text
        assert server.url not in text
    # The four schema statements, then the delete the server refused.
    assert len(server.requests) == 5
    assert str(server.requests[-1][1]["command"]).startswith("UPDATE Node SET")
    assert _get_registry().knowledge.graph_store.get_node(target_id) is not None
