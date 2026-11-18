"""Fail live-infra when a floored live contract ran less than it should.

live-infra writes a junit report on every run (``PYTEST_ADDOPTS`` on its test
step) and, until this script, read it only when the job had already failed. A
skip is not a failure, so a contract that skipped every case -- a skip mark, a
mistyped env var, a dropped ``neo4j`` or ``psycopg`` extra, a roster widened
to every case -- left the job green while the contract stopped running.

This reads that report and fails when a floored module passes fewer cases than
its floor, or skips more than its allowance. Both bounds leave room to
improve: a new case that passes, or a backend that stops needing a skip, never
turns the job red.

The floored modules are the cloud graph and vector store contracts. Every case
of one skips when its backend's env toggle is unset, so an unset toggle is the
failure this reads for.

Run from the repository root::

    python scripts/check_live_floor.py ci-evidence/junit.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

#: Test module -> (fewest cases that must pass, most that may skip).
#:
#: Each row is hand-read off a green live-infra run's own per-test lines,
#: never from this script, and its comment names the run. A change that
#: removes passing contract cases, or gates more of them behind a capability,
#: edits that row in the same diff. A floor is a minimum: adding a case never
#: turns the job red.
FLOORS: dict[str, tuple[int, int]] = {
    # Every row below was read off live-infra run 44027876125 at 2eb74266
    # (2026-11-17), from the job's own per-test lines.
    #
    # Neo4j vector: 44 passed, 0 skipped. ``neo4j:2025.12`` refuses the
    # ``SEARCH`` clause, and the store falls back to the
    # ``db.index.vector.queryNodes`` procedure, so the 19 cases that once
    # skipped behind a capability gate now run. Before the fallback: 25
    # passed, 19 skipped (run 37484813426 at d873f570, 2026-10-06).
    # live-infra now runs ``neo4j:2026.10.2``, which answers ``SEARCH``, so
    # the cases take that path instead; the counts did not move (44 passed,
    # 0 skipped, measured on a local ``neo4j:2026.10.2`` container on
    # 2026-11-18, not yet read off a live-infra run). The Neo4j graph row
    # below measured the same 168 / 1 there.
    "tests/unit/stores/contracts/test_neo4j_vector_contract.py": (44, 0),
    # Postgres graph: 169 passed, 0 skipped.
    "tests/unit/stores/contracts/test_postgres_graph_contract.py": (169, 0),
    # The two Bolt engines: 168 passed, 1 skipped on each. The skip is
    # ``test_create_node_if_absent_is_atomic_for_concurrent_writers``
    # (``ATOMIC_CREATE_IF_ABSENT = False``): a node's versions share one
    # ``node_id`` under SCD2, so no uniqueness constraint can arbitrate two
    # first creates that start together.
    "tests/unit/stores/contracts/test_arcadedb_graph_contract.py": (168, 1),
    "tests/unit/stores/contracts/test_neo4j_graph_contract.py": (168, 1),
    "tests/unit/stores/contracts/test_pgvector_contract.py": (44, 0),
    "tests/unit/stores/contracts/test_arcadedb_vector_contract.py": (44, 0),
    # The event log contract under a Los Angeles session: 59 passed, 0
    # skipped.
    "tests/unit/stores/contracts/test_postgres_event_log_session_zone_contract.py": (
        59,
        0,
    ),
}


def _dotted(path: str) -> str:
    """``tests/a/test_b.py`` -> ``tests.a.test_b``, as junit names it."""
    return ".".join(PurePosixPath(path).with_suffix("").parts)


def tally(root: ET.Element, path: str) -> tuple[int, int]:
    """(passed, skipped) for the test module at *path* in a junit tree.

    A case belongs to the module when its ``classname`` is the module or a
    class in it. A module skipped at collection has one case with an empty
    ``classname`` and the module as its ``name``, so ``name`` stands in. A
    ``<skipped>`` child (xfail is written that way too) is a skip; a case with
    no skipped, failure or error child passed.
    """
    module = _dotted(path)
    passed = skipped = 0
    for case in root.iter("testcase"):
        owner = case.get("classname") or case.get("name") or ""
        if owner != module and not owner.startswith(module + "."):
            continue
        if case.find("skipped") is not None:
            skipped += 1
        elif case.find("failure") is None and case.find("error") is None:
            passed += 1
    return passed, skipped


def check(root: ET.Element, floors: dict[str, tuple[int, int]]) -> list[str]:
    """Every floor or allowance *root* breaks, one line each."""
    problems = []
    for path, (fewest, most) in floors.items():
        passed, skipped = tally(root, path)
        if passed < fewest:
            problems.append(f"{path}: {passed} passed, below its floor of {fewest}")
        if skipped > most:
            problems.append(f"{path}: {skipped} skipped, over its allowance of {most}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python scripts/check_live_floor.py <junit.xml>", file=sys.stderr)
        return 2
    try:
        root = ET.parse(args[0]).getroot()  # noqa: S314 - our own CI's report
    except (OSError, ET.ParseError) as exc:
        print(f"cannot read {args[0]}: {exc}", file=sys.stderr)
        return 1
    for path in FLOORS:
        passed, skipped = tally(root, path)
        print(f"{path}: {passed} passed, {skipped} skipped")
    problems = check(root, FLOORS)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
