"""Tests for ``validate_value`` — the coerce-and-bounds-check half of the
settings registry that :class:`~trellis.mutate.handlers.SettingsUpdateHandler`
calls. Always raises ``ValueError`` on a bad input; never clamps.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trellis.core.settings_registry import (
    SETTINGS_REGISTRY,
    get_setting,
    validate_value,
)


class TestBool:
    @pytest.mark.parametrize("raw", [True, "true", "True", "1", "yes", "on"])
    def test_truthy_spellings_coerce_to_true(self, raw: object) -> None:
        spec = get_setting("memory_extraction")
        assert spec is not None
        assert validate_value(spec, raw) is True

    @pytest.mark.parametrize("raw", [False, "false", "0", "no", "off"])
    def test_falsy_spellings_coerce_to_false(self, raw: object) -> None:
        spec = get_setting("memory_extraction")
        assert spec is not None
        assert validate_value(spec, raw) is False

    def test_an_unrecognised_string_is_rejected(self) -> None:
        spec = get_setting("memory_extraction")
        assert spec is not None
        with pytest.raises(ValueError, match="boolean"):
            validate_value(spec, "maybe")

    def test_a_number_is_rejected_not_coerced_by_truthiness(self) -> None:
        spec = get_setting("memory_extraction")
        assert spec is not None
        with pytest.raises(ValueError):
            validate_value(spec, 1)


class TestInt:
    def test_an_int_in_range_passes(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        assert validate_value(spec, 500) == 500

    def test_a_numeric_string_coerces(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        assert validate_value(spec, "500") == 500

    def test_a_whole_float_coerces(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        assert validate_value(spec, 500.0) == 500

    def test_a_fractional_float_is_rejected(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        with pytest.raises(ValueError, match="integer"):
            validate_value(spec, 500.5)

    def test_below_minimum_is_rejected(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        with pytest.raises(ValueError, match="minhash_seed_max_docs"):
            validate_value(spec, -1)

    def test_a_bool_is_rejected_even_though_bool_is_an_int_subclass(self) -> None:
        spec = get_setting("minhash_seed_max_docs")
        assert spec is not None
        with pytest.raises(ValueError, match="boolean"):
            validate_value(spec, True)


class TestFloat:
    def test_in_range_passes(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        assert validate_value(spec, 0.25) == 0.25

    def test_numeric_string_coerces(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        assert validate_value(spec, "0.25") == 0.25

    def test_above_maximum_is_rejected(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        with pytest.raises(ValueError, match="pack_holdout_rate"):
            validate_value(spec, 1.5)

    def test_below_minimum_is_rejected(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        with pytest.raises(ValueError):
            validate_value(spec, -0.1)

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_is_rejected(self, raw: float) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        with pytest.raises(ValueError, match="finite"):
            validate_value(spec, raw)

    def test_unparseable_string_is_rejected(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        with pytest.raises(ValueError):
            validate_value(spec, "not-a-number")


class TestStr:
    def test_free_text_with_no_enum_is_accepted_verbatim(self) -> None:
        spec = get_setting("reconcile_model")
        assert spec is not None
        assert validate_value(spec, "qwen2.5:14b") == "qwen2.5:14b"

    def test_a_non_string_is_rejected(self) -> None:
        spec = get_setting("reconcile_model")
        assert spec is not None
        with pytest.raises(ValueError, match="string"):
            validate_value(spec, 5)

    def test_enum_restricts_to_declared_members(self) -> None:
        """No shipped entry declares an ``enum`` today, so this is proven
        against a synthetic spec built by ``replace`` on a real one — a
        deliberately defective subject, not a real registry entry.
        """
        base = get_setting("reconcile_model")
        assert base is not None
        spec = replace(base, enum=("a", "b"))

        assert validate_value(spec, "a") == "a"
        with pytest.raises(ValueError, match="one of"):
            validate_value(spec, "c")


class TestEveryRegistryEntryValidatesItsOwnDefaultOrDocumentedSentinel:
    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_default_round_trips_through_validate_value(self, name: str) -> None:
        spec = SETTINGS_REGISTRY[name]
        if spec.default is None:
            # trace_extraction_min_confidence's documented "unset" sentinel
            # — not a value validate_value is ever asked to accept; the
            # handler's null-means-reset path never reaches this function.
            pytest.skip("documented unset sentinel, not a settable value")
        assert validate_value(spec, spec.default) == spec.default


class TestRefusesWhatFromEnvWouldSilentlyDegrade:
    """Q5 (gate report on #857): ``validate_value`` must refuse every value
    ``WriteBehaviourConfig.from_env`` would silently replace with the
    shipped default, rather than storing something that later turns out to
    have no effect. See ``validate_value``'s own docstring and
    ``reconcile_timeout_s``'s ``minimum``/``minimum_exclusive`` in the
    registry.
    """

    @pytest.mark.parametrize("raw", [0, 0.0, -0.0, -1.0, -5])
    def test_reconcile_timeout_s_non_positive_is_rejected(self, raw: float) -> None:
        """``_reconcile_timeout`` degrades any ``<= 0`` env value (``-0.0``
        included, equal to ``0.0`` in Python) to the default rather than
        hanging or busy-looping on it; a settings write must refuse the
        same values instead of storing one ``from_env`` would discard.
        """
        spec = get_setting("reconcile_timeout_s")
        assert spec is not None
        with pytest.raises(ValueError, match="reconcile_timeout_s"):
            validate_value(spec, raw)

    def test_reconcile_timeout_s_positive_is_accepted(self) -> None:
        spec = get_setting("reconcile_timeout_s")
        assert spec is not None
        assert validate_value(spec, 5.0) == 5.0

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_blank_reconcile_model_is_rejected(self, raw: str) -> None:
        """``WriteBehaviourConfig.from_env`` treats a blank
        ``TRELLIS_RECONCILE_MODEL`` as absent and falls back to
        ``DEFAULT_RECONCILE_MODEL``; a settings write of the same blank
        value must be refused, not stored as a no-op override.
        """
        spec = get_setting("reconcile_model")
        assert spec is not None
        with pytest.raises(ValueError, match="blank"):
            validate_value(spec, raw)

    @pytest.mark.parametrize("name", ["pack_holdout_rate", "reconcile_timeout_s"])
    def test_overflowing_float_raises_value_error_not_overflow_error(
        self, name: str
    ) -> None:
        """``10**400`` is a Python ``int`` too large for ``float()``, which
        raises ``OverflowError`` -- a different exception class than the
        ``ValueError`` every other rejection in this module raises.
        ``validate_value``'s one contract is "always ``ValueError``, never
        anything else", via the shared ``parse_finite_float`` (see its
        docstring): this must not escape as an uncaught ``OverflowError``,
        which the handler's ``except ValueError`` would miss and let
        surface as an executor-level ``FAILED`` instead of a governed
        ``invalid_value`` rejection.
        """
        spec = get_setting(name)
        assert spec is not None
        with pytest.raises(ValueError, match="finite"):
            validate_value(spec, 10**400)
