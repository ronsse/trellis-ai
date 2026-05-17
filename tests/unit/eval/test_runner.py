"""Tests for ``eval.runner`` CLI plumbing — specifically the
``--scenario-arg`` pass-through added by swarm-wave Unit A5.

The broader runner smoke tests live in ``test_runner_smoke.py``. This
file is scoped to the kwargs-forwarding flag: value coercion, name
validation, repeatability, and the end-to-end wiring through
``run_scenario`` / ``run_scenarios``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from eval import runner
from eval.runner import (
    ScenarioReport,
    _coerce_scenario_value,
    _parse_scenario_args,
    main,
    run_scenario,
)

# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def test_coerce_int() -> None:
    assert _coerce_scenario_value("42") == 42
    assert isinstance(_coerce_scenario_value("42"), int)


def test_coerce_negative_int() -> None:
    assert _coerce_scenario_value("-7") == -7
    assert isinstance(_coerce_scenario_value("-7"), int)


def test_coerce_float() -> None:
    val = _coerce_scenario_value("3.14")
    assert val == pytest.approx(3.14)
    assert isinstance(val, float)


def test_coerce_negative_float() -> None:
    val = _coerce_scenario_value("-0.5")
    assert val == pytest.approx(-0.5)
    assert isinstance(val, float)


def test_coerce_bool_true_lowercase() -> None:
    assert _coerce_scenario_value("true") is True


def test_coerce_bool_false_lowercase() -> None:
    assert _coerce_scenario_value("false") is False


def test_coerce_bool_mixed_case() -> None:
    """Bool parsing is case-insensitive per the spec."""
    assert _coerce_scenario_value("True") is True
    assert _coerce_scenario_value("FALSE") is False
    assert _coerce_scenario_value("TrUe") is True


def test_coerce_string_fallback() -> None:
    """Non-numeric, non-bool strings stay strings."""
    assert _coerce_scenario_value("real") == "real"
    assert _coerce_scenario_value("synthetic") == "synthetic"


def test_coerce_int_takes_precedence_over_float() -> None:
    """``"1"`` should be ``int(1)``, not ``float(1.0)`` — int probes first."""
    val = _coerce_scenario_value("1")
    assert val == 1
    assert isinstance(val, int)
    assert not isinstance(val, bool)


def test_coerce_empty_string_stays_string() -> None:
    """Empty value is a deliberate scenario-side choice, not an error."""
    assert _coerce_scenario_value("") == ""


def test_coerce_does_not_treat_yes_no_as_bool() -> None:
    """Only ``true`` / ``false`` are bool — ``yes`` / ``no`` stay strings."""
    assert _coerce_scenario_value("yes") == "yes"
    assert _coerce_scenario_value("no") == "no"


# ---------------------------------------------------------------------------
# Name / format validation
# ---------------------------------------------------------------------------


def test_parse_single_arg() -> None:
    assert _parse_scenario_args(["rounds=20"]) == {"rounds": 20}


def test_parse_multiple_flags() -> None:
    parsed = _parse_scenario_args(["profile=real", "rounds=20", "run_satellites=false"])
    assert parsed == {
        "profile": "real",
        "rounds": 20,
        "run_satellites": False,
    }


def test_parse_none_returns_empty_dict() -> None:
    assert _parse_scenario_args(None) == {}


def test_parse_empty_list_returns_empty_dict() -> None:
    assert _parse_scenario_args([]) == {}


def test_parse_value_with_equals_in_it() -> None:
    """``partition`` splits on the first ``=`` so values can contain ``=``."""
    parsed = _parse_scenario_args(["comment=a=b=c"])
    assert parsed == {"comment": "a=b=c"}


def test_parse_missing_equals_raises_value_error() -> None:
    with pytest.raises(ValueError, match="missing '=' separator"):
        _parse_scenario_args(["profile"])


def test_parse_empty_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-empty Python identifier"):
        _parse_scenario_args(["=value"])


def test_parse_non_identifier_name_raises_value_error() -> None:
    """Dashes are not valid Python identifiers; reject loudly."""
    with pytest.raises(ValueError, match="non-empty Python identifier"):
        _parse_scenario_args(["bad-name=value"])


def test_parse_numeric_name_raises_value_error() -> None:
    """Names starting with a digit are not identifiers."""
    with pytest.raises(ValueError, match="non-empty Python identifier"):
        _parse_scenario_args(["1bad=value"])


def test_parse_repeated_name_last_wins() -> None:
    """argparse ``append`` preserves order; right-most occurrence wins."""
    parsed = _parse_scenario_args(["rounds=10", "rounds=20", "rounds=30"])
    assert parsed == {"rounds": 30}


# ---------------------------------------------------------------------------
# Wiring: run_scenario / run_scenarios forward kwargs
# ---------------------------------------------------------------------------


class _FakeScenarioModule:
    """Stand-in for an ``eval.scenarios.<name>.scenario`` module."""

    def __init__(self, run: Any) -> None:
        self.run = run


def test_run_scenario_forwards_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(registry: Any, **kwargs: Any) -> ScenarioReport:
        captured.update(kwargs)
        return ScenarioReport(name="probe", status="pass")

    monkeypatch.setattr(runner, "_load_scenario", lambda name: fake_run)
    registry = MagicMock()
    report = run_scenario("probe", registry, profile="real", rounds=20)

    assert report.status == "pass"
    assert captured == {"profile": "real", "rounds": 20}


def test_run_scenario_unknown_kwarg_becomes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``TypeError`` from a missing kwarg shows up as a fail finding."""

    def strict_run(registry: Any) -> ScenarioReport:
        return ScenarioReport(name="strict", status="pass")

    monkeypatch.setattr(runner, "_load_scenario", lambda name: strict_run)
    registry = MagicMock()
    report = run_scenario("strict", registry, bogus="x")

    assert report.status == "fail"
    assert any(
        "TypeError" in f.message or "unexpected keyword" in f.message
        for f in report.findings
    )


def test_main_forwards_scenario_arg_to_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """End-to-end: ``main`` parses ``--scenario-arg`` and the kwargs reach
    the scenario callable."""
    captured: dict[str, Any] = {}

    def fake_run(registry: Any, **kwargs: Any) -> ScenarioReport:
        captured.update(kwargs)
        return ScenarioReport(name="probe", status="pass")

    monkeypatch.setattr(runner, "_load_scenario", lambda name: fake_run)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))

    rc = main(
        [
            "--scenario",
            "probe",
            "--scenario-arg",
            "profile=real",
            "--scenario-arg",
            "rounds=20",
            "--scenario-arg",
            "run_satellites=false",
            "--no-write",
        ]
    )
    assert rc == 0
    assert captured == {
        "profile": "real",
        "rounds": 20,
        "run_satellites": False,
    }


def test_main_rejects_malformed_scenario_arg(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ``--scenario-arg`` should fail loudly via ``parser.error``
    (exit code 2), not silently coerce to a string-valued kwarg."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(SystemExit) as exc_info:
        main(["--scenario", "_example", "--scenario-arg", "no_equals_here"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "missing '=' separator" in err
