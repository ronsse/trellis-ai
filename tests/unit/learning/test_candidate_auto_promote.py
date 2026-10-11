"""Tests for :mod:`trellis.learning.candidate_auto_promote` (plan p1 PR 7, e177).

Report-only first: ``enabled=False`` / ``mode="report_only"`` ship as the
default, and the load-bearing claims under test are (1) a single-session
(n=1) candidate can never qualify no matter how it is configured, (2)
report-only truly never writes, (3) commit mode writes only through the
governed ``MutationExecutor``, never the ``EventLog`` directly, and (4) the
auto-demote sweep fires on a promoted candidate's first unhelpful citation.

``TestFloorRefusalOnConstruction`` mirrors
``tests/unit/learning/tuners/test_resolve_auto_promote_overrides.py``'s
sibling pattern: the resolver never validates (confirmed by grep — no
``settings_registry.validate_value`` exists anywhere in ``src/``), so the
only real enforcement point is
``LearningCandidateAutoPromotePolicy.__post_init__``, exercised here by
constructing the policy from a resolved dict, for both an env-sourced and a
settings-sourced override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.learning.candidate_auto_promote import (
    AUTO_PROMOTE_REQUESTED_BY,
    CANDIDATE_AUTO_PROMOTE_ENABLED_ENV,
    CANDIDATE_AUTO_PROMOTE_ENABLED_SETTING,
    CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV,
    CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_SETTING,
    CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV,
    CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_SETTING,
    CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV,
    CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING,
    CANDIDATE_AUTO_PROMOTE_MODE_ENV,
    CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_SETTING,
    DEFAULT_MAX_PROMOTIONS_PER_RUN,
    DEFAULT_MIN_DISTINCT_SESSIONS,
    DEFAULT_MIN_HELPFUL_COUNT,
    DEFAULT_REQUIRE_ZERO_UNHELPFUL,
    AgreementMetric,
    CandidateAutoPromoteOutcome,
    CandidateAutoPromoteReport,
    LearningCandidateAutoPromotePolicy,
    _evaluate_candidate,
    _holdout_arm_status,
    agreement_metric_to_dict,
    compute_agreement_metric,
    report_to_dict,
    resolve_candidate_auto_promote_overrides,
    run_candidate_auto_promotion,
    sweep_auto_demotions,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog


def _candidate(
    candidate_id: str = "cand-1",
    *,
    recommendation_type: str = "promote_precedent",
    helpful_count: int = 5,
    distinct_helpful_sessions: int = 3,
    unhelpful_count: int = 0,
    target_entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "recommendation_type": recommendation_type,
        "precedent_name": f"Precedent for {candidate_id}",
        "citation_evidence": {
            "helpful_count": helpful_count,
            "distinct_helpful_sessions": distinct_helpful_sessions,
            "unhelpful_count": unhelpful_count,
        },
        "target_entity_ids": target_entity_ids or [],
    }


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


class TestPolicyFloors:
    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="report_only"):
            LearningCandidateAutoPromotePolicy(mode="bogus")  # type: ignore[arg-type]

    def test_min_helpful_count_floor(self) -> None:
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(min_helpful_count=0)

    def test_min_distinct_sessions_floor_refuses_n_of_1(self) -> None:
        """The brief's explicit requirement: a single-session candidate must
        never be configurable into qualifying, at the construction seam."""
        with pytest.raises(ValueError, match="single-session"):
            LearningCandidateAutoPromotePolicy(min_distinct_sessions=1)

    def test_max_promotions_per_run_floor(self) -> None:
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(max_promotions_per_run=0)

    def test_floor_boundary_values_are_accepted(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(
            min_helpful_count=1, min_distinct_sessions=2, max_promotions_per_run=1
        )
        assert policy.min_helpful_count == 1
        assert policy.min_distinct_sessions == 2
        assert policy.max_promotions_per_run == 1


class TestEffectiveMode:
    def test_disabled_is_always_report_only_even_when_configured_commit(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False, mode="commit")
        assert policy.effective_mode == "report_only"

    def test_enabled_commit_stays_commit(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
        assert policy.effective_mode == "commit"

    def test_enabled_report_only_stays_report_only(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="report_only")
        assert policy.effective_mode == "report_only"


class TestResolveOverridesPrecedence:
    def test_default_when_nothing_set(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(env={})
        assert resolved == {
            "enabled": False,
            "mode": "report_only",
            "min_helpful_count": DEFAULT_MIN_HELPFUL_COUNT,
            "min_distinct_sessions": DEFAULT_MIN_DISTINCT_SESSIONS,
            "require_zero_unhelpful": DEFAULT_REQUIRE_ZERO_UNHELPFUL,
            "max_promotions_per_run": DEFAULT_MAX_PROMOTIONS_PER_RUN,
        }

    def test_settings_override_default(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING: 7}, env={}
        )
        assert resolved["min_helpful_count"] == 7

    def test_env_overrides_settings(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING: 7},
            env={CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV: "9"},
        )
        assert resolved["min_helpful_count"] == 9

    def test_enabled_env_truthy_values(self) -> None:
        for raw in ("1", "true", "YES", "on"):
            resolved = resolve_candidate_auto_promote_overrides(
                env={CANDIDATE_AUTO_PROMOTE_ENABLED_ENV: raw}
            )
            assert resolved["enabled"] is True

    def test_enabled_settings_override(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_ENABLED_SETTING: True}, env={}
        )
        assert resolved["enabled"] is True

    def test_mode_env_override(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            env={CANDIDATE_AUTO_PROMOTE_MODE_ENV: "commit"}
        )
        assert resolved["mode"] == "commit"

    def test_require_zero_unhelpful_settings_override_false(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_REQUIRE_ZERO_UNHELPFUL_SETTING: False},
            env={},
        )
        assert resolved["require_zero_unhelpful"] is False


class TestFloorRefusalOnConstruction:
    """The resolver never validates; construction does. Both a value
    arriving via settings and the same value arriving via env must still be
    refused once ``LearningCandidateAutoPromotePolicy`` is built from the
    resolved dict — there is no separate ``settings_registry.validate_value``
    path to test instead."""

    def test_looser_min_helpful_count_via_settings_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_SETTING: 0}, env={}
        )
        assert resolved["min_helpful_count"] == 0
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(**resolved)

    def test_looser_min_helpful_count_via_env_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            env={CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV: "0"}
        )
        assert resolved["min_helpful_count"] == 0
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(**resolved)

    def test_looser_min_distinct_sessions_via_settings_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_SETTING: 1}, env={}
        )
        assert resolved["min_distinct_sessions"] == 1
        with pytest.raises(ValueError, match="single-session"):
            LearningCandidateAutoPromotePolicy(**resolved)

    def test_looser_min_distinct_sessions_via_env_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            env={CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV: "1"}
        )
        assert resolved["min_distinct_sessions"] == 1
        with pytest.raises(ValueError, match="single-session"):
            LearningCandidateAutoPromotePolicy(**resolved)

    def test_looser_max_promotions_per_run_via_settings_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            settings={CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_SETTING: 0}, env={}
        )
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(**resolved)

    def test_looser_max_promotions_per_run_via_env_is_refused(self) -> None:
        resolved = resolve_candidate_auto_promote_overrides(
            env={CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV: "0"}
        )
        with pytest.raises(ValueError, match="looser than the floor"):
            LearningCandidateAutoPromotePolicy(**resolved)


class TestEvaluateCandidate:
    def test_qualifying_candidate_passes(self) -> None:
        policy = LearningCandidateAutoPromotePolicy()
        qualifies, reason = _evaluate_candidate(_candidate(), policy=policy)
        assert qualifies is True
        assert reason == "qualifies"

    def test_not_promotable_recommendation_type(self) -> None:
        policy = LearningCandidateAutoPromotePolicy()
        candidate = _candidate(recommendation_type="demote_noise")
        qualifies, reason = _evaluate_candidate(candidate, policy=policy)
        assert qualifies is False
        assert reason == "ineligible:not_promotable"

    def test_unhelpful_citation_disqualifies_by_default(self) -> None:
        policy = LearningCandidateAutoPromotePolicy()
        candidate = _candidate(unhelpful_count=1)
        qualifies, reason = _evaluate_candidate(candidate, policy=policy)
        assert qualifies is False
        assert reason == "ineligible:unhelpful_citations"

    def test_require_zero_unhelpful_false_loosens_that_check(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(require_zero_unhelpful=False)
        candidate = _candidate(unhelpful_count=1)
        qualifies, _reason = _evaluate_candidate(candidate, policy=policy)
        assert qualifies is True

    def test_insufficient_helpful_count(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(min_helpful_count=5)
        candidate = _candidate(helpful_count=4, distinct_helpful_sessions=3)
        qualifies, reason = _evaluate_candidate(candidate, policy=policy)
        assert qualifies is False
        assert reason == "ineligible:insufficient_helpful_count"

    def test_single_session_candidate_never_qualifies(self) -> None:
        """The brief's explicit n=1 requirement: a candidate whose helpful
        citations all come from one session must never qualify, no matter
        how high its helpful_count is or how loosely min_helpful_count is
        configured."""
        policy = LearningCandidateAutoPromotePolicy(min_helpful_count=1)
        candidate = _candidate(helpful_count=50, distinct_helpful_sessions=1)
        qualifies, reason = _evaluate_candidate(candidate, policy=policy)
        assert qualifies is False
        assert reason == "ineligible:insufficient_distinct_sessions"

    def test_not_promotable_takes_priority_over_unhelpful_citations(self) -> None:
        policy = LearningCandidateAutoPromotePolicy()
        candidate = _candidate(recommendation_type="demote_noise", unhelpful_count=5)
        _qualifies, reason = _evaluate_candidate(candidate, policy=policy)
        assert reason == "ineligible:not_promotable"


class TestHoldoutArmStatus:
    def test_always_unknown(self) -> None:
        assert _holdout_arm_status({}) == "unknown"
        assert _holdout_arm_status({"intent_family": "billing"}) == "unknown"


class TestRunCandidateAutoPromotionReportOnly:
    def test_disabled_policy_never_calls_executor(self, event_log: EventLog) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False, mode="commit")
        executor = MagicMock(spec=MutationExecutor)

        report = run_candidate_auto_promotion(
            candidates=[_candidate("cand-1")],
            policy=policy,
            event_log=event_log,
            executor=executor,
        )

        executor.execute.assert_not_called()
        assert report.mode == "report_only"
        assert report.configured_mode == "commit"
        assert report.would_promote_count == 1
        # holdout_arm is reported, never gating, on every outcome.
        assert report.outcomes[0].holdout_arm == "unknown"

    def test_report_only_mode_never_calls_executor(self, event_log: EventLog) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="report_only")
        executor = MagicMock(spec=MutationExecutor)

        report = run_candidate_auto_promotion(
            candidates=[_candidate()],
            policy=policy,
            event_log=event_log,
            executor=executor,
        )

        executor.execute.assert_not_called()
        assert report.would_promote_count == 1

    def test_force_report_only_overrides_configured_commit_mode(
        self, event_log: EventLog
    ) -> None:
        """``--dry-run``'s override: force_report_only=True wins even with
        enabled=True and mode="commit" both configured."""
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
        executor = MagicMock(spec=MutationExecutor)

        report = run_candidate_auto_promotion(
            candidates=[_candidate()],
            policy=policy,
            event_log=event_log,
            executor=executor,
            force_report_only=True,
        )

        executor.execute.assert_not_called()
        assert report.mode == "report_only"
        assert report.configured_mode == "commit"

    def test_executor_none_is_fine_in_report_only_mode(
        self, event_log: EventLog
    ) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False)
        report = run_candidate_auto_promotion(
            candidates=[_candidate()], policy=policy, event_log=event_log, executor=None
        )
        assert report.would_promote_count == 1

    def test_commit_mode_without_executor_raises(self, event_log: EventLog) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
        with pytest.raises(ValueError, match="no executor"):
            run_candidate_auto_promotion(
                candidates=[_candidate()],
                policy=policy,
                event_log=event_log,
                executor=None,
            )

    def test_exactly_one_event_emitted_with_correct_counts_and_snapshot(
        self, event_log: EventLog
    ) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False)
        candidates = [
            _candidate("cand-1"),
            _candidate("cand-2", recommendation_type="demote_noise"),
        ]

        report = run_candidate_auto_promotion(
            candidates=candidates, policy=policy, event_log=event_log
        )

        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["mode"] == "report_only"
        assert payload["configured_mode"] == "report_only"
        assert payload["counts"] == {
            "total": 2,
            "would_promote": 1,
            "promoted": 0,
            "ineligible": 1,
        }
        assert payload["policy_snapshot"]["enabled"] is False
        assert payload["policy_snapshot"]["min_distinct_sessions"] == (
            DEFAULT_MIN_DISTINCT_SESSIONS
        )
        assert report.event_id == events[0].event_id


class TestRunCandidateAutoPromotionCommit:
    """Real ``StoreRegistry`` + ``build_curate_executor`` — commit mode must
    write only through the governed executor."""

    def test_writes_entity_link_and_promote_through_executor(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
            candidate = _candidate("cand-1")

            report = run_candidate_auto_promotion(
                candidates=[candidate],
                policy=policy,
                event_log=registry.operational.event_log,
                executor=executor,
            )

            assert report.mode == "commit"
            assert report.promoted_count == 1
            outcome = report.outcomes[0]
            assert outcome.disposition == "promoted"
            assert outcome.entity_id == "precedent://learning/cand-1"

            promote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_PROMOTE.value,
                    "requested_by": AUTO_PROMOTE_REQUESTED_BY,
                    "status": "success",
                },
            )
            assert len(promote_events) == 1
            assert promote_events[0].entity_id == "precedent://learning/cand-1"
        finally:
            registry.close()

    def test_requested_by_propagated_into_every_submitted_command(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
            custom_requested_by = "auto:learning_candidate"

            run_candidate_auto_promotion(
                candidates=[_candidate("cand-1")],
                policy=policy,
                event_log=registry.operational.event_log,
                executor=executor,
                requested_by=custom_requested_by,
            )

            mutation_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            # ENTITY_CREATE + PRECEDENT_PROMOTE (no target_entity_ids, so no
            # LINK_CREATE commands were submitted).
            assert len(mutation_events) == 2
            assert all(
                e.payload["requested_by"] == custom_requested_by
                for e in mutation_events
            )
        finally:
            registry.close()

    def test_max_promotions_per_run_cap_honoured(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            policy = LearningCandidateAutoPromotePolicy(
                enabled=True, mode="commit", max_promotions_per_run=2
            )
            candidates = [_candidate(f"cand-{i}") for i in range(4)]

            report = run_candidate_auto_promotion(
                candidates=candidates,
                policy=policy,
                event_log=registry.operational.event_log,
                executor=executor,
            )

            dispositions = [o.disposition for o in report.outcomes]
            assert dispositions == [
                "promoted",
                "promoted",
                "ineligible:run_cap_reached",
                "ineligible:run_cap_reached",
            ]
            assert report.promoted_count == 2
        finally:
            registry.close()


class TestSweepAutoDemotions:
    @staticmethod
    def _promote_one(
        registry: StoreRegistry,
        executor: MutationExecutor,
        candidate_id: str = "cand-1",
    ) -> str:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")
        report = run_candidate_auto_promotion(
            candidates=[_candidate(candidate_id)],
            policy=policy,
            event_log=registry.operational.event_log,
            executor=executor,
        )
        entity_id = report.outcomes[0].entity_id
        assert entity_id is not None
        return entity_id

    def test_no_prior_promotions_returns_empty(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            outcomes = sweep_auto_demotions(
                event_log=registry.operational.event_log, executor=executor
            )
            assert outcomes == []
        finally:
            registry.close()

    def test_demotes_on_first_unhelpful_citation(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            entity_id = self._promote_one(registry, executor)

            registry.operational.event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp:record_feedback",
                payload={"unhelpful_item_ids": [entity_id]},
            )

            outcomes = sweep_auto_demotions(
                event_log=registry.operational.event_log, executor=executor
            )

            assert len(outcomes) == 1
            assert outcomes[0].disposition == "demoted"
            assert outcomes[0].entity_id == entity_id

            node = registry.knowledge.graph_store.get_node(entity_id)
            assert node is not None
            assert node["properties"]["demoted"] is True
        finally:
            registry.close()

    def test_already_demoted_entries_skipped_on_second_sweep(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            entity_id = self._promote_one(registry, executor)
            registry.operational.event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp:record_feedback",
                payload={"unhelpful_item_ids": [entity_id]},
            )

            first = sweep_auto_demotions(
                event_log=registry.operational.event_log, executor=executor
            )
            assert len(first) == 1

            second = sweep_auto_demotions(
                event_log=registry.operational.event_log, executor=executor
            )
            assert second == []
        finally:
            registry.close()

    def test_helpful_only_citation_does_not_trigger_demotion(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            entity_id = self._promote_one(registry, executor)
            registry.operational.event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp:record_feedback",
                payload={"helpful_item_ids": [entity_id]},
            )

            outcomes = sweep_auto_demotions(
                event_log=registry.operational.event_log, executor=executor
            )
            assert outcomes == []
        finally:
            registry.close()

    def test_writes_only_through_executor_never_event_log_directly(
        self, tmp_path: Path
    ) -> None:
        """The policy must never copy ``PrecedentMiner``'s direct-emit
        bypass: the demotion event comes only from ``PrecedentUpdateHandler``
        invoked through the executor, never from this module calling
        ``event_log.emit``/``append`` itself. Wrapping the real event log in
        a spy proves the only calls this function makes on its own
        ``event_log`` argument are reads."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            entity_id = self._promote_one(registry, executor)
            registry.operational.event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp:record_feedback",
                payload={"unhelpful_item_ids": [entity_id]},
            )

            spy_log = MagicMock(wraps=registry.operational.event_log, spec=EventLog)
            outcomes = sweep_auto_demotions(event_log=spy_log, executor=executor)

            assert len(outcomes) == 1
            assert outcomes[0].disposition == "demoted"
            spy_log.emit.assert_not_called()
            spy_log.append.assert_not_called()
        finally:
            registry.close()


