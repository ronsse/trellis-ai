"""Tests for ``scripts/check_live_floor.py``.

The script is the only thing between a Neo4j vector contract that stopped
running and a green live-infra job, so a checker that cannot fail is the
defect it exists to catch, one level up. Each row below is a junit report the
script has to judge. The failing rows are the shapes a contract that stopped
running really leaves behind -- every case skipped, the module skipped at
collection, the cases missing altogether -- and each sits beside a row that
must pass, so a check that went blind in either direction fails here.

The two floored modules differ in floor, allowance and junit shape (bare
module functions against a test class), so no row passes by reading the
wrong module's counts.

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
CONTRACT = "tests/unit/stores/contracts/test_neo4j_vector_contract.py"

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


def test_the_contract_is_floored(floor: ModuleType) -> None:
    fewest, _ = floor.FLOORS[CONTRACT]

    assert (REPO_ROOT / CONTRACT).is_file()
    # A floor of zero passes a contract that ran nothing, which is the whole
    # defect. The exact numbers are hand-read off a green run (the comment on
    # FLOORS) and are not pinned here, where they would only be restated.
    assert fewest >= 1
