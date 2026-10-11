"""Tests for :mod:`trellis.core.write_config`.

Consolidating the write-behaviour knobs changed no observable behaviour:
same env var names, same defaults, same parsing quirks. These tests pin
that, and they pin that each legacy reader function — which deployments
and other modules still call — is still driven by exactly the variable it
was always driven by. The one deliberate difference, warning frequency for
a malformed confidence floor, is pinned too.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import structlog.testing

from trellis.classify.ingest import classify_on_ingest_enabled
from trellis.core import write_config
from trellis.core.write_config import (
    ENV_VAR_BY_FIELD,
    TRUTHY,
    WriteBehaviourConfig,
    resolve_overridden_by,
)
from trellis.extract.memory_ingest_hook import memory_extraction_env_enabled
from trellis.extract.trace_ingest_hook import (
    trace_extraction_enabled,
    trace_extraction_min_confidence,
)
from trellis.mcp.reconcile import (
    configured_model_id,
    reconcile_on_write_enabled,
    reconcile_timeout_seconds,
)
from trellis.retrieve.embed_ingest_hook import embed_on_ingest_enabled

#: Every environment variable this module owns.
ALL_ENV_VARS = sorted(ENV_VAR_BY_FIELD.values())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "nothing set" — the shipped default."""
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_empty_environment_yields_shipped_defaults(self) -> None:
        """The pre-consolidation defaults, restated once, on purpose."""
        assert WriteBehaviourConfig.from_env() == WriteBehaviourConfig(
            classify_on_ingest=False,
            embed_on_ingest=False,
            memory_extraction=False,
            reconcile_on_write=False,
            trace_extraction=False,
            trace_extraction_min_confidence=None,
            reconcile_model="hermes3:8b",
            reconcile_timeout_s=20.0,
        )

    def test_dataclass_defaults_match_empty_environment(self) -> None:
        """``WriteBehaviourConfig()`` is by construction the default config."""
        assert WriteBehaviourConfig.from_env() == WriteBehaviourConfig()

    def test_every_field_has_a_declared_env_var(self) -> None:
        """A new knob without an env var must not silently report nothing."""
        assert set(ENV_VAR_BY_FIELD) == set(WriteBehaviourConfig().as_dict())

    def test_blank_values_read_as_unset(self) -> None:
        """Whitespace-only is "not configured", exactly as before."""
        env = dict.fromkeys(ALL_ENV_VARS, "   ")
        assert WriteBehaviourConfig.from_env(env) == WriteBehaviourConfig()


#: The knobs that are plain on/off switches — **derived**, not listed.
#: A hand-written roster here silently stops covering a new flag, which
#: is how a roster rots (#443 declared three control keys against six
#: call sites). The derivation is the dataclass's own defaults, so a
#: boolean field that is not exercised below cannot exist.
BOOLEAN_FIELDS = tuple(
    name for name, value in WriteBehaviourConfig().as_dict().items() if value is False
)


class TestBooleanFlags:
    @pytest.mark.parametrize("field", BOOLEAN_FIELDS)
    @pytest.mark.parametrize("spelling", [*sorted(TRUTHY), "TRUE", "On", " 1 "])
    def test_truthy_spellings_enable(self, field: str, spelling: str) -> None:
        env = {ENV_VAR_BY_FIELD[field]: spelling}
        assert getattr(WriteBehaviourConfig.from_env(env), field) is True

    @pytest.mark.parametrize("field", BOOLEAN_FIELDS)
    @pytest.mark.parametrize("spelling", ["0", "false", "no", "off", "maybe", ""])
    def test_other_spellings_stay_off(self, field: str, spelling: str) -> None:
        env = {ENV_VAR_BY_FIELD[field]: spelling}
        assert getattr(WriteBehaviourConfig.from_env(env), field) is False

    @pytest.mark.parametrize("field", BOOLEAN_FIELDS)
    def test_each_flag_moves_only_its_own_field(self, field: str) -> None:
        """No knob may have a side effect on another knob."""
        config = WriteBehaviourConfig.from_env({ENV_VAR_BY_FIELD[field]: "1"})
        defaults = WriteBehaviourConfig()
        changed = {
            name
            for name, value in config.as_dict().items()
            if value != defaults.as_dict()[name]
        }
        assert changed == {field}