class TestComputeAgreementMetric:
    def test_zero_data_returns_null_rate_with_real_zero_counts(
        self, event_log: EventLog
    ) -> None:
        metric = compute_agreement_metric(event_log)
        assert metric == AgreementMetric(agree=0, disagree=0, undecided=0, rate=None)

    def test_agree_and_undecided_computed_from_id_sets(
        self, event_log: EventLog
    ) -> None:
        event_log.emit(
            EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
            source="candidate_auto_promote",
            payload={
                "dispositions": [
                    {"candidate_id": "cand-1", "disposition": "would_promote"},
                    {"candidate_id": "cand-2", "disposition": "would_promote"},
                    {
                        "candidate_id": "cand-3",
                        "disposition": "ineligible:not_promotable",
                    },
                ]
            },
        )
        event_log.emit(
            EventType.REVIEW_DECISION_RECORDED,
            source="api:review",
            payload={
                "surface": "learning_promotion",
                "action": "promote",
                "candidate_ids": ["cand-1"],
            },
        )

        metric = compute_agreement_metric(event_log)

        assert metric.agree == 1
        assert metric.disagree == 0
        assert metric.undecided == 1  # cand-2 flagged, no decision recorded yet
        assert metric.rate == 1.0

    def test_disagree_is_structurally_always_zero(self, event_log: EventLog) -> None:
        """No reject event exists for this surface today — documented in
        the module docstring as a fact about the data, not a shortcut."""
        event_log.emit(
            EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
            source="candidate_auto_promote",
            payload={
                "dispositions": [
                    {"candidate_id": "cand-1", "disposition": "would_promote"}
                ]
            },
        )
        metric = compute_agreement_metric(event_log)
        assert metric.disagree == 0
        assert metric.undecided == 1
        assert metric.rate is None  # agree + disagree == 0, never a divide-by-zero


class TestSerializationHelpers:
    def test_report_to_dict_shape(self) -> None:
        report = CandidateAutoPromoteReport(
            enabled=True,
            mode="report_only",
            configured_mode="report_only",
            outcomes=[
                CandidateAutoPromoteOutcome(
                    candidate_id="cand-1",
                    disposition="would_promote",
                    reason="qualifies",
                )
            ],
            event_id="evt-1",
        )
        data = report_to_dict(report)
        assert data["would_promote_count"] == 1
        assert data["promoted_count"] == 0
        assert data["ineligible_count"] == 0
        assert data["event_id"] == "evt-1"
        assert data["outcomes"][0] == {
            "candidate_id": "cand-1",
            "disposition": "would_promote",
            "reason": "qualifies",
            "entity_id": None,
            "holdout_arm": "unknown",
        }

    def test_agreement_metric_to_dict_shape(self) -> None:
        metric = AgreementMetric(agree=1, disagree=0, undecided=2, rate=1.0)
        assert agreement_metric_to_dict(metric) == {
            "agree": 1,
            "disagree": 0,
            "undecided": 2,
            "rate": 1.0,
        }
