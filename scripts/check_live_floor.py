"""Fail live-infra when the Neo4j vector contract ran less than it should.

live-infra writes a junit report on every run (``PYTEST_ADDOPTS`` on its test
step) and, until this script, read it only when the job had already failed. A
skip is not a failure, so a contract that skipped every case -- a skip mark, a
mistyped env var, a dropped ``neo4j`` extra, a roster widened to every case --
left the job green while the contract stopped running.

This reads that report and fails when a floored module passes fewer cases than
its floor, or skips more than its allowance. Both bounds leave room to
improve: a new case that passes, or a backend that stops needing a skip, never
turns the job red.

Run from the repository root::

    python scripts/check_live_floor.py ci-evidence/junit.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

#: Test module -> (fewest cases that must pass, most that may skip).
#:
#: Hand-read off live-infra run 36293450400 at b51896b3 (2026-09-27), from
#: that run's own per-test lines, never from this script: 25 passed, 15
#: skipped. The skips are ``SEARCH_ISSUING_TESTS``, which ``neo4j:2025.12``
#: cannot run. The four ``test_query_filter_key_is_one_flat_key_as_written``
#: cases joined that roster, and a run against this workflow's images counted
#: 25 passed, 19 skipped from its per-test lines. A change that removes
#: passing contract cases, or gates more of them behind the ``SEARCH``
#: capability, edits this row in the same diff.
FLOORS: dict[str, tuple[int, int]] = {
    "tests/unit/stores/contracts/test_neo4j_vector_contract.py": (25, 19),
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
