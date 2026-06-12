"""Tests for ``trellis worker`` — config plumbing for tier-1 auto-promotion.

The store-touching behaviour of ``worker tune`` is exercised end-to-end in
``tests/unit/learning/tuners/test_auto_promote.py`` (the library it calls).
These tests pin the CLI-side contract: the ``learning.auto_promote`` config
section parses correctly, is absent-safe (disabled default), rejects
malformed input loudly, and never weakens the gate below the manual floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from trellis_cli import worker
from trellis_cli.main import worker_app


def _write_config(config_dir: Path, body: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / worker.CONFIG_FILENAME).write_text(body, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# worker_app moved here from main; tune is its sole subcommand today.
# ---------------------------------------------------------------------------


def test_worker_app_exposes_tune() -> None:
    names = {
        cmd.name or cmd.callback.__name__ for cmd in worker_app.registered_commands
    }
    assert "tune" in names


def test_main_imports_worker_app_from_module() -> None:
    # worker_app on main is the same object defined in trellis_cli.worker.
    assert worker_app is worker.worker_app


# ---------------------------------------------------------------------------
# Config absent => disabled default (global default OFF).
# ---------------------------------------------------------------------------


def test_absent_config_yields_disabled_policy(config_dir: Path) -> None:
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is False
    # Still armed with monitoring, still stricter than manual.
    assert policy.post_promotion.auto_demote is True
    assert policy.min_sample_size >= 30


def test_section_absent_yields_disabled_policy(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  scoring:\n    foo: 1\n")
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is False


# ---------------------------------------------------------------------------
# Config present and well-formed.
# ---------------------------------------------------------------------------


def test_enabled_config_parses(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n"
        "  auto_promote:\n"
        "    enabled: true\n"
        "    min_sample_size: 50\n"
        "    min_effect_size: 0.30\n"
        "    post_min_samples: 40\n"
        "    post_regression_threshold: 0.15\n"
        "    post_lookback_days: 14\n",
    )
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is True
    assert policy.min_sample_size == 50
    assert policy.min_effect_size == 0.30
    assert policy.post_promotion.min_samples_post_promote == 40
    assert policy.post_promotion.regression_threshold == 0.15
    assert policy.post_promotion.lookback_window.days == 14
    assert policy.post_promotion.auto_demote is True


def test_partial_config_uses_defaults(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote:\n    enabled: true\n")
    policy = worker._build_auto_promote_policy()
    assert policy.enabled is True
    assert policy.min_sample_size == 30  # default
    assert policy.min_effect_size == 0.25  # default


# ---------------------------------------------------------------------------
# Loud on malformed input.
# ---------------------------------------------------------------------------


def test_unknown_key_rejected(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    enabled: true\n    bogus: 1\n",
    )
    with pytest.raises(typer.BadParameter, match="unknown key"):
        worker._build_auto_promote_policy()


def test_non_bool_enabled_rejected(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote:\n    enabled: yesplease\n")
    with pytest.raises(typer.BadParameter, match="true/false"):
        worker._build_auto_promote_policy()


def test_non_numeric_threshold_rejected(config_dir: Path) -> None:
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    min_effect_size: abc\n",
    )
    with pytest.raises(typer.BadParameter, match="not a number"):
        worker._build_auto_promote_policy()


def test_section_not_mapping_rejected(config_dir: Path) -> None:
    _write_config(config_dir, "learning:\n  auto_promote: 7\n")
    with pytest.raises(typer.BadParameter, match="must be a mapping"):
        worker._build_auto_promote_policy()


def test_looser_than_manual_rejected_via_exit(config_dir: Path) -> None:
    # min_sample_size below the manual floor (5) must be rejected — the
    # AutoPromotePolicy constructor raises ValueError, surfaced as Exit.
    _write_config(
        config_dir,
        "learning:\n  auto_promote:\n    enabled: true\n    min_sample_size: 2\n",
    )
    with pytest.raises(typer.Exit):
        worker._build_auto_promote_policy_or_exit()
