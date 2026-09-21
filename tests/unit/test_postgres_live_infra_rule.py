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
    here, because this assertion has now rotted twice in opposite
    directions and neither failure was a bad roster -- both were the
    idea that a roster belongs here at all.

    It first asserted an exact four-file set. That is a proxy for *"do
    not sweep the directory"*, and a proxy that fails when a file is
    legitimately **added**: four suites that pass against this job's own
    container images ran in no workflow at all, and wiring them in broke
    this assertion while satisfying its intent. #579's first replacement
    then pinned *"``test_neo4j_vector.py`` is never collected"* -- the
    same mistake one layer in, because an assertion that a *fix* must
    not happen is worse than a roster, and #356 landed exactly that fix.

    So that second clause is retired, not weakened. It held only while
    the suite's four ``SEARCH ... IN (VECTOR INDEX ...)`` cases had no
    capability gate, and the message it failed with said so: *give them
    the probe (#356) before naming this file here*. #356 did.
    Re-measured against ``neo4j:2025.12``, the image this workflow
    starts, that suite now runs 24 passed / 4 skipped where it was
    4 failed / 23 never executed by any workflow.

    What is left is the invariant neither version was: the directory
    itself is never a target, and something under it always is. Naming a
    file individually *is* the review -- that is the whole property, and
    it says nothing about the status of any one file. An addition
    therefore costs an edit to the workflow, not to this test.

    Three sibling rules carry the parts that do need to track files, all
    of them derived rather than declared:
    :file:`tests/unit/test_neo4j_vector_live_infra_rule.py` pins the
    selection above *and* the two conditions that make it safe -- every
    ``SEARCH``-issuing test carries the gate, and the two suites sharing
    one Neo4j resolve to one vector index name;
    :file:`tests/unit/test_arcadedb_live_infra_rule.py` scans for the
    ArcadeDB marker and fails when a marked file is not selected here;
    and :file:`tests/unit/test_ci_coverage_rule.py` covers the
    complementary direction, a file that runs nowhere with no recorded
    reason. None of the four asserts a roster; together they pin one.
    """
    test_step = _step("Run live + contract suites against the containers")
    targets = _pytest_targets(test_step["run"])
    store_dir = Path("tests/unit/stores")

    assert store_dir not in targets, (
        "live-infra sweeps all of tests/unit/stores/, which enrols suites "
        "nobody has run against these containers; name each path instead"
    )
    named = sorted(str(target) for target in targets if store_dir in target.parents)
    assert len(named) >= 2, (
        f"expected live-infra to name store paths under {store_dir}/ "
        f"individually, found {named} -- a target list that names none "
        "would satisfy the sweep check vacuously"
    )
