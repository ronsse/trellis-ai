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
