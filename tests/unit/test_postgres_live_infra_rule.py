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


def test_live_infra_does_not_sweep_the_stores_directory() -> None:
    """``tests/unit/stores/`` is enrolled per path, never swept.

    Stated as the property rather than as the roster that used to stand
    here. The exact set of four targets this asserted was a proxy for
    *"don't sweep the directory, because ``test_neo4j_vector.py`` cannot
    run against a self-hosted Neo4j"* — and a proxy that fails when a
    file is legitimately **added**. It did: four suites that pass against
    this job's own container images executed in no workflow at all, and
    wiring them in broke this assertion while satisfying its intent. The
    roster is not reinstated here for that reason; a sweep still fails,
    and an addition still costs an edit to the workflow rather than to
    this test.

    The second half of the old property — *"``test_neo4j_vector.py`` must
    stay unselected"* — is retired, not weakened. It held only while its
    four ``SEARCH ... IN (VECTOR INDEX ...)`` cases had no capability
    gate, and the message it failed with said so: *give them the probe
    (#356) before naming this file here*. #356 did. Re-measured against
    ``neo4j:2025.12``, the image this workflow starts, the suite now runs
    24 passed / 4 skipped where it was 4 failed / 23 never executed by
    any workflow. What replaced the negative assertion is
    :file:`tests/unit/test_neo4j_vector_live_infra_rule.py`, which pins
    the selection *and* the condition that makes it safe — that every
    ``SEARCH``-issuing test carries the gate, and that the two suites
    sharing one Neo4j resolve to one vector index name.

    The complementary direction — a file that runs nowhere and has no
    recorded reason — is
    :file:`tests/unit/test_ci_coverage_rule.py`. None of the three
    asserts a roster; together they pin one.
    """
    test_step = _step("Run live + contract suites against the containers")
    store_targets = {
        target
        for target in _pytest_targets(test_step["run"])
        if target.parts[:3] == ("tests", "unit", "stores")
    }

    assert Path("tests/unit/stores") not in store_targets, (
        "live-infra sweeps all of tests/unit/stores/, which enrols suites "
        "nobody has run against these containers; name each path instead"
    )
    assert store_targets, "live-infra selects no tests/unit/stores/ target at all"
