"""Tests for :mod:`trellis.learning.candidate_auto_promote`.

Report-only, evaluation-only: ``enabled=False`` / ``mode="report_only"``
ship as the default, and the load-bearing claims under test are (1) a
single-session (n=1) candidate can never qualify no matter how it is
configured, (2) report-only truly never writes, (3) ``mode="commit"`` is
refused with a :class:`~trellis.errors.ConfigError` at construction on
every path, unconditionally, and (4) a candidate that already has any
successful ``precedent.promote`` — human or automatic — is excluded from
``would_promote`` and reported as ``already_promoted``. Auto-demotion and
a working commit mode are deferred to a follow-up PR; this module never
demotes anything (there is no ``sweep_auto_demotions`` here to test).

``TestFloorRefusalOnConstruction`` mirrors
``tests/unit/learning/tuners/test_resolve_auto_promote_overrides.py``'s
sibling pattern: the resolver never validates the three int floors
(confirmed by grep — no ``settings_registry.validate_value`` exists
anywhere in ``src/``), so the only real enforcement point for those is
``LearningCandidateAutoPromotePolicy.__post_init__``, exercised here by
constructing the policy from a resolved dict, for both an env-sourced and a
settings-sourced override. ``mode`` is the exception: the resolver
validates it directly (``TestResolveModeValidation``), because an invalid
``mode`` has to be refused before it is ever recorded as a
``configured_mode`` — raising only after construction would be too late.

``_force_commit_policy`` bypasses that same ``__post_init__`` gate via
``object.__setattr__`` (the dataclass is frozen) to exercise the
commit-execution branch the module docstring says is kept in
``run_candidate_auto_promotion`` "unreachable ... deliberately" rather than
deleted — nothing in production can reach it this way either, but the
branch still needs to stay correct for the follow-up that lifts the gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.errors import ConfigError
from trellis.learning.candidate_auto_promote import (
    _PROMOTION_SCAN_LIMIT,
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
    CANDIDATE_AUTO_PROMOTE_MODE_SETTING,
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
    _scan_already_promoted_entity_ids,
    agreement_metric_to_dict,
    compute_agreement_metric,
    emit_candidate_auto_promote_failure_event,
    precedent_entity_id_for_candidate,
    report_to_dict,
    resolve_candidate_auto_promote_overrides,
    run_candidate_auto_promotion,
    sanitize_configured_mode,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import Event, EventLog, EventType
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


def _force_commit_policy(**kwargs: Any) -> LearningCandidateAutoPromotePolicy:
    """Build a policy with ``mode="commit"`` by bypassing
    ``__post_init__`` — see the module docstring above."""
    policy = LearningCandidateAutoPromotePolicy(mode="report_only", **kwargs)
    object.__setattr__(policy, "mode", "commit")
    return policy


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
        """A single-session candidate must never be configurable into
        qualifying, at the construction seam."""
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


class TestCommitModeConstructionRefused:
    """Item 2: commit mode must refuse to run on every resolution path.

    ``mode="commit"`` raises :class:`~trellis.errors.ConfigError` at
    construction, unconditionally — regardless of ``enabled`` — so there
    is no way to produce a policy whose configured ``mode`` is ``"commit"``
    through the public API today."""

    def test_mode_commit_raises_configerror(self) -> None:
        with pytest.raises(
            ConfigError, match="commit mode is not available until auto-demotion ships"
        ):
            LearningCandidateAutoPromotePolicy(mode="commit")

    def test_mode_commit_raises_even_when_disabled(self) -> None:
        with pytest.raises(ConfigError):
            LearningCandidateAutoPromotePolicy(enabled=False, mode="commit")

    def test_mode_commit_raises_even_when_enabled(self) -> None:
        with pytest.raises(ConfigError):
            LearningCandidateAutoPromotePolicy(enabled=True, mode="commit")

    def test_configerror_names_the_mode_setting(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            LearningCandidateAutoPromotePolicy(mode="commit")
        assert exc_info.value.setting == CANDIDATE_AUTO_PROMOTE_MODE_SETTING

    def test_configerror_via_resolved_overrides_dict(self) -> None:
        """The resolver itself never validates (``TestFloorRefusalOnConstruction``
        below exercises the same pattern for the int floors); the same
        refusal must still hold once the resolved dict reaches
        construction, for a mode value sourced from env."""
        resolved = resolve_candidate_auto_promote_overrides(
            env={CANDIDATE_AUTO_PROMOTE_MODE_ENV: "commit"}
        )
        assert resolved["mode"] == "commit"
        with pytest.raises(ConfigError):
            LearningCandidateAutoPromotePolicy(**resolved)


class TestEffectiveMode:
    def test_disabled_report_only_stays_report_only(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False, mode="report_only")
        assert policy.effective_mode == "report_only"

    def test_enabled_report_only_stays_report_only(self) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=True, mode="report_only")
        assert policy.effective_mode == "report_only"

    def test_disabled_is_always_report_only_even_when_mode_is_commit(self) -> None:
        """``mode="commit"`` cannot be constructed normally (see
        ``TestCommitModeConstructionRefused``); this exercises
        ``effective_mode``'s enabled-gates-mode behavior via the same
        bypass the module docstring describes."""
        policy = _force_commit_policy(enabled=False)
        assert policy.effective_mode == "report_only"

    def test_enabled_commit_stays_commit(self) -> None:
        policy = _force_commit_policy(enabled=True)
        assert policy.effective_mode == "commit"


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


class TestResolveModeValidation:
    """``mode`` is the one knob the resolver validates itself: an
    unrecognized value — stored or env-sourced — must never survive long
    enough to be recorded as a ``configured_mode`` or echoed as a settings
    ``effective_value``. The raised message never embeds the offending
    value, so it is safe to let reach a CLI's stderr."""

    def test_valid_modes_pass_through(self) -> None:
        for mode in ("report_only", "commit"):
            resolved = resolve_candidate_auto_promote_overrides(
                env={CANDIDATE_AUTO_PROMOTE_MODE_ENV: mode}
            )
            assert resolved["mode"] == mode

    def test_invalid_env_mode_raises_value_free_error(self) -> None:
        secret = "SEKRETmode4b2e1d"  # noqa: S105 - synthetic test placeholder, not a credential
        with pytest.raises(ValueError) as excinfo:
            resolve_candidate_auto_promote_overrides(
                env={CANDIDATE_AUTO_PROMOTE_MODE_ENV: secret}
            )
        assert secret not in str(excinfo.value)
        assert "report_only" in str(excinfo.value)

    def test_invalid_settings_mode_raises_value_free_error(self) -> None:
        secret = "SEKRETstored91c0"  # noqa: S105 - synthetic test placeholder, not a credential
        with pytest.raises(ValueError) as excinfo:
            resolve_candidate_auto_promote_overrides(
                settings={CANDIDATE_AUTO_PROMOTE_MODE_SETTING: secret}, env={}
            )
        assert secret not in str(excinfo.value)


