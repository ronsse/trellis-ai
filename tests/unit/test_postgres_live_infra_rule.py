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

    Stated as the property rather than as the roster that used to stand
    here. The exact set of four targets this asserted was a proxy for
    *"don't sweep the directory"* — and a proxy that fails when a file is
    legitimately **added**. It did, twice over: four suites that pass
    against this job's own container images executed in no workflow at
    all, and wiring them in broke this assertion while satisfying its
    intent; #356 then added a fifth.

    What the sweep would cost is still real, and the directory has
    already produced one instance of it. ``test_neo4j_vector.py``'s
    ``SEARCH ... IN (VECTOR INDEX ...)`` cases are rejected at parse time
    by the self-hosted ``neo4j:2025.12`` service this job starts, and the
    file also used to provision a second vector index on a pair Neo4j
    allows one of — a failure that lands 30s later on a *different*
    suite. It is in the set now because #356 gave those four cases a
    capability gate and took the store's production-default index name;
    the condition on that entry, and the reason it is safe, is pinned
    separately by ``tests/unit/test_neo4j_vector_live_infra_rule.py``.

    So the invariant is enrollment granularity, not membership: the
    directory itself is never a target, and every store target except
    ``contracts/`` — which is swept on purpose, being the shared ABC
    semantics every backend must honour — is a single file. Adding one
    means editing this workflow by hand, which is the review this rule
    exists to force.

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
        "live-infra sweeps all of tests/unit/stores/, which enrols every "
        "future suite in this job without anyone running it against these "
        "containers"
    )
    swept = {
        target
        for target in store_targets
        if target.suffix != ".py" and target != Path("tests/unit/stores/contracts")
    }
    assert not swept, (
        f"live-infra names directories under tests/unit/stores/: {sorted(swept)}. "
        "Only contracts/ is swept on purpose; everything else is enrolled one "
        "reviewed file at a time"
    )
    assert store_targets, "live-infra selects no tests/unit/stores/ target at all"
