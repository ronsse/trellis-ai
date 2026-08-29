"""Tests for AdvisoryGenerator."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from structlog.testing import capture_logs

from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import run_advisory_fitness_loop
from trellis.schemas.advisory import AdvisoryCategory, AdvisoryStatus
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
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
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

        Seven: the ``ubiquitous`` and ``narrow`` strategies, plus the five
        words of the shared intent, every one of which is on all 20 packs.
        It is the number that distinguishes "the analysis found nothing"
        from "the analysis found things it could not attribute" — and on
        this corpus almost everything is the latter.
        """
        packs, feedback = _two_arm_corpus()
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]
        gen = AdvisoryGenerator(event_log, AdvisoryStore(tmp_path / "adv.json"))

        report = gen.generate(days=30)

        assert report.advisories_generated == 1
        assert report.findings_refused_no_comparison_arm == 7

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
        assert report.store_degradation["recovery"] == f"mv {path} {path}.corrupt"

        lines = [
            e
            for e in logs
            if e["event"] == "advisory_generation_refused_degraded_store"
        ]
        assert len(lines) == 1
        assert lines[0]["log_level"] == "error"
        assert lines[0]["recovery"] == f"mv {path} {path}.corrupt"

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