class TestBooleanRosterIsNotVacuous:
    """The derivation above divides by its own output; pin a floor.

    Every other guard in this file is satisfied by a roster that merely
    *shrinks*, so the count is the one thing the derivation cannot
    compute for itself.
    """

    def test_covers_the_known_switches(self) -> None:
        assert set(BOOLEAN_FIELDS) >= {
            "classify_on_ingest",
            "embed_on_ingest",
            "memory_extraction",
            "reconcile_on_write",
            "trace_extraction",
            "require_pack_attribution",
            "require_bodied_attribution",
        }

    def test_excludes_the_non_boolean_knobs(self) -> None:
        """``value is False`` must not sweep in ``0`` / ``0.0`` / ``""``."""
        assert "minhash_seed_max_docs" not in BOOLEAN_FIELDS
        assert "trace_extraction_min_confidence" not in BOOLEAN_FIELDS
        assert "reconcile_timeout_s" not in BOOLEAN_FIELDS
        assert "pack_holdout_rate" not in BOOLEAN_FIELDS


class TestBodiedAttributionFlag:
    """#550's capability ships **off**, and that is the decision.

    The measured ask is ~6 more verdicts per pack on top of the ~8.9 a
    grader already volunteers — a ~46% increase that exceeds the observed
    ceiling. The failure mode of asking too much of a grading surface is
    the surface going quiet, so the default is off and an operator turns
    it on against their own callers.
    """

    def test_ships_off(self) -> None:
        assert WriteBehaviourConfig().require_bodied_attribution is False
        assert WriteBehaviourConfig.from_env({}).require_bodied_attribution is False

    def test_is_independent_of_the_pack_attribution_gate(self) -> None:
        """Two requirements, two switches — neither implies the other.

        They ask different questions (*can this join at all?* against
        *is every body accounted for?*), so an operator must be able to
        run either alone.
        """
        bodied_only = WriteBehaviourConfig.from_env(
            {ENV_VAR_BY_FIELD["require_bodied_attribution"]: "1"}
        )
        assert bodied_only.require_bodied_attribution is True
        assert bodied_only.require_pack_attribution is False

        pack_only = WriteBehaviourConfig.from_env(
            {ENV_VAR_BY_FIELD["require_pack_attribution"]: "1"}
        )
        assert pack_only.require_pack_attribution is True
        assert pack_only.require_bodied_attribution is False

    def test_appears_in_the_operator_report(self) -> None:
        """``trellis admin write-config`` is how a host is compared."""
        config = WriteBehaviourConfig.from_env(
            {ENV_VAR_BY_FIELD["require_bodied_attribution"]: "1"}
        )
        row = next(
            entry
            for entry in config.describe()
            if entry["name"] == "require_bodied_attribution"
        )
        assert row["env_var"] == "TRELLIS_REQUIRE_BODIED_ATTRIBUTION"
        assert row["value"] is True
        assert row["default"] is False
        assert row["overridden"] is True