class TestSanitizeConfiguredMode:
    """``sanitize_configured_mode`` is the single point every event
    payload, API response and CLI output routes ``configured_mode``
    through before it is recorded or rendered."""

    def test_none_passes_through(self) -> None:
        assert sanitize_configured_mode(None) is None

    @pytest.mark.parametrize("mode", ["report_only", "commit"])
    def test_enum_members_pass_through(self, mode: str) -> None:
        assert sanitize_configured_mode(mode) == mode

    @pytest.mark.parametrize(
        "value",
        ["SEKRETmode4b2e1d", "", "REPORT_ONLY", "report_only ", 1, True],
    )
    def test_everything_else_becomes_the_fixed_marker(self, value: Any) -> None:
        assert sanitize_configured_mode(value) == "invalid"


class TestFloorRefusalOnConstruction:
    """The resolver never validates the three int floors; construction
    does (``mode`` is the one knob the resolver validates itself — see
    ``TestResolveModeValidation`` below). Both a value arriving via
    settings and the same value arriving via env must still be refused
    once ``LearningCandidateAutoPromotePolicy`` is built from the
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


class TestNonFiniteEnvValuesRaiseCleanly:
    """Mirrors ``tests/unit/learning/tuners/test_resolve_auto_promote_overrides
    .py``'s sibling class. All three int-typed knobs here resolve through
    the same ``resolve_int`` closure, and ``int(float("inf"))`` raises
    ``OverflowError`` — not ``ValueError`` — unless the closure's
    ``raw.strip()`` branch rejects non-finite values before ``int()`` ever
    sees them. A caller that wraps only ``except ValueError`` (the settings
    route, ``admin_settings.py``) does not catch ``OverflowError``.
    """

    @pytest.mark.parametrize(
        "env_name",
        [
            CANDIDATE_AUTO_PROMOTE_MIN_HELPFUL_COUNT_ENV,
            CANDIDATE_AUTO_PROMOTE_MIN_DISTINCT_SESSIONS_ENV,
            CANDIDATE_AUTO_PROMOTE_MAX_PROMOTIONS_PER_RUN_ENV,
        ],
    )
    @pytest.mark.parametrize("raw", ["inf", "-inf", "1e999", "nan"])
    def test_non_finite_env_raises_value_error_never_overflow(
        self, env_name: str, raw: str
    ) -> None:
        try:
            with pytest.raises(ValueError, match="finite"):
                resolve_candidate_auto_promote_overrides(env={env_name: raw})
        except OverflowError:
            pytest.fail(
                "OverflowError escaped resolve_candidate_auto_promote_overrides"
            )


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
        """A candidate whose helpful citations all come from one session
        must never qualify, no matter how high its helpful_count is or
        how loosely min_helpful_count is configured."""
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

    def test_already_promoted_takes_priority_over_every_other_check(self) -> None:
        """B2: ``already_promoted`` is checked before ``not_promotable`` and
        ``unhelpful_citations`` — a candidate that would otherwise fail
        every other check still reports ``already_promoted``, never an
        ``ineligible:*`` reason, once a precedent already exists for it."""
        policy = LearningCandidateAutoPromotePolicy()
        candidate = _candidate(recommendation_type="demote_noise", unhelpful_count=5)
        qualifies, reason = _evaluate_candidate(
            candidate, policy=policy, already_promoted=True
        )
        assert qualifies is False
        assert reason == "already_promoted"


class TestHoldoutArmStatus:
    def test_always_unknown(self) -> None:
        assert _holdout_arm_status({}) == "unknown"
        assert _holdout_arm_status({"intent_family": "billing"}) == "unknown"


class TestRunCandidateAutoPromotionReportOnly:
    def test_disabled_policy_never_calls_executor(self, event_log: EventLog) -> None:
        policy = _force_commit_policy(enabled=False)
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
        policy = _force_commit_policy(enabled=True)
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
        """Unreachable through normal construction today (see
        ``TestCommitModeConstructionRefused``) — exercised via the bypass
        so the follow-up that lifts the ``ConfigError`` gate inherits a
        working guard rather than dead, untested code."""
        policy = _force_commit_policy(enabled=True)
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
            "already_promoted": 0,
            "ineligible": 1,
            "skipped": 0,
        }
        assert payload["policy_snapshot"]["enabled"] is False
        assert payload["policy_snapshot"]["min_distinct_sessions"] == (
            DEFAULT_MIN_DISTINCT_SESSIONS
        )
        assert report.event_id == events[0].event_id
        assert report.skipped_count == 0

    @pytest.mark.parametrize("bad_id", ["", "   ", "\t\n"])
    def test_candidate_with_no_id_is_skipped_not_evaluated(
        self, event_log: EventLog, bad_id: str
    ) -> None:
        """A candidate with a blank/whitespace-only ``candidate_id`` gets
        no :class:`CandidateAutoPromoteOutcome` at all (there is nothing
        to name it with) -- it must not silently vanish: it is counted in
        both ``report.skipped_count`` and the emitted event's
        ``counts.skipped``, so a run that drops input is distinguishable
        from one that evaluated everything it was given.
        """
        policy = LearningCandidateAutoPromotePolicy(enabled=False)
        candidates = [
            _candidate(bad_id),
            _candidate("cand-good"),
        ]

        report = run_candidate_auto_promotion(
            candidates=candidates, policy=policy, event_log=event_log
        )

        # Only the one real candidate produced an outcome.
        assert len(report.outcomes) == 1
        assert report.outcomes[0].candidate_id == "cand-good"
        assert report.skipped_count == 1

        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert len(events) == 1
        counts = events[0].payload["counts"]
        assert counts["total"] == 1
        assert counts["skipped"] == 1

    def test_missing_candidate_id_key_is_also_skipped(
        self, event_log: EventLog
    ) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False)
        candidate_no_key = _candidate("cand-1")
        del candidate_no_key["candidate_id"]

        report = run_candidate_auto_promotion(
            candidates=[candidate_no_key], policy=policy, event_log=event_log
        )

        assert report.outcomes == []
        assert report.skipped_count == 1

    def test_all_candidates_with_ids_report_zero_skipped(
        self, event_log: EventLog
    ) -> None:
        policy = LearningCandidateAutoPromotePolicy(enabled=False)
        candidates = [_candidate("cand-1"), _candidate("cand-2")]

        report = run_candidate_auto_promotion(
            candidates=candidates, policy=policy, event_log=event_log
        )

        assert report.skipped_count == 0
        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )
        assert events[0].payload["counts"]["skipped"] == 0


class TestRunCandidateAutoPromotionCommit:
    """Real ``StoreRegistry`` + ``build_curate_executor`` — commit mode must
    write only through the governed executor. ``mode="commit"`` cannot be
    constructed through the normal API today
    (``TestCommitModeConstructionRefused``); these use ``_force_commit_policy``
    to exercise the branch the module docstring says is kept "unreachable
    ... deliberately" rather than deleted, so it stays correct for the
    follow-up that lifts the gate."""

    def test_writes_entity_link_and_promote_through_executor(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            policy = _force_commit_policy(enabled=True)
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
            policy = _force_commit_policy(enabled=True)
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
            policy = _force_commit_policy(enabled=True, max_promotions_per_run=2)
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

    def test_already_promoted_excluded_before_the_cap_is_consulted(
        self, tmp_path: Path
    ) -> None:
        """B2 + the cap: an already-promoted candidate is excluded
        entirely, never counted against ``max_promotions_per_run`` — a
        genuinely new candidate behind it in the batch still gets
        promoted even with a cap of 1."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            policy = _force_commit_policy(enabled=True, max_promotions_per_run=1)
            run_candidate_auto_promotion(
                candidates=[_candidate("cand-already")],
                policy=policy,
                event_log=registry.operational.event_log,
                executor=executor,
            )

            report = run_candidate_auto_promotion(
                candidates=[_candidate("cand-already"), _candidate("cand-new")],
                policy=policy,
                event_log=registry.operational.event_log,
                executor=executor,
            )

            dispositions = {o.candidate_id: o.disposition for o in report.outcomes}
            assert dispositions["cand-already"] == "already_promoted"
            assert dispositions["cand-new"] == "promoted"
            assert report.promoted_count == 1
        finally:
            registry.close()


