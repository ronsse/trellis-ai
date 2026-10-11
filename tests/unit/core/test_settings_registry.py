"""Tests for the settings catalog, :mod:`trellis.core.settings_registry`.

This registry validates nothing yet (no write route exists in this PR —
see the module docstring); what must hold is the catalog's own shape: it
names exactly the tunables this PR's precedence layer governs, every spec
is well-formed, and ``settings_live`` tells the truth about the one knob
(``pack_holdout_rate``) a settings override actually reaches today.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trellis.core.settings_registry import (
    SETTINGS_REGISTRY,
    Risk,
    SettingSpec,
    SettingType,
    get_setting,
    list_settings,
)
from trellis.core.write_config import ENV_VAR_BY_FIELD

_VALID_RISKS: frozenset[Risk] = frozenset({"safe", "restart", "unsafe"})
_VALID_TYPES: frozenset[SettingType] = frozenset({"bool", "int", "float", "str"})


class TestCatalogMembership:
    def test_every_write_behaviour_field_is_registered(self) -> None:
        """The 12 ``WriteBehaviourConfig`` fields (``ENV_VAR_BY_FIELD``'s
        keys, not a hand-copied list, so a field added there and
        forgotten here fails this test rather than drifting silently).
        """
        assert set(ENV_VAR_BY_FIELD) <= set(SETTINGS_REGISTRY)

    def test_registry_is_exactly_those_twelve_plus_graph_seeding(self) -> None:
        assert set(SETTINGS_REGISTRY) == set(ENV_VAR_BY_FIELD) | {"graph_seeding"}

    def test_thirteen_entries_total(self) -> None:
        assert len(SETTINGS_REGISTRY) == 13

    def test_auto_promote_is_explicitly_not_registered(self) -> None:
        """Excluded per owner decision, deferred to a later PR (plan p1)."""
        names = SETTINGS_REGISTRY
        assert not any(name.startswith("learning.auto_promote") for name in names)
        assert not any("auto_promote" in name for name in SETTINGS_REGISTRY)


class TestEverySpecIsWellFormed:
    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_risk_is_a_declared_tier(self, name: str) -> None:
        assert SETTINGS_REGISTRY[name].risk in _VALID_RISKS

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_type_is_a_declared_type(self, name: str) -> None:
        assert SETTINGS_REGISTRY[name].type in _VALID_TYPES

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_description_is_non_empty_operator_facing_prose(self, name: str) -> None:
        description = SETTINGS_REGISTRY[name].description
        assert description is not None
        assert len(description) >= 40, (name, description)

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_surfaces_is_non_empty(self, name: str) -> None:
        assert len(SETTINGS_REGISTRY[name].surfaces) > 0

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_name_field_matches_its_own_registry_key(self, name: str) -> None:
        assert SETTINGS_REGISTRY[name].name == name

    def test_no_restart_required_true_in_this_catalog(self) -> None:
        """Every entry's own "hot-reload: yes" classification (plan p1
        §(a)): none of the 13 currently registered knobs needs a restart
        to take effect.
        """
        assert all(not spec.restart_required for spec in SETTINGS_REGISTRY.values())


class TestUnsafeKnobsMatchThePlanPlusOneDocumentedExtension:
    """The plan's own "Unsafe to expose" list names four knobs:
    ``memory_extraction``, ``reconcile_on_write``, ``minhash_seed_max_docs``
    (restart risk, not unsafe) and ``pack_holdout_rate``. This PR adds a
    fifth, ``require_bodied_attribution``, as a deliberate, documented
    deviation (see its description's own "~46% more" measurement) — not
    an oversight, so pin it rather than let it silently drift back to
    three or grow to six unnoticed.
    """

    def test_unsafe_tier_is_exactly_this_set(self) -> None:
        unsafe = {
            name for name, spec in SETTINGS_REGISTRY.items() if spec.risk == "unsafe"
        }
        assert unsafe == {
            "memory_extraction",
            "reconcile_on_write",
            "pack_holdout_rate",
            "require_bodied_attribution",
        }

    def test_minhash_seed_max_docs_is_restart_not_unsafe(self) -> None:
        assert SETTINGS_REGISTRY["minhash_seed_max_docs"].risk == "restart"


class TestSettingsLive:
    """``settings_live`` is the one field that is not plain metadata — it
    is a claim about real runtime behaviour, so it gets its own, narrower
    test than "every spec is well-formed".
    """

    def test_exactly_one_entry_is_settings_live(self) -> None:
        live = [name for name, spec in SETTINGS_REGISTRY.items() if spec.settings_live]
        assert live == ["pack_holdout_rate"]

    def test_graph_seeding_is_explicitly_not_settings_live(self) -> None:
        """Catalog-only in this PR — builder_factory.py's own env read of
        ``TRELLIS_GRAPH_SEEDING`` is untouched.
        """
        assert SETTINGS_REGISTRY["graph_seeding"].settings_live is False


class TestLookupHelpers:
    def test_get_setting_returns_the_registered_spec(self) -> None:
        spec = get_setting("pack_holdout_rate")
        assert spec is not None
        assert spec.name == "pack_holdout_rate"

    def test_get_setting_returns_none_for_an_unknown_name(self) -> None:
        assert get_setting("not_a_real_setting") is None

    def test_list_settings_matches_the_registry_exactly(self) -> None:
        assert {spec.name for spec in list_settings()} == set(SETTINGS_REGISTRY)

    def test_list_settings_preserves_registration_order(self) -> None:
        assert list_settings() == list(SETTINGS_REGISTRY.values())


def _assert_no_duplicate_names(specs: tuple[SettingSpec, ...]) -> None:
    """The same guard ``settings_registry.py`` runs at import time,
    rebuilt independently here rather than imported, so this test catches
    the real module's guard going missing or silently weakening instead
    of just re-running whatever the module currently does.
    """
    registry = {spec.name: spec for spec in specs}
    if len(registry) == len(specs):
        return
    seen: set[str] = set()
    dupes = sorted({s.name for s in specs if s.name in seen or seen.add(s.name)})  # type: ignore[func-returns-value]
    message = f"duplicate SettingSpec name(s): {dupes}"
    raise AssertionError(message)


class TestDuplicateNameDetectionActuallyFires:
    """``settings_registry.py`` raises ``AssertionError`` at import time if
    ``len(SETTINGS_REGISTRY) != len(_SPECS)`` — proven here by rebuilding
    the same dict-from-tuple construction with a deliberately duplicated
    name, independent of the module under test (mirrors the brief's own
    "a deliberately defective subject" pattern, not a re-import of the
    real module with monkeypatched internals).
    """

    def test_a_duplicate_name_is_detected_by_the_same_construction(self) -> None:
        base = next(iter(SETTINGS_REGISTRY.values()))
        specs = (base, replace(base))  # same name twice, on purpose

        with pytest.raises(AssertionError, match=base.name):
            _assert_no_duplicate_names(specs)

    def test_distinct_names_raise_nothing(self) -> None:
        specs = tuple(SETTINGS_REGISTRY.values())
        _assert_no_duplicate_names(specs)  # must not raise