class TestConfidenceFloor:
    @pytest.mark.parametrize(("raw", "expected"), [("0.0", 0.0), ("0.75", 0.75)])
    def test_valid_values_parse(self, raw: str, expected: float) -> None:
        env = {ENV_VAR_BY_FIELD["trace_extraction_min_confidence"]: raw}
        assert WriteBehaviourConfig.from_env(env).trace_extraction_min_confidence == (
            expected
        )

    @pytest.mark.parametrize("raw", ["high", "", "  ", "1.5", "-0.1"])
    def test_unusable_values_degrade_to_no_gate(self, raw: str) -> None:
        """Never to ``0.0`` — that would silently drop every draft."""
        env = {ENV_VAR_BY_FIELD["trace_extraction_min_confidence"]: raw}
        assert WriteBehaviourConfig.from_env(env).trace_extraction_min_confidence is (
            None
        )

    def test_a_malformed_value_warns_once_not_once_per_read(self) -> None:
        """Every flag reader now builds the whole config.

        Before consolidation, only the trace-extraction batch parsed this
        knob. Warning per read would turn one typo into several log lines
        per ingested document, since classify/embed fire per document.
        """
        write_config._parse_min_confidence.cache_clear()
        env = {ENV_VAR_BY_FIELD["trace_extraction_min_confidence"]: "0.85f"}
        with structlog.testing.capture_logs() as logs:
            for _ in range(5):
                WriteBehaviourConfig.from_env(env)
        events = [entry["event"] for entry in logs]
        assert events == ["trace_extraction_min_confidence_unparseable"]


class TestReconcileKnobs:
    @pytest.mark.parametrize(("raw", "expected"), [("5", 5.0), ("0.5", 0.5)])
    def test_timeout_parses(self, raw: str, expected: float) -> None:
        env = {ENV_VAR_BY_FIELD["reconcile_timeout_s"]: raw}
        assert WriteBehaviourConfig.from_env(env).reconcile_timeout_s == expected

    @pytest.mark.parametrize("raw", ["abc", "0", "-3"])
    def test_unusable_timeout_falls_back_to_default(self, raw: str) -> None:
        env = {ENV_VAR_BY_FIELD["reconcile_timeout_s"]: raw}
        assert WriteBehaviourConfig.from_env(env).reconcile_timeout_s == 20.0

    def test_model_override(self) -> None:
        env = {ENV_VAR_BY_FIELD["reconcile_model"]: "qwen2.5:7b"}
        assert WriteBehaviourConfig.from_env(env).reconcile_model == "qwen2.5:7b"


class TestMinHashSeedBound:
    """``TRELLIS_MINHASH_SEED_MAX_DOCS`` — the one knob that is both a
    switch and a bound (#402).

    Seeding the MCP fuzzy-dedup index is O(corpus) and gates a *rejection*
    path, so the number an operator sets is the cost they are agreeing to.
    Splitting it into an enable flag plus a bound would admit a state that
    means nothing (enabled, seed zero rows) and would let the cost hide
    behind a word.
    """

    def test_default_is_seed_nothing(self) -> None:
        """The shipped posture: ``save_memory`` keeps comparing only
        against memories written by the same process, exactly as it does
        today with the broken ``search("")`` seed."""
        assert WriteBehaviourConfig.from_env().minhash_seed_max_docs == 0

    @pytest.mark.parametrize(
        ("raw", "expected"), [("1", 1), ("500", 500), (" 20 ", 20)]
    )
    def test_positive_values_parse(self, raw: str, expected: int) -> None:
        env = {ENV_VAR_BY_FIELD["minhash_seed_max_docs"]: raw}
        assert WriteBehaviourConfig.from_env(env).minhash_seed_max_docs == expected

    @pytest.mark.parametrize("raw", ["lots", "5.5", "", "  ", "-1", "0"])
    def test_unusable_values_degrade_to_seed_nothing(self, raw: str) -> None:
        """Never to "unbounded". A typo must not silently switch on a
        rejection path, and must not silently switch on an unbounded walk
        over an arbitrarily large corpus either."""
        env = {ENV_VAR_BY_FIELD["minhash_seed_max_docs"]: raw}
        assert WriteBehaviourConfig.from_env(env).minhash_seed_max_docs == 0

    def test_a_malformed_value_warns_once_not_once_per_read(self) -> None:
        """Same reason as the confidence floor: every flag reader builds
        the whole config, so an uncached warning would fire on reads that
        have nothing to do with this knob."""
        write_config._parse_seed_max_docs.cache_clear()
        env = {ENV_VAR_BY_FIELD["minhash_seed_max_docs"]: "five hundred"}
        with structlog.testing.capture_logs() as logs:
            for _ in range(5):
                WriteBehaviourConfig.from_env(env)
        assert [entry["event"] for entry in logs] == [
            "minhash_seed_max_docs_unparseable"
        ]

    def test_a_negative_value_warns_about_being_negative(self) -> None:
        write_config._parse_seed_max_docs.cache_clear()
        env = {ENV_VAR_BY_FIELD["minhash_seed_max_docs"]: "-7"}
        with structlog.testing.capture_logs() as logs:
            WriteBehaviourConfig.from_env(env)
        assert [entry["event"] for entry in logs] == ["minhash_seed_max_docs_negative"]

    def test_it_moves_only_its_own_field(self) -> None:
        config = WriteBehaviourConfig.from_env(
            {ENV_VAR_BY_FIELD["minhash_seed_max_docs"]: "100"}
        )
        defaults = WriteBehaviourConfig().as_dict()
        changed = {
            name for name, value in config.as_dict().items() if value != defaults[name]
        }
        assert changed == {"minhash_seed_max_docs"}


