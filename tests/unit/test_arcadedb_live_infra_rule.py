"""The blessed ArcadeDB graph contract must execute in live-infra CI."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"


def _live_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["live-infra"]


def test_live_infra_provisions_arcadedb_with_bolt() -> None:
    service = _live_job()["services"]["arcadedb"]

    assert service["image"] == "arcadedata/arcadedb:26.8.1"
    assert set(service["ports"]) >= {"2480:2480", "17687:7687"}
    java_opts = service["env"]["JAVA_OPTS"]
    assert "-Darcadedb.server.rootPassword=playwithdata" in java_opts
    assert (
        "-Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin"
    ) in java_opts
    assert "/api/v1/ready" in service["options"]


def test_live_infra_selects_arcadedb_graph_contract() -> None:
    [test_step] = [
        step
        for step in _live_job()["steps"]
        if step.get("name") == "Run live + contract suites against the containers"
    ]
    env = test_step["env"]

    assert env["TRELLIS_TEST_ARCADEDB"] == "1"
    assert env["TRELLIS_TEST_ARCADEDB_URI"] == "bolt://localhost:17687"
    assert env["TRELLIS_TEST_ARCADEDB_HTTP_URL"] == "http://localhost:2480"
    assert env["TRELLIS_TEST_ARCADEDB_USER"] == "root"
    assert env["TRELLIS_TEST_ARCADEDB_PASSWORD"] == "playwithdata"  # noqa: S105
    assert env["TRELLIS_TEST_ARCADEDB_DATABASE"] == "trellis_test"
    assert "tests/unit/stores/contracts/" in test_step["run"]
