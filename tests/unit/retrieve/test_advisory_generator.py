"""Tests for AdvisoryGenerator."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from structlog.testing import capture_logs

if TYPE_CHECKING:
    import pytest

from tests.recovery_command import expected_recovery
from trellis.retrieve.advisory_generator import (
    DEFAULT_MAX_KEYWORD_DOC_FREQ,
    MIN_EFFECT_SIZE_ENV,
    MIN_EFFECT_SIZE_SETTING,
    MIN_SAMPLE_SIZE_ENV,
    MIN_SAMPLE_SIZE_SETTING,
    QUERY_STOP_WORDS,
    SUGGESTED_MAX_KEYWORD_DOC_FREQ,
    AdvisoryGenerator,
    resolve_advisory_thresholds,
)
from trellis.retrieve.effectiveness import run_advisory_fitness_loop
from trellis.retrieve.formatters import format_advisories_as_markdown
from trellis.schemas.advisory import (
    RETIRED_BY_KEY,
    RETIRED_BY_QUERY_FILTER,
    RETIRED_CEILING_KEY,
    RETIRED_RULE_KEY,
    RETIREMENT_KEYS,
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    AdvisoryStatus,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType


def _event(
    event_type: EventType,
    entity_id: str,
    payload: dict,
) -> MagicMock:
    """Create a mock Event."""
    ev = MagicMock()
    ev.event_type = event_type
    ev.entity_id = entity_id
    ev.payload = payload
    ev.occurred_at = datetime.now(tz=UTC)
    return ev


def _pack_event(
    pack_id: str,
    item_ids: list[str],
    *,
    domain: str = "global",
    intent: str = "test query",
    items: list[dict] | None = None,
) -> MagicMock:
    """Create a PACK_ASSEMBLED event."""
    payload: dict = {
        "injected_item_ids": item_ids,
        "domain": domain,
        "intent": intent,
        "strategies_used": ["keyword"],
    }
    if items is not None:
        payload["injected_items"] = items
    return _event(EventType.PACK_ASSEMBLED, pack_id, payload)


def _feedback_event(
    pack_id: str,
    *,
    success: bool,
) -> MagicMock:
    """Create a FEEDBACK_RECORDED event."""
    return _event(
        EventType.FEEDBACK_RECORDED,
        pack_id,
        {"pack_id": pack_id, "success": success},
    )


class TestResolveAdvisoryThresholds:
    """``resolve_advisory_thresholds`` is the one path every caller (the
    nightly curation worker, the REST route and the CLI command) uses to
    turn a settings-store snapshot into the two thresholds the generator
    is constructed with.
    """

    def test_no_env_and_no_settings_returns_the_module_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        assert resolve_advisory_thresholds(None) == (5, 0.15)

    def test_a_settings_only_override_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: 12, MIN_EFFECT_SIZE_SETTING: 0.3}

        assert resolve_advisory_thresholds(settings) == (12, 0.3)

    def test_env_beats_a_settings_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "20")
        monkeypatch.setenv(MIN_EFFECT_SIZE_ENV, "0.5")
        settings = {MIN_SAMPLE_SIZE_SETTING: 12, MIN_EFFECT_SIZE_SETTING: 0.3}

        assert resolve_advisory_thresholds(settings) == (20, 0.5)

    def test_an_unparseable_env_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "not-a-number")
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 5

    def test_an_unparseable_settings_value_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stored side has its own ``except`` block, independent of
        the env side above -- a malformed *stored* sample size must fall
        back to the default too.
        """
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: "not-a-number"}

        sample_size, _effect_size = resolve_advisory_thresholds(settings)

        assert sample_size == 5

    def test_a_non_finite_env_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``int(float("inf"))`` raises ``OverflowError``, not
        ``ValueError`` -- routing through ``validate_value`` (which
        checks ``math.isfinite`` first) means the bare
        ``except (TypeError, ValueError)`` here never needs to see one.
        """
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "inf")
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 5

    def test_a_non_finite_settings_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: float("nan")}

        sample_size, _effect_size = resolve_advisory_thresholds(settings)

        assert sample_size == 5

    def test_a_negative_env_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "-1")
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 5

    def test_a_negative_settings_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: -1}

        sample_size, _effect_size = resolve_advisory_thresholds(settings)

        assert sample_size == 5

    def test_a_nan_env_effect_size_never_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B4: a ``nan`` effect floor must never fail open. ``effect <
        nan`` is always ``False``, so an unguarded ``nan`` floor would
        skip nothing it should skip -- this must resolve to the module
        default (0.15) instead.
        """
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.setenv(MIN_EFFECT_SIZE_ENV, "nan")

        _sample_size, effect_size = resolve_advisory_thresholds(None)

        assert effect_size == 0.15

    def test_a_nan_settings_effect_size_never_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_EFFECT_SIZE_SETTING: float("nan")}

        _sample_size, effect_size = resolve_advisory_thresholds(settings)

        assert effect_size == 0.15

    def test_a_negative_env_effect_size_never_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.setenv(MIN_EFFECT_SIZE_ENV, "-1.0")

        _sample_size, effect_size = resolve_advisory_thresholds(None)

        assert effect_size == 0.15

    def test_an_out_of_range_settings_effect_size_never_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5.5 is outside the registered ``0..1`` bound -- it must be
        rejected by ``validate_value``'s bounds check, not passed
        straight through as a floor nothing can cross.
        """
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_EFFECT_SIZE_SETTING: 5.5}

        _sample_size, effect_size = resolve_advisory_thresholds(settings)

        assert effect_size == 0.15

    def test_the_exact_maximum_settings_sample_size_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``advisory_min_sample_size`` is registered with
        ``maximum=1_000_000`` -- a sanity cap against a typo or an
        overflow-class value, not an operational ceiling, so the cap
        value itself must still resolve rather than be rejected.
        """
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: 1_000_000}

        sample_size, _effect_size = resolve_advisory_thresholds(settings)

        assert sample_size == 1_000_000

    def test_one_past_the_maximum_settings_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)
        settings = {MIN_SAMPLE_SIZE_SETTING: 1_000_001}

        sample_size, _effect_size = resolve_advisory_thresholds(settings)

        assert sample_size == 5

    def test_the_exact_maximum_env_sample_size_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "1000000")
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 1_000_000

    def test_one_past_the_maximum_env_sample_size_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, "1000001")
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 5


def _assert_no_env_value_leak(logs: list[dict], event: str, secret: str) -> dict:
    """SF3 (env half): the log line for a malformed env value must carry
    ``var=<ENV_NAME>`` and ``error_type=...``, never the raw value --
    matching the settings-store half #867 already fixed. Checking for
    the full secret is not enough on its own (a truncating formatter
    could still leak a damaging fragment), so this also rules out every
    8-character substring of it appearing anywhere in the captured
    entries.
    """
    matches = [entry for entry in logs if entry["event"] == event]
    assert len(matches) == 1, logs
    entry = matches[0]
    rendered = repr(entry)
    assert secret not in rendered, rendered
    for start in range(len(secret) - 7):
        chunk = secret[start : start + 8]
        assert chunk not in rendered, (chunk, rendered)
    return entry


class TestEnvLogLineNeverLeaksTheRawValue:
    """SF3 (env half). The settings-store half of this bug (a malformed
    *stored* value logged at ``value=<raw>``) was fixed in #867; the env
    half -- the exact same resolvers' *other* branch -- was not. A
    secret-shaped env override (an API key pasted into the wrong
    variable, say) must not reach stderr when it fails to parse.
    """

    def test_bad_env_int_logs_the_var_name_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        needle = "sk-proj-FAKEAdvisoryEnvIntNeedle000111"
        monkeypatch.setenv(MIN_SAMPLE_SIZE_ENV, needle)
        monkeypatch.delenv(MIN_EFFECT_SIZE_ENV, raising=False)

        with capture_logs() as logs:
            sample_size, _effect_size = resolve_advisory_thresholds(None)

        assert sample_size == 5
        entry = _assert_no_env_value_leak(
            logs, "advisory_generator.bad_env_int", needle
        )
        assert entry["var"] == MIN_SAMPLE_SIZE_ENV
        assert "error_type" in entry
        assert "value" not in entry

    def test_bad_env_float_logs_the_var_name_not_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        needle = "sk-proj-FAKEAdvisoryEnvFloatNeedle000111"
        monkeypatch.delenv(MIN_SAMPLE_SIZE_ENV, raising=False)
        monkeypatch.setenv(MIN_EFFECT_SIZE_ENV, needle)

        with capture_logs() as logs:
            _sample_size, effect_size = resolve_advisory_thresholds(None)

        assert effect_size == 0.15
        entry = _assert_no_env_value_leak(
            logs, "advisory_generator.bad_env_float", needle
        )
        assert entry["var"] == MIN_EFFECT_SIZE_ENV
        assert "error_type" in entry
        assert "value" not in entry


