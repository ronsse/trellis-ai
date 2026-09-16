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

    Naming the directory enrols every suite in it against containers
    nobody chose them for, and that directory has already produced an
    instance: ``test_neo4j_vector.py`` issues AuraDB-grade ``SEARCH ...
    IN (VECTOR INDEX ...)`` that this job's self-hosted Neo4j cannot
    parse (#356). Naming a file individually *is* the review, which is
    the whole property — not the status of any one file.

    That distinction was learned twice. This asserted an exact
    four-file roster until #579, and the roster rotted the moment a
    legitimately unwired file was added. #579's first replacement then
    pinned "``test_neo4j_vector.py`` is never collected" — which is the
    same mistake one layer in, because the capability-probe work on
    #356 enrols exactly that file the moment it lands, and an
    assertion that a *fix* must not happen is worse than a roster.
    What is left is the invariant neither of those was: the directory
    itself is never a target, and the rule is not vacuous because
    something under it must be.
    """
    test_step = _step("Run live + contract suites against the containers")
    targets = _pytest_targets(test_step["run"])
    store_dir = Path("tests/unit/stores")

    assert store_dir not in targets, (
        "live-infra must name store paths individually; sweeping "
        f"{store_dir}/ enrols suites nobody ran against these containers"
    )
    named = sorted(str(t) for t in targets if store_dir in t.parents)
    assert len(named) >= 2, (
        f"expected live-infra to name store paths under {store_dir}/ "
        f"individually, found {named} — a target list that names none "
        "would satisfy the sweep check vacuously"
    )
