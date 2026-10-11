"""Tests for :func:`resolve_auto_promote_overrides`.

Precedence: env > settings > config.yaml > default.

Registers the three tuner knobs under ``learning.auto_promote.*`` in
``SETTINGS_REGISTRY`` (item 1 of the auto-promote-report-only plan). This
function resolves *precedence only*; the floor check itself lives in
:meth:`AutoPromotePolicy.__post_init__` and is exercised here by
constructing the policy from the resolved values, never duplicated.
"""

from __future__ import annotations

import pytest

from trellis.learning.tuners.auto_promote import (
    AUTO_PROMOTE_ENABLED_ENV,
    AUTO_PROMOTE_ENABLED_SETTING,
    AUTO_PROMOTE_MIN_EFFECT_SIZE_ENV,
    AUTO_PROMOTE_MIN_EFFECT_SIZE_SETTING,
    AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV,
    AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING,
    AutoPromotePolicy,
    resolve_auto_promote_overrides,
)


class TestPrecedenceOrder:
    def test_default_when_nothing_set(self) -> None:
        resolved = resolve_auto_promote_overrides(env={})
        assert resolved == {
            "enabled": False,
            "min_sample_size": 30,
            "min_effect_size": 0.25,
        }

    def test_config_yaml_overrides_default(self) -> None:
        resolved = resolve_auto_promote_overrides(
            config_defaults={
                "enabled": True,
                "min_sample_size": 40,
                "min_effect_size": 0.30,
            },
            env={},
        )
        assert resolved == {
            "enabled": True,
            "min_sample_size": 40,
            "min_effect_size": 0.30,
        }

    def test_settings_override_config_yaml(self) -> None:
        resolved = resolve_auto_promote_overrides(
            config_defaults={"min_sample_size": 40},
            settings={AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING: 50},
            env={},
        )
        assert resolved["min_sample_size"] == 50

    def test_env_overrides_settings_and_config_yaml(self) -> None:
        resolved = resolve_auto_promote_overrides(
            config_defaults={"min_sample_size": 40},
            settings={AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING: 50},
            env={AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV: "60"},
        )
        assert resolved["min_sample_size"] == 60

    def test_enabled_env_truthy_values(self) -> None:
        for raw in ("1", "true", "YES", "on"):
            resolved = resolve_auto_promote_overrides(
                env={AUTO_PROMOTE_ENABLED_ENV: raw}
            )
            assert resolved["enabled"] is True

    def test_enabled_settings_override(self) -> None:
        resolved = resolve_auto_promote_overrides(
            settings={AUTO_PROMOTE_ENABLED_SETTING: True}, env={}
        )
        assert resolved["enabled"] is True

    def test_min_effect_size_env_override(self) -> None:
        resolved = resolve_auto_promote_overrides(
            env={AUTO_PROMOTE_MIN_EFFECT_SIZE_ENV: "0.5"}
        )
        assert resolved["min_effect_size"] == 0.5


class TestFloorRefusalOnConstruction:
    """The resolver never validates; construction does. Both paths — a
    looser value arriving via settings, and via env — must still be
    refused once ``AutoPromotePolicy`` is built from the resolved dict."""

    def test_looser_min_sample_size_via_settings_is_refused(self) -> None:
        resolved = resolve_auto_promote_overrides(
            settings={AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING: 1},
            env={},
        )
        assert resolved["min_sample_size"] == 1
        with pytest.raises(ValueError, match="looser than the manual"):
            AutoPromotePolicy(
                min_sample_size=resolved["min_sample_size"],
                min_effect_size=resolved["min_effect_size"],
            )

    def test_looser_min_sample_size_via_env_is_refused(self) -> None:
        resolved = resolve_auto_promote_overrides(
            env={AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV: "1"},
        )
        assert resolved["min_sample_size"] == 1
        with pytest.raises(ValueError, match="looser than the manual"):
            AutoPromotePolicy(
                min_sample_size=resolved["min_sample_size"],
                min_effect_size=resolved["min_effect_size"],
            )

    def test_looser_min_effect_size_via_settings_is_refused(self) -> None:
        resolved = resolve_auto_promote_overrides(
            settings={AUTO_PROMOTE_MIN_EFFECT_SIZE_SETTING: 0.01},
            env={},
        )
        with pytest.raises(ValueError, match="looser than the manual"):
            AutoPromotePolicy(
                min_sample_size=resolved["min_sample_size"],
                min_effect_size=resolved["min_effect_size"],
            )

    def test_looser_min_effect_size_via_env_is_refused(self) -> None:
        resolved = resolve_auto_promote_overrides(
            env={AUTO_PROMOTE_MIN_EFFECT_SIZE_ENV: "0.01"},
        )
        with pytest.raises(ValueError, match="looser than the manual"):
            AutoPromotePolicy(
                min_sample_size=resolved["min_sample_size"],
                min_effect_size=resolved["min_effect_size"],
            )

    def test_floor_exactly_at_manual_default_is_accepted(self) -> None:
        """The manual defaults (5, 0.15) are the floor, not a forbidden
        value — resolving down to exactly that boundary still constructs."""
        resolved = resolve_auto_promote_overrides(
            settings={
                AUTO_PROMOTE_MIN_SAMPLE_SIZE_SETTING: 5,
                AUTO_PROMOTE_MIN_EFFECT_SIZE_SETTING: 0.15,
            },
            env={},
        )
        policy = AutoPromotePolicy(
            min_sample_size=resolved["min_sample_size"],
            min_effect_size=resolved["min_effect_size"],
        )
        assert policy.min_sample_size == 5
        assert policy.min_effect_size == 0.15


class TestNonFiniteEnvValuesRaiseCleanly:
    """``min_sample_size`` goes through ``int(float(raw))`` internally.

    ``int(float("nan"))`` raises ``ValueError`` on its own, but
    ``int(float("inf"))`` — and ``"1e999"``, which overflows ``float`` to
    ``inf`` — raises ``OverflowError`` instead. A caller (the settings
    route, the worker CLI) that only wraps ``except ValueError`` around
    this resolver would let that ``OverflowError`` fly uncaught: an API
    500 or a crashing CLI traceback instead of a clean refusal. Every
    non-finite spelling must come out as the same ``ValueError``.
    """

    @pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "1e999", "nan"])
    def test_min_sample_size_non_finite_env_raises_value_error(self, raw: str) -> None:
        with pytest.raises(ValueError, match="finite"):
            resolve_auto_promote_overrides(env={AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV: raw})

    def test_min_sample_size_non_finite_env_never_raises_overflow_error(
        self,
    ) -> None:
        # The specific regression: OverflowError must never escape this
        # function, because `except ValueError` at every real call site
        # (routes/settings.py, worker.py, admin_settings.py) does not
        # catch it.
        try:
            resolve_auto_promote_overrides(
                env={AUTO_PROMOTE_MIN_SAMPLE_SIZE_ENV: "inf"}
            )
        except OverflowError:
            pytest.fail("OverflowError escaped resolve_auto_promote_overrides")
        except ValueError:
            pass