class TestAlreadyPromotedExclusion:
    """B2: a candidate that already has any successful ``precedent.promote``
    — human or automatic — is excluded from ``would_promote`` and reported
    as ``already_promoted``. The scan has no ``requested_by`` filter (see
    ``_scan_already_promoted_entity_ids``'s docstring), so a human-approved
    promotion and an earlier automatic one are both reasons never to
    re-propose the same candidate."""

    @staticmethod
    def _emit_promoted(
        event_log: EventLog, candidate_id: str, *, requested_by: str
    ) -> str:
        entity_id = precedent_entity_id_for_candidate(candidate_id)
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            source="test",
            entity_id=entity_id,
            payload={
                "operation": Operation.PRECEDENT_PROMOTE.value,
                "status": "success",
                "requested_by": requested_by,
            },
        )
        return entity_id

    def test_automatic_prior_promotion_excluded_from_would_promote(
        self, event_log: EventLog
    ) -> None:
        entity_id = self._emit_promoted(
            event_log, "cand-1", requested_by=AUTO_PROMOTE_REQUESTED_BY
        )
        policy = LearningCandidateAutoPromotePolicy(enabled=False)

        report = run_candidate_auto_promotion(
            candidates=[_candidate("cand-1")], policy=policy, event_log=event_log
        )

        assert report.would_promote_count == 0
        assert report.already_promoted_count == 1
        outcome = report.outcomes[0]
        assert outcome.disposition == "already_promoted"
        assert outcome.entity_id == entity_id

    def test_human_approved_prior_promotion_excluded_from_would_promote(
        self, event_log: EventLog
    ) -> None:
        self._emit_promoted(event_log, "cand-1", requested_by="cli:promote-learning")
        policy = LearningCandidateAutoPromotePolicy(enabled=False)

        report = run_candidate_auto_promotion(
            candidates=[_candidate("cand-1")], policy=policy, event_log=event_log
        )

        assert report.would_promote_count == 0
        assert report.already_promoted_count == 1
        assert report.outcomes[0].disposition == "already_promoted"

    def test_unpromoted_candidate_in_same_batch_still_reports_would_promote(
        self, event_log: EventLog
    ) -> None:
        """The already-promoted scan must not swallow a genuinely new
        candidate evaluated in the same run."""
        self._emit_promoted(event_log, "cand-old", requested_by="cli:promote-learning")
        policy = LearningCandidateAutoPromotePolicy(enabled=False)

        report = run_candidate_auto_promotion(
            candidates=[_candidate("cand-old"), _candidate("cand-new")],
            policy=policy,
            event_log=event_log,
        )

        dispositions = {o.candidate_id: o.disposition for o in report.outcomes}
        assert dispositions["cand-old"] == "already_promoted"
        assert dispositions["cand-new"] == "would_promote"
        assert report.would_promote_count == 1
        assert report.already_promoted_count == 1


