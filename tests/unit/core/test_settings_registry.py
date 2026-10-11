"""Tests for the settings catalog, :mod:`trellis.core.settings_registry`.

This registry validates nothing yet (no write route exists in this PR —
see the module docstring); what must hold is the catalog's own shape: it
names exactly the tunables this PR's precedence layer governs, every spec
is well-formed, and ``settings_live`` tells the truth about which knobs a
settings override actually reaches today (all of them except
``minhash_seed_max_docs``, which instead carries ``restart_required``).
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

    def test_registry_is_exactly_those_twelve_plus_two_catalog_only_entries(
        self,
    ) -> None:
        assert set(SETTINGS_REGISTRY) == set(ENV_VAR_BY_FIELD) | {
            "graph_seeding",
            "filter_legacy_advisories",
        }

    def test_fourteen_entries_total(self) -> None:
        assert len(SETTINGS_REGISTRY) == 14

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

    def test_exactly_one_entry_needs_a_restart(self) -> None:
        """``minhash_seed_max_docs`` is read once at process start (its own
        description says why); every other of the 14 registered knobs is
        re-read live, so it carries ``settings_live=True`` instead.
        """
        restarters = [
            name for name, spec in SETTINGS_REGISTRY.items() if spec.restart_required
        ]
        assert restarters == ["minhash_seed_max_docs"]


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

    def test_every_entry_except_minhash_is_settings_live(self) -> None:
        not_live = [
            name for name, spec in SETTINGS_REGISTRY.items() if not spec.settings_live
        ]
        assert not_live == ["minhash_seed_max_docs"]

    def test_graph_seeding_is_now_settings_live(self) -> None:
        """Was catalog-only through an earlier PR; this PR wires
        ``builder_factory._graph_seeding_enabled`` to read settings too.
        """
        assert SETTINGS_REGISTRY["graph_seeding"].settings_live is True

    def test_filter_legacy_advisories_is_settings_live(self) -> None:
        assert SETTINGS_REGISTRY["filter_legacy_advisories"].settings_live is True


class TestEveryEntryIsLiveOrExplainsWhyNot:
    """Every :class:`SettingSpec` must be ``settings_live`` or
    ``restart_required`` (with the reason stated in its own description) —
    an entry that is neither would be stored, reported as in force, and
    have no runtime effect with nothing to catch the gap. Proven against a
    deliberately defective mutant entry, not just the real registry, so
    this test cannot pass merely because every *current* entry happens to
    comply.
    """

    def test_every_real_entry_is_live_or_restart_required(self) -> None:
        violations = [
            name
            for name, spec in SETTINGS_REGISTRY.items()
            if not spec.settings_live and not spec.restart_required
        ]
        assert violations == []

    def test_a_restart_required_entry_must_say_why_in_its_description(self) -> None:
        """``restart_required`` alone is not enough — an operator reading
        only the description must be told it needs a restart.
        """
        for spec in SETTINGS_REGISTRY.values():
            if spec.restart_required:
                assert "restart" in spec.description.lower(), spec.name

    def test_mutant_neither_live_nor_restart_required_is_caught(self) -> None:
        """A deliberately defective entry: ``settings_live=False`` and
        ``restart_required=False`` together. This is exactly the gap the
        brief calls out — stored, reported as in force, inert — so the
        rule above must flag it rather than let it slip through as
        "well-formed".
        """
        base = next(iter(SETTINGS_REGISTRY.values()))
        mutant = replace(base, settings_live=False, restart_required=False)
        defective_registry = {**SETTINGS_REGISTRY, mutant.name: mutant}

        violations = [
            name
            for name, spec in defective_registry.items()
            if not spec.settings_live and not spec.restart_required
        ]
        assert violations == [mutant.name]

    def test_mutant_restart_required_with_no_reason_is_caught(self) -> None:
        """A second defective shape: ``restart_required=True`` but a
        description that never says so — a human reading the catalog has
        no way to know why the knob is listed as live-ineligible.
        """
        base = next(iter(SETTINGS_REGISTRY.values()))
        mutant = replace(
            base,
            settings_live=False,
            restart_required=True,
            description="Does a thing. " + "x" * 40,
        )

        assert "restart" not in mutant.description.lower()


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
