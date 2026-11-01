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
    BEHAVIOUR_FLAT,
    BEHAVIOUR_VARIANTS,
    CHANGED_TASKS,
    MONDAY,
    REFUSAL_KEYS,
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


def test_an_early_read_is_refused_in_both_formats_with_one_exit(
    log: HoldoutLog,
) -> None:
    _seed(log)
    log.sweep(at=MONDAY + timedelta(days=2))

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert set(payload) == REFUSAL_KEYS
    assert payload["status"] == "window not settled"
    assert payload["settled_window"]["settled"] is False
    assert payload["settled_window"]["needs_sweep_after"] == UNTIL.isoformat()
    flat = _flat(text)
    assert "INCOMPLETE READ: window not settled." in flat
    assert f"re-run once a completed sweep has reached {UNTIL.isoformat()}" in flat


def test_by_commit_on_an_unsettled_read_prints_the_refusal_and_no_table(
    log: HoldoutLog,
) -> None:
    """``--by-commit`` is in the pinned command, and a refusal has no table."""
    _seed(log)
    log.sweep(at=MONDAY + timedelta(days=2))

    text, machine = _run()

    assert "by_commit" not in _payload(machine)
    flat = _flat(text)
    assert "By serving build" not in flat
    assert "--operational" in flat
    assert "None" not in flat


def test_an_unsettled_window_still_gives_per_commit_uptake_operationally(
    log: HoldoutLog,
) -> None:
    """The count of tasks per build and arm needs no settled window."""
    _seed(log)
    log.sweep(at=MONDAY + timedelta(days=2))

    argv = [
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
    ]
    text = runner.invoke(app, argv)
    machine = runner.invoke(app, [*argv, "--format", "json"])

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["mode"] == "operational"
    assert "settled_window" not in payload
    assert payload["status"] == "ok"
    rows = payload["by_commit"]["rows"]
    assert {row["build"] for row in rows} >= {build_label("0.0.0", OTHER_COMMIT)}
    assert sum(row["served"] + row["withheld"] for row in rows) == 9
    # Each task is counted under the build stamped on its first pack.
    assert "first pack" in payload["by_commit"]["note"]
    assert "By serving build (eligible task counts only)" in _flat(text)


def test_a_read_with_no_until_says_in_text_that_the_window_was_not_checked(
    log: HoldoutLog,
) -> None:
    _seed(log)
    log.sweep()
    argv = [
        "analyze",
        "holdout",
        "--days",
        "400",
        "--rate",
        "0.2",
        "--limit",
        "20000",
        "--permutations",
        "299",
        "--bootstraps",
        "100",
        "--power-sims",
        "20",
    ]

    text = runner.invoke(app, argv)
    machine = runner.invoke(app, [*argv, "--format", "json"])

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert "window not checked" in _flat(text)
    window = _payload(machine)["settled_window"]
    assert window["checked"] is False
    assert window["settled"] is None
    assert "inference" in _payload(machine)


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
# An unsettled read is a refusal the store cannot move
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


def _read_behaviour(
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


@pytest.mark.parametrize("variant", sorted(BEHAVIOUR_VARIANTS))
@pytest.mark.parametrize(
    ("outcome", "itt"), [("log1p_turns", False), ("commits", True)]
)
def test_an_unsettled_read_prints_the_same_whatever_the_store_holds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
    outcome: str,
    itt: bool,
) -> None:
    """Two stores that differ in what the sessions did print the same read.

    The whole payload and the whole text are compared, as the whole of what the
    command prints is what must not move. The pairs differ in the withheld
    arm's values, cut-offs and missing outcomes, its pack calls and join times,
    the timing's tool-call and hook-message counts, and an outcome block.
    """
    flat_json, flat_text = _read_behaviour(
        monkeypatch,
        tmp_path / "flat",
        seed=BEHAVIOUR_FLAT,
        settled=False,
        outcome=outcome,
        itt=itt,
    )
    changed_json, changed_text = _read_behaviour(
        monkeypatch,
        tmp_path / "changed",
        seed=BEHAVIOUR_VARIANTS[variant],
        settled=False,
        outcome=outcome,
        itt=itt,
    )

    assert set(flat_json) == REFUSAL_KEYS
    assert "INCOMPLETE READ: window not settled." in flat_text
    assert flat_json == changed_json
    assert flat_text == changed_text


