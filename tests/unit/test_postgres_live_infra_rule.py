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
    """The one store file this job cannot run must stay unselected.

    Stated as the property rather than as the roster that used to stand
    here. The exact set of four targets this asserted was a proxy for
    *"don't sweep the directory, because ``test_neo4j_vector.py`` cannot
    run against a self-hosted Neo4j"* — and a proxy that fails when a
    file is legitimately **added**. It did: four suites that pass against
    this job's own container images executed in no workflow at all, and
    wiring them in broke this assertion while satisfying its intent.

    Re-measured 2026-09-12 against ``neo4j:2025.12``, the image this
    workflow starts: 23 of ``test_neo4j_vector.py``'s tests pass and its
    four ``TestQuery`` cases fail with ``Invalid input 'SEARCH'``. The
    capability probe that would let those four self-skip is #356.

    The complementary direction — a file that runs nowhere and has no
    recorded reason — is
    :file:`tests/unit/test_ci_coverage_rule.py`. Neither test asserts a
    roster; together they pin one.
    """
    test_step = _step("Run live + contract suites against the containers")
    store_targets = {
        target
        for target in _pytest_targets(test_step["run"])
        if target.parts[:3] == ("tests", "unit", "stores")
    }

    assert Path("tests/unit/stores") not in store_targets, (
        "live-infra sweeps all of tests/unit/stores/, which selects "
        "test_neo4j_vector.py's AuraDB-only TestQuery cases"
    )
    assert Path("tests/unit/stores/test_neo4j_vector.py") not in store_targets, (
        "test_neo4j_vector.py's TestQuery cases need AuraDB's SEARCH "
        "clause; give them tests/integration/conftest.py's capability "
        "probe (#356) before naming this file here"
    )
    assert store_targets, "live-infra selects no tests/unit/stores/ target at all"
