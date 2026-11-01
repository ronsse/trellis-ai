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
from tests.unit.analyze._holdout_fixture import (
    ARM_VARIANTS,
    CHANGED_TASKS,
    MONDAY,
    UNTIL,
    HoldoutLog,
    provenance,
    seed_arm_shift,
)
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


# ---------------------------------------------------------------------------
# An unsettled read carries no figure computed from an outcome
# ---------------------------------------------------------------------------

#: The wall-clock stamps a read writes for itself: not a figure.
_CLOCK_KEYS = ("generated_at", "as_of")


def _without_clock(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_clock(v) for k, v in node.items() if k not in _CLOCK_KEYS}
    if isinstance(node, list):
        return [_without_clock(v) for v in node]
    return node


def _store_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> StoreRegistry:
    stores_dir = root / "data" / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(root / "data"))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _argv(outcome: str, *extra: str) -> list[str]:
    argv = list(READOUT)
    argv[argv.index("--outcome") + 1] = outcome
    return [*argv, *extra]


def _read_shifted(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    seed: dict[str, Any],
    settled: bool,
    outcome: str,
    itt: bool = False,
) -> tuple[Any, str]:
    registry = _store_at(monkeypatch, root)
    log = HoldoutLog(registry.operational.event_log)
    seed_arm_shift(log, **seed)
    if settled:
        log.sweep()
    else:
        log.sweep(at=UNTIL - timedelta(hours=1))
    extra = ["--itt"] if itt else []
    text = runner.invoke(app, _argv(outcome, *extra))
    machine = runner.invoke(app, _argv(outcome, *extra, "--format", "json"))
    assert text.exit_code == machine.exit_code
    return _without_clock(_payload(machine)), plain(text.stdout)


@pytest.mark.parametrize(
    ("variant", "outcome", "itt"),
    [
        ("values", "log1p_turns", False),
        ("values", "commits", False),
        ("values", "prs_created", False),
        ("cut_offs", "log1p_turns", False),
        ("cut_offs", "commits", True),
        ("missing", "log1p_turns", False),
        ("missing", "prs_created", True),
    ],
)
def test_an_unsettled_read_does_not_move_with_the_withheld_arms_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
    outcome: str,
    itt: bool,
) -> None:
    """Two stores that differ only in one arm's outcomes print the same read.

    The whole payload and the whole text are compared, as the whole of what
    the command prints is what must not carry an outcome; a list of withheld
    keys would only name the figures someone remembered. The outcome is the
    value read, whether the session ended on a usage limit, and whether a
    value exists at all, so a count of who was analysed moves with it.
    """
    flat_json, flat_text = _read_shifted(
        monkeypatch, tmp_path / "flat", seed={}, settled=False, outcome=outcome, itt=itt
    )
    changed_json, changed_text = _read_shifted(
        monkeypatch,
        tmp_path / "changed",
        seed=ARM_VARIANTS[variant],
        settled=False,
        outcome=outcome,
        itt=itt,
    )

    assert flat_json["settled_window"]["settled"] is False
    assert flat_json == changed_json
    assert flat_text == changed_text


@pytest.mark.parametrize("variant", sorted(ARM_VARIANTS))
def test_the_difference_does_move_a_settled_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str
) -> None:
    """The comparison above is not vacuous: settled, the same stores differ."""
    flat_json, flat_text = _read_shifted(
        monkeypatch, tmp_path / "flat", seed={}, settled=True, outcome="commits"
    )
    changed_json, changed_text = _read_shifted(
        monkeypatch,
        tmp_path / "changed",
        seed=ARM_VARIANTS[variant],
        settled=True,
        outcome="commits",
    )

    assert flat_json["settled_window"]["settled"] is True
    assert flat_json != changed_json
    assert flat_text != changed_text
    if variant == "cut_offs":
        assert changed_json["funnel"]["cut_offs_excluded"] == CHANGED_TASKS
    if variant == "missing":
        assert changed_json["funnel"]["outcome_missing"] == CHANGED_TASKS


def test_the_text_says_what_is_withheld_and_where_the_early_check_is(
    log: HoldoutLog,
) -> None:
    seed_arm_shift(log, shift=2)
    log.sweep(at=UNTIL - timedelta(hours=1))

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    flat = _flat(text)
    assert "No figure computed from an outcome is printed on this read" in flat
    assert "every count of who is analysed" in flat
    assert "--operational" in flat
    assert "cut-offs withheld" in flat
    # A count the read declines to state prints as the word, never as None.
    assert "None" not in flat
    assert (
        "cut-offs excluded withheld, outcome missing withheld, analysed withheld"
        in flat
    )
    assert "analysed withheld" in flat