@pytest.mark.parametrize("variant", sorted(set(BEHAVIOUR_VARIANTS) - {"join_time"}))
def test_the_same_pairs_do_differ_when_the_window_is_settled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str
) -> None:
    """The comparison above is not vacuous: settled, the same stores differ."""
    flat_json, flat_text = _read_behaviour(
        monkeypatch,
        tmp_path / "flat",
        seed=BEHAVIOUR_FLAT,
        settled=True,
        outcome="commits",
    )
    changed_json, changed_text = _read_behaviour(
        monkeypatch,
        tmp_path / "changed",
        seed=BEHAVIOUR_VARIANTS[variant],
        settled=True,
        outcome="commits",
    )

    assert flat_json["settled_window"]["settled"] is True
    assert "funnel" in flat_json
    assert flat_json != changed_json
    assert flat_text != changed_text
    if variant == "cut_offs":
        assert changed_json["funnel"]["cut_offs_excluded"] == CHANGED_TASKS
    if variant == "missing":
        assert changed_json["funnel"]["outcome_missing"] == CHANGED_TASKS


def _fresh_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> StoreRegistry:
    """A second, empty store the CLI reads next; the autouse one is left alone."""
    data_dir = tmp_path / name / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


def _swapped(argv: list[str], option: str, value: str | None) -> list[str]:
    """``argv`` with ``option``'s value replaced, or the option dropped."""
    index = argv.index(option)
    head, tail = argv[:index], argv[index + 2 :]
    return head + ([] if value is None else [option, value]) + tail


def test_the_rate_error_of_a_checked_read_is_the_same_whatever_the_pack_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _swapped(READOUT, "--rate", None)
    seen: dict[str, list[str]] = {"text": [], "json": []}
    for name, calls in (("one", 1), ("three", 3)):
        registry = _fresh_store(monkeypatch, tmp_path, name)
        log = HoldoutLog(registry.operational.event_log)
        seed_arm_shift(log, withheld_calls=calls)
        log.task(start=MONDAY + timedelta(hours=300), arms=[False], rate=0.5)
        log.sweep(at=UNTIL - timedelta(hours=1))
        text = runner.invoke(app, argv)
        machine = runner.invoke(app, [*argv, "--format", "json"])
        registry.close()
        assert text.exit_code == machine.exit_code
        assert machine.exit_code != EXIT_OK
        seen["text"].append(_flat(text))
        seen["json"].append(_payload(machine)["message"])

    assert seen["text"][0] == seen["text"][1]
    assert seen["json"][0] == seen["json"][1]
    assert "Several holdout rates" in seen["json"][0]
    assert "rows" not in seen["json"][0]
    assert "rows" not in seen["text"][0]


def test_a_truncated_checked_read_is_the_same_refusal_whatever_the_pack_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _swapped(READOUT, "--limit", "14")
    payloads: list[dict[str, Any]] = []
    texts: list[str] = []
    for name, calls in (("one", 1), ("three", 3)):
        registry = _fresh_store(monkeypatch, tmp_path, name)
        log = HoldoutLog(registry.operational.event_log)
        seed_arm_shift(log, withheld_calls=calls)
        log.sweep()
        text = runner.invoke(app, argv)
        machine = runner.invoke(app, [*argv, "--format", "json"])
        registry.close()
        assert text.exit_code == machine.exit_code == EXIT_OK
        payloads.append(_payload(machine))
        texts.append(_flat(text))

    assert set(payloads[0]) == REFUSAL_KEYS
    assert payloads[0]["settled_window"]["settled"] is False
    assert payloads[0]["settled_window"]["needs_sweep_after"] is None
    assert "--limit" in payloads[0]["settled_window"]["reason"]
    assert "INCOMPLETE READ" in texts[0]
    assert "re-run once" not in texts[0]
    drop = lambda p: {k: v for k, v in p.items() if k != "generated_at"}  # noqa: E731
    assert drop(payloads[0]) == drop(payloads[1])
    assert texts[0] == texts[1]
