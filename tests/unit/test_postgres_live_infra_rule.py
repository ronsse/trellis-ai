"""Postgres store suites must execute in live-infra CI."""

import shlex
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"


def _live_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["live-infra"]


def _step(name: str) -> dict[str, Any]:
    [test_step] = [step for step in _live_job()["steps"] if step.get("name") == name]
    return test_step


def _pytest_targets(command: str) -> frozenset[Path]:
    tokens = shlex.split(command.replace("\\\n", " "))
    pytest_at = tokens.index("pytest")
    return frozenset(
        Path(token.rstrip("/"))
        for token in tokens[pytest_at + 1 :]
        if not token.startswith("-") and token.startswith("tests/")
    )


def test_live_infra_selects_unwired_postgres_store_suites() -> None:
    test_step = _step("Run live + contract suites against the containers")
    env = test_step["env"]
    targets = _pytest_targets(test_step["run"])

    assert str(env["TRELLIS_TEST_POSTGRES"]).lower() in {"1", "true", "yes", "on"}
    assert env["TRELLIS_TEST_PG_DSN"]
    assert Path("tests/unit/stores/test_postgres_stores.py") in targets
    assert Path("tests/unit/stores/test_api_key_store.py") in targets

    install = _step("Install Trellis with live-infra extras")
    assert ".[dev,cloud,neo4j]" in shlex.split(install["run"])


def test_live_infra_does_not_sweep_in_neo4j_only_store_tests() -> None:
    test_step = _step("Run live + contract suites against the containers")
    store_targets = {
        target
        for target in _pytest_targets(test_step["run"])
        if target.parts[:3] == ("tests", "unit", "stores")
    }

    assert store_targets == {
        Path("tests/unit/stores/contracts"),
        Path("tests/unit/stores/test_pgvector.py"),
        Path("tests/unit/stores/test_postgres_stores.py"),
        Path("tests/unit/stores/test_api_key_store.py"),
    }