class TestAdvisoryGeneratorEmpty:
    """Generator produces empty report with no data."""

    def test_no_events(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.return_value = []
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0
        assert report.advisories_stored == 0

    def test_packs_without_feedback(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [
            [_pack_event("p1", ["a", "b"])],  # pack events
            [],  # no feedback
        ]
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0
        assert report.total_packs == 1
        assert report.total_feedback == 0


class TestEntityCorrelation:
    """Entity correlation finds items disproportionately in successes."""

    def test_entity_with_high_success_rate(self, tmp_path: Path) -> None:
        """An item appearing mostly in successful packs → ENTITY advisory."""
        packs = []
        feedback = []
        # Item "good_entity" in 8 successful packs, 1 failure
        for i in range(8):
            packs.append(_pack_event(f"p{i}", ["good_entity", f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=True))

        packs.append(_pack_event("p8", ["good_entity"]))
        feedback.append(_feedback_event("p8", success=False))

        # 5 packs without "good_entity" — 2 success, 3 failure
        for i in range(9, 14):
            packs.append(_pack_event(f"p{i}", [f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=(i < 11)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        entity_advs = [
            a
            for a in store.list()
            if a.category == AdvisoryCategory.ENTITY and a.entity_id == "good_entity"
        ]
        assert len(entity_advs) >= 1
        adv = entity_advs[0]
        assert adv.evidence.success_rate_with > 0.8
        assert adv.evidence.effect_size > 0

    def test_no_advisory_below_min_sample(self, tmp_path: Path) -> None:
        """Items with too few appearances produce no advisory."""
        packs = [_pack_event("p1", ["rare_item"])]
        feedback = [_feedback_event("p1", success=True)]

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=5)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0


class TestAntiPatternDetection:
    """Anti-pattern detection finds items disproportionately in failures."""

    def test_item_correlating_with_failure(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # "bad_entity" in 8 failed packs, 1 success
        for i in range(8):
            packs.append(_pack_event(f"p{i}", ["bad_entity"]))
            feedback.append(_feedback_event(f"p{i}", success=False))

        packs.append(_pack_event("p8", ["bad_entity"]))
        feedback.append(_feedback_event("p8", success=True))

        # 5 packs without "bad_entity" — 4 success, 1 failure
        for i in range(9, 14):
            packs.append(_pack_event(f"p{i}", [f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=(i < 13)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        anti_advs = [
            a
            for a in store.list()
            if a.category == AdvisoryCategory.ANTI_PATTERN
            and a.entity_id == "bad_entity"
        ]
        assert len(anti_advs) >= 1
        adv = anti_advs[0]
        assert adv.evidence.effect_size < 0


class TestStrategyCorrelation:
    """Strategy correlation finds strategies with outcome differences."""

    def test_strategy_with_high_success(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Packs with semantic strategy → mostly success
        for i in range(6):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    items=[{"strategy_source": "semantic"}],
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=True))

        # Packs without semantic → mostly failure
        for i in range(6, 12):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    items=[{"strategy_source": "keyword"}],
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=(i == 6)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        approach_advs = [
            a for a in store.list() if a.category == AdvisoryCategory.APPROACH
        ]
        assert len(approach_advs) >= 1


class TestScopeAnalysis:
    """Scope analysis compares pack breadth with outcomes."""

    def test_narrow_packs_better(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Small packs (3 items) → 5/6 success
        for i in range(6):
            packs.append(_pack_event(f"s{i}", [f"a{i}", f"b{i}", f"c{i}"]))
            feedback.append(_feedback_event(f"s{i}", success=(i < 5)))

        # Large packs (20 items) → 1/6 success
        for i in range(6):
            items = [f"item{j}" for j in range(20)]
            packs.append(_pack_event(f"l{i}", items))
            feedback.append(_feedback_event(f"l{i}", success=(i == 0)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        scope_advs = [a for a in store.list() if a.category == AdvisoryCategory.SCOPE]
        assert len(scope_advs) >= 1
        assert scope_advs[0].evidence.effect_size > 0


class TestQueryImprovement:
    """Query improvement finds keywords that correlate with success."""

    def test_keyword_correlation(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Packs with "deployment" in intent → mostly success
        for i in range(6):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    intent="deployment checklist review",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=True))

        # Packs without "deployment" → mostly failure
        for i in range(6, 12):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    intent="general task review",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=(i == 6)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        # "deployment" is in half the intents here; this test is about the
        # correlation, so the document-frequency ceiling is switched off.
        gen = AdvisoryGenerator(
            event_log, store, min_sample_size=3, max_keyword_doc_freq=1.0
        )
        gen.generate(days=30)

        query_advs = [a for a in store.list() if a.category == AdvisoryCategory.QUERY]
        # "deployment" should appear as a correlating keyword
        deployment_advs = [a for a in query_advs if "deployment" in a.message.lower()]
        assert len(deployment_advs) >= 1


class TestConfidenceComputation:
    """Confidence scales with sample size and effect size."""

    def test_low_sample_low_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=2, effect=0.5 → sample_factor=0.2, effect_factor=1.0 → 0.2
        assert gen._compute_confidence(2, 0.5) == 0.2

    def test_high_sample_high_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=20, effect=0.5 → sample_factor=1.0, effect_factor=1.0 → 1.0
        assert gen._compute_confidence(20, 0.5) == 1.0

    def test_weak_effect_low_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=20, effect=0.1 → sample_factor=1.0, effect_factor=0.2 → 0.2
        assert gen._compute_confidence(20, 0.1) == 0.2


class TestAdvisoryReport:
    """AdvisoryReport captures generation metadata."""

    def test_report_fields(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.return_value = []
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=7)

        assert report.analysis_window_days == 7
        assert report.total_packs == 0
        assert report.total_feedback == 0
        assert report.advisories_generated == 0
        assert report.advisories_stored == 0


def _strategy_pack(
    pack_id: str,
    strategies: list[str],
    *,
    item_ids: list[str] | None = None,
    intent: str = "constant intent for every pack",
) -> MagicMock:
    """A PACK_ASSEMBLED event whose items came from ``strategies``."""
    return _pack_event(
        pack_id,
        item_ids if item_ids is not None else [f"item-{pack_id}"],
        intent=intent,
        items=[{"strategy_source": s} for s in strategies],
    )


def _two_arm_corpus() -> tuple[list[MagicMock], list[MagicMock]]:
    """20 packs carrying three strategies with deliberately different arms.

    * ``ubiquitous`` — on all 20 packs, so there is no comparison arm at all.
    * ``narrow``     — on 19, leaving a one-pack arm: present, but not evidence.
    * ``real``       — on 10, against 10 without: the arm that can be measured.

    Outcomes: packs 0-7 succeed, 8-9 fail, 10-11 succeed, 12-19 fail — so
    ``real`` runs 8/10 against 2/10 and its effect (+0.6) is nothing like its
    own success rate (0.8). That gap is the point: an ``effect_size`` that
    can only ever read back ``success_rate_with`` is a constant.
    """
    packs: list[MagicMock] = []
    feedback: list[MagicMock] = []
    for i in range(20):
        strategies = ["ubiquitous"]
        if i < 19:
            strategies.append("narrow")
        if i < 10:
            strategies.append("real")
        packs.append(_strategy_pack(f"p{i}", strategies))
        feedback.append(_feedback_event(f"p{i}", success=i < 8 or i in (10, 11)))
    return packs, feedback


def _run(
    tmp_path: Path,
    packs: list[MagicMock],
    feedback: list[MagicMock],
    *,
    runs: int = 1,
    store: AdvisoryStore | None = None,
    **kwargs: object,
) -> tuple[AdvisoryStore, MagicMock]:
    """Generate ``runs`` times over the same events; return store + event log."""
    event_log = MagicMock(spec=EventLog)
    event_log.get_events.side_effect = [packs, feedback] * runs
    adv_store = store if store is not None else AdvisoryStore(tmp_path / "adv.json")
    gen = AdvisoryGenerator(event_log, adv_store, **kwargs)  # type: ignore[arg-type]
    for _ in range(runs):
        gen.generate(days=30)
    return adv_store, event_log


class TestComparisonArmRequired:
    """#383 — an advisory with no comparison arm is not emitted."""

    def test_effect_size_is_not_the_success_rate(self, tmp_path: Path) -> None:
        """The measurable arm survives, and its effect is not its own rate.

        This is the assertion that fails if ``success_rate_without`` ever
        reverts to a ``0.0`` fallback: with that fallback ``effect_size``
        equals ``success_rate_with`` *exactly*, which is how all 36 of the
        reference deployment's 37 advisories read.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        by_strategy = {
            a.metadata["strategy"]: a
            for a in store.list()
            if a.category == AdvisoryCategory.APPROACH
        }
        assert set(by_strategy) == {"real"}

        real = by_strategy["real"]
        assert real.evidence.success_rate_with == 0.8
        assert real.evidence.success_rate_without == 0.2
        assert real.evidence.effect_size == 0.6
        assert real.evidence.effect_size != real.evidence.success_rate_with

    def test_feature_on_every_pack_is_refused(self, tmp_path: Path) -> None:
        """``packs_without == 0`` — the exact shape of the live defect."""
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        assert not [
            a
            for a in store.list()
            if a.category == AdvisoryCategory.APPROACH
            and a.metadata.get("strategy") == "ubiquitous"
        ]

    def test_one_pack_comparison_arm_is_refused(self, tmp_path: Path) -> None:
        """A comparison arm below ``min_sample_size`` is not evidence either.

        ``lift_vs_baseline`` alone would happily divide by a single pack and
        return a lift; the sample floor on the *without* arm is what stops it.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        assert not [a for a in store.list() if a.metadata.get("strategy") == "narrow"]

    def test_ubiquitous_query_term_is_refused(self, tmp_path: Path) -> None:
        """Every intent shares the same words, so no word has an arm."""
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        assert not [a for a in store.list() if a.category == AdvisoryCategory.QUERY]

    @staticmethod
    def _entity_corpus(
        outside_successes: int,
    ) -> tuple[list[MagicMock], list[MagicMock]]:
        """Item ``shared`` on 6 packs, against 6 packs without it.

        ``outside_successes`` of the six packs *without* the item succeed,
        which is the only thing that moves ``success_rate_without``.
        """
        packs: list[MagicMock] = []
        feedback: list[MagicMock] = []
        for i in range(6):
            packs.append(_strategy_pack(f"in{i}", ["s"], item_ids=["shared"]))
            feedback.append(_feedback_event(f"in{i}", success=i < 5))
        for i in range(6):
            packs.append(_strategy_pack(f"out{i}", ["s"], item_ids=[f"solo{i}"]))
            feedback.append(_feedback_event(f"out{i}", success=i < outside_successes))
        return packs, feedback

    def test_a_real_arm_still_produces_an_entity_advisory(self, tmp_path: Path) -> None:
        """The gate is not a blanket refusal — items with an arm still land.

        Note ``success_rate_without == 0.0`` here, and that is *correct*:
        six packs without the item were observed and none of them
        succeeded. The defect this suite guards was never "the value is
        zero", it was "no comparison arm was observed and zero was
        substituted for one" — which is why the gate is on the arm's size
        and not on the value it produces.
        """
        packs, feedback = self._entity_corpus(outside_successes=0)
        store, _ = _run(tmp_path, packs, feedback)

        entity = [a for a in store.list() if a.category == AdvisoryCategory.ENTITY]
        assert len(entity) == 1
        assert entity[0].entity_id == "shared"
        assert entity[0].evidence.sample_size == 6
        assert entity[0].evidence.success_rate_without == 0.0

    def test_without_rate_moves_with_the_comparison_arm(self, tmp_path: Path) -> None:
        """Same item, same with-arm; only the other side of the join changes.

        ``success_rate_with`` is identical across the two corpora, so if
        ``effect_size`` reads the same in both, the comparison arm is not
        being consulted at all.
        """
        results = {}
        for outside in (0, 2, 4):
            packs, feedback = self._entity_corpus(outside_successes=outside)
            store, _ = _run(tmp_path / f"n{outside}", packs, feedback)
            adv = next(a for a in store.list() if a.category == AdvisoryCategory.ENTITY)
            results[outside] = (
                adv.evidence.success_rate_with,
                adv.evidence.success_rate_without,
                adv.evidence.effect_size,
            )

        assert {r[0] for r in results.values()} == {0.833}
        assert [results[k][1] for k in (0, 2, 4)] == [0.0, 0.333, 0.667]
        assert [results[k][2] for k in (0, 2, 4)] == [0.833, 0.5, 0.167]


class TestRefusalIsReported:
    """A refusal nobody can see is the quiet half of the #383 defect."""

    def test_refused_findings_are_counted_on_the_report(self, tmp_path: Path) -> None:
        """Refusals are counted, not just silently dropped.

        Two: the ``ubiquitous`` and ``narrow`` strategies. The five words of
        the shared intent used to make it seven, each being on all 20 packs;
        the QUERY filter now takes them out before they are priced (two are
        function words, three are over the document-frequency ceiling, which
        this test turns on at 0.25: it is off by default), so
        they are neither refused nor withheld rows: none could have been
        written, there being no comparison arm. The count still separates
        "the analysis found nothing" from "found things it could not
        attribute" for what the filter lets through.
        """
        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        gen = AdvisoryGenerator(
            event_log,
            AdvisoryStore(tmp_path / "adv.json"),
            max_keyword_doc_freq=SUGGESTED_MAX_KEYWORD_DOC_FREQ,
        )

        report = gen.generate(days=30)

        assert report.advisories_generated == 1
        assert report.findings_refused_no_comparison_arm == 2
        assert report.query_keywords_withheld == {
            "stop_list": 0,
            "document_frequency": 0,
        }

    def test_no_refusals_when_every_finding_has_an_arm(self, tmp_path: Path) -> None:
        packs: list[MagicMock] = []
        feedback: list[MagicMock] = []
        for i in range(20):
            packs.append(
                _strategy_pack(
                    f"p{i}",
                    ["alpha"] if i < 10 else ["beta"],
                    item_ids=[f"solo{i}"],
                    intent="",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=i < 8 or i >= 18))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        gen = AdvisoryGenerator(event_log, AdvisoryStore(tmp_path / "adv.json"))

        report = gen.generate(days=30)

        assert report.findings_refused_no_comparison_arm == 0


class TestEvidencePointers:
    """#383 — advisories name the packs behind the claim."""

    def test_approach_advisory_carries_pack_ids(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        real = next(a for a in store.list() if a.metadata.get("strategy") == "real")
        assert real.evidence.representative_trace_ids
        # Positive effect → the exemplars are the successful packs.
        assert set(real.evidence.representative_trace_ids) <= {
            f"p{i}" for i in range(8)
        }

    def test_every_emitted_advisory_has_pointers(self, tmp_path: Path) -> None:
        packs: list[MagicMock] = []
        feedback: list[MagicMock] = []
        # Small packs succeed, large packs fail → a SCOPE finding too.
        for i in range(6):
            packs.append(_strategy_pack(f"s{i}", ["narrowly"], item_ids=["good"]))
            feedback.append(_feedback_event(f"s{i}", success=True))
        for i in range(6):
            packs.append(
                _strategy_pack(
                    f"l{i}",
                    ["broadly"],
                    item_ids=[f"x{j}" for j in range(20)],
                    intent=f"unshared intent {i}",
                )
            )
            feedback.append(_feedback_event(f"l{i}", success=False))

        store, _ = _run(tmp_path, packs, feedback, min_sample_size=5)
        emitted = store.list()
        assert emitted
        assert all(a.evidence.representative_trace_ids for a in emitted)
        assert {a.category for a in emitted} >= {
            AdvisoryCategory.APPROACH,
            AdvisoryCategory.SCOPE,
        }


class TestStableIds:
    """#383 — a finding keeps one id across runs so presentations accumulate."""

    def test_regeneration_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback, runs=1)
        first = {a.advisory_id for a in store.list()}
        assert first

        _run(tmp_path, packs, feedback, runs=1, store=store)
        assert {a.advisory_id for a in store.list()} == first

    def test_id_is_deterministic_and_subject_scoped(self) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        first = gen._stable_id(AdvisoryCategory.QUERY, "deploy")
        again = gen._stable_id(AdvisoryCategory.QUERY, "deploy")
        other_subject = gen._stable_id(AdvisoryCategory.QUERY, "rollback")
        other_category = gen._stable_id(AdvisoryCategory.ENTITY, "deploy")

        assert first == again
        assert len({first, other_subject, other_category}) == 3
        assert first.startswith("adv-query-")

    def test_id_survives_a_scope_change(self, tmp_path: Path) -> None:
        """An entity that turns up in a second domain keeps its row.

        ``scope`` is derived from the evidence, so it moves. If it were in
        the id key, the move would mint a second row and orphan the first
        — the very defect stable ids exist to remove.
        """

        def corpus(second_domain: str) -> tuple[list[MagicMock], list[MagicMock]]:
            packs: list[MagicMock] = []
            feedback: list[MagicMock] = []
            for i in range(6):
                domain = "infra" if i < 5 else second_domain
                packs.append(
                    _pack_event(
                        f"in{i}",
                        ["shared"],
                        domain=domain,
                        intent="",
                        items=[{"strategy_source": "s"}],
                    )
                )
                feedback.append(_feedback_event(f"in{i}", success=i < 5))
            for i in range(6):
                packs.append(
                    _pack_event(
                        f"out{i}",
                        [f"solo{i}"],
                        domain="infra",
                        intent="",
                        items=[{"strategy_source": "s"}],
                    )
                )
                feedback.append(_feedback_event(f"out{i}", success=False))
            return packs, feedback

        store, _ = _run(tmp_path, *corpus("infra"))
        narrow = next(a for a in store.list() if a.category == AdvisoryCategory.ENTITY)
        assert narrow.scope == "infra"

        _run(tmp_path, *corpus("platform"), store=store)
        widened = store.get(narrow.advisory_id)
        assert widened is not None
        assert widened.scope == "global"
        assert len([a for a in store.list() if a.entity_id == "shared"]) == 1


class TestDomainNormalisation:
    """``PACK_ASSEMBLED.payload["domain"]`` is present-and-null in production."""

    def test_null_domain_does_not_abort_the_run(self, tmp_path: Path) -> None:
        """A null domain used to reach ``Advisory.scope: str`` and raise.

        Not "one advisory is lost" — the ValidationError propagates out of
        ``generate()``, so the whole nightly run dies. 36 of the reference
        deployment's 46 packs carry ``"domain": None``.
        """
        packs: list[MagicMock] = []
        feedback: list[MagicMock] = []
        for i in range(12):
            packs.append(
                _pack_event(
                    f"p{i}",
                    ["shared"] if i < 6 else [f"solo{i}"],
                    domain=None,  # type: ignore[arg-type]
                    intent="",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=i < 5))

        store, _ = _run(tmp_path, packs, feedback)

        entity = [a for a in store.list() if a.category == AdvisoryCategory.ENTITY]
        assert len(entity) == 1
        assert entity[0].scope == "global"

    def test_created_at_survives_regeneration(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        before = {a.advisory_id: a.created_at for a in store.list()}

        _run(tmp_path, packs, feedback, store=store)
        after = {a.advisory_id: a.created_at for a in store.list()}
        assert after == before


class TestSuppressionSurvivesRegeneration:
    """#383 — the in-place write must not undo the fitness loop's decisions."""

    def test_suppressed_advisory_is_not_revived(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        target = store.list()[0].advisory_id
        store.suppress(target, reason="fitness loop said so")

        _run(tmp_path, packs, feedback, store=store)

        revived = store.get(target)
        assert revived is not None
        assert revived.status == AdvisoryStatus.SUPPRESSED
        assert revived.suppression_reason == "fitness loop said so"
        assert revived.suppressed_at is not None
        assert target not in {a.advisory_id for a in store.list()}

    def test_suppressed_confidence_is_not_reset(self, tmp_path: Path) -> None:
        """A reset would let the next fitness pass blend it back above the bar.

        Deliberately leaves ``fitness_scored_at`` unset — the shape a
        hand-suppression through the public :meth:`AdvisoryStore.suppress`
        would leave. Carrying confidence for a suppressed row is a second,
        independent clause for exactly this case.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        target = store.list()[0]
        store.suppress(target.advisory_id, reason="weak")
        floored = store.get(target.advisory_id)
        assert floored is not None
        store.put(floored.model_copy(update={"confidence": 0.01}))

        _run(tmp_path, packs, feedback, store=store)

        after = store.get(target.advisory_id)
        assert after is not None
        assert after.confidence == 0.01
        assert after.status == AdvisoryStatus.SUPPRESSED

    def test_unscored_advisory_confidence_tracks_fresh_evidence(
        self, tmp_path: Path
    ) -> None:
        """Until the loop has scored a row, the generator keeps it current.

        This is not a nicety. Measured on the reference deployment, a
        finding's statistic moved 0.4 -> 0.6 over two days of window roll as
        feedback accrued — and since no advisory there has ever been served,
        every row is in this state. Freezing confidence at creation would
        pin the delivery gate to a stale number for the entire corpus.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        target = store.list()[0]
        assert target.fitness_scored_at is None
        store.put(target.model_copy(update={"confidence": 0.02}))

        _run(tmp_path, packs, feedback, store=store)

        after = store.get(target.advisory_id)
        assert after is not None
        assert after.confidence == target.confidence

    def test_scored_advisory_confidence_is_the_loops_to_keep(
        self, tmp_path: Path
    ) -> None:
        """Once ``fitness_scored_at`` is set, the generator stops writing it.

        The inverse of the test above, and the one that keeps demotion
        reachable: an advisory the loop has blended down must not be reset
        to its statistical value by the next night's generation.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        target = store.list()[0]
        store.put(
            target.model_copy(
                update={
                    "confidence": 0.12,
                    "fitness_scored_at": datetime(2026, 8, 1, tzinfo=UTC),
                }
            )
        )

        _run(tmp_path, packs, feedback, store=store)

        after = store.get(target.advisory_id)
        assert after is not None
        assert after.confidence == 0.12
        # The handoff marker itself survives, or it would be forgotten nightly.
        assert after.fitness_scored_at == datetime(2026, 8, 1, tzinfo=UTC)
        # The fresh statistic is not lost — it moves to the evidence block.
        assert after.evidence.evidence_confidence == target.confidence


class TestCappedReads:
    """#374 — the cap must drop the oldest events, not the newest."""

    def test_reads_are_issued_newest_first(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        _, event_log = _run(tmp_path, packs, feedback)

        assert event_log.get_events.call_count == 2
        for call in event_log.get_events.call_args_list:
            assert call.kwargs["order"] == "desc"

    def test_report_carries_scan_coverage(self, tmp_path: Path) -> None:
        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        gen = AdvisoryGenerator(event_log, AdvisoryStore(tmp_path / "adv.json"))

        report = gen.generate(days=30)

        assert report.coverage.scanned == len(packs) + len(feedback)
        assert report.coverage.truncated is False


class TestDemotionRemainsReachable:
    """#383 review — the fix must not make the *demote* half unreachable.

    The generator and the fitness loop run in one nightly cycle
    (``trellis_cli/worker.py``), and stable ids make the generator's write
    land on the loop's row. If the generator also rewrites ``confidence``,
    the blend can never compound: an emitted advisory has
    ``confidence >= 0.15`` by construction, so ``0.7 * 0.15 = 0.105`` stays
    above ``suppress_below = 0.1`` in a single pass, at any success rate.

    These run the **real** ``run_advisory_fitness_loop`` rather than
    simulating the arithmetic, because the arithmetic was not the thing
    that was wrong — the ownership was.
    """

    @staticmethod
    def _event_log(packs: list[MagicMock], feedback: list[MagicMock]) -> MagicMock:
        """An event log that answers any number of reads, keyed by type."""
        log = MagicMock(spec=EventLog)

        def get_events(
            *, event_type: EventType | None = None, **_kwargs: object
        ) -> list[MagicMock]:
            if event_type == EventType.PACK_ASSEMBLED:
                return list(packs)
            if event_type == EventType.FEEDBACK_RECORDED:
                return list(feedback)
            return []

        log.get_events.side_effect = get_events
        return log

    def _cycle_until_suppressed(
        self, tmp_path: Path, *, cycles: int = 8
    ) -> tuple[AdvisoryStore, str, list[float]]:
        packs, feedback = _two_arm_corpus()
        store = AdvisoryStore(tmp_path / "adv.json")

        # Learn the finding's id, which is stable — that is the property
        # under test, so the test is entitled to rely on it.
        seed_log = self._event_log(packs, feedback)
        AdvisoryGenerator(seed_log, store).generate(days=30)
        target = next(a for a in store.list() if a.metadata.get("strategy") == "real")
        advisory_id = target.advisory_id

        # Six more packs that carried the advisory and all failed. No items
        # and no intent, so they add nothing the generator would mine.
        for i in range(6):
            pack_id = f"served{i}"
            packs.append(
                _event(
                    EventType.PACK_ASSEMBLED,
                    pack_id,
                    {
                        "injected_item_ids": [],
                        "injected_items": [],
                        "domain": None,
                        "intent": "",
                        "advisory_ids": [advisory_id],
                    },
                )
            )
            feedback.append(_feedback_event(pack_id, success=False))

        log = self._event_log(packs, feedback)
        trail: list[float] = []
        for _ in range(cycles):
            AdvisoryGenerator(log, store).generate(days=30)
            run_advisory_fitness_loop(log, store, days=30)
            current = store.get(advisory_id)
            assert current is not None
            trail.append(current.confidence)
            if current.status == AdvisoryStatus.SUPPRESSED:
                break
        return store, advisory_id, trail

    def test_an_advisory_that_only_ever_fails_is_suppressed(
        self, tmp_path: Path
    ) -> None:
        store, advisory_id, trail = self._cycle_until_suppressed(tmp_path)

        final = store.get(advisory_id)
        assert final is not None
        assert final.status == AdvisoryStatus.SUPPRESSED, (
            f"confidence never crossed suppress_below; trail={trail}"
        )
        # Monotonically decreasing — proof the blend compounds across
        # cycles instead of being reset by each night's generation.
        assert trail == sorted(trail, reverse=True)
        assert trail[-1] < 0.1

    def test_the_generator_records_the_handoff(self, tmp_path: Path) -> None:
        """After the loop scores it, the row says the loop owns confidence."""
        store, advisory_id, _ = self._cycle_until_suppressed(tmp_path)

        final = store.get(advisory_id)
        assert final is not None
        assert final.fitness_scored_at is not None
        # The generator's statistic is still current beside the blended gate.
        assert final.evidence.evidence_confidence is not None
        assert final.evidence.evidence_confidence > final.confidence


#: Appended to a valid advisory file to make it unparseable while leaving
#: every row still present in the bytes — the shape of a torn write, and
#: the only shape that lets a test tell "preserved" from "rewritten".
_TORN_MARKER = "\n<<< torn write >>>"


class TestDegradedStoreCannotUnsuppress:
    """#393's second symptom — the one stable ids (#394) introduced.

    ``_carry_forward_status`` preserves the fitness loop's decisions by
    reading each finding's prior row. Against a store that could not read
    its file every ``get`` returns ``None``, so every regenerated advisory
    is written fresh — ``status=ACTIVE``, ``suppressed_at=None`` — over the
    rows that recorded the suppression. Suppression is the only mechanism
    that takes a bad advisory out of circulation, and this reverses it
    while reporting an ordinary ``advisories_generated: N``.

    Losing the data shows up on the next read. Reversing the decision looks
    like normal operation, which is why this is the worse half.
    """

    @staticmethod
    def _suppressed_then_corrupted(tmp_path: Path) -> tuple[Path, str, str]:
        """Generate, suppress one row, then corrupt the file it lives in.

        Returns ``(path, target_id, good_bytes)``. The corruption appends
        trailing junk rather than truncating, so the suppressed row is
        provably still *in* the file — the question under test is whether
        anything overwrites it, not whether it survived the corruption.
        """
        path = tmp_path / "adv.json"
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        target = store.list()[0].advisory_id
        store.suppress(target, reason="fitness loop said so")

        good_bytes = path.read_text(encoding="utf-8")
        path.write_text(good_bytes + _TORN_MARKER, encoding="utf-8")
        return path, target, good_bytes

    def test_the_file_holding_the_suppression_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        path, target, good_bytes = self._suppressed_then_corrupted(tmp_path)
        corrupted = path.read_text(encoding="utf-8")

        store = AdvisoryStore(path)
        assert store.is_degraded is True

        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        report = AdvisoryGenerator(event_log, store).generate(days=30)

        assert report.store_degradation is not None
        assert report.advisories_generated == 0
        assert report.advisories_stored == 0
        assert path.read_text(encoding="utf-8") == corrupted

        # The suppression is still there, in the bytes, recoverable.
        rows = json.loads(good_bytes)["advisories"]
        row = next(r for r in rows if r["advisory_id"] == target)
        assert row["status"] == AdvisoryStatus.SUPPRESSED.value
        assert row["suppression_reason"] == "fitness loop said so"

    def test_a_suppressed_advisory_stays_suppressed_after_recovery(
        self, tmp_path: Path
    ) -> None:
        """The symptom, stated end to end.

        A nightly run happens against the corrupt file; the operator then
        clears the corruption. Because nothing was written in between, the
        suppression is still the store's state. Against the pre-#393 store
        this row comes back ``ACTIVE`` with no suppression metadata, and
        nothing anywhere says a curation decision was reversed.
        """
        path, target, _good_bytes = self._suppressed_then_corrupted(tmp_path)

        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        AdvisoryGenerator(event_log, AdvisoryStore(path)).generate(days=30)

        # Recover from what is *on disk*, not from a copy this test kept.
        # The corruption is trailing junk, so the rows are still there — but
        # only if nothing overwrote them. Restoring a saved copy instead
        # would repair the damage the test exists to detect, and would pass
        # against the very code the fix removes.
        on_disk = path.read_text(encoding="utf-8")
        path.write_text(on_disk.split(_TORN_MARKER)[0], encoding="utf-8")
        recovered = AdvisoryStore(path)

        assert recovered.is_degraded is False
        after = recovered.get(target)
        assert after is not None
        assert after.status == AdvisoryStatus.SUPPRESSED
        assert after.suppression_reason == "fitness loop said so"
        assert target not in {a.advisory_id for a in recovered.list()}

    def test_the_refusal_is_reported_and_logged(self, tmp_path: Path) -> None:
        """Zeros with no explanation are the failure this issue is about.

        The counts on a refused run are indistinguishable from a quiet
        night unless the report says which it was, and the log level has to
        be one the nightly surface prints — ``trellis_cli.main._root`` pins
        WARNING, so ``info`` would be a no-op there.
        """
        path, _target, _good = self._suppressed_then_corrupted(tmp_path)
        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        with capture_logs() as logs:
            report = AdvisoryGenerator(event_log, AdvisoryStore(path)).generate(days=30)

        assert report.store_degradation is not None
        assert report.store_degradation["reason"] == "malformed_json"
        assert report.store_degradation["recovery"] == expected_recovery(path)

        lines = [
            e
            for e in logs
            if e["event"] == "advisory_generation_refused_degraded_store"
        ]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["recovery"] == expected_recovery(path)

    def test_the_event_log_is_never_read(self, tmp_path: Path) -> None:
        """Refuse before analysing, not after.

        Analysing and then discarding would report ``generated: N,
        stored: 0`` — a store problem worth shrugging at rather than the
        curation decision that just failed to apply.
        """
        path, _target, _good = self._suppressed_then_corrupted(tmp_path)
        event_log = MagicMock(spec=EventLog)

        AdvisoryGenerator(event_log, AdvisoryStore(path)).generate(days=30)

        event_log.get_events.assert_not_called()

    def test_a_clean_store_still_generates(self, tmp_path: Path) -> None:
        """The refusal must not be over-broad — the normal path is untouched."""
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        assert store.list()
        assert store.is_degraded is False


class TestMessagesCarryTheirOwnEvidence:
    """#392 — the premise that lets the formatter drop its evidence suffix.

    ``format_advisories_as_markdown`` used to append ``(n=..., effect=...)``
    to a message the generator had already written those figures into, so
    every rendered line printed them twice. Removing the suffix is only
    safe while the generator keeps embedding them — this is the test that
    fails if that ever stops being true, and it reads the *real* generator
    output rather than a hand-written fixture.
    """

    def test_generated_messages_embed_sample_size_and_effect(
        self, tmp_path: Path
    ) -> None:
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)

        emitted = store.list()
        assert emitted, "corpus produced no advisories; the assertion below is vacuous"
        for advisory in emitted:
            assert f"n={advisory.evidence.sample_size}" in advisory.message
            assert "effect=" in advisory.message

    def test_rendering_real_generator_output_prints_each_figure_once(
        self, tmp_path: Path
    ) -> None:
        """End to end: generate, render, and count.

        The regression this pins is a *rendered* one, so it is asserted on
        rendered text rather than on the formatter's inputs.
        """
        packs, feedback = _two_arm_corpus()
        store, _ = _run(tmp_path, packs, feedback)
        emitted = store.list()
        assert emitted

        rendered = format_advisories_as_markdown(emitted)
        lines = {
            advisory.advisory_id: next(
                line for line in rendered.splitlines() if advisory.advisory_id in line
            )
            for advisory in emitted
        }

        for index, advisory in enumerate(emitted, start=1):
            assert lines[advisory.advisory_id] == (
                f"{index}. `{advisory.advisory_id}`"
                f" **[{advisory.category.value}]** {advisory.message}"
            )


def _intent_corpus(
    carriers: int,
    others: int,
    *,
    carrier_intent: str,
    other_intent: str = "gadget review",
) -> tuple[list[MagicMock], list[MagicMock]]:
    """``carriers`` successful packs with ``carrier_intent``, ``others`` mostly not.

    Exactly one of the ``others`` succeeds, so every word peculiar to the
    carriers' intent shows a large positive effect against a real comparison
    arm: whether it becomes an advisory is then decided by the QUERY filter
    alone, not by the sample or effect floors.
    """
    packs: list[MagicMock] = []
    feedback: list[MagicMock] = []
    for i in range(carriers):
        packs.append(_pack_event(f"c{i}", [f"ci{i}"], intent=carrier_intent))
        feedback.append(_feedback_event(f"c{i}", success=True))
    for i in range(others):
        packs.append(_pack_event(f"o{i}", [f"oi{i}"], intent=other_intent))
        feedback.append(_feedback_event(f"o{i}", success=i == 0))
    return packs, feedback


def _keywords(store: AdvisoryStore) -> set[str]:
    return {
        a.metadata["keyword"]
        for a in store.list(include_suppressed=True)
        if a.category == AdvisoryCategory.QUERY
    }


def _query_row(
    keyword: str, *, confidence: float = 0.6, status: AdvisoryStatus | None = None
) -> Advisory:
    """A QUERY row as an earlier run would have stored it."""
    return Advisory(
        advisory_id=AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, keyword),
        category=AdvisoryCategory.QUERY,
        confidence=confidence,
        message=f"Including '{keyword}' in your context query correlates (n=7).",
        evidence=AdvisoryEvidence(
            sample_size=7,
            success_rate_with=0.8,
            success_rate_without=0.2,
            effect_size=0.6,
        ),
        scope="global",
        metadata={"keyword": keyword},
        **({"status": status} if status is not None else {}),
    )


class TestQueryKeywordStopList:
    """#960 — a function word cannot be a query choice."""

    def test_function_words_do_not_key_an_advisory(self, tmp_path: Path) -> None:
        packs, feedback = _intent_corpus(
            6, 24, carrier_intent="widget for the rollout with care"
        )
        store, _ = _run(tmp_path, packs, feedback)

        keywords = _keywords(store)
        assert {"widget", "rollout", "care"} <= keywords
        assert not keywords & {"for", "the", "with"}

    def test_the_report_counts_the_rows_the_stop_list_withheld(
        self, tmp_path: Path
    ) -> None:
        packs, feedback = _intent_corpus(6, 24, carrier_intent="widget for the rollout")
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        store = AdvisoryStore(tmp_path / "adv.json")

        report = AdvisoryGenerator(event_log, store).generate(days=30)

        assert report.query_keywords_withheld == {
            "stop_list": 2,
            "document_frequency": 0,
        }

    def test_withheld_words_are_not_counted_as_refusals(self, tmp_path: Path) -> None:
        """Pricing an excluded word must not look like refusing a live one."""
        refusals = []
        for name, intent in (
            ("bare", "widget rollout"),
            ("stop", "widget for rollout"),
        ):
            packs, feedback = _intent_corpus(6, 24, carrier_intent=intent)
            event_log = MagicMock(spec=EventLog)
            event_log.get_events.side_effect = [packs, feedback]
            gen = AdvisoryGenerator(event_log, AdvisoryStore(tmp_path / f"{name}.json"))
            gen.generate(days=30)
            refusals.append(dict(gen._refusals))

        assert refusals[0] == refusals[1]

    def test_a_hand_read_of_the_stop_list(self) -> None:
        """The list is function words only, and long enough to matter."""
        assert {
            "for",
            "that",
            "with",
            "and",
            "from",
            "before",
            "after",
            "are",
            "one",
        } <= (QUERY_STOP_WORDS)
        assert all(len(w) >= 3 for w in QUERY_STOP_WORDS)  # shorter never reach it
        # Domain verbs and nouns are the ceiling's business, not the list's.
        assert not QUERY_STOP_WORDS & {
            "fix",
            "add",
            "test",
            "deploy",
            "pack",
            "context",
        }


class TestQueryKeywordPunctuation:
    """#960 — the split left edge punctuation on the keyword."""

    def test_edge_punctuation_is_stripped(self, tmp_path: Path) -> None:
        packs, feedback = _intent_corpus(
            6, 24, carrier_intent='widget, "rollout". (care)'
        )
        store, _ = _run(tmp_path, packs, feedback)

        assert _keywords(store) == {"widget", "rollout", "care"}

    def test_punctuated_and_bare_forms_share_one_advisory(self, tmp_path: Path) -> None:
        """Before: ``widget,`` and ``widget`` were two ids with half the packs."""
        packs, feedback = _intent_corpus(6, 24, carrier_intent="widget rollout")
        for pack in packs[:3]:
            pack.payload["intent"] = "widget, rollout."
        store, _ = _run(tmp_path, packs, feedback)

        widget = [a for a in store.list() if a.metadata["keyword"] == "widget"]
        assert len(widget) == 1
        assert widget[0].evidence.sample_size == 6


class TestQueryKeywordDocumentFrequency:
    """#960 — a word in a large share of intents cannot discriminate."""

    def test_a_word_over_the_ceiling_is_withheld(self, tmp_path: Path) -> None:
        # 12 of 30 packs: share 0.40 against a 0.25 ceiling.
        packs, feedback = _intent_corpus(12, 18, carrier_intent="widget rollout")
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        store = AdvisoryStore(tmp_path / "adv.json")

        report = AdvisoryGenerator(
            event_log, store, max_keyword_doc_freq=SUGGESTED_MAX_KEYWORD_DOC_FREQ
        ).generate(days=30)

        assert _keywords(store) == set()
        assert report.query_keywords_withheld == {
            "stop_list": 0,
            "document_frequency": 2,
        }
        assert report.query_keyword_df_ceiling == SUGGESTED_MAX_KEYWORD_DOC_FREQ
        assert report.query_keyword_df_rule == "enforced"

    def test_the_same_word_under_the_ceiling_is_kept(self, tmp_path: Path) -> None:
        # 6 of 30: share 0.20.
        packs, feedback = _intent_corpus(6, 24, carrier_intent="widget rollout")
        store, _ = _run(
            tmp_path,
            packs,
            feedback,
            max_keyword_doc_freq=SUGGESTED_MAX_KEYWORD_DOC_FREQ,
        )

        assert _keywords(store) == {"widget", "rollout"}

    def test_the_ceiling_is_a_constructor_parameter(self, tmp_path: Path) -> None:
        packs, feedback = _intent_corpus(12, 18, carrier_intent="widget rollout")
        store, _ = _run(tmp_path, packs, feedback, max_keyword_doc_freq=0.5)

        assert _keywords(store) == {"widget", "rollout"}

    def test_a_ceiling_of_one_turns_the_rule_off(self, tmp_path: Path) -> None:
        packs, feedback = _intent_corpus(12, 18, carrier_intent="widget rollout")
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        store = AdvisoryStore(tmp_path / "adv.json")

        report = AdvisoryGenerator(event_log, store, max_keyword_doc_freq=1.0).generate(
            days=30
        )

        assert _keywords(store) == {"widget", "rollout"}
        assert report.query_keywords_withheld["document_frequency"] == 0
        assert report.query_keyword_df_rule == "enforced"

    def test_the_ceiling_is_skipped_when_it_could_never_admit_a_word(
        self, tmp_path: Path
    ) -> None:
        """12 packs x 0.25 = 3 carriers, under the sample floor of 5.

        Enforced anyway it would withhold every word the floor admits, turning
        QUERY analysis off without saying so; the report says it stood down.
        """
        packs, feedback = _intent_corpus(6, 6, carrier_intent="widget rollout")
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        store = AdvisoryStore(tmp_path / "adv.json")

        report = AdvisoryGenerator(
            event_log, store, max_keyword_doc_freq=SUGGESTED_MAX_KEYWORD_DOC_FREQ
        ).generate(days=30)

        assert _keywords(store) == {"widget", "rollout"}
        assert report.query_keyword_df_ceiling is None
        assert report.query_keyword_df_rule == "stood_down"

    def test_the_stop_list_still_applies_when_the_ceiling_stands_down(
        self, tmp_path: Path
    ) -> None:
        packs, feedback = _intent_corpus(6, 6, carrier_intent="widget for rollout")
        store, _ = _run(
            tmp_path,
            packs,
            feedback,
            max_keyword_doc_freq=SUGGESTED_MAX_KEYWORD_DOC_FREQ,
        )

        assert _keywords(store) == {"widget", "rollout"}

    def test_a_bad_ceiling_is_refused(self) -> None:
        import pytest

        for bad in (0.0, -0.1, 1.5, float("nan")):
            with pytest.raises(ValueError, match="max_keyword_doc_freq"):
                AdvisoryGenerator(
                    MagicMock(spec=EventLog), MagicMock(), max_keyword_doc_freq=bad
                )


class TestQueryRowRetirement:
    """#960 — rows the generator would no longer write are suppressed, auditably."""

    @staticmethod
    def _generate(
        store: AdvisoryStore,
        packs: list[MagicMock],
        feedback: list[MagicMock],
        **kwargs: object,
    ) -> tuple[object, MagicMock]:
        # The frequency rule is off by default; these tests are about it.
        kwargs.setdefault("max_keyword_doc_freq", SUGGESTED_MAX_KEYWORD_DOC_FREQ)
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        report = AdvisoryGenerator(event_log, store, **kwargs).generate(days=30)  # type: ignore[arg-type]
        return report, event_log

    def _seeded(self, tmp_path: Path) -> AdvisoryStore:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put_many(
            [
                _query_row("for"),
                _query_row("widget,"),
                _query_row("rollout"),
                _query_row("care"),
            ]
        )
        return store

    def _corpus(self) -> tuple[list[MagicMock], list[MagicMock]]:
        # "rollout" is in 12 of 30 intents (over the ceiling); "care" in 6.
        packs, feedback = _intent_corpus(12, 18, carrier_intent="rollout")
        extra_p, extra_f = _intent_corpus(6, 0, carrier_intent="care gadget review")
        for p in extra_p:
            p.entity_id = "x" + p.entity_id
        for f in extra_f:
            f.entity_id = "x" + f.entity_id
            f.payload["pack_id"] = "x" + f.payload["pack_id"]
        return packs + extra_p, feedback + extra_f

    def test_each_rule_retires_its_rows_and_only_those(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()

        report, _ = self._generate(store, packs, feedback)

        by_keyword = {
            a.metadata["keyword"]: a for a in store.list(include_suppressed=True)
        }
        assert by_keyword["for"].status == AdvisoryStatus.SUPPRESSED
        assert by_keyword["widget,"].status == AdvisoryStatus.SUPPRESSED
        assert by_keyword["rollout"].status == AdvisoryStatus.SUPPRESSED
        assert by_keyword["care"].status == AdvisoryStatus.ACTIVE
        assert report.query_rows_retired == {  # type: ignore[attr-defined]
            "stop_list": 1,
            "document_frequency": 1,
            "unnormalised_keyword": 1,
        }

    def test_a_retirement_is_a_suppression_not_a_deletion(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()
        self._generate(store, packs, feedback)

        row = store.get(AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "for"))
        assert row is not None
        assert row.status == AdvisoryStatus.SUPPRESSED
        assert row.suppressed_at is not None
        assert "stop_list" in (row.suppression_reason or "")
        assert row.retired_by == RETIRED_BY_QUERY_FILTER
        # Reversible with the store's own restore.
        restored = store.restore(row.advisory_id)
        assert restored is not None
        assert restored.status == AdvisoryStatus.ACTIVE
        assert restored.retired_by is None

    def test_every_retirement_emits_an_audit_event(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()

        _, event_log = self._generate(store, packs, feedback)

        emitted = [
            c
            for c in event_log.emit.call_args_list
            if c.args[0] == EventType.ADVISORY_SUPPRESSED
        ]
        assert len(emitted) == 3
        by_rule = {c.kwargs["payload"]["rule"]: c.kwargs["payload"] for c in emitted}
        assert set(by_rule) == {
            "stop_list",
            "document_frequency",
            "unnormalised_keyword",
        }
        assert by_rule["stop_list"]["keyword"] == "for"
        assert all(c.kwargs["entity_type"] == "advisory" for c in emitted)
        # The dashboard averages ``new_confidence`` over fitness events.
        assert all("new_confidence" not in c.kwargs["payload"] for c in emitted)

    def test_a_failing_event_log_does_not_fail_the_run(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        event_log.emit.side_effect = RuntimeError("log down")

        AdvisoryGenerator(event_log, store).generate(days=30)

        row = store.get(AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "for"))
        assert row is not None
        assert row.status == AdvisoryStatus.SUPPRESSED

    def test_a_second_run_retires_nothing_more(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()
        self._generate(store, packs, feedback)

        report, event_log = self._generate(store, packs, feedback)

        assert sum(report.query_rows_retired.values()) == 0  # type: ignore[attr-defined]
        assert not event_log.emit.called

    def test_other_categories_and_fitness_suppressions_are_left_alone(
        self, tmp_path: Path
    ) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        entity = _query_row("for").model_copy(
            update={"category": AdvisoryCategory.ENTITY, "advisory_id": "ent-1"}
        )
        fitness_suppressed = _query_row("with")
        store.put_many([entity, fitness_suppressed])
        store.suppress(fitness_suppressed.advisory_id, reason="fitness: weak")
        packs, feedback = self._corpus()

        _, event_log = self._generate(store, packs, feedback)

        assert store.get("ent-1").status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        kept = store.get(fitness_suppressed.advisory_id)
        assert kept is not None
        assert kept.suppression_reason == "fitness: weak"
        assert kept.retired_by is None
        assert not event_log.emit.called

    def test_a_row_absent_from_the_window_is_not_retired_on_frequency(
        self, tmp_path: Path
    ) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_query_row("elsewhere"))
        packs, feedback = self._corpus()

        self._generate(store, packs, feedback)

        row = store.get(
            AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "elsewhere")
        )
        assert row is not None
        assert row.status == AdvisoryStatus.ACTIVE

    def test_raising_the_ceiling_puts_a_retired_row_back(self, tmp_path: Path) -> None:
        store = self._seeded(tmp_path)
        packs, feedback = self._corpus()
        self._generate(store, packs, feedback)
        rollout = AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "rollout")
        assert store.get(rollout).status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

        report, event_log = self._generate(
            store, packs, feedback, max_keyword_doc_freq=0.5
        )

        row = store.get(rollout)
        assert row is not None
        assert row.status == AdvisoryStatus.ACTIVE
        assert row.suppression_reason is None
        assert row.retired_by is None
        assert RETIRED_BY_KEY not in row.metadata
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]
        restored = [
            c
            for c in event_log.emit.call_args_list
            if c.args[0] == EventType.ADVISORY_RESTORED
        ]
        assert [c.kwargs["entity_id"] for c in restored] == [rollout]

    def test_a_fitness_suppression_is_still_carried_forward(
        self, tmp_path: Path
    ) -> None:
        """Only a row this filter retired is un-retired on re-emission."""
        packs, feedback = _intent_corpus(6, 24, carrier_intent="widget")
        store = AdvisoryStore(tmp_path / "adv.json")
        self._generate(store, packs, feedback)
        widget = AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "widget")
        store.suppress(widget, reason="fitness: weak")

        report, _ = self._generate(store, packs, feedback)

        assert store.get(widget).status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]
        assert report.query_rows_unretired == 0  # type: ignore[attr-defined]


class TestDocumentFrequencyRetirementIsStable:
    """Review of #965 — a frequency retirement must not flap on a noisy share.

    The document-frequency rule judges a nightly measurement of the task mix,
    so a row it retires comes back only on a clear signal: the ceiling was
    enforced tonight and either raised, or the word's share fell to
    :data:`_DF_UNRETIRE_FRACTION` of it. A night too short to enforce the
    ceiling is no signal at all. Every night below emits the word, so under
    "emitted means un-retire" each would have flipped the row.
    """

    ID = AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "rollout")

    @staticmethod
    def _night(
        store: AdvisoryStore, carriers: int, others: int, **kwargs: object
    ) -> tuple[object, MagicMock]:
        packs, feedback = _intent_corpus(carriers, others, carrier_intent="rollout")
        return TestQueryRowRetirement._generate(store, packs, feedback, **kwargs)

    def _retired(
        self, tmp_path: Path, carriers: int = 12, others: int = 18
    ) -> AdvisoryStore:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_query_row("rollout"))
        self._night(store, carriers, others)  # over the 0.25 ceiling
        row = store.get(self.ID)
        assert row is not None
        assert row.retired_by == RETIRED_BY_QUERY_FILTER
        return store

    def _assert_still_retired(self, store: AdvisoryStore, report: object) -> None:
        row = store.get(self.ID)
        assert row is not None
        assert row.status == AdvisoryStatus.SUPPRESSED
        assert row.retired_by == RETIRED_BY_QUERY_FILTER
        # The marker is carried across the regenerated row, not dropped by it.
        assert row.metadata[RETIRED_RULE_KEY] == "document_frequency"
        assert row.metadata[RETIRED_CEILING_KEY] == SUGGESTED_MAX_KEYWORD_DOC_FREQ
        assert report.query_rows_unretired == 0  # type: ignore[attr-defined]

    def test_a_retirement_records_its_rule_and_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        store = TestQueryRowRetirement()._seeded(tmp_path)
        packs, feedback = TestQueryRowRetirement()._corpus()
        TestQueryRowRetirement._generate(store, packs, feedback)

        def meta(keyword: str) -> dict[str, object]:
            row = store.get(
                AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, keyword)
            )
            assert row is not None
            return {k: row.metadata[k] for k in RETIREMENT_KEYS if k in row.metadata}

        base = {RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER}
        assert meta("for") == {**base, RETIRED_RULE_KEY: "stop_list"}
        assert meta("widget,") == {**base, RETIRED_RULE_KEY: "unnormalised_keyword"}
        assert meta("rollout") == {
            **base,
            RETIRED_RULE_KEY: "document_frequency",
            RETIRED_CEILING_KEY: SUGGESTED_MAX_KEYWORD_DOC_FREQ,
        }

    def test_a_short_night_does_not_un_retire(self, tmp_path: Path) -> None:
        """30 packs at 40%, then 12 at 50%: the ceiling stands down on the 12."""
        store = self._retired(tmp_path)

        for _ in range(3):
            short_report, short_log = self._night(store, 6, 6)
            assert short_report.advisories_generated == 1  # type: ignore[attr-defined]
            assert short_report.query_keyword_df_ceiling is None  # type: ignore[attr-defined]
            self._assert_still_retired(store, short_report)
            assert not short_log.emit.called

            long_report, long_log = self._night(store, 12, 18)
            self._assert_still_retired(store, long_report)
            assert not long_log.emit.called

    def test_a_raised_ceiling_waits_for_a_night_that_can_enforce_it(
        self, tmp_path: Path
    ) -> None:
        """12 packs at a 0.3 ceiling cannot admit a word: no measurement, no restore.

        The share test alone never holds a row on a short night, because a word
        that earned a row has at least ``min_sample_size`` carriers and so a
        share above the ceiling there. The one way a short night could restore
        is the raised-ceiling clause, which the stand-down guard must veto.
        """
        store = self._retired(tmp_path)

        report, event_log = self._night(store, 6, 6, max_keyword_doc_freq=0.3)

        assert report.query_keyword_df_ceiling is None  # type: ignore[attr-defined]
        assert report.advisories_generated == 1  # type: ignore[attr-defined]
        row = store.get(self.ID)
        assert row is not None
        assert row.retired_by == RETIRED_BY_QUERY_FILTER
        assert not event_log.emit.called

        report, _ = self._night(store, 12, 18, max_keyword_doc_freq=0.5)  # full night

        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]

    def test_a_share_hovering_at_the_ceiling_does_not_flap(
        self, tmp_path: Path
    ) -> None:
        """23% and 27% against a 0.25 ceiling: retired once, then left alone."""
        store = self._retired(tmp_path, carriers=8, others=22)  # 26.7%

        for _ in range(3):
            under_report, under_log = self._night(store, 7, 23)  # 23.3%
            assert under_report.advisories_generated == 1  # type: ignore[attr-defined]
            self._assert_still_retired(store, under_report)
            assert not under_log.emit.called

            over_report, over_log = self._night(store, 8, 22)
            self._assert_still_retired(store, over_report)
            assert not over_log.emit.called

    def test_a_share_well_under_the_ceiling_un_retires(self, tmp_path: Path) -> None:
        store = self._retired(tmp_path)

        report, event_log = self._night(store, 6, 34)  # 15% of 40

        row = store.get(self.ID)
        assert row is not None
        assert row.status == AdvisoryStatus.ACTIVE
        assert row.suppression_reason is None
        assert not any(k in row.metadata for k in RETIREMENT_KEYS)
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]
        restored = [
            c
            for c in event_log.emit.call_args_list
            if c.args[0] == EventType.ADVISORY_RESTORED
        ]
        assert [c.kwargs["entity_id"] for c in restored] == [self.ID]

    def test_the_bar_is_at_or_below_eighty_percent_of_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        """6 of 30 is exactly 0.8 * 0.25; 7 of 30 is not."""
        store = self._retired(tmp_path)
        self._night(store, 7, 23)
        assert store.get(self.ID).status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

        report, _ = self._night(store, 6, 24)

        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]

    def test_an_un_retired_row_is_not_retired_again_inside_the_band(
        self, tmp_path: Path
    ) -> None:
        """Back at 23% after the 15% night: under the ceiling, so it stays active."""
        store = self._retired(tmp_path)
        self._night(store, 6, 34)
        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]

        report, event_log = self._night(store, 7, 23)

        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert not event_log.emit.called
        assert sum(report.query_rows_retired.values()) == 0  # type: ignore[attr-defined]

    def test_a_raised_ceiling_restores_even_inside_the_band(
        self, tmp_path: Path
    ) -> None:
        """26.7% is over 0.8 * 0.3, so only the raised ceiling can restore it."""
        store = self._retired(tmp_path, carriers=8, others=22)

        report, _ = self._night(store, 8, 22, max_keyword_doc_freq=0.3)

        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]

    def test_a_row_with_no_recorded_rule_gets_the_strict_test(
        self, tmp_path: Path
    ) -> None:
        """A retirement stored before the rule was recorded: no flapping."""
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(_query_row("rollout"))
        store.suppress(
            self.ID,
            reason="query keyword excluded by document_frequency: 'rollout'",
            metadata={RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER},
        )

        for carriers, others in ((6, 6), (7, 23)):  # short night, then in-band
            report, event_log = self._night(store, carriers, others)
            row = store.get(self.ID)
            assert row is not None
            assert row.retired_by == RETIRED_BY_QUERY_FILTER
            assert report.query_rows_unretired == 0  # type: ignore[attr-defined]
            assert not event_log.emit.called

        report, _ = self._night(store, 6, 34)

        assert store.get(self.ID).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]

    def test_a_relaxed_stop_list_restores_without_the_frequency_margin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop-list retirements keep "emitted again means the rule is gone"."""
        store = AdvisoryStore(tmp_path / "adv.json")
        word_id = AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "rollout")
        store.put(_query_row("rollout"))
        monkeypatch.setattr(
            "trellis.retrieve.advisory_generator.QUERY_STOP_WORDS",
            frozenset({"rollout"}),
        )
        self._night(store, 7, 23)
        row = store.get(word_id)
        assert row is not None
        assert row.metadata[RETIRED_RULE_KEY] == "stop_list"
        assert RETIRED_CEILING_KEY not in row.metadata

        monkeypatch.setattr(
            "trellis.retrieve.advisory_generator.QUERY_STOP_WORDS", frozenset()
        )
        report, _ = self._night(store, 7, 23)  # 23%: inside the frequency band

        assert store.get(word_id).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]

    def test_the_first_night_retires_in_one_write_and_emits_per_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retirement is batched; every retired row still gets its audit event."""
        store = TestQueryRowRetirement()._seeded(tmp_path)
        packs, feedback = TestQueryRowRetirement()._corpus()
        calls: list[int] = []
        real = store.suppress_many
        monkeypatch.setattr(
            store,
            "suppress_many",
            lambda items: (calls.append(len(items)), real(items))[1],
        )

        _, event_log = TestQueryRowRetirement._generate(store, packs, feedback)

        assert calls == [3]  # for, widget, rollout: one call
        suppressed = [
            c.kwargs["entity_id"]
            for c in event_log.emit.call_args_list
            if c.args[0] == EventType.ADVISORY_SUPPRESSED
        ]
        assert len(suppressed) == 3


