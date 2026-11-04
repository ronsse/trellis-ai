"""``trellis analyze holdout`` behind the readout seal.

``TRELLIS_HOLDOUT_READOUT_AFTER`` keeps the outcome unread until the horizon.

Each case runs one store state through both format arms and compares exit
codes, a dynamic witness beside ``tests/unit/test_format_exit_parity_rule.py``.
Time is frozen at the gate's own clock (``holdout_gate._now``). Every id the
fixtures write is synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import UNTIL, HoldoutLog, seed_arm_shift
from tests.unit.analyze._operational_seed import seed_store
from trellis.analyze import holdout_gate
from trellis.analyze.holdout_gate import HOLDOUT_READOUT_AFTER_ENV, OPERATIONAL_HINT
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_POLICY, EXIT_STORE
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

HORIZON = datetime(2031, 3, 11, tzinfo=UTC)
BEFORE = HORIZON - timedelta(seconds=1)
WINDOW = ["--until", UNTIL.isoformat(), "--days", "60", "--rate", "0.2"]
FAST = ["--permutations", "99", "--bootstraps", "50", "--power-sims", "5"]
REFUSAL_PAYLOAD_KEYS = {"status", "reason", "horizon", "message", "hint"}


@pytest.fixture(autouse=True)
def temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.delenv(HOLDOUT_READOUT_AFTER_ENV, raising=False)
    _reset_registry()
    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture
def log(temp_stores: StoreRegistry) -> HoldoutLog:
    held = HoldoutLog(temp_stores.operational.event_log)
    seed_arm_shift(held, shift=2)
    held.sweep()
    return held


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(holdout_gate, "_now", lambda: moment)


def _seal(monkeypatch: pytest.MonkeyPatch, *, now: datetime) -> None:
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, HORIZON.isoformat())
    _freeze(monkeypatch, now)


def _run(*args: str) -> tuple[Result, Result]:
    """The command in text and in JSON over the same store."""
    base = ["analyze", "holdout", *WINDOW, *FAST, *args]
    return runner.invoke(app, base), runner.invoke(app, [*base, "--format", "json"])


def _payload(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


def _flat(result: Result) -> str:
    return " ".join(plain(result.stdout).split())


def _event_count(registry: StoreRegistry) -> int:
    return len(registry.operational.event_log.get_events(limit=1_000_000))


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--by-commit"],
        ["--outcome", "commits"],
        ["--itt"],
        ["--limit", "20000"],
        ["--by-commit", "--outcome", "prs_created", "--itt"],
    ],
    ids=["plain", "by-commit", "outcome", "itt", "limit", "everything"],
)
def test_the_outcome_read_is_refused_before_the_horizon_and_exits_alike(
    monkeypatch: pytest.MonkeyPatch,
    log: HoldoutLog,
    temp_stores: StoreRegistry,
    extra: list[str],
) -> None:
    _seal(monkeypatch, now=BEFORE)
    events = _event_count(temp_stores)

    text, machine = _run(*extra)

    assert text.exit_code == machine.exit_code == EXIT_POLICY
    payload = _payload(machine)
    assert set(payload) == REFUSAL_PAYLOAD_KEYS
    assert payload["status"] == "refused"
    assert payload["reason"] == "outcome_sealed"
    assert payload["horizon"] == HORIZON.isoformat()
    assert payload["hint"] == OPERATIONAL_HINT
    assert HORIZON.isoformat() in payload["message"]
    assert _event_count(temp_stores) == events


def test_the_text_refusal_states_the_horizon_and_prints_no_figure(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    _seal(monkeypatch, now=BEFORE)

    text, _ = _run("--by-commit")

    flat = _flat(text)
    assert f"sealed until {HORIZON.isoformat()}" in flat
    assert "analyze holdout --operational" in flat
    leftover = flat.replace(HORIZON.isoformat(), "")
    for absent in ("Inference", "mean", "p-value", "95%", "tasks", "served", "arm"):
        assert absent not in leftover, absent
    assert not [c for c in leftover if c.isdigit()], leftover


def test_the_refusal_is_the_same_whatever_the_store_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: HoldoutLog
) -> None:
    _seal(monkeypatch, now=BEFORE)
    _, with_data = _run()

    empty_dir = tmp_path / "empty"
    (empty_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(empty_dir))
    _reset_registry()
    _, without_data = _run()

    assert with_data.exit_code == without_data.exit_code == EXIT_POLICY
    assert _payload(with_data) == _payload(without_data)


def test_the_operational_read_is_unaffected_before_the_horizon(
    monkeypatch: pytest.MonkeyPatch, temp_stores: StoreRegistry
) -> None:
    seed_store(HoldoutLog(temp_stores.operational.event_log))
    for extra in ([], ["--by-commit"]):
        unsealed = _run("--operational", *extra)
        _seal(monkeypatch, now=BEFORE)
        sealed = _run("--operational", *extra)
        monkeypatch.delenv(HOLDOUT_READOUT_AFTER_ENV)

        assert unsealed[0].exit_code == unsealed[1].exit_code == EXIT_OK
        assert sealed[0].exit_code == sealed[1].exit_code == EXIT_OK
        assert _payload(sealed[1]) == _payload(unsealed[1])
        assert _flat(sealed[0]) == _flat(unsealed[0])


def test_the_full_report_opens_at_the_horizon(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    _seal(monkeypatch, now=HORIZON)

    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["inference"]["status"] == "ok"
    assert "by_commit" in payload
    assert "Pack Holdout" in _flat(text)


def test_the_report_after_the_horizon_is_the_report_with_no_seal(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    _, plain_json = _run("--by-commit")
    assert plain_json.exit_code == EXIT_OK
    _seal(monkeypatch, now=HORIZON + timedelta(days=30))

    _, opened = _run("--by-commit")

    assert opened.exit_code == EXIT_OK
    assert _payload(opened) == _payload(plain_json)


def test_an_unset_horizon_is_no_gate(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    _freeze(monkeypatch, datetime(2000, 1, 1, tzinfo=UTC))
    for blank in (None, "", "  "):
        if blank is None:
            monkeypatch.delenv(HOLDOUT_READOUT_AFTER_ENV, raising=False)
        else:
            monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, blank)
        text, machine = _run()
        assert text.exit_code == machine.exit_code == EXIT_OK
        assert "inference" in _payload(machine)


def test_a_malformed_horizon_refuses_the_outcome_and_exits_alike(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "after the holidays")
    _freeze(monkeypatch, datetime(2099, 1, 1, tzinfo=UTC))

    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_STORE
    payload = _payload(machine)
    assert payload["setting"] == HOLDOUT_READOUT_AFTER_ENV
    assert "inference" not in payload
    assert "by_commit" not in payload
    assert HOLDOUT_READOUT_AFTER_ENV in _flat(text)
    assert "Inference" not in _flat(text)


def test_a_malformed_horizon_leaves_the_operational_read_open(
    monkeypatch: pytest.MonkeyPatch, temp_stores: StoreRegistry
) -> None:
    seed_store(HoldoutLog(temp_stores.operational.event_log))
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "after the holidays")

    text, machine = _run("--operational")

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert _payload(machine)["mode"] == "operational"
