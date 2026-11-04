"""The offline relabel report, against counts worked out by hand.

The scenario and its hand-computed table live in ``_intent_family_seed``. The
report must agree with that table, withhold a family's rates below the floor of
20 graded packs, drop a held-out pack and the feedback naming it, and never
carry an intent string in anything it renders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.learning import _intent_family_seed as seed_mod
from trellis.learning.intent_families import (
    MIN_GRADED_PACKS,
    STATUS_OK,
    STATUS_TRUNCATED,
    IntentFamilyReport,
    RuleSet,
    load_rule_set,
    pack_bootstrap_interval,
    render_markdown,
    summarize_intent_families,
)
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield event_log
    event_log.close()


def _report(log: EventLog, **kwargs: object) -> IntentFamilyReport:
    return summarize_intent_families(log, days=30, until=seed_mod.UNTIL, **kwargs)  # type: ignore[arg-type]


def _by_family(report: IntentFamilyReport) -> dict[str, object]:
    return {row.family: row for row in report.families}


class TestAgainstHandComputedCounts:
    def test_population_and_fallback(self, log: EventLog) -> None:
        seed_mod.seed(log)

        report = _report(log)

        assert report.status == STATUS_OK
        assert report.empty_reason == ""
        # 38 packs with item rows, one without; ``old`` and ``held`` are out.
        assert report.packs_served == 39
        assert report.packs_analyzed == 38
        assert report.packs_without_item_rows == 1
        assert report.packs_without_intent == 1
        assert report.fallback_packs == 5
        assert report.fallback_share == pytest.approx(5 / 38, abs=1e-4)
        # ``gen-0`` is stamped with a family the legacy map does not return.
        assert report.legacy_stamp_differs == 1
        assert report.rule_set.source == "default"
        assert report.interval_method == "pack_bootstrap_95"
        assert report.bootstrap_resamples == 2000
        assert report.sectioned_packs_without_rows == 0
        assert report.cited_not_served == 0
        assert report.rule_set.fallback == "general"
        assert report.rule_set.rules == 17

    def test_packs_per_new_family(self, log: EventLog) -> None:
        seed_mod.seed(log)

        rows = _by_family(_report(log))

        assert {name: row.packs for name, row in rows.items()} == {
            "review": 5,
            "bugfix": 20,
            "performance": 0,
            "refactor": 0,
            "investigate": 0,
            "data": 2,
            "docs": 2,
            "maintenance": 0,
            "ops": 1,
            "test": 3,
            "feature": 0,
            "design": 0,
            "general": 5,
        }

    def test_the_family_past_the_floor_states_its_rates(self, log: EventLog) -> None:
        seed_mod.seed(log)

        bugfix = _by_family(_report(log))["bugfix"]

        assert bugfix.graded_packs == 20
        assert bugfix.helpful_citations == 40
        assert bugfix.unhelpful_citations == 20
        assert bugfix.withheld_reason == ""
        assert bugfix.helpful_rate == pytest.approx(40 / 60, abs=1e-4)
        # The interval resamples the 20 packs, which cite 2/3, 3/3 and 1/3 of
        # their items as helpful (ten, five and five packs); the pair list is
        # worked by hand from the seed, not read back from the report.
        pairs = [(2, 3)] * 10 + [(3, 3)] * 5 + [(1, 3)] * 5
        low, high = pack_bootstrap_interval(pairs) or (None, None)
        assert bugfix.helpful_rate_low == pytest.approx(low, abs=1e-4)
        assert bugfix.helpful_rate_high == pytest.approx(high, abs=1e-4)
        assert 0.5 < bugfix.helpful_rate_low < 40 / 60 < bugfix.helpful_rate_high < 0.85
        assert bugfix.cited_not_served == 0
        assert bugfix.rated_packs == 20
        assert bugfix.mean_rating == pytest.approx(0.75, abs=1e-4)
        assert [(r.rule_id, r.packs) for r in bugfix.top_rules] == [
            ("bugfix.fix", 17),
            ("bugfix.defect", 3),
        ]

    def test_a_family_below_the_floor_withholds_every_rate_and_says_why(
        self, log: EventLog
    ) -> None:
        seed_mod.seed(log)

        rows = _by_family(_report(log))
        review = rows["review"]

        assert review.graded_packs == 5
        # ``rev-0``'s two events name one item, so the union is five citations.
        assert review.helpful_citations == 5
        assert review.unhelpful_citations == 0
        assert review.helpful_rate is None
        assert review.helpful_rate_low is None
        assert review.helpful_rate_high is None
        assert review.mean_rating is None
        assert review.withheld_reason == (
            f"5 graded packs, below the floor of {MIN_GRADED_PACKS}"
        )
        # A family with packs and no grading says so as well.
        assert rows["test"].graded_packs == 0
        assert rows["test"].withheld_reason.startswith("0 graded packs")
        # A rating alone grades a pack, and cites nothing.
        assert rows["docs"].graded_packs == 1
        assert rows["docs"].helpful_citations == 0
        assert rows["docs"].rated_packs == 1

    def test_legacy_by_new_crosstab(self, log: EventLog) -> None:
        seed_mod.seed(log)

        cells = {(c.legacy, c.new): c.packs for c in _report(log).crosstab}

        assert cells == {
            ("general_context", "bugfix"): 20,
            ("general_context", "docs"): 2,
            ("general_context", "general"): 4,
            ("general_context", "ops"): 1,
            ("general_context", "review"): 5,
            ("validation_diagnostics", "data"): 2,
            ("validation_diagnostics", "general"): 1,
            ("validation_diagnostics", "test"): 3,
        }
        assert sum(cells.values()) == 38

    def test_the_fallback_family_names_its_rule(self, log: EventLog) -> None:
        seed_mod.seed(log)

        general = _by_family(_report(log))["general"]

        assert [(r.rule_id, r.packs) for r in general.top_rules] == [("fallback", 5)]


class TestFloor:
    @pytest.mark.parametrize(
        ("graded", "stated"), [(19, False), (20, True), (21, True)]
    )
    def test_the_floor_is_twenty_graded_packs(
        self, log: EventLog, graded: int, stated: bool
    ) -> None:
        for i in range(graded):
            seed_mod.pack(log, f"p{i}", "fix the loader")
            seed_mod.grade(log, f"p{i}", helpful=1, unhelpful=1, rating=0.5)

        bugfix = _by_family(_report(log))["bugfix"]

        assert bugfix.graded_packs == graded
        assert (bugfix.helpful_rate is not None) is stated
        assert (bugfix.withheld_reason == "") is stated

    def test_a_graded_family_with_no_citations_says_so(self, log: EventLog) -> None:
        for i in range(MIN_GRADED_PACKS):
            seed_mod.pack(log, f"p{i}", "fix the loader")
            seed_mod.grade(log, f"p{i}", rating=0.9)

        bugfix = _by_family(_report(log))["bugfix"]

        assert bugfix.helpful_rate is None
        assert "no helpful or unhelpful citation of a served item" in (
            bugfix.withheld_reason
        )
        assert bugfix.mean_rating == pytest.approx(0.9, abs=1e-4)


class TestHoldout:
    def test_a_held_out_pack_and_its_feedback_change_nothing(
        self, log: EventLog, tmp_path: Path
    ) -> None:
        seed_mod.seed(log, holdout=True)
        other = SQLiteEventLog(tmp_path / "without.db")
        try:
            seed_mod.seed(other, holdout=False)
            with_held = _report(log).model_dump(mode="json")
            without = _report(other).model_dump(mode="json")
        finally:
            other.close()

        # The scan block counts raw reads, which the held-out rows do inflate;
        # every figure the report states is identical.
        for report in (with_held, without):
            report.pop("scan")
        assert with_held == without
        ops = {row["family"]: row for row in with_held["families"]}["ops"]
        assert ops["packs"] == 1
        assert ops["graded_packs"] == 0

    def test_a_window_of_only_held_out_packs_is_empty(self, log: EventLog) -> None:
        seed_mod.pack(log, "held", "deploy the stack", holdout=True)
        seed_mod.feedback(log, "held", helpful=("doc:held:0",), rating=0.1)

        report = _report(log)

        assert report.packs_served == 0
        assert report.empty_reason == "no served packs in the window"


class TestEmptyAndTruncated:
    def test_an_empty_store_is_a_report_with_a_reason(self, log: EventLog) -> None:
        report = _report(log)

        assert report.status == STATUS_OK
        assert report.empty_reason == "no served packs in the window"
        assert report.packs_analyzed == 0
        assert report.fallback_share is None
        assert report.crosstab == []
        assert all(row.packs == 0 for row in report.families)

    def test_packs_without_item_rows_are_a_distinct_reason(self, log: EventLog) -> None:
        seed_mod.pack(log, "bare", "fix the loader", rows=False)

        report = _report(log)

        assert report.packs_served == 1
        assert report.empty_reason == "no served pack in the window carries item rows"

    def test_a_scan_cap_is_stated_not_hidden(self, log: EventLog) -> None:
        seed_mod.seed(log)

        report = _report(log, limit=7)

        assert report.status == STATUS_TRUNCATED
        assert report.scan.truncated is True
        assert report.notes[-1] == report.scan.note

    def test_since_wins_over_days(self, log: EventLog) -> None:
        seed_mod.seed(log)

        report = summarize_intent_families(
            log,
            days=1,
            since=seed_mod.BEFORE_WINDOW.replace(day=1),
            until=seed_mod.UNTIL,
        )

        # The out-of-window pack now lies inside the window.
        assert report.packs_served == 40


class TestRuleSetOverride:
    def test_the_report_relabels_under_a_configured_set(self, log: EventLog) -> None:
        seed_mod.seed(log)
        rules: RuleSet = load_rule_set(
            {
                "intent_families": {
                    "fallback": "misc",
                    "rules": [
                        {
                            "id": "x.fix",
                            "family": "repairs",
                            "keywords": ["fix", "flaky"],
                        }
                    ],
                }
            }
        )

        report = _report(log, rule_set=rules)

        assert report.rule_set.source == "config"
        assert report.rule_set.families == ["repairs", "misc"]
        assert {row.family: row.packs for row in report.families} == {
            "repairs": 20,
            "misc": 18,
        }
        assert report.fallback_packs == 18


class TestNoIntentTextInAnyOutput:
    def test_the_sentinel_is_absent_from_json_and_markdown(self, log: EventLog) -> None:
        seed_mod.seed(log)
        seed_mod.seed_sentinel(log)

        report = _report(log)
        rendered = {
            "json": json.dumps(report.model_dump(mode="json")),
            "markdown": render_markdown(report),
        }

        # The sentinel pack is in the population, so this is not vacuous.
        assert report.packs_analyzed == 39
        assert _by_family(report)["bugfix"].packs == 21
        for name, text in rendered.items():
            assert seed_mod.SENTINEL not in text.lower(), name
        assert "bugfix" in rendered["markdown"]


def _value_report(log: EventLog):  # type: ignore[no-untyped-def]
    return summarize_pack_value(log, days=30, until=seed_mod.UNTIL)


class TestCitationsJoinAsValueDoes:
    """25 packs: a served id cited helpful, a stray id, then the served id unhelpful."""

    def _seed(self, log: EventLog, packs: int = 25) -> None:
        for i in range(packs):
            seed_mod.pack(log, f"p{i}", "fix the parser")
            seed_mod.feedback(
                log,
                f"p{i}",
                helpful=(seed_mod.item(f"p{i}", 0), "doc:never-served"),
                rating=1.0,
            )
            seed_mod.feedback(
                log, f"p{i}", unhelpful=(seed_mod.item(f"p{i}", 0),), rating=0.0
            )

    def test_only_served_ids_count_and_helpful_wins(self, log: EventLog) -> None:
        self._seed(log)

        report = _report(log)
        bugfix = _by_family(report)["bugfix"]

        assert bugfix.graded_packs == 25
        assert bugfix.helpful_citations == 25
        assert bugfix.unhelpful_citations == 0
        assert bugfix.cited_not_served == 25
        assert report.cited_not_served == 25
        assert bugfix.helpful_rate == 1.0
        assert (bugfix.helpful_rate_low, bugfix.helpful_rate_high) == (1.0, 1.0)
        assert any("25 cited id(s) were not served" in note for note in report.notes)

    def test_the_counts_agree_with_analyze_value_on_the_same_log(
        self, log: EventLog
    ) -> None:
        self._seed(log)

        bugfix = _by_family(_report(log))["bugfix"]
        value = _value_report(log)

        # Every seeded item is served at 10 estimated tokens.
        assert value.cited_ids_not_served == bugfix.cited_not_served == 25
        assert value.distinct_helpful_items == bugfix.helpful_citations
        assert value.helpful_tokens == 10 * bugfix.helpful_citations
        assert value.unhelpful_tokens == 10 * bugfix.unhelpful_citations

    def test_an_unserved_id_alone_grades_no_rate(self, log: EventLog) -> None:
        for i in range(MIN_GRADED_PACKS):
            seed_mod.pack(log, f"p{i}", "fix the parser")
            seed_mod.feedback(log, f"p{i}", helpful=("doc:never-served",))

        bugfix = _by_family(_report(log))["bugfix"]

        assert bugfix.cited_not_served == MIN_GRADED_PACKS
        assert bugfix.helpful_citations == bugfix.unhelpful_citations == 0
        assert bugfix.helpful_rate is None
        assert "no helpful or unhelpful citation" in bugfix.withheld_reason


class TestFeedbackAndSectionedCounting:
    def test_one_feedback_id_recorded_twice_is_one_vote(self, log: EventLog) -> None:
        for i in range(MIN_GRADED_PACKS):
            seed_mod.pack(log, f"p{i}", "fix the loader")
            if i:
                seed_mod.grade(log, f"p{i}", helpful=1, rating=0.5)
        for _ in range(2):  # a replay of the same event
            seed_mod._append(
                log,
                EventType.FEEDBACK_RECORDED,
                at=seed_mod.AT,
                entity_id="p0",
                payload={"pack_id": "p0", "feedback_id": "fb-1", "rating": 1.0},
            )
        seed_mod._append(
            log,
            EventType.FEEDBACK_RECORDED,
            at=seed_mod.AT,
            entity_id="p0",
            payload={"pack_id": "p0", "feedback_id": "fb-2", "rating": 0.0},
        )

        bugfix = _by_family(_report(log))["bugfix"]

        # p0 is (1.0 + 0.0) / 2 = 0.5, not (1.0 + 1.0 + 0.0) / 3.
        assert bugfix.rated_packs == MIN_GRADED_PACKS
        assert bugfix.mean_rating == pytest.approx(0.5, abs=1e-4)

    def test_a_sectioned_pack_without_rows_is_counted_by_name(
        self, log: EventLog
    ) -> None:
        seed_mod.pack(log, "flat-0", "fix the loader")
        seed_mod._append(
            log,
            EventType.PACK_ASSEMBLED,
            at=seed_mod.AT,
            entity_id="sec-0",
            payload={"intent": "fix x", "sections": [{"name": "a", "items": []}]},
        )

        report = _report(log)

        assert report.packs_served == 2
        assert report.packs_analyzed == 1
        assert report.packs_without_item_rows == 1
        assert report.sectioned_packs_without_rows == 1
        assert any("sectioned pack(s)" in note for note in report.notes)