class TestFailureEventPayloadIsValueFree:
    """A failure event's payload must carry ``error.type`` and a fixed
    reason, never ``str(exc)`` — a resolver or policy ``ValueError`` can
    embed the raw stored/env value it rejected — and ``configured_mode``
    is recorded only when it is an enum member."""

    def test_error_message_is_fixed_never_str_exc(self, event_log: EventLog) -> None:
        secret = "SEKRETtb11aa"  # noqa: S105 - synthetic test placeholder, not a credential
        error = ValueError(f"not a number: {secret!r}")

        event_id = emit_candidate_auto_promote_failure_event(
            event_log, error=error, configured_mode="report_only"
        )

        assert event_id is not None
        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )[0]
        assert event.payload["error"]["type"] == "ValueError"
        assert secret not in json.dumps(event.payload)
        assert str(error) != event.payload["error"]["message"]

    def test_invalid_configured_mode_becomes_the_fixed_marker(
        self, event_log: EventLog
    ) -> None:
        secret = "SEKRETmode4b2e1d"  # noqa: S105 - synthetic test placeholder, not a credential

        emit_candidate_auto_promote_failure_event(
            event_log, error=ValueError("boom"), configured_mode=secret
        )

        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )[0]
        assert event.payload["configured_mode"] == "invalid"
        assert secret not in json.dumps(event.payload)

    def test_none_configured_mode_stays_none(self, event_log: EventLog) -> None:
        emit_candidate_auto_promote_failure_event(
            event_log, error=ValueError("boom"), configured_mode=None
        )

        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )[0]
        assert event.payload["configured_mode"] is None

    def test_valid_configured_mode_passes_through(self, event_log: EventLog) -> None:
        emit_candidate_auto_promote_failure_event(
            event_log, error=ValueError("boom"), configured_mode="commit"
        )

        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
        )[0]
        assert event.payload["configured_mode"] == "commit"


