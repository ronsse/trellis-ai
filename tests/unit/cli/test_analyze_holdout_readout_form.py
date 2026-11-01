"""``trellis analyze holdout`` as the readout command: window, pooling, parity.

Each case runs one store state through both formats and compares exit codes.
Every id and commit the fixture writes is synthetic.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import MONDAY, UNTIL, HoldoutLog, provenance
from trellis.ops.serving_builds import build_label
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

#: The pinned readout, with the horizon and the rate of the live experiment's
#: shape, and a small simulation so the suite stays fast.
READOUT = [
    "analyze",
    "holdout",
    "--until",
    UNTIL.isoformat(),
    "--days",
    "60",
    "--rate",
    "0.2",
    "--by-commit",
    "--limit",
    "20000",
    "--outcome",
    "commits",
    "--permutations",
    "299",
    "--bootstraps",
    "100",
    "--power-sims",
    "20",
]
MARKUP_COMMIT = "[b]x:smile:y"
MARKUP_BUILD = build_label("0.0.0", MARKUP_COMMIT)
OTHER_COMMIT = "9f8e7d6c5b4a"


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def log(_temp_stores: StoreRegistry) -> HoldoutLog:
    return HoldoutLog(_temp_stores.operational.event_log)


def _run(*extra: str) -> tuple[Result, Result]:
    return (
        runner.invoke(app, [*READOUT, *extra]),
        runner.invoke(app, [*READOUT, *extra, "--format", "json"]),
    )


def _payload(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


def _flat(result: Result) -> str:
    return " ".join(plain(result.stdout).split())


def _seed(log: HoldoutLog) -> None:
    """A markup-named build with 5 withheld tasks (stands), another with 2 (pooled)."""
    layout = [(MARKUP_COMMIT, False, 6)] + [(MARKUP_COMMIT, True, 2)] * 5
    layout += [
        (OTHER_COMMIT, False, 9),
        (OTHER_COMMIT, True, 3),
        (OTHER_COMMIT, True, 1),
    ]
    for hours, (commit, withheld, commits) in enumerate(layout):
        log.task(
            start=MONDAY + timedelta(hours=hours),
            arms=[withheld],
            commits=commits,
            rate=0.2,
            pack_metadata=[provenance(commit)],
        )


def test_text_and_json_agree_on_the_table_and_both_label_it(log: HoldoutLog) -> None:
    _seed(log)
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    table = _payload(machine)["by_commit"]
    assert [row["build"] for row in table["rows"]] == [MARKUP_BUILD, "other"]
    standing, other = table["rows"]
    assert standing["difference"] == 4.0
    assert other["difference"] == pytest.approx(9.0 - 2.0)
    assert table["verdict"] == "descriptive, not decisive"
    flat = _flat(text)
    # The commit string reaches the screen as written: Rich would otherwise
    # delete the [b] tag and turn :smile: into an emoji.
    assert MARKUP_BUILD in flat
    assert "By serving build (descriptive, non-decisive, no inference)" in flat
    assert "served - withheld" in flat
    assert format(standing["difference"], ".3g") in flat
    assert format(other["difference"], ".3g") in flat
    assert "1 build(s) with fewer than 5 withheld tasks are pooled in 'other'" in flat


def test_the_pinned_command_parses_and_honours_its_limit(log: HoldoutLog) -> None:
    _seed(log)
    log.sweep()

    _, machine = _run()

    assert machine.exit_code == EXIT_OK
    scan = _payload(machine)["scan"]
    assert scan["truncated"] is False
    assert 20000 in {v for v in scan.values() if isinstance(v, int)}


def test_an_early_read_is_incomplete_in_both_formats_with_one_exit(
    log: HoldoutLog,
) -> None:
    _seed(log)
    log.sweep(at=MONDAY + timedelta(days=2))

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["settled_window"]["settled"] is False
    assert payload["settled_window"]["needs_sweep_after"] == UNTIL.isoformat()
    assert payload["inference"]["status"] == "window not settled"
    assert payload["inference"]["difference"] is None
    assert all(row["difference"] is None for row in payload["by_commit"]["rows"])
    flat = _flat(text)
    assert "INCOMPLETE READ: window not settled." in flat
    assert f"re-run once a completed sweep has reached {UNTIL.isoformat()}" in flat


def test_a_settled_read_is_reproduced_by_a_later_run(log: HoldoutLog) -> None:
    _seed(log)
    log.sweep()
    _, first = _run()
    log.sweep(at=UNTIL + timedelta(days=3))

    _, second = _run()

    one, two = _payload(first), _payload(second)
    assert one["settled_window"]["settled"] is True
    for key in ("inference", "by_commit", "funnel", "descriptive"):
        assert one[key] == two[key], key
    assert "window settled" in _flat(runner.invoke(app, [*READOUT]))


def test_the_operational_read_prints_none_of_the_readout_form(log: HoldoutLog) -> None:
    _seed(log)
    log.sweep()

    result = runner.invoke(
        app,
        [
            "analyze",
            "holdout",
            "--operational",
            "--until",
            UNTIL.isoformat(),
            "--days",
            "60",
            "--rate",
            "0.2",
            "--by-commit",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == EXIT_OK
    out = result.stdout
    assert "difference" not in out
    assert "outcome_mean" not in out
    assert "settled_window" not in out