class TestDocumentFrequencyRuleShipsOff:
    """The frequency rule is off by default; the stop-list is not.

    At 0.25 the rule would also retire domain nouns that still have a 75%
    comparison arm, and nobody has counted on production data how many rows
    that is. So a default run measures it (the preview) and retires nothing on
    frequency grounds, while the stop-list and unnormalised-keyword rules stay on.
    """

    def test_the_default_is_off(self) -> None:
        assert DEFAULT_MAX_KEYWORD_DOC_FREQ is None
        assert SUGGESTED_MAX_KEYWORD_DOC_FREQ == 0.25

    def test_a_default_run_retires_nothing_on_frequency_but_the_stop_list_still_does(
        self, tmp_path: Path
    ) -> None:
        store = TestQueryRowRetirement()._seeded(tmp_path)
        packs, feedback = TestQueryRowRetirement()._corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        report = AdvisoryGenerator(event_log, store).generate(days=30)

        by_keyword = {
            a.metadata["keyword"]: a for a in store.list(include_suppressed=True)
        }
        # "rollout" is in 12 of 30 intents: over 0.25, and it is kept.
        assert by_keyword["rollout"].status == AdvisoryStatus.ACTIVE
        assert by_keyword["care"].status == AdvisoryStatus.ACTIVE
        assert by_keyword["for"].status == AdvisoryStatus.SUPPRESSED
        assert by_keyword["widget,"].status == AdvisoryStatus.SUPPRESSED
        assert report.query_rows_retired == {
            "stop_list": 1,
            "document_frequency": 0,
            "unnormalised_keyword": 1,
        }
        assert report.query_keywords_withheld["document_frequency"] == 0
        # The report says the ceiling is off rather than leaving a bare null.
        assert report.query_keyword_df_ceiling is None
        assert report.query_keyword_df_rule == "off"

    def test_the_stop_list_withholds_with_the_rule_off(self, tmp_path: Path) -> None:
        packs, feedback = _intent_corpus(6, 24, carrier_intent="widget for rollout")
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        report = AdvisoryGenerator(
            event_log, AdvisoryStore(tmp_path / "a.json")
        ).generate(days=30)

        assert report.query_keywords_withheld == {
            "stop_list": 1,
            "document_frequency": 0,
        }

    def test_the_preview_counts_what_the_suggested_ceiling_would_do(
        self, tmp_path: Path
    ) -> None:
        """Off, the run reports the figure an enforced run would act on."""
        packs, feedback = TestQueryRowRetirement()._corpus()
        (tmp_path / "off").mkdir()
        (tmp_path / "on").mkdir()
        off_store = TestQueryRowRetirement()._seeded(tmp_path / "off")
        on_store = TestQueryRowRetirement()._seeded(tmp_path / "on")
        off_report, _ = TestQueryRowRetirement._generate(
            off_store, packs, feedback, max_keyword_doc_freq=None
        )
        on_report, _ = TestQueryRowRetirement._generate(on_store, packs, feedback)

        preview = off_report.query_keyword_df_preview  # type: ignore[attr-defined]
        assert preview == {
            "ceiling": SUGGESTED_MAX_KEYWORD_DOC_FREQ,
            "keywords_would_withhold": on_report.query_keywords_withheld[  # type: ignore[attr-defined]
                "document_frequency"
            ],
            "rows_would_retire": on_report.query_rows_retired["document_frequency"],  # type: ignore[attr-defined]
        }
        # Non-vacuous: the corpus has a word over the ceiling and a row on it.
        assert preview["keywords_would_withhold"] >= 1
        assert preview["rows_would_retire"] == 1
        # Measured, not acted on.
        assert (
            off_store.get(  # type: ignore[union-attr]
                AdvisoryGenerator._stable_id(AdvisoryCategory.QUERY, "rollout")
            ).status
            == AdvisoryStatus.ACTIVE
        )

    def test_there_is_no_preview_when_enforced_or_when_the_window_is_too_small(
        self, tmp_path: Path
    ) -> None:
        packs, feedback = TestQueryRowRetirement()._corpus()
        enforced, _ = TestQueryRowRetirement._generate(
            AdvisoryStore(tmp_path / "e.json"), packs, feedback
        )
        assert enforced.query_keyword_df_preview is None  # type: ignore[attr-defined]

        short_packs, short_feedback = _intent_corpus(6, 6, carrier_intent="widget")
        short, _ = TestQueryRowRetirement._generate(
            AdvisoryStore(tmp_path / "s.json"),
            short_packs,
            short_feedback,
            max_keyword_doc_freq=None,
        )
        assert short.query_keyword_df_preview is None  # type: ignore[attr-defined]
        assert short.query_keyword_df_rule == "off"  # type: ignore[attr-defined]

    def test_dropping_the_ceiling_puts_a_frequency_retired_row_back(
        self, tmp_path: Path
    ) -> None:
        """Retired under 0.25, then the operator runs with the rule off."""
        store = TestDocumentFrequencyRetirementIsStable()._retired(tmp_path)
        row_id = TestDocumentFrequencyRetirementIsStable.ID
        assert store.get(row_id).status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

        report, _ = TestDocumentFrequencyRetirementIsStable._night(
            store, 12, 18, max_keyword_doc_freq=None
        )

        assert store.get(row_id).status == AdvisoryStatus.ACTIVE  # type: ignore[union-attr]
        assert report.query_rows_unretired == 1  # type: ignore[attr-defined]

    def test_a_bad_ceiling_is_still_refused_and_none_is_not(self) -> None:
        import pytest

        AdvisoryGenerator(
            MagicMock(spec=EventLog), MagicMock(), max_keyword_doc_freq=None
        )
        for bad in (0.0, 1.5, float("nan")):
            with pytest.raises(ValueError, match="max_keyword_doc_freq"):
                AdvisoryGenerator(
                    MagicMock(spec=EventLog), MagicMock(), max_keyword_doc_freq=bad
                )


