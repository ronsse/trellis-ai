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


def test_live_infra_store_targets_are_named_one_at_a_time() -> None:
    """``tests/unit/stores/`` is enrolled per path, never swept.

    A sweep would enrol suites nobody has run against these containers, and
    the directory has already produced one instance of exactly that:
    ``test_neo4j_vector.py``, whose ``SEARCH ... IN (VECTOR INDEX ...)`` cases
    the self-hosted ``neo4j:2025.12`` service rejects at parse time. It is in
    the set now because #356 gave those four cases a capability gate — the
    condition on that entry, and the reason it is safe, is pinned separately
    by ``tests/unit/test_neo4j_vector_live_infra_rule.py``. Adding a sixth
    path means editing this set, which is the review this rule exists to
    force.
    """
    test_step = _step("Run live + contract suites against the containers")
    store_targets = {
        target
        for target in _pytest_targets(test_step["run"])
        if target.parts[:3] == ("tests", "unit", "stores")
    }

    assert store_targets == {
        Path("tests/unit/stores/contracts"),
        Path("tests/unit/stores/test_neo4j_vector.py"),
        Path("tests/unit/stores/test_pgvector.py"),
        Path("tests/unit/stores/test_postgres_stores.py"),
        Path("tests/unit/stores/test_api_key_store.py"),
    }
