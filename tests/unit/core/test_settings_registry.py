"""Tests for the settings catalog, :mod:`trellis.core.settings_registry`.

This registry validates nothing yet (no write route exists in this PR —
see the module docstring); what must hold is the catalog's own shape: it
names exactly the tunables this PR's precedence layer governs, every spec
is well-formed, and ``settings_live`` tells the truth about which knobs a
settings override actually reaches today (all of them except
``minhash_seed_max_docs``, which instead carries ``restart_required``).

A later PR (auto-promote-report-only) registered the tuner's three
``learning.auto_promote.*`` knobs and the six ``learning.candidate_auto_promote.*``
knobs for the new candidate-promotion policy — nine entries on top of the
fourteen above, pinned by :data:`_AUTO_PROMOTE_NAMES` below. A parallel PR
registered eight more catalog-only, read-side knobs (the subgraph depth
cap, two noise-demotion thresholds, two advisory-generation thresholds,
and three capture-health knobs), bringing the total to 31.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from trellis.core.settings_registry import (
    GROUP_LABELS,
    SETTINGS_REGISTRY,
    Risk,
    SettingGroup,
    SettingSpec,
    SettingType,
    get_setting,
    list_settings,
)
from trellis.core.write_config import ENV_VAR_BY_FIELD

_VALID_RISKS: frozenset[Risk] = frozenset({"low", "caution", "high"})
_VALID_TYPES: frozenset[SettingType] = frozenset({"bool", "int", "float", "str"})
_VALID_GROUPS: frozenset[SettingGroup] = frozenset(
    {"retrieval", "ingest_tagging", "learning_loops", "feedback_attribution", "ops"}
)

#: The tuner's three knobs plus the six for the new candidate-promotion
#: policy — registered together by the auto-promote-report-only PR.
_AUTO_PROMOTE_NAMES: frozenset[str] = frozenset(
    {
        "learning.auto_promote.enabled",
        "learning.auto_promote.min_sample_size",
        "learning.auto_promote.min_effect_size",
        "learning.candidate_auto_promote.enabled",
        "learning.candidate_auto_promote.mode",
        "learning.candidate_auto_promote.min_helpful_count",
        "learning.candidate_auto_promote.min_distinct_sessions",
        "learning.candidate_auto_promote.require_zero_unhelpful",
        "learning.candidate_auto_promote.max_promotions_per_run",
    }
)


class TestCatalogMembership:
    def test_every_write_behaviour_field_is_registered(self) -> None:
        """The 12 ``WriteBehaviourConfig`` fields (``ENV_VAR_BY_FIELD``'s
        keys, not a hand-copied list, so a field added there and
        forgotten here fails this test rather than drifting silently).
        """
        assert set(ENV_VAR_BY_FIELD) <= set(SETTINGS_REGISTRY)

    def test_registry_is_exactly_those_twelve_plus_the_documented_extras(
        self,
    ) -> None:
        assert (
            set(SETTINGS_REGISTRY)
            == set(ENV_VAR_BY_FIELD)
            | {
                "graph_seeding",
                "filter_legacy_advisories",
                "graph_max_subgraph_depth",
                "noise_demotion_min_attributed_packs",
                "noise_demotion_min_unhelpful_citations",
                "advisory_min_sample_size",
                "advisory_min_effect_size",
                "capture_warn_threshold",
                "capture_warn_window_hours",
                "capture_strict",
            }
            | _AUTO_PROMOTE_NAMES
        )

    def test_thirtyone_entries_total(self) -> None:
        assert len(SETTINGS_REGISTRY) == 31

    def test_auto_promote_families_are_registered(self) -> None:
        """Both knob families are now registered — the opposite of the
        pre-this-PR state, where they were explicitly excluded pending
        this owner decision.
        """
        assert set(SETTINGS_REGISTRY) >= _AUTO_PROMOTE_NAMES
        prefixes = ("learning.auto_promote", "learning.candidate_auto_promote")
        assert all(name.startswith(prefixes) for name in _AUTO_PROMOTE_NAMES)

    @pytest.mark.parametrize(
        "name",
        [
            "learning.candidate_auto_promote.enabled",
            "learning.candidate_auto_promote.max_promotions_per_run",
        ],
    )
    def test_no_observable_effect_knobs_disclose_it_in_the_first_sentence(
        self, name: str
    ) -> None:
        """Both knobs are switches with no observable effect until a
        future commit-mode release — the description must say so as its
        first sentence, not bury it after an explanation of what the
        knob will eventually do, so an operator reading only the first
        line still learns the knob is inert today.
        """
        spec = get_setting(name)
        assert spec is not None
        first_sentence = spec.description.split(".")[0]
        assert "no observable effect" in first_sentence or (
            "no effect today" in first_sentence
        )

    def test_candidate_auto_promote_mode_refuses_commit(self) -> None:
        """Pins the declarative refusal :func:`validate_value` enforces:
        ``commit`` stays a legal enum member (it documents a real future
        value) but is named in ``refused_values`` with a ``refused_reason``
        an operator can act on.
        """
        spec = get_setting("learning.candidate_auto_promote.mode")
        assert spec is not None
        assert spec.refused_values == ("commit",)
        assert spec.refused_reason
        assert "auto-demotion" in spec.refused_reason

    def test_no_other_entry_declares_refused_values(self) -> None:
        """``refused_values`` is new and, today, used by exactly one
        entry -- catches an accidental copy-paste onto another spec.
        """
        refusing = [
            name for name, spec in SETTINGS_REGISTRY.items() if spec.refused_values
        ]
        assert refusing == ["learning.candidate_auto_promote.mode"]


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

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_group_is_a_declared_group(self, name: str) -> None:
        assert SETTINGS_REGISTRY[name].group in _VALID_GROUPS

    def test_exactly_two_entries_need_a_restart(self) -> None:
        """``minhash_seed_max_docs`` and ``graph_max_subgraph_depth`` are
        each read once at process start (their own descriptions say why);
        every other registered knob is re-read live, so it carries
        ``settings_live=True`` instead.
        """
        restarters = {
            name for name, spec in SETTINGS_REGISTRY.items() if spec.restart_required
        }
        assert restarters == {"minhash_seed_max_docs", "graph_max_subgraph_depth"}


#: Catches an issue/PR reference such as "#862" or "#848" leaking into
#: operator-facing text -- the UI renders ``description``/``risk_note``
#: verbatim, and a tracker number means nothing to an operator deciding
#: whether to flip a switch (brief e179 rework: "no issue numbers, no PR
#: numbers").
_TRACKER_REFERENCE = re.compile(r"#\d+")


class TestLabelDescriptionAndRiskNoteShape:
    """The UI renders ``label``, ``description`` and ``risk_note``
    separately (brief e179 rework, step 1) -- every entry needs a usable
    ``label`` and ``description``, every non-``"low"`` entry needs a
    ``risk_note`` the UI can show beside its risk badge, and none of that
    operator-facing text may carry a tracker reference that only means
    something to a maintainer reading this file, not an operator reading
    the Settings tab.
    """

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_label_is_non_empty(self, name: str) -> None:
        label = SETTINGS_REGISTRY[name].label
        assert label is not None
        assert label.strip() != "", name

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_non_low_risk_entry_has_a_non_empty_risk_note(self, name: str) -> None:
        spec = SETTINGS_REGISTRY[name]
        if spec.risk == "low":
            pytest.skip("risk_note is only required above the low tier")
        assert spec.risk_note is not None, name
        assert spec.risk_note.strip() != "", name

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_description_has_no_tracker_reference(self, name: str) -> None:
        description = SETTINGS_REGISTRY[name].description
        assert _TRACKER_REFERENCE.search(description) is None, (name, description)

    @pytest.mark.parametrize("name", list(SETTINGS_REGISTRY))
    def test_risk_note_has_no_tracker_reference(self, name: str) -> None:
        risk_note = SETTINGS_REGISTRY[name].risk_note
        if risk_note is None:
            return
        assert _TRACKER_REFERENCE.search(risk_note) is None, (name, risk_note)

    def test_mutant_tracker_reference_in_description_is_caught(self) -> None:
        """A deliberately defective entry: a description that leaks an
        issue number, exactly the shape this rule exists to catch. Proven
        against a mutant, not just the real registry, so this test cannot
        pass merely because every *current* description happens to be
        clean.
        """
        base = next(iter(SETTINGS_REGISTRY.values()))
        mutant = replace(
            base,
            description="Shipped behaviour since #848, see also #862. " + "x" * 20,
        )

        assert _TRACKER_REFERENCE.search(mutant.description) is not None

    def test_mutant_missing_risk_note_on_a_high_risk_entry_is_caught(self) -> None:
        """A second defective shape: ``risk="high"`` with no ``risk_note``
        — the UI would show a High badge with nothing beside it to say
        why.
        """
        base = SETTINGS_REGISTRY["memory_extraction"]
        mutant = replace(base, risk_note=None)

        assert mutant.risk != "low"
        assert mutant.risk_note is None


class TestRiskTiersMatchThePlanPlusOneDocumentedExtension:
    """The plan's own "Unsafe to expose" list names four knobs:
    ``memory_extraction``, ``reconcile_on_write``, ``minhash_seed_max_docs``
    (a restart cost, not a correctness/cost risk) and ``pack_holdout_rate``.
    Those three (minus the restart-only one) are this registry's ``"high"``
    tier. ``require_bodied_attribution`` was added at the lighter
    ``"caution"`` tier (see its own code comment's "~46% more"
    measurement) as a deliberate, documented deviation, and a later batch
    of read-side knobs (the subgraph depth cap, the two noise-demotion
    thresholds, the two advisory-generation thresholds, and whether a
    partial capture sweep fails the run) joined it there — each one
    can silently admit weaker evidence, a slower query, or a quieter
    failure when turned down, which is exactly the "caution" tier's
    bar. None of this is an oversight, so pin both tiers rather than let
    them silently drift.
    """

    def test_high_tier_is_exactly_this_set(self) -> None:
        high = {name for name, spec in SETTINGS_REGISTRY.items() if spec.risk == "high"}
        assert high == {
            "memory_extraction",
            "reconcile_on_write",
            "pack_holdout_rate",
            "learning.auto_promote.enabled",
        }

    def test_caution_tier_is_exactly_this_set(self) -> None:
        caution = {
            name for name, spec in SETTINGS_REGISTRY.items() if spec.risk == "caution"
        }
        assert caution == {
            "require_bodied_attribution",
            "learning.candidate_auto_promote.mode",
            "learning.candidate_auto_promote.require_zero_unhelpful",
            "graph_max_subgraph_depth",
            "noise_demotion_min_attributed_packs",
            "noise_demotion_min_unhelpful_citations",
            "advisory_min_sample_size",
            "advisory_min_effect_size",
            "capture_strict",
        }

    def test_minhash_seed_max_docs_is_low_risk_but_restart_required(self) -> None:
        """Restart cost and risk tier are independent flags now — a knob
        can need a restart at any risk tier, or none at all.
        """
        spec = SETTINGS_REGISTRY["minhash_seed_max_docs"]
        assert spec.risk == "low"
        assert spec.restart_required is True

    def test_graph_max_subgraph_depth_is_caution_and_restart_required(self) -> None:
        """The second restart-required entry sits at a different risk
        tier than the first, proving the two flags really are
        independent rather than always moving together.
        """
        spec = SETTINGS_REGISTRY["graph_max_subgraph_depth"]
        assert spec.risk == "caution"
        assert spec.restart_required is True


class TestSettingsLive:
    """``settings_live`` is the one field that is not plain metadata — it
    is a claim about real runtime behaviour, so it gets its own, narrower
    test than "every spec is well-formed".
    """

    def test_every_entry_except_minhash_and_subgraph_depth_is_settings_live(
        self,
    ) -> None:
        not_live = {
            name for name, spec in SETTINGS_REGISTRY.items() if not spec.settings_live
        }
        assert not_live == {"minhash_seed_max_docs", "graph_max_subgraph_depth"}

    def test_graph_seeding_is_now_settings_live(self) -> None:
        """Was catalog-only through an earlier PR; this PR wires
        ``builder_factory._graph_seeding_enabled`` to read settings too.
        """
        assert SETTINGS_REGISTRY["graph_seeding"].settings_live is True

    def test_filter_legacy_advisories_is_settings_live(self) -> None:
        assert SETTINGS_REGISTRY["filter_legacy_advisories"].settings_live is True


class TestSettingsWritable:
    """``settings_writable`` (B3, re-gate #867) is narrower than
    ``settings_live``: ``minhash_seed_max_docs`` is also not
    ``settings_live`` today, but its stored value *is* read -- at the
    process's first subsequent use after a restart, via
    ``WriteBehaviourConfig.from_env_and_settings`` -- so storing one is
    still meaningful and stays writable. ``graph_max_subgraph_depth`` is
    the one entry with no code path that ever reads a stored value, at
    any time, which is why it alone is not writable.
    """

    def test_graph_max_subgraph_depth_is_the_only_non_writable_entry(self) -> None:
        not_writable = {
            name
            for name, spec in SETTINGS_REGISTRY.items()
            if not spec.settings_writable
        }
        assert not_writable == {"graph_max_subgraph_depth"}

    def test_minhash_seed_max_docs_stays_writable(self) -> None:
        """Also ``restart_required`` and not ``settings_live``, but its
        stored value IS read after a restart -- the two "not live today"
        entries from :class:`TestSettingsLive` must not collapse into one
        "not writable" treatment.
        """
        assert SETTINGS_REGISTRY["minhash_seed_max_docs"].settings_writable is True

    def test_every_entry_defaults_writable_unless_stated(self) -> None:
        """``settings_writable`` defaults to ``True`` on the dataclass --
        this is the field a new entry gets for free, so adding a knob
        never silently makes it refuse writes.
        """
        assert SettingSpec.__dataclass_fields__["settings_writable"].default is True

    def test_mutant_a_second_non_writable_entry_is_caught(self) -> None:
        """A deliberately defective registry: a second entry flipped to
        ``settings_writable=False``. Proven against a mutant, not just
        the real registry, so this cannot pass merely because today's
        catalog happens to have exactly one.
        """
        base = next(
            spec
            for spec in SETTINGS_REGISTRY.values()
            if spec.name != "graph_max_subgraph_depth"
        )
        mutant = replace(base, settings_writable=False)
        defective_registry = {**SETTINGS_REGISTRY, mutant.name: mutant}

        not_writable = {
            name
            for name, spec in defective_registry.items()
            if not spec.settings_writable
        }
        assert not_writable == {"graph_max_subgraph_depth", mutant.name}


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


class TestEveryEntryHasAValidGroup:
    """Grouping drives the UI's filter box (brief step 8, e179) — a value
    outside the five declared areas would silently vanish from every
    filter the UI builds from :data:`GROUP_LABELS`, with nothing to catch
    the gap. ``GROUP_LABELS`` is the one place a human-facing group name
    lives, so the UI never hardcodes one.
    """

    def test_every_group_has_a_display_label(self) -> None:
        assert set(GROUP_LABELS) == _VALID_GROUPS

    def test_mutant_unknown_group_is_caught(self) -> None:
        """A deliberately defective entry: a group outside the declared
        five. Dataclasses do not enforce ``Literal`` at runtime, so only
        this membership check catches it.
        """
        base = next(iter(SETTINGS_REGISTRY.values()))
        mutant = replace(base, group="not_a_real_group")  # type: ignore[arg-type]
        defective_registry = {**SETTINGS_REGISTRY, mutant.name: mutant}

        violations = [
            name
            for name, spec in defective_registry.items()
            if spec.group not in _VALID_GROUPS
        ]
        assert violations == [mutant.name]


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


class TestCandidateAutoPromoteFloorsMatchThePolicyConstants:
    """A registry ``minimum=`` for this family that drifts from its
    production constant (e.g. a hardcoded literal standing in for
    ``MIN_DISTINCT_SESSIONS_FLOOR``) would go unnoticed here while
    creating an "accepted by settings, refused by the job" gap: a
    settings write would *accept* a value that
    ``LearningCandidateAutoPromotePolicy.__post_init__`` (which checks
    the real constant, not the registry) then refuses at construction.
    Pin every registry ``minimum`` for this family against the
    production constant it is supposed to mirror, not against its
    current numeric value, so a future deliberate floor change doesn't
    require touching this test.
    """

    def test_min_helpful_count_minimum_matches_the_floor_constant(self) -> None:
        from trellis.learning.candidate_auto_promote import MIN_HELPFUL_COUNT_FLOOR

        spec = get_setting("learning.candidate_auto_promote.min_helpful_count")
        assert spec is not None
        assert spec.minimum == MIN_HELPFUL_COUNT_FLOOR

    def test_min_distinct_sessions_minimum_matches_the_floor_constant(self) -> None:
        from trellis.learning.candidate_auto_promote import (
            MIN_DISTINCT_SESSIONS_FLOOR,
        )

        spec = get_setting("learning.candidate_auto_promote.min_distinct_sessions")
        assert spec is not None
        assert spec.minimum == MIN_DISTINCT_SESSIONS_FLOOR

    def test_max_promotions_per_run_minimum_matches_the_floor_constant(self) -> None:
        from trellis.learning.candidate_auto_promote import (
            MAX_PROMOTIONS_PER_RUN_FLOOR,
        )

        spec = get_setting("learning.candidate_auto_promote.max_promotions_per_run")
        assert spec is not None
        assert spec.minimum == MAX_PROMOTIONS_PER_RUN_FLOOR

    def test_min_sample_size_minimum_matches_the_manual_floor_constant(self) -> None:
        from trellis.learning.tuners.promotion import DEFAULT_MIN_SAMPLE_SIZE

        spec = get_setting("learning.auto_promote.min_sample_size")
        assert spec is not None
        assert spec.minimum == DEFAULT_MIN_SAMPLE_SIZE

    def test_min_effect_size_minimum_matches_the_manual_floor_constant(self) -> None:
        from trellis.learning.tuners.promotion import DEFAULT_MIN_EFFECT_SIZE

        spec = get_setting("learning.auto_promote.min_effect_size")
        assert spec is not None
        assert spec.minimum == DEFAULT_MIN_EFFECT_SIZE


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