#: Spelled out rather than read from ``ENV_VAR_BY_FIELD``: the name is set
#: in deployed wrappers, so it is part of the contract this file pins.
PACK_HOLDOUT_RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"


class TestPackHoldoutRate:
    """``TRELLIS_PACK_HOLDOUT_RATE`` — the share of packs withheld whole.

    A read-side knob living here on purpose: every event carries this
    module's values in ``write_provenance.env_flags``, so the rate in force
    is recorded on every row without a second stamp. A value outside
    ``[0, 1]`` is refused the way the other numeric knobs refuse one — the
    shipped default (withhold nothing) stands and one warning says why.
    """

    def test_default_withholds_nothing(self) -> None:
        assert WriteBehaviourConfig.from_env().pack_holdout_rate == 0.0
        assert WriteBehaviourConfig().pack_holdout_rate == 0.0

    def test_the_env_var_is_the_declared_one(self) -> None:
        assert ENV_VAR_BY_FIELD["pack_holdout_rate"] == PACK_HOLDOUT_RATE_ENV

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", 0.0),
            ("0.1", 0.1),
            (" 0.5 ", 0.5),
            ("1", 1.0),
            ("1.0", 1.0),
            ("2.5e-1", 0.25),
        ],
    )
    def test_rates_in_the_unit_interval_parse(self, raw: str, expected: float) -> None:
        env = {PACK_HOLDOUT_RATE_ENV: raw}
        assert WriteBehaviourConfig.from_env(env).pack_holdout_rate == expected

    @pytest.mark.parametrize(
        "raw",
        ["-0.1", "-1", "1.5", "2", "lots", "10%", "nan", "inf", "-inf", "", "  "],
    )
    def test_unusable_values_degrade_to_withhold_nothing(self, raw: str) -> None:
        """Never to "withhold everything": a typo must not blank every pack."""
        env = {PACK_HOLDOUT_RATE_ENV: raw}
        assert WriteBehaviourConfig.from_env(env).pack_holdout_rate == 0.0

    @pytest.mark.parametrize("raw", ["-0", "-0.0", " -0e3 "])
    def test_negative_zero_parses_to_zero(self, raw: str) -> None:
        """Not to ``-0.0``: equal to ``0.0``, but recorded with its sign.

        ``env_flags`` and every ``PACK_ASSEMBLED`` row would carry ``-0.0``.
        """
        rate = WriteBehaviourConfig.from_env({PACK_HOLDOUT_RATE_ENV: raw}).as_dict()[
            "pack_holdout_rate"
        ]
        assert (rate, math.copysign(1.0, rate)) == (0.0, 1.0)

    def test_a_malformed_value_warns_once_not_once_per_read(self) -> None:
        write_config._parse_pack_holdout_rate.cache_clear()
        env = {PACK_HOLDOUT_RATE_ENV: "a tenth"}
        with structlog.testing.capture_logs() as logs:
            for _ in range(5):
                WriteBehaviourConfig.from_env(env)
        assert [entry["event"] for entry in logs] == ["pack_holdout_rate_unparseable"]

    @pytest.mark.parametrize("raw", ["-0.25", "1.01", "nan"])
    def test_an_out_of_range_value_warns_about_its_range(self, raw: str) -> None:
        write_config._parse_pack_holdout_rate.cache_clear()
        env = {PACK_HOLDOUT_RATE_ENV: raw}
        with structlog.testing.capture_logs() as logs:
            WriteBehaviourConfig.from_env(env)
        assert [entry["event"] for entry in logs] == ["pack_holdout_rate_out_of_range"]

    def test_it_moves_only_its_own_field(self) -> None:
        config = WriteBehaviourConfig.from_env({PACK_HOLDOUT_RATE_ENV: "0.2"})
        defaults = WriteBehaviourConfig().as_dict()
        changed = {
            name for name, value in config.as_dict().items() if value != defaults[name]
        }
        assert changed == {"pack_holdout_rate"}

    def test_it_reaches_env_flags_and_the_operator_report(self) -> None:
        config = WriteBehaviourConfig.from_env({PACK_HOLDOUT_RATE_ENV: "0.2"})
        assert config.as_dict()["pack_holdout_rate"] == 0.2
        row = next(r for r in config.describe() if r["name"] == "pack_holdout_rate")
        assert row == {
            "name": "pack_holdout_rate",
            "env_var": PACK_HOLDOUT_RATE_ENV,
            "value": 0.2,
            "default": 0.0,
            "overridden": True,
            "overridden_by": "env",
        }


