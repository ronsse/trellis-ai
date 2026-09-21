"""Tests for :mod:`trellis.core.write_config`.

Consolidating the write-behaviour knobs changed no observable behaviour:
same env var names, same defaults, same parsing quirks. These tests pin
that, and they pin that each legacy reader function — which deployments
and other modules still call — is still driven by exactly the variable it
was always driven by. The one deliberate difference, warning frequency for
a malformed confidence floor, is pinned too.
"""

from __future__ import annotations

import pytest
import structlog.testing

from trellis.classify.ingest import classify_on_ingest_enabled
from trellis.core import write_config
from trellis.core.write_config import (
    ENV_VAR_BY_FIELD,
    TRUTHY,
    WriteBehaviourConfig,
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
