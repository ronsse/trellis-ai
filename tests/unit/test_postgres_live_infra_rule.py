"""Postgres store suites must execute in live-infra CI."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"


def _live_test_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    [test_step] = [
        step
        for step in workflow["jobs"]["live-infra"]["steps"]
        if step.get("name") == "Run live + contract suites against the containers"
    ]
    return test_step


def test_live_infra_selects_unwired_postgres_store_suites() -> None:
    test_step = _live_test_step()

    assert test_step["env"]["TRELLIS_TEST_POSTGRES"] == "1"
    assert "tests/unit/stores/test_postgres_stores.py" in test_step["run"]
    assert "tests/unit/stores/test_api_key_store.py" in test_step["run"]


def test_live_infra_does_not_sweep_in_neo4j_only_store_tests() -> None:
    assert "tests/unit/stores/" not in _live_test_step()["run"].split()