class TestDescribe:
    def test_reports_every_knob_with_its_env_var(self) -> None:
        rows = WriteBehaviourConfig.from_env().describe()
        assert [row["env_var"] for row in rows] == [
            ENV_VAR_BY_FIELD[row["name"]] for row in rows
        ]
        assert sorted(row["env_var"] for row in rows) == ALL_ENV_VARS

    def test_defaults_are_not_flagged_as_overridden(self) -> None:
        rows = WriteBehaviourConfig.from_env().describe()
        assert not any(row["overridden"] for row in rows)

    def test_overrides_are_flagged(self) -> None:
        env = {ENV_VAR_BY_FIELD["embed_on_ingest"]: "1"}
        rows = WriteBehaviourConfig.from_env(env).describe()
        overridden = {row["name"] for row in rows if row["overridden"]}
        assert overridden == {"embed_on_ingest"}

    def test_describe_falls_back_to_env_or_default_when_overridden_by_is_omitted(
        self,
    ) -> None:
        """A caller that never calls ``from_env_and_settings`` / passes no
        provenance map still gets a legible ``overridden_by`` — it can
        only ever be "env" or "default" from that caller's point of view,
        exactly :meth:`WriteBehaviourConfig.describe`'s own fallback rule.
        """
        env = {ENV_VAR_BY_FIELD["embed_on_ingest"]: "1"}
        rows = WriteBehaviourConfig.from_env(env).describe()
        by_name = {row["name"]: row["overridden_by"] for row in rows}
        assert by_name["embed_on_ingest"] == "env"
        assert by_name["trace_extraction"] == "default"


