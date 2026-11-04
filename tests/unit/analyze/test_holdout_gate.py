"""The readout seal on the pack holdout's outcome figures (library level).

``TRELLIS_HOLDOUT_READOUT_AFTER`` keeps the outcome unread until the
pre-registered horizon. These cases pin the gate itself: unset is no gate, a
malformed value fails closed, the horizon is inclusive, and a refusal carries
the horizon and nothing the store holds. The per-surface cases are in
``tests/unit/cli/test_analyze_holdout_gate.py`` and
``tests/unit/api/test_analyze_holdout_gate.py``; the structural rule that no
surface skips the gate is ``test_holdout_gate_rule.py``. Every id the fixtures
write is synthetic.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.unit.analyze._holdout_fixture import DAYS, UNTIL, HoldoutLog
from tests.unit.analyze._operational_seed import seed_store
from trellis.analyze import holdout, holdout_gate
from trellis.analyze.holdout import (
    HoldoutOperationalReport,
    HoldoutReport,
    analyze_holdout,
    analyze_holdout_operational,
)
from trellis.analyze.holdout_gate import (
    HOLDOUT_READOUT_AFTER_ENV,
    OPERATIONAL_HINT,
    analyze_holdout_gated,
    readout_horizon,
    require_readout_open,
)
from trellis.errors import (
    ConfigError,
    HoldoutReadoutSealedError,
    PolicyViolationError,
)
from trellis.stores.sqlite.event_log import SQLiteEventLog

HORIZON = datetime(2031, 3, 11, tzinfo=UTC)
FAST = {"permutations": 99, "bootstraps": 50, "power_sims": 5}


@pytest.fixture
def log(tmp_path: Path):
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield HoldoutLog(event_log)
    event_log.close()


def _env(value: str | None) -> dict[str, str]:
    return {} if value is None else {HOLDOUT_READOUT_AFTER_ENV: value}


def test_unset_or_blank_seals_nothing() -> None:
    for value in (None, "", "   "):
        assert readout_horizon(_env(value)) is None
        require_readout_open(HORIZON - timedelta(days=3650), env=_env(value))


def test_a_read_before_the_horizon_is_refused_and_names_only_the_horizon() -> None:
    with pytest.raises(HoldoutReadoutSealedError) as raised:
        require_readout_open(
            HORIZON - timedelta(seconds=1), env=_env("2031-03-11T00:00:00Z")
        )

    exc = raised.value
    assert exc.horizon == HORIZON
    assert exc.message == (
        "The holdout outcome readout is sealed until 2031-03-11T00:00:00+00:00 "
        f"({HOLDOUT_READOUT_AFTER_ENV}). `{OPERATIONAL_HINT}` shows whether "
        "the experiment is running without reading an outcome."
    )
    assert exc.code == "HOLDOUT_READOUT_SEALED"
    assert isinstance(exc, PolicyViolationError)


def test_the_horizon_instant_itself_is_open() -> None:
    env = _env("2031-03-11")
    with pytest.raises(HoldoutReadoutSealedError):
        require_readout_open(HORIZON - timedelta(microseconds=1), env=env)
    require_readout_open(HORIZON, env=env)
    require_readout_open(HORIZON + timedelta(microseconds=1), env=env)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2031-03-11", datetime(2031, 3, 11, tzinfo=UTC)),
        ("2031-03-11T08:30:00", datetime(2031, 3, 11, 8, 30, tzinfo=UTC)),
        ("2031-03-11T08:30:00Z", datetime(2031, 3, 11, 8, 30, tzinfo=UTC)),
        ("  2031-03-11T08:30:00+00:00 ", datetime(2031, 3, 11, 8, 30, tzinfo=UTC)),
        ("2031-03-11T08:30:00-05:00", datetime(2031, 3, 11, 13, 30, tzinfo=UTC)),
    ],
)
def test_the_horizon_is_read_as_utc(raw: str, expected: datetime) -> None:
    assert readout_horizon(_env(raw)) == expected


def test_a_naive_now_is_read_as_utc_and_an_offset_now_is_converted() -> None:
    env = _env("2031-03-11T12:00:00Z")
    with pytest.raises(HoldoutReadoutSealedError):
        require_readout_open(datetime.fromisoformat("2031-03-11T11:59:00"), env=env)
    require_readout_open(datetime.fromisoformat("2031-03-11T12:00:00"), env=env)
    # 07:30-05:00 is 12:30Z: open, though the wall clock reads earlier.
    require_readout_open(
        datetime(2031, 3, 11, 7, 30, tzinfo=timezone(timedelta(hours=-5))), env=env
    )


@pytest.mark.parametrize(
    "raw", ["soon", "2026-13-45", "23/12/2026", "0", "9999-12-31T23:59:59-23:59"]
)
def test_a_malformed_horizon_fails_closed(raw: str) -> None:
    """Refuse the outcome rather than open it, however far ahead the clock is."""
    with pytest.raises(ConfigError) as raised:
        readout_horizon(_env(raw))
    assert raised.value.setting == HOLDOUT_READOUT_AFTER_ENV
    assert HOLDOUT_READOUT_AFTER_ENV in raised.value.message
    with pytest.raises(ConfigError):
        require_readout_open(datetime(9999, 12, 30, tzinfo=UTC), env=_env(raw))


def test_a_malformed_horizon_is_not_a_sealed_refusal() -> None:
    with pytest.raises(ConfigError) as raised:
        require_readout_open(env=_env("someday"))
    assert not isinstance(raised.value, HoldoutReadoutSealedError)
    assert "someday" in raised.value.message


def test_the_gate_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HOLDOUT_READOUT_AFTER_ENV, raising=False)
    require_readout_open(datetime(2000, 1, 1, tzinfo=UTC))
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "2031-03-11")
    with pytest.raises(HoldoutReadoutSealedError):
        require_readout_open(datetime(2031, 3, 10, tzinfo=UTC))


def test_the_gated_entry_reads_the_clock_and_keeps_the_signature(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    assert inspect.signature(analyze_holdout_gated) == inspect.signature(
        analyze_holdout
    )
    assert analyze_holdout_gated.__wrapped__ is analyze_holdout  # type: ignore[attr-defined]
    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, HORIZON.isoformat())
    seed_store(log)
    log.sweep()
    options = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}

    monkeypatch.setattr(holdout_gate, "_now", lambda: HORIZON - timedelta(seconds=1))
    with pytest.raises(HoldoutReadoutSealedError):
        analyze_holdout_gated(log.event_log, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(holdout_gate, "_now", lambda: HORIZON)
    opened = analyze_holdout_gated(log.event_log, **options)  # type: ignore[arg-type]
    assert isinstance(opened, HoldoutReport)


def test_a_refusal_computes_nothing_and_reads_no_row(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    """The seal comes before the function it guards: no read, no write, no figure."""
    seed_store(log)
    before = len(log.event_log.get_events(limit=100_000))
    calls: list[str] = []

    def spy(*args: object, **kwargs: object) -> None:
        calls.append("reached")

    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "2999-01-01")
    with pytest.raises(HoldoutReadoutSealedError) as raised:
        holdout_gate._sealed(spy)(log.event_log, days=DAYS, until=UNTIL)

    assert calls == []
    assert len(log.event_log.get_events(limit=100_000)) == before
    leftover = raised.value.message.replace("2999-01-01T00:00:00+00:00", "")
    assert not any(c.isdigit() for c in leftover), leftover


def test_the_operational_read_is_not_sealed(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    seed_store(log)
    options = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}
    unsealed = analyze_holdout_operational(log.event_log, **options)  # type: ignore[arg-type]

    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "2999-01-01")
    monkeypatch.setattr(holdout_gate, "_now", lambda: HORIZON - timedelta(days=3650))
    sealed_env = analyze_holdout_operational(log.event_log, **options)  # type: ignore[arg-type]

    assert isinstance(sealed_env, HoldoutOperationalReport)
    assert sealed_env.model_dump() == unsealed.model_dump()


def test_the_seal_changes_no_computed_figure(
    monkeypatch: pytest.MonkeyPatch, log: HoldoutLog
) -> None:
    """Past the horizon the gated entry returns exactly what the library does."""
    seed_store(log)
    log.sweep()
    options = {"days": DAYS, "until": UNTIL, "seed": 7, **FAST}
    direct = holdout.analyze_holdout(log.event_log, **options)  # type: ignore[arg-type]

    monkeypatch.setenv(HOLDOUT_READOUT_AFTER_ENV, "2026-01-01")
    gated = analyze_holdout_gated(log.event_log, **options)  # type: ignore[arg-type]

    assert gated.model_dump() == direct.model_dump()
