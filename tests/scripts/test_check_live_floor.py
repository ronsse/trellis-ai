"""Tests for ``scripts/check_live_floor.py``.

The script is the only thing between a cloud graph or vector contract that
stopped running and a green live-infra job, so a checker that cannot fail is
the defect it exists to catch, one level up. Each row below is a junit report the
script has to judge. The failing rows are the shapes a contract that stopped
running really leaves behind -- every case skipped, the module skipped at
collection, the cases missing altogether -- and each sits beside a row that
must pass, so a check that went blind in either direction fails here.

The two floored modules differ in floor, allowance and junit shape (bare
module functions against a test class), so no row passes by reading the
wrong module's counts. The last tests run against the real ``FLOORS`` rows: a
report in which one floored contract skipped every case must fail, for each
row, and a report at every row's floor must pass.

The ``scripts/`` directory has no ``__init__.py``, so the module is loaded
via ``importlib.util`` -- same pattern as ``test_check_tool_pins``.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_live_floor.py"
#: The contracts live-infra must never stop running. Pinned by name so that
#: deleting a ``FLOORS`` row fails here; a new row needs no edit.
NEO4J_VECTOR = "tests/unit/stores/contracts/test_neo4j_vector_contract.py"
CONTRACTS = (
    NEO4J_VECTOR,
    "tests/unit/stores/contracts/test_pgvector_contract.py",
    "tests/unit/stores/contracts/test_arcadedb_vector_contract.py",
    "tests/unit/stores/contracts/test_postgres_graph_contract.py",
    "tests/unit/stores/contracts/test_arcadedb_graph_contract.py",
    "tests/unit/stores/contracts/test_neo4j_graph_contract.py",
)

MODULE_A = "tests/unit/stores/test_a.py"
MODULE_B = "tests/integration/test_b.py"
FLOORS = {MODULE_A: (3, 1), MODULE_B: (2, 0)}

A = "tests.unit.stores.test_a"  # module-level functions: classname is the module
B = "tests.integration.test_b.TestB"  # a test class: classname is module.Class

_OUTCOME = {
    "passed": "",
    "skipped": '<skipped type="pytest.skip" message="gated">gated</skipped>',
    "xfailed": '<skipped type="pytest.xfail" message="known">known</skipped>',
    "failed": '<failure message="boom">boom</failure>',
    "errored": '<error message="boom">boom</error>',
}


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_live_floor", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        msg = f"could not load spec for {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_live_floor"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def floor() -> ModuleType:
    return _load_module()


def _cases(owner: str, **counts: int) -> list[str]:
    """``counts`` cases of each outcome, named uniquely, owned by ``owner``."""
    return [
        f'<testcase classname="{owner}" name="test_{outcome}_{i}" time="0.01">'
        f"{_OUTCOME[outcome]}</testcase>"
        for outcome, count in counts.items()
        for i in range(count)
    ]


def _collection_skip(dotted_module: str) -> str:
    """What pytest writes for a module skipped at collection time."""
    return (
        f'<testcase classname="" name="{dotted_module}" time="0.000">'
        f"{_OUTCOME['skipped']}</testcase>"
    )


def _report(cases: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest">{"".join(cases)}</testsuite></testsuites>'
    )


def _root(cases: list[str]) -> ET.Element:
    return ET.fromstring(_report(cases))  # noqa: S314 - a report this test wrote


HEALTHY_B = _cases(B, passed=2)

ROWS = [
    pytest.param(_cases(A, passed=3, skipped=1) + HEALTHY_B, [], id="at-floor"),
    pytest.param(
        _cases(A, passed=5) + _cases(B, passed=3), [], id="more-passes-fewer-skips"
    ),
    pytest.param(
        _cases(A, passed=2, skipped=1) + HEALTHY_B,
        [f"{MODULE_A}: 2 passed, below its floor of 3"],
        id="one-below-floor",
    ),
    pytest.param(
        HEALTHY_B,
        [f"{MODULE_A}: 0 passed, below its floor of 3"],
        id="module-absent",
    ),
    pytest.param(
        _cases(A, skipped=4) + HEALTHY_B,
        [
            f"{MODULE_A}: 0 passed, below its floor of 3",
            f"{MODULE_A}: 4 skipped, over its allowance of 1",
        ],
        id="every-case-skipped",
    ),
    pytest.param(
        [*_cases(A, passed=3, skipped=1), _collection_skip("tests.integration.test_b")],
        [
            f"{MODULE_B}: 0 passed, below its floor of 2",
            f"{MODULE_B}: 1 skipped, over its allowance of 0",
        ],
        id="skipped-at-collection",
    ),
    pytest.param(
        _cases(A, passed=3, skipped=1, xfailed=1) + HEALTHY_B,
        [f"{MODULE_A}: 2 skipped, over its allowance of 1"],
        id="xfail-is-a-skip",
    ),
    pytest.param(
        _cases(A, passed=2, skipped=1)
        + _cases("tests.unit.stores.test_a_other", passed=3, skipped=4)
        + HEALTHY_B,
        [f"{MODULE_A}: 2 passed, below its floor of 3"],
        id="sibling-module-not-counted",
    ),
    pytest.param(
        _cases(A, passed=2, skipped=1, failed=1, errored=1) + HEALTHY_B,
        [f"{MODULE_A}: 2 passed, below its floor of 3"],
        id="failed-and-errored-are-not-passes",
    ),
]


@pytest.mark.parametrize(("cases", "expected"), ROWS)
def test_check_judges_each_report(
    floor: ModuleType, cases: list[str], expected: list[str]
) -> None:
    assert floor.check(_root(cases), FLOORS) == expected


def test_main_passes_a_report_at_its_floors(
    floor: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(floor, "FLOORS", FLOORS)
    report = tmp_path / "junit.xml"
    report.write_text(_report(_cases(A, passed=3, skipped=1) + HEALTHY_B))

    assert floor.main([str(report)]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        f"{MODULE_A}: 3 passed, 1 skipped",
        f"{MODULE_B}: 2 passed, 0 skipped",
    ]
    assert err == ""


def test_main_fails_a_report_below_a_floor(
    floor: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(floor, "FLOORS", FLOORS)
    report = tmp_path / "junit.xml"
    report.write_text(_report(_cases(A, skipped=4) + HEALTHY_B))

    assert floor.main([str(report)]) == 1
    _, err = capsys.readouterr()
    assert err.splitlines() == [
        f"{MODULE_A}: 0 passed, below its floor of 3",
        f"{MODULE_A}: 4 skipped, over its allowance of 1",
    ]


@pytest.mark.parametrize("content", [None, "<testsuites><testsuite>"])
def test_main_fails_a_report_it_cannot_read(
    floor: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
) -> None:
    report = tmp_path / "junit.xml"
    if content is not None:
        report.write_text(content)

    assert floor.main([str(report)]) == 1
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["one.xml", "two.xml"]])
def test_main_refuses_anything_but_one_report(
    floor: ModuleType, argv: list[str]
) -> None:
    assert floor.main(argv) == 2


@pytest.mark.parametrize("contract", CONTRACTS)
def test_the_contract_is_floored(floor: ModuleType, contract: str) -> None:
    assert contract in floor.FLOORS
    fewest, _ = floor.FLOORS[contract]

    assert (REPO_ROOT / contract).is_file()
    # A floor of zero passes a contract that ran nothing, which is the whole
    # defect. The exact numbers are hand-read off a green run (the comments on
    # FLOORS) and are not pinned here, where they would only be restated.
    assert fewest >= 1


@pytest.mark.parametrize("contract", [c for c in CONTRACTS if c != NEO4J_VECTOR])
def test_a_contract_with_no_capability_gate_may_skip_nothing(
    floor: ModuleType, contract: str
) -> None:
    """Only the Neo4j vector contract gates cases by name (``SEARCH``).

    A wider allowance on any other row lets that many cases skip unseen, so
    it stays zero until a case is gated in the same diff that raises it.
    """
    _, most = floor.FLOORS[contract]

    assert most == 0


def _dotted(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def _real_cases(
    floors: dict[str, tuple[int, int]],
    *,
    all_skipped: str | None = None,
    collection_skipped: str | None = None,
) -> list[str]:
    """A report with every floored contract exactly at its floor.

    ``all_skipped`` names one contract whose every case skipped instead, the
    shape an unset env toggle leaves; ``collection_skipped`` one replaced by
    a single case skipped at collection, as an import guard leaves it.
    """
    cases: list[str] = []
    for path, (fewest, most) in floors.items():
        owner = _dotted(path) + ".TestContract"
        if path == all_skipped:
            cases += _cases(owner, skipped=fewest + most)
        elif path == collection_skipped:
            cases.append(_collection_skip(_dotted(path)))
        else:
            cases += _cases(owner, passed=fewest, skipped=most)
    return cases


def test_real_floors_pass_a_report_at_their_floors(floor: ModuleType) -> None:
    assert floor.check(_root(_real_cases(floor.FLOORS)), floor.FLOORS) == []


@pytest.mark.parametrize("contract", CONTRACTS)
def test_real_floors_fail_a_contract_that_skipped_every_case(
    floor: ModuleType, contract: str
) -> None:
    fewest, most = floor.FLOORS[contract]
    root = _root(_real_cases(floor.FLOORS, all_skipped=contract))

    # Only the skipped contract is named, so a row cannot pass by reading a
    # sibling's counts, and a zero-skip allowance cannot hide the skips.
    assert floor.check(root, floor.FLOORS) == [
        f"{contract}: 0 passed, below its floor of {fewest}",
        f"{contract}: {fewest + most} skipped, over its allowance of {most}",
    ]


@pytest.mark.parametrize("contract", CONTRACTS)
def test_real_floors_fail_a_contract_skipped_at_collection(
    floor: ModuleType, contract: str
) -> None:
    fewest, most = floor.FLOORS[contract]
    root = _root(_real_cases(floor.FLOORS, collection_skipped=contract))

    expected = [f"{contract}: 0 passed, below its floor of {fewest}"]
    if most < 1:  # the one skip is within a wider allowance
        expected.append(f"{contract}: 1 skipped, over its allowance of {most}")
    assert floor.check(root, floor.FLOORS) == expected