class TestAlreadyPromotedScanOrderMatchesUnderTruncation:
    """``_scan_already_promoted_entity_ids`` must pass ``order="desc"``
    when scanning ``precedent.promote`` events: ``get_events``'s default
    is ``order="asc"`` (oldest-first), so on a truncated scan past
    ``_PROMOTION_SCAN_LIMIT`` (500) successful promotions, that default
    would keep the *oldest* rows and drop the newest — a just-promoted
    entity would read back as not-yet-promoted on a busy log and the
    auto-promote job would propose it all over again.
    """

    @staticmethod
    def _emit_promoted_at(
        event_log: EventLog, candidate_id: str, *, at: datetime
    ) -> str:
        entity_id = precedent_entity_id_for_candidate(candidate_id)
        event_log.append(
            Event(
                event_type=EventType.MUTATION_EXECUTED,
                source="test",
                entity_id=entity_id,
                payload={
                    "operation": Operation.PRECEDENT_PROMOTE.value,
                    "status": "success",
                    "requested_by": AUTO_PROMOTE_REQUESTED_BY,
                },
                occurred_at=at,
            )
        )
        return entity_id

    def test_newest_promotion_survives_truncation_not_the_oldest(
        self, event_log: EventLog
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # One more successful promotion than the scan limit, each at a
        # distinct, strictly increasing timestamp so truncation direction
        # is unambiguous: candidate 0 is the oldest, candidate
        # ``_PROMOTION_SCAN_LIMIT`` (500) is the single newest.
        for n in range(_PROMOTION_SCAN_LIMIT + 1):
            self._emit_promoted_at(
                event_log, f"cand-{n}", at=base + timedelta(seconds=n)
            )
        oldest_entity_id = precedent_entity_id_for_candidate("cand-0")
        newest_entity_id = precedent_entity_id_for_candidate(
            f"cand-{_PROMOTION_SCAN_LIMIT}"
        )

        already_promoted = _scan_already_promoted_entity_ids(
            event_log, limit=_PROMOTION_SCAN_LIMIT
        )

        assert len(already_promoted) == _PROMOTION_SCAN_LIMIT
        assert newest_entity_id in already_promoted
        assert oldest_entity_id not in already_promoted


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
        assert data["already_promoted_count"] == 0
        assert data["ineligible_count"] == 0
        assert data["event_id"] == "evt-1"
        assert data["outcomes"][0] == {
            "candidate_id": "cand-1",
            "disposition": "would_promote",
            "reason": "qualifies",
            "entity_id": None,
            "holdout_arm": "unknown",
        }

    def test_report_to_dict_sanitizes_an_invalid_configured_mode(self) -> None:
        """A report built with a bogus ``configured_mode`` — not reachable
        through the normal resolver/policy path, but ``report_to_dict``
        must not trust that and echo it anyway — never serialises the raw
        value."""
        secret = "SEKRETmode4b2e1d"  # noqa: S105 - synthetic test placeholder, not a credential
        report = CandidateAutoPromoteReport(
            enabled=True,
            mode="report_only",
            configured_mode=secret,  # type: ignore[arg-type]
        )
        data = report_to_dict(report)
        assert data["configured_mode"] == "invalid"
        assert secret not in json.dumps(data)

    def test_agreement_metric_to_dict_shape(self) -> None:
        """F1: the serialised payload carries the raw ``agree``/``disagree``/
        ``undecided`` counts *and* ``note`` — a reader must never mistake
        the structural ``rate`` ceiling (``None`` or exactly ``1.0``) for a
        measured agreement rate."""
        metric = AgreementMetric(agree=1, disagree=0, undecided=2, rate=1.0)
        data = agreement_metric_to_dict(metric)
        assert data == {
            "agree": 1,
            "disagree": 0,
            "undecided": 2,
            "rate": 1.0,
            "note": metric.note,
        }
        assert "disagree is always 0" in data["note"]

    def test_agreement_metric_note_has_a_default(self) -> None:
        """F1: a caller that builds ``AgreementMetric`` without naming
        ``note`` still gets the explanatory text, not an empty string."""
        metric = AgreementMetric(agree=0, disagree=0, undecided=0, rate=None)
        assert metric.note
        assert "None" in metric.note