class TestEnvSettingsDefaultPrecedence:
    """PR 2's own failing-before tests: env > settings > default, and
    :func:`resolve_overridden_by` must agree with what
    :meth:`WriteBehaviourConfig.from_env_and_settings` actually resolved —
    the two are asked separately (see ``resolve_overridden_by``'s own
    docstring for why) and could in principle disagree.
    """

    def test_env_and_settings_both_set_env_wins(self) -> None:
        env = {ENV_VAR_BY_FIELD["pack_holdout_rate"]: "0.1"}
        settings = {"pack_holdout_rate": 0.9}
        config = WriteBehaviourConfig.from_env_and_settings(settings=settings, env=env)
        assert config.pack_holdout_rate == 0.1

        provenance = resolve_overridden_by(settings=settings, env=env)
        assert provenance["pack_holdout_rate"] == "env"
        rows = config.describe(overridden_by=provenance)
        (row,) = [r for r in rows if r["name"] == "pack_holdout_rate"]
        assert row["overridden_by"] == "env"
        assert row["value"] == 0.1

    def test_settings_set_no_env_settings_wins(self) -> None:
        settings = {"pack_holdout_rate": 0.4}
        config = WriteBehaviourConfig.from_env_and_settings(settings=settings, env={})
        assert config.pack_holdout_rate == 0.4

        provenance = resolve_overridden_by(settings=settings, env={})
        assert provenance["pack_holdout_rate"] == "settings"
        rows = config.describe(overridden_by=provenance)
        (row,) = [r for r in rows if r["name"] == "pack_holdout_rate"]
        assert row["overridden_by"] == "settings"
        assert row["value"] == 0.4
        assert row["overridden"] is True

    def test_neither_set_default_wins(self) -> None:
        config = WriteBehaviourConfig.from_env_and_settings(settings={}, env={})
        assert config.pack_holdout_rate == 0.0

        provenance = resolve_overridden_by(settings={}, env={})
        assert provenance["pack_holdout_rate"] == "default"
        rows = config.describe(overridden_by=provenance)
        (row,) = [r for r in rows if r["name"] == "pack_holdout_rate"]
        assert row["overridden_by"] == "default"
        assert row["overridden"] is False

    def test_a_blank_env_var_does_not_count_as_env_set(self) -> None:
        """An env var present but empty is "unset", matching ``from_env``'s
        own blank-and-unset-both-mean-off parsing — so a settings value
        still wins underneath it.
        """
        env = {ENV_VAR_BY_FIELD["pack_holdout_rate"]: "   "}
        settings = {"pack_holdout_rate": 0.4}
        config = WriteBehaviourConfig.from_env_and_settings(settings=settings, env=env)
        assert config.pack_holdout_rate == 0.4
        provenance = resolve_overridden_by(settings=settings, env=env)
        assert provenance["pack_holdout_rate"] == "settings"

    def test_a_bool_settings_value_renders_correctly_both_ways(self) -> None:
        """Every other precedence test here uses ``pack_holdout_rate``
        (a float) — eleven of the twelve ``WriteBehaviourConfig`` fields
        are bool, and ``_settings_as_env_strings`` renders a bool
        differently (``"true"``/``"false"``) than a float (``str(value)``),
        so a float-only test suite cannot see a swapped bool rendering.
        Pin both a ``True`` and a ``False`` settings override through to
        the resolved config, not just one.
        """
        true_config = WriteBehaviourConfig.from_env_and_settings(
            settings={"memory_extraction": True}, env={}
        )
        assert true_config.memory_extraction is True

        false_config = WriteBehaviourConfig.from_env_and_settings(
            settings={"memory_extraction": False}, env={}
        )
        assert false_config.memory_extraction is False

        provenance = resolve_overridden_by(settings={"memory_extraction": True}, env={})
        assert provenance["memory_extraction"] == "settings"

    def test_resolve_overridden_by_covers_every_write_behaviour_field(self) -> None:
        provenance = resolve_overridden_by(settings={}, env={})
        assert set(provenance) == set(ENV_VAR_BY_FIELD)
        assert set(provenance.values()) <= {"env", "settings", "default"}

    def test_an_out_of_range_settings_value_degrades_like_a_bad_env_value(self) -> None:
        """A settings override is rendered into the same raw-string shape
        ``from_env`` parses, so an out-of-range value hits the exact same
        clamp — not a second, divergent validation rule.
        """
        config = WriteBehaviourConfig.from_env_and_settings(
            settings={"pack_holdout_rate": 2.0}, env={}
        )
        # Clamped to the default, same as from_env.
        assert config.pack_holdout_rate == 0.0

    def test_a_degraded_settings_file_falls_back_and_says_so(
        self, tmp_path: Path
    ) -> None:
        """``load_settings_values`` has no channel to report degradation
        (its own docstring) — a caller that must surface it reads
        ``SettingsStore.is_degraded`` directly. Pin both halves: the
        precedence layer still falls back cleanly (env, then default) off
        a degraded read, and the degradation itself is visible via the
        store, not merely absorbed as "no overrides".
        """
        from trellis.stores.settings_store import SettingsStore

        path = tmp_path / "settings.json"
        path.write_text("{ not json", encoding="utf-8")
        store = SettingsStore(path)
        assert store.is_degraded is True  # visible, not swallowed

        # Falls back to default with no env set at all.
        config = WriteBehaviourConfig.from_env_and_settings(
            settings=store.as_values(), env={}
        )
        assert config.pack_holdout_rate == 0.0
        empty_env_provenance = resolve_overridden_by(settings=store.as_values(), env={})
        assert empty_env_provenance["pack_holdout_rate"] == "default"

        # Falls back to env, not stuck, when an env var is also set.
        env = {ENV_VAR_BY_FIELD["pack_holdout_rate"]: "0.2"}
        config = WriteBehaviourConfig.from_env_and_settings(
            settings=store.as_values(), env=env
        )
        assert config.pack_holdout_rate == 0.2
        set_env_provenance = resolve_overridden_by(settings=store.as_values(), env=env)
        assert set_env_provenance["pack_holdout_rate"] == "env"