class TestFitnessLoopLeavesRetiredRowsAlone:
    """#960 — a retirement must survive the fitness loop that follows it.

    Nightly curation runs generate then the loop. Without the guard the loop
    scores the retired row (it was served, and it is healthy), blends its
    confidence to well over the restore bar, and revives it that same night.
    """

    @staticmethod
    def _log(advisory_id: str) -> MagicMock:
        packs = []
        feedback = []
        for i in range(4):
            packs.append(
                _event(
                    EventType.PACK_ASSEMBLED,
                    f"p{i}",
                    {
                        "injected_item_ids": [],
                        "injected_items": [],
                        "domain": None,
                        "intent": "",
                        "advisory_ids": [advisory_id],
                    },
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=True))
        return TestDemotionRemainsReachable._event_log(packs, feedback)

    def test_a_retired_row_is_not_restored(self, tmp_path: Path) -> None:
        store = AdvisoryStore(tmp_path / "adv.json")
        row = store.put(_query_row("for", confidence=0.6))
        store.suppress(
            row.advisory_id,
            reason="query keyword excluded by stop_list: 'for'",
            metadata={RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER},
        )

        report = run_advisory_fitness_loop(self._log(row.advisory_id), store, days=30)

        after = store.get(row.advisory_id)
        assert after is not None
        assert after.status == AdvisoryStatus.SUPPRESSED
        assert report.advisories_restored == []
        assert after.confidence == 0.6  # not even re-scored

    def test_a_fitness_suppressed_row_is_still_restored(self, tmp_path: Path) -> None:
        """The control: the guard is specific to retirement."""
        store = AdvisoryStore(tmp_path / "adv.json")
        row = store.put(_query_row("widget", confidence=0.6))
        store.suppress(row.advisory_id, reason="fitness: weak")

        report = run_advisory_fitness_loop(self._log(row.advisory_id), store, days=30)

        assert report.advisories_restored == [row.advisory_id]

    def test_an_operator_restored_row_is_scored_again(self, tmp_path: Path) -> None:
        """``restore`` clears the retirement, so the loop owns the row again."""
        store = AdvisoryStore(tmp_path / "adv.json")
        row = store.put(_query_row("for", confidence=0.6))
        store.suppress(
            row.advisory_id,
            reason="r",
            metadata={RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER},
        )
        restored = store.restore(row.advisory_id)

        assert restored is not None
        assert RETIRED_BY_KEY not in restored.metadata
        assert restored.retired_by is None
        run_advisory_fitness_loop(self._log(row.advisory_id), store, days=30)
        after = store.get(row.advisory_id)
        assert after is not None
        assert after.fitness_scored_at is not None

    def test_a_fitness_suppression_after_a_restore_is_not_a_retirement(
        self, tmp_path: Path
    ) -> None:
        """Retire, restore, then a *fitness* suppression: the loop still owns it.

        The marker used to survive ``restore``, so the later ``suppress`` kept
        it, ``retired_by`` read the fitness suppression as a retirement, and
        the loop skipped the row for good (and the generator un-retired it).
        """
        store = AdvisoryStore(tmp_path / "adv.json")
        row = store.put(_query_row("widget", confidence=0.6))
        store.suppress(
            row.advisory_id,
            reason="query keyword excluded by document_frequency: 'widget'",
            metadata={
                RETIRED_BY_KEY: RETIRED_BY_QUERY_FILTER,
                RETIRED_RULE_KEY: "document_frequency",
                RETIRED_CEILING_KEY: 0.25,
            },
        )
        store.restore(row.advisory_id)
        store.suppress(row.advisory_id, reason="fitness: weak")

        suppressed = store.get(row.advisory_id)
        assert suppressed is not None
        assert suppressed.status == AdvisoryStatus.SUPPRESSED
        assert suppressed.retired_by is None
        assert not any(k in suppressed.metadata for k in RETIREMENT_KEYS)
        report = run_advisory_fitness_loop(self._log(row.advisory_id), store, days=30)
        assert report.advisories_restored == [row.advisory_id]