class TestLegacyReadersStillWork:
    """Every deployed env var still controls exactly what it controlled.

    These call the *original* per-module reader functions, which live
    wrappers and other modules import by name — the consolidation must be
    invisible to them.
    """

    @pytest.mark.parametrize(
        ("reader", "field"),
        [
            (classify_on_ingest_enabled, "classify_on_ingest"),
            (embed_on_ingest_enabled, "embed_on_ingest"),
            (memory_extraction_env_enabled, "memory_extraction"),
            (reconcile_on_write_enabled, "reconcile_on_write"),
            (trace_extraction_enabled, "trace_extraction"),
        ],
    )
    def test_boolean_reader_tracks_its_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reader: object,
        field: str,
    ) -> None:
        assert reader() is False  # type: ignore[operator]
        monkeypatch.setenv(ENV_VAR_BY_FIELD[field], "1")
        assert reader() is True  # type: ignore[operator]
        monkeypatch.setenv(ENV_VAR_BY_FIELD[field], "0")
        assert reader() is False  # type: ignore[operator]

    def test_min_confidence_reader_tracks_its_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert trace_extraction_min_confidence() is None
        monkeypatch.setenv(ENV_VAR_BY_FIELD["trace_extraction_min_confidence"], "0.42")
        assert trace_extraction_min_confidence() == 0.42

    def test_reconcile_readers_track_their_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert reconcile_timeout_seconds() == 20.0
        assert configured_model_id() == "hermes3:8b"
        monkeypatch.setenv(ENV_VAR_BY_FIELD["reconcile_timeout_s"], "3.5")
        monkeypatch.setenv(ENV_VAR_BY_FIELD["reconcile_model"], "llama3.2:3b")
        assert reconcile_timeout_seconds() == 3.5
        assert configured_model_id() == "llama3.2:3b"

    def test_readers_stay_live_against_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config reads are deliberately uncached — only the stamp is."""
        assert embed_on_ingest_enabled() is False
        monkeypatch.setenv(ENV_VAR_BY_FIELD["embed_on_ingest"], "yes")
        assert embed_on_ingest_enabled() is True
