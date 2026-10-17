"""Tests for :mod:`trellis.learning.precedent_auto_demote`.

Mirrors ``test_candidate_auto_promote.py``'s shape for its sibling
policy. The load-bearing claims under test: (1) ``enabled=False`` /
``mode="report_only"`` ship as the default and a run at the default
posture never writes anything, for *either* policy; (2) report-only
truly never writes; (3) commit mode writes only through the governed
``MutationExecutor``, stamped with
:data:`~trellis.mutate.handlers.AUTO_DEMOTE_REQUESTED_BY`, and
``max_demotions_per_run`` caps a single run's blast radius with every
precedent above the cap reported ``"skipped_rate_limited"``, never
silently dropped; (4) a human-promoted precedent is pre-filtered by the
evidence gate itself and reported ``"ineligible:not_auto_promoted"``,
never reaching ``would_demote`` — the handler's own provenance check
exists only as defence in depth for the race the gate's read cannot
close; (5) the idempotency case (a precedent already demoted, or a
duplicate command on the same evidence window) is reported and never
re-attempted or double-counted; and (6) end to end, a precedent this
policy auto-demoted is still excluded from re-promotion by the
promotion policy's own idempotency scan — the loop actually closes.

Unlike :mod:`trellis.learning.candidate_auto_promote`, this policy's own
``mode="commit"`` carries no sibling-policy construction gate (see the
module docstring), so there is no ``TestCommitModeConstructionRefused``
analogue here — only the floor/enum validation
:class:`~trellis.learning.precedent_auto_demote.PrecedentAutoDemotePolicy.__post_init__`
itself performs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis.learning.candidate_auto_promote import (
    AUTO_PROMOTE_REQUESTED_BY,
    LearningCandidateAutoPromotePolicy,
    run_candidate_auto_promotion,
)
from trellis.learning.precedent_auto_demote import (
    DEFAULT_MAX_DEMOTIONS_PER_RUN,
    MAX_DEMOTIONS_PER_RUN_FLOOR,
    PRECEDENT_AUTO_DEMOTE_ENABLED_ENV,
    PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING,
    PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV,
    PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING,
    PRECEDENT_AUTO_DEMOTE_MODE_ENV,
    PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
    PrecedentAutoDemotePolicy,
    _demote_idempotency_key,
    _gate_outcome_to_disposition,
    emit_precedent_auto_demote_failure_event,
    report_to_dict,
    resolve_precedent_auto_demote_overrides,
    run_precedent_auto_demotion,
    sanitize_configured_mode,
)
from trellis.learning.precedent_evidence_gate import (
    DISPOSITION_REFUSED,
    DISPOSITION_SKIPPED,
    DISPOSITION_WOULD_DEMOTE,
    REFUSED_NO_EVIDENCE,
    SKIPPED_ALREADY_DEMOTED,
    SKIPPED_ARCHIVED,
    PrecedentDemotionOutcome,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.handlers import AUTO_DEMOTE_REQUESTED_BY
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import Event, EventLog, EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.graph import SQLiteGraphStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def graph_store(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(tmp_path / "graph.db")


def _emit_promote(
    event_log: EventLog,
    entity_id: str,
    *,
    at: datetime,
    requested_by: str = "cli:promote-learning",
) -> None:
    """Append a raw ``precedent.promote`` audit row.

    Mirrors the shape ``MutationExecutor._emit_event`` writes
    (``operation``/``status``/``requested_by`` in the payload) closely
    enough for ``_was_auto_promoted`` and ``discover_current_precedents``
    to read it, without going through the executor itself — this module's
    own tests care about what the gate and this module do with an
    existing audit trail, not about ``PrecedentPromoteHandler``.
    """
    event_log.append(
        Event(
            event_type=EventType.MUTATION_EXECUTED,
            source="test",
            entity_id=entity_id,
            payload={
                "operation": Operation.PRECEDENT_PROMOTE.value,
                "status": "success",
                "requested_by": requested_by,
            },
            occurred_at=at,
        )
    )


def _emit_pack_assembled(
    event_log: EventLog,
    *,
    pack_id: str,
    item_ids: list[str],
    at: datetime,
) -> None:
    event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "intent": "test",
                "domain": "x",
                "items_count": len(item_ids),
                "injected_items": [
                    {"item_id": item_id, "item_type": "precedent", "rank": rank + 1}
                    for rank, item_id in enumerate(item_ids)
                ],
            },
            occurred_at=at,
        )
    )


def _emit_feedback(
    event_log: EventLog,
    *,
    pack_id: str,
    at: datetime,
    helpful: list[str] | None = None,
    unhelpful: list[str] | None = None,
    feedback_id: str | None = None,
) -> None:
    payload: dict[str, object] = {"pack_id": pack_id, "rating": 0.5, "success": True}
    if helpful:
        payload["helpful_item_ids"] = list(helpful)
    if unhelpful:
        payload["unhelpful_item_ids"] = list(unhelpful)
    event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=feedback_id or f"fb-{pack_id}-{id(payload)}",
            payload=payload,
            occurred_at=at,
        )
    )


def _give_two_unhelpful_citations(
    event_log: EventLog, entity_id: str, *, promoted_at: datetime
) -> None:
    """Clear the evidence gate for ``entity_id`` with zero contest."""
    at1 = promoted_at + timedelta(hours=1)
    _emit_pack_assembled(
        event_log, pack_id=f"pack-{entity_id}-1", item_ids=[entity_id], at=at1
    )
    _emit_feedback(
        event_log, pack_id=f"pack-{entity_id}-1", at=at1, unhelpful=[entity_id]
    )
    at2 = promoted_at + timedelta(hours=2)
    _emit_pack_assembled(
        event_log, pack_id=f"pack-{entity_id}-2", item_ids=[entity_id], at=at2
    )
    _emit_feedback(
        event_log, pack_id=f"pack-{entity_id}-2", at=at2, unhelpful=[entity_id]
    )


def _candidate(
    candidate_id: str = "cand-1",
    *,
    helpful_count: int = 5,
    distinct_helpful_sessions: int = 3,
    unhelpful_count: int = 0,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "recommendation_type": "promote_precedent",
        "precedent_name": f"Precedent for {candidate_id}",
        "citation_evidence": {
            "helpful_count": helpful_count,
            "distinct_helpful_sessions": distinct_helpful_sessions,
            "unhelpful_count": unhelpful_count,
        },
        "target_entity_ids": [],
    }


class TestPolicyConstruction:
    def test_defaults_are_disabled_report_only(self) -> None:
        policy = PrecedentAutoDemotePolicy()
        assert policy.enabled is False
        assert policy.mode == "report_only"
        assert policy.max_demotions_per_run == DEFAULT_MAX_DEMOTIONS_PER_RUN
        assert policy.effective_mode == "report_only"

    def test_bad_mode_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="report_only"):
            PrecedentAutoDemotePolicy(mode="delete_everything")  # type: ignore[arg-type]

    def test_max_demotions_per_run_below_floor_raises(self) -> None:
        with pytest.raises(ValueError, match="floor"):
            PrecedentAutoDemotePolicy(
                max_demotions_per_run=MAX_DEMOTIONS_PER_RUN_FLOOR - 1
            )

    def test_max_demotions_per_run_at_floor_is_accepted(self) -> None:
        policy = PrecedentAutoDemotePolicy(
            max_demotions_per_run=MAX_DEMOTIONS_PER_RUN_FLOOR
        )
        assert policy.max_demotions_per_run == MAX_DEMOTIONS_PER_RUN_FLOOR

    def test_commit_mode_construction_is_never_refused_by_this_policy_alone(
        self,
    ) -> None:
        """Unlike the promotion policy, nothing here gates ``mode="commit"``
        at construction — demotion is itself the rollback, so there is
        nothing further to wait on."""
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")
        assert policy.effective_mode == "commit"


class TestEffectiveMode:
    def test_disabled_is_always_report_only_even_configured_for_commit(self) -> None:
        policy = PrecedentAutoDemotePolicy(enabled=False, mode="commit")
        assert policy.effective_mode == "report_only"

    def test_enabled_commit_is_commit(self) -> None:
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")
        assert policy.effective_mode == "commit"

    def test_enabled_report_only_stays_report_only(self) -> None:
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="report_only")
        assert policy.effective_mode == "report_only"


class TestResolveOverridesPrecedence:
    def test_all_defaults_when_nothing_given(self) -> None:
        overrides = resolve_precedent_auto_demote_overrides(settings={}, env={})
        assert overrides == {
            "enabled": False,
            "mode": "report_only",
            "max_demotions_per_run": DEFAULT_MAX_DEMOTIONS_PER_RUN,
        }

    def test_settings_override_the_default(self) -> None:
        # ``mode`` cannot be exercised here: "commit" is a stored
        # value refused at read time (see
        # TestModeSettingRefusedValue below), so this covers the two
        # knobs settings can actually move, and leaves mode at its
        # settings-provided "report_only" to show it still passes
        # through unchanged.
        overrides = resolve_precedent_auto_demote_overrides(
            settings={
                PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING: True,
                PRECEDENT_AUTO_DEMOTE_MODE_SETTING: "report_only",
                PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING: 7,
            },
            env={},
        )
        assert overrides == {
            "enabled": True,
            "mode": "report_only",
            "max_demotions_per_run": 7,
        }

    def test_env_overrides_settings(self) -> None:
        overrides = resolve_precedent_auto_demote_overrides(
            settings={
                PRECEDENT_AUTO_DEMOTE_ENABLED_SETTING: True,
                PRECEDENT_AUTO_DEMOTE_MODE_SETTING: "commit",
                PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_SETTING: 7,
            },
            env={
                PRECEDENT_AUTO_DEMOTE_ENABLED_ENV: "false",
                PRECEDENT_AUTO_DEMOTE_MODE_ENV: "report_only",
                PRECEDENT_AUTO_DEMOTE_MAX_DEMOTIONS_PER_RUN_ENV: "2",
            },
        )
        assert overrides == {
            "enabled": False,
            "mode": "report_only",
            "max_demotions_per_run": 2,
        }

    def test_invalid_mode_from_settings_raises_value_free_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_precedent_auto_demote_overrides(
                settings={PRECEDENT_AUTO_DEMOTE_MODE_SETTING: "launch_the_missiles"},
                env={},
            )
        assert "launch_the_missiles" not in str(excinfo.value)

    def test_invalid_mode_from_env_raises_value_free_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_precedent_auto_demote_overrides(
                settings={}, env={PRECEDENT_AUTO_DEMOTE_MODE_ENV: "launch_the_missiles"}
            )
        assert "launch_the_missiles" not in str(excinfo.value)

    def test_stored_commit_mode_is_refused_even_hand_written(
        self, tmp_path: Path
    ) -> None:
        """A ``settings.json`` edited by hand, outside the governed
        write path, must not be able to smuggle ``mode="commit"`` in —
        it is read back exactly as any other stored value and refused
        the same way."""
        import json

        from trellis.stores.settings_store import load_settings_values

        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        (stores_dir / "settings.json").write_text(
            json.dumps(
                {
                    "settings": [
                        {
                            "name": PRECEDENT_AUTO_DEMOTE_MODE_SETTING,
                            "value": "commit",
                        }
                    ]
                }
            )
        )

        settings = load_settings_values(stores_dir)
        assert settings[PRECEDENT_AUTO_DEMOTE_MODE_SETTING] == "commit"

        with capture_logs() as logs, pytest.raises(ConfigError) as excinfo:
            resolve_precedent_auto_demote_overrides(settings=settings, env={})
        assert excinfo.value.setting == PRECEDENT_AUTO_DEMOTE_MODE_SETTING
        assert "TRELLIS_PRECEDENT_AUTO_DEMOTE_MODE" in str(excinfo.value)
        # The message itself must name the setting's dotted path, not
        # just the structured ``.setting`` attribute -- a human reading
        # text-format CLI output has no other way to learn it
        # (gate-879 follow-up #6).
        assert PRECEDENT_AUTO_DEMOTE_MODE_SETTING in str(excinfo.value)
        warnings = [e for e in logs if e["log_level"] == "warning"]
        assert any(
            e["event"] == "precedent_auto_demote_mode_refused_in_settings"
            and e["setting"] == PRECEDENT_AUTO_DEMOTE_MODE_SETTING
            for e in warnings
        )

    def test_refusal_warning_fires_once_per_cause_not_once_per_call(self) -> None:
        """A stored refused ``mode`` is a standing misconfiguration, not a
        per-call event: ``GET /settings`` calls this resolver on every
        request, so logging on every call would turn one bad
        ``settings.json`` entry into one warning per poll forever."""
        settings = {PRECEDENT_AUTO_DEMOTE_MODE_SETTING: "commit"}
        with capture_logs() as logs:
            for _ in range(3):
                with pytest.raises(ConfigError):
                    resolve_precedent_auto_demote_overrides(settings=settings, env={})
        refusal_warnings = [
            e
            for e in logs
            if e["log_level"] == "warning"
            and e["event"] == "precedent_auto_demote_mode_refused_in_settings"
        ]
        assert len(refusal_warnings) == 1


class TestSanitizeConfiguredMode:
    def test_none_passes_through(self) -> None:
        assert sanitize_configured_mode(None) is None

    def test_valid_members_pass_through(self) -> None:
        assert sanitize_configured_mode("report_only") == "report_only"
        assert sanitize_configured_mode("commit") == "commit"

    def test_anything_else_becomes_invalid(self) -> None:
        assert sanitize_configured_mode("delete_everything") == "invalid"
        assert sanitize_configured_mode(123) == "invalid"


class TestGateOutcomeToDisposition:
    def test_would_demote_passes_through(self) -> None:
        outcome = PrecedentDemotionOutcome(
            entity_id="p1", disposition=DISPOSITION_WOULD_DEMOTE, reason=""
        )
        disposition, reason = _gate_outcome_to_disposition(outcome)
        assert disposition == "would_demote"
        assert reason == ""

    def test_skipped_already_demoted_becomes_already_demoted(self) -> None:
        outcome = PrecedentDemotionOutcome(
            entity_id="p1",
            disposition=DISPOSITION_SKIPPED,
            reason=SKIPPED_ALREADY_DEMOTED,
        )
        disposition, reason = _gate_outcome_to_disposition(outcome)
        assert disposition == "already_demoted"
        assert reason == SKIPPED_ALREADY_DEMOTED

    def test_other_skip_becomes_ineligible(self) -> None:
        outcome = PrecedentDemotionOutcome(
            entity_id="p1", disposition=DISPOSITION_SKIPPED, reason=SKIPPED_ARCHIVED
        )
        disposition, reason = _gate_outcome_to_disposition(outcome)
        assert disposition == f"ineligible:{SKIPPED_ARCHIVED}"
        assert reason == SKIPPED_ARCHIVED

    def test_refused_becomes_ineligible(self) -> None:
        outcome = PrecedentDemotionOutcome(
            entity_id="p1",
            disposition=DISPOSITION_REFUSED,
            reason=REFUSED_NO_EVIDENCE,
        )
        disposition, reason = _gate_outcome_to_disposition(outcome)
        assert disposition == f"ineligible:{REFUSED_NO_EVIDENCE}"
        assert reason == REFUSED_NO_EVIDENCE

    def test_unrecognised_disposition_falls_back_to_ineligible_not_dropped(
        self,
    ) -> None:
        """Defensive branch: the gate's vocabulary is closed today, but a
        caller reading this module alone should never see a disposition
        it does not recognise vanish silently."""
        outcome = PrecedentDemotionOutcome(
            entity_id="p1", disposition="some_future_disposition", reason="why"
        )
        disposition, reason = _gate_outcome_to_disposition(outcome)
        assert disposition == "ineligible:some_future_disposition"
        assert reason == "why"


class TestRunPrecedentAutoDemotionReportOnly:
    def test_would_demote_is_reported_and_nothing_is_written(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        policy = PrecedentAutoDemotePolicy()  # default: disabled, report_only
        report = run_precedent_auto_demotion(
            policy=policy, event_log=event_log, graph_store=graph_store
        )

        assert report.mode == "report_only"
        assert report.would_demote_count == 1
        assert report.outcomes[0].disposition == "would_demote"
        node = graph_store.get_node("p1")
        assert node is not None
        assert node["properties"].get(LIFECYCLE_KEY) is None

    def test_enabled_but_report_only_mode_never_writes(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        policy = PrecedentAutoDemotePolicy(enabled=True, mode="report_only")
        report = run_precedent_auto_demotion(
            policy=policy, event_log=event_log, graph_store=graph_store
        )

        assert report.mode == "report_only"
        assert report.would_demote_count == 1

    def test_enabled_commit_but_force_report_only_never_writes(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")
        report = run_precedent_auto_demotion(
            policy=policy,
            event_log=event_log,
            graph_store=graph_store,
            force_report_only=True,
        )

        assert report.mode == "report_only"
        assert report.would_demote_count == 1

    def test_commit_mode_with_no_executor_raises_value_error(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")
        with pytest.raises(ValueError, match="executor"):
            run_precedent_auto_demotion(
                policy=policy, event_log=event_log, graph_store=graph_store
            )

    def test_disabled_policy_configured_for_commit_needs_no_executor(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """``effective_mode`` is ``report_only`` while disabled, so the
        'commit needs an executor' guard must not fire for a disabled
        policy whose ``mode`` field happens to say ``commit``."""
        policy = PrecedentAutoDemotePolicy(enabled=False, mode="commit")
        report = run_precedent_auto_demotion(
            policy=policy, event_log=event_log, graph_store=graph_store
        )
        assert report.mode == "report_only"
        assert report.configured_mode == "commit"

    def test_exactly_one_event_emitted_per_run(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        policy = PrecedentAutoDemotePolicy()
        report = run_precedent_auto_demotion(
            policy=policy, event_log=event_log, graph_store=graph_store
        )
        events = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert len(events) == 1
        assert events[0].event_id == report.event_id
        assert events[0].payload["mode"] == "report_only"
        assert events[0].payload["counts"]["total"] == 0


class TestRunPrecedentAutoDemotionCommit:
    """Real ``StoreRegistry`` + ``build_curate_executor`` — commit mode must
    write only through the governed executor, stamped with
    ``AUTO_DEMOTE_REQUESTED_BY``."""

    def test_auto_promoted_precedent_is_demoted_through_the_executor(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                registry.operational.event_log,
                "p1",
                at=_T0,
                requested_by=AUTO_PROMOTE_REQUESTED_BY,
            )
            _give_two_unhelpful_citations(
                registry.operational.event_log, "p1", promoted_at=_T0
            )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            assert report.mode == "commit"
            assert report.demoted_count == 1
            outcome = report.outcomes[0]
            assert outcome.entity_id == "p1"
            assert outcome.disposition == "demoted"

            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_DEMOTE.value,
                    "requested_by": AUTO_DEMOTE_REQUESTED_BY,
                    "status": "success",
                },
            )
            assert len(demote_events) == 1
            assert demote_events[0].entity_id == "p1"

            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        finally:
            registry.close()

    def test_human_promoted_precedent_is_ineligible_before_the_gate_judges_it(
        self, tmp_path: Path
    ) -> None:
        """A human-promoted precedent is pre-filtered by the evidence gate
        itself (``_was_auto_promoted``), never reaching ``would_demote`` at
        all: it is reported ``ineligible:not_auto_promoted`` and this
        module never submits a command for it. The handler's own
        provenance check (``refused:target_not_auto_promoted``) still
        exists as defence in depth for the race the gate's read cannot
        close, but a normal run — gate reads first, commits second —
        never reaches it, so that disposition is now the unexpected case,
        not the routine one."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                registry.operational.event_log,
                "p1",
                at=_T0,
                requested_by="cli:promote-learning",
            )
            _give_two_unhelpful_citations(
                registry.operational.event_log, "p1", promoted_at=_T0
            )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            assert report.demoted_count == 0
            assert report.refused_count == 0
            assert report.ineligible_count == 1
            outcome = report.outcomes[0]
            assert outcome.disposition == "ineligible:not_auto_promoted"

            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"].get(LIFECYCLE_KEY) is None
        finally:
            registry.close()

    def test_already_demoted_precedent_is_reported_without_a_redundant_commit(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            registry.knowledge.graph_store.upsert_node(
                node_id="p1",
                node_type="precedent",
                properties={LIFECYCLE_KEY: {"state": "demoted"}},
            )
            _emit_promote(
                registry.operational.event_log,
                "p1",
                at=_T0,
                requested_by=AUTO_PROMOTE_REQUESTED_BY,
            )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            assert report.already_demoted_count == 1
            assert report.outcomes[0].disposition == "already_demoted"

            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={"operation": Operation.PRECEDENT_DEMOTE.value},
            )
            assert len(demote_events) == 0
        finally:
            registry.close()

    def test_max_demotions_per_run_caps_commits_the_rest_are_rate_limited(
        self, tmp_path: Path
    ) -> None:
        """Acceptance test: N ``would_demote`` verdicts with
        ``max_demotions_per_run=1`` commit exactly one
        ``precedent.demote``; the other N-1 are reported
        ``skipped_rate_limited`` — none silently dropped."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            entity_ids = ["p1", "p2", "p3"]
            for i, entity_id in enumerate(entity_ids):
                registry.knowledge.graph_store.upsert_node(
                    node_id=entity_id, node_type="precedent", properties={}
                )
                promoted_at = _T0 + timedelta(days=i)
                _emit_promote(
                    registry.operational.event_log,
                    entity_id,
                    at=promoted_at,
                    requested_by=AUTO_PROMOTE_REQUESTED_BY,
                )
                _give_two_unhelpful_citations(
                    registry.operational.event_log, entity_id, promoted_at=promoted_at
                )
            policy = PrecedentAutoDemotePolicy(
                enabled=True, mode="commit", max_demotions_per_run=1
            )

            report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            assert len(report.outcomes) == 3
            assert report.demoted_count == 1
            assert report.skipped_rate_limited_count == 2
            assert report.refused_count == 0
            assert report.ineligible_count == 0
            dispositions = {o.disposition for o in report.outcomes}
            assert dispositions == {"demoted", "skipped_rate_limited"}

            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_DEMOTE.value,
                    "requested_by": AUTO_DEMOTE_REQUESTED_BY,
                    "status": "success",
                },
            )
            assert len(demote_events) == 1

            demoted_entity_id = next(
                o.entity_id for o in report.outcomes if o.disposition == "demoted"
            )
            for entity_id in entity_ids:
                node = registry.knowledge.graph_store.get_node(entity_id)
                assert node is not None
                state = node["properties"].get(LIFECYCLE_KEY, {}).get("state")
                if entity_id == demoted_entity_id:
                    assert state == "demoted"
                else:
                    assert state in (None, "current")
        finally:
            registry.close()

    def test_overlapping_run_reports_already_demoted_and_does_not_spend_cap(
        self, tmp_path: Path
    ) -> None:
        """Two real, interleaved runs through the same governed executor
        (gate-876 follow-up #8 / #8 idempotency): the second run's loser —
        the precedent the first run already committed — is reported
        ``already_demoted``, not a second ``demoted`` or a redundant
        commit, and that disposition does not consume the second run's
        own ``max_demotions_per_run`` cap: a different, still-eligible
        precedent in that same second run still gets demoted."""
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            for entity_id, offset in (("p1", 0), ("p2", 1)):
                registry.knowledge.graph_store.upsert_node(
                    node_id=entity_id, node_type="precedent", properties={}
                )
                promoted_at = _T0 + timedelta(days=offset)
                _emit_promote(
                    registry.operational.event_log,
                    entity_id,
                    at=promoted_at,
                    requested_by=AUTO_PROMOTE_REQUESTED_BY,
                )
                _give_two_unhelpful_citations(
                    registry.operational.event_log, entity_id, promoted_at=promoted_at
                )
            policy = PrecedentAutoDemotePolicy(
                enabled=True, mode="commit", max_demotions_per_run=1
            )

            first_run = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert first_run.demoted_count == 1
            assert first_run.skipped_rate_limited_count == 1
            first_demoted_id = next(
                o.entity_id for o in first_run.outcomes if o.disposition == "demoted"
            )

            second_run = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            second_by_id = {o.entity_id: o.disposition for o in second_run.outcomes}
            assert second_by_id[first_demoted_id] == "already_demoted"
            assert second_run.already_demoted_count == 1
            # already_demoted did not spend the cap: the other, still
            # eligible precedent is demoted in this same second run.
            assert second_run.demoted_count == 1
            other_id = "p1" if first_demoted_id == "p2" else "p2"
            assert second_by_id[other_id] == "demoted"
            assert second_run.skipped_rate_limited_count == 0

            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_DEMOTE.value,
                    "requested_by": AUTO_DEMOTE_REQUESTED_BY,
                    "status": "success",
                },
            )
            # Exactly one commit per precedent across both runs combined —
            # the first run's winner is never re-committed by the second.
            assert len(demote_events) == 2
            assert {e.entity_id for e in demote_events} == {"p1", "p2"}
        finally:
            registry.close()

    def test_requested_by_override_is_propagated(self, tmp_path: Path) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                registry.operational.event_log,
                "p1",
                at=_T0,
                requested_by=AUTO_PROMOTE_REQUESTED_BY,
            )
            _give_two_unhelpful_citations(
                registry.operational.event_log, "p1", promoted_at=_T0
            )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            # p1 is genuinely auto-promoted, so the gate's own
            # provenance pre-filter passes it through unchanged; this
            # test is about a *different* requested_by: the one
            # stamped on the demote command itself, which the caller
            # can override independent of either policy's own
            # requested-by constants.
            report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
                requested_by="custom:requester",
            )

            assert report.demoted_count == 1
            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_DEMOTE.value,
                    "requested_by": "custom:requester",
                    "status": "success",
                },
            )
            assert len(demote_events) == 1
        finally:
            registry.close()

    def test_human_reinstatement_resets_the_evidence_window(
        self, tmp_path: Path
    ) -> None:
        """A human reinstatement must not be undone by the next run on old
        evidence (Blocker 1). Auto-promote, then two unhelpful citations,
        then a commit run demotes. A human reinstates. The next commit
        run must not demote -- the evidence window now starts at the
        reinstatement, so the pre-reinstatement citations contribute
        nothing and the precedent is reported ineligible with a distinct
        (no-evidence) reason, never ``"demoted"`` again on the strength of
        the same two citations. A later, genuinely new round of two
        unhelpful citations (after the reinstatement) demotes it again.
        """
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            event_log = registry.operational.event_log
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY
            )
            _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            first_run = run_precedent_auto_demotion(
                policy=policy,
                event_log=event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert first_run.demoted_count == 1
            assert first_run.outcomes[0].disposition == "demoted"
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

            reinstate_cmd = Command(
                operation=Operation.PRECEDENT_REINSTATE,
                args={"target_id": "p1", "reason": "re-reviewed and still useful"},
                target_id="p1",
                target_type="precedent",
                requested_by="cli:reinstate-precedent",
            )
            reinstate_result = executor.execute(reinstate_cmd)
            assert reinstate_result.status == CommandStatus.SUCCESS
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "current"

            reinstate_events = event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                entity_id="p1",
                payload_filters={
                    "operation": Operation.PRECEDENT_REINSTATE.value,
                    "status": "success",
                },
            )
            assert len(reinstate_events) == 1
            reinstated_at = reinstate_events[0].occurred_at

            second_run = run_precedent_auto_demotion(
                policy=policy,
                event_log=event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert second_run.demoted_count == 0
            assert second_run.ineligible_count == 1
            second_outcome = second_run.outcomes[0]
            assert second_outcome.disposition == "ineligible:no_evidence_supplied"
            assert second_outcome.disposition != first_run.outcomes[0].disposition
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "current"

            _give_two_unhelpful_citations(event_log, "p1", promoted_at=reinstated_at)

            third_run = run_precedent_auto_demotion(
                policy=policy,
                event_log=event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert third_run.demoted_count == 1
            assert third_run.outcomes[0].disposition == "demoted"
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        finally:
            registry.close()

    def test_truncated_join_never_demotes_full_scan_reaches_contested(
        self, tmp_path: Path
    ) -> None:
        """Commit mode must never demote on a truncated evidence scan
        (Blocker 2). Three helpful citations land before two unhelpful
        ones; with a small ``join_limit`` the newest-first scan drops the
        three older helpful citations, which would make the raw evidence
        wrongly look clean (0 helpful / 2 unhelpful, admitted). The
        truncation guard catches this instead: the precedent's own
        evidence-window start (``_T0``) predates the scan's
        ``covered_since``, so it is reported
        ``"skipped_incomplete_evidence"`` -- counted, not dropped -- and
        nothing is demoted. With the full join limit the same five
        citations are all visible and the precedent is contested
        (3 helpful >= 2 unhelpful): ``"ineligible:contested_by_helpful_citations"``.
        """
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            event_log = registry.operational.event_log
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY
            )
            for i in range(1, 4):
                at = _T0 + timedelta(hours=i)
                _emit_pack_assembled(
                    event_log, pack_id=f"pack-p1-helpful-{i}", item_ids=["p1"], at=at
                )
                _emit_feedback(
                    event_log, pack_id=f"pack-p1-helpful-{i}", at=at, helpful=["p1"]
                )
            for i in range(4, 6):
                at = _T0 + timedelta(hours=i)
                _emit_pack_assembled(
                    event_log, pack_id=f"pack-p1-unhelpful-{i}", item_ids=["p1"], at=at
                )
                _emit_feedback(
                    event_log, pack_id=f"pack-p1-unhelpful-{i}", at=at, unhelpful=["p1"]
                )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            truncated_report = run_precedent_auto_demotion(
                policy=policy,
                event_log=event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
                join_limit=2,
            )
            assert truncated_report.demoted_count == 0
            assert len(truncated_report.outcomes) == 1
            truncated_outcome = truncated_report.outcomes[0]
            assert truncated_outcome.disposition == "skipped_incomplete_evidence"
            assert truncated_report.scan.truncated is True

            demote_events = event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={"operation": Operation.PRECEDENT_DEMOTE.value},
            )
            assert len(demote_events) == 0
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"].get(LIFECYCLE_KEY) is None

            full_report = run_precedent_auto_demotion(
                policy=policy,
                event_log=event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert full_report.demoted_count == 0
            assert full_report.scan.truncated is False
            assert len(full_report.outcomes) == 1
            full_outcome = full_report.outcomes[0]
            assert (
                full_outcome.disposition == "ineligible:contested_by_helpful_citations"
            )

            demote_events = event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={"operation": Operation.PRECEDENT_DEMOTE.value},
            )
            assert len(demote_events) == 0
            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"].get(LIFECYCLE_KEY) is None
        finally:
            registry.close()

    def test_duplicate_demote_command_is_reported_deduplicated_not_demoted(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """An overlapping run (follow-up 7): the governed executor reports
        the submitted demote command as ``DUPLICATE`` -- the exact
        ``idempotency_key`` an identical overlapping run would already
        have recorded. The outcome must be reported ``"deduplicated"``,
        never ``"demoted"``, and must not burn the per-run cap."""
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        mock_executor = MagicMock()
        mock_executor.execute.return_value = CommandResult(
            command_id="dupe",
            status=CommandStatus.DUPLICATE,
            operation=Operation.PRECEDENT_DEMOTE,
            target_id="p1",
            message="duplicate command",
        )
        policy = PrecedentAutoDemotePolicy(
            enabled=True, mode="commit", max_demotions_per_run=1
        )

        report = run_precedent_auto_demotion(
            policy=policy,
            event_log=event_log,
            graph_store=graph_store,
            executor=mock_executor,
        )

        assert report.demoted_count == 0
        assert report.deduplicated_count == 1
        outcome = report.outcomes[0]
        assert outcome.entity_id == "p1"
        assert outcome.disposition == "deduplicated"

        assert mock_executor.execute.call_count == 1
        submitted = mock_executor.execute.call_args.args[0]
        assert submitted.idempotency_key == _demote_idempotency_key(
            "p1", _T0.isoformat()
        )

    def test_status_is_failed_when_every_attempted_demote_is_refused(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """``status``/``outcome`` is ``"failed"`` only once a write was
        actually attempted and none of the attempts succeeded -- the
        handler rejects the only ``would_demote`` precedent this run
        has, so ``demoted_count`` stays 0 while ``refused_count`` is 1."""
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        mock_executor = MagicMock()
        mock_executor.execute.return_value = CommandResult(
            command_id="rejected",
            status=CommandStatus.REJECTED,
            operation=Operation.PRECEDENT_DEMOTE,
            target_id="p1",
            message="refused",
            metadata={"rejection_reason": "target_not_auto_promoted"},
        )
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

        report = run_precedent_auto_demotion(
            policy=policy,
            event_log=event_log,
            graph_store=graph_store,
            executor=mock_executor,
        )

        assert report.demoted_count == 0
        assert report.refused_count == 1
        outcome = report.outcomes[0]
        assert outcome.disposition == "refused:target_not_auto_promoted"

        payload = report_to_dict(report)
        assert payload["status"] == "failed"
        evaluated = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert evaluated[-1].payload["outcome"] == "failed"

    def test_status_is_degraded_when_some_attempted_demotes_are_refused(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        """A mixed run -- one precedent demotes, the other's write is
        refused -- reports ``"degraded"``, not ``"failed"``."""
        for i, entity_id in enumerate(("p1", "p2")):
            graph_store.upsert_node(
                node_id=entity_id, node_type="precedent", properties={}
            )
            promoted_at = _T0 + timedelta(days=i)
            _emit_promote(
                event_log,
                entity_id,
                at=promoted_at,
                requested_by=AUTO_PROMOTE_REQUESTED_BY,
            )
            _give_two_unhelpful_citations(event_log, entity_id, promoted_at=promoted_at)

        mock_executor = MagicMock()
        mock_executor.execute.side_effect = [
            CommandResult(
                command_id="ok",
                status=CommandStatus.SUCCESS,
                operation=Operation.PRECEDENT_DEMOTE,
                target_id="p1",
                created_id="p1",
                message="demoted",
            ),
            CommandResult(
                command_id="rejected",
                status=CommandStatus.REJECTED,
                operation=Operation.PRECEDENT_DEMOTE,
                target_id="p2",
                message="refused",
                metadata={"rejection_reason": "target_not_auto_promoted"},
            ),
        ]
        policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

        report = run_precedent_auto_demotion(
            policy=policy,
            event_log=event_log,
            graph_store=graph_store,
            executor=mock_executor,
        )

        assert report.demoted_count == 1
        assert report.refused_count == 1
        payload = report_to_dict(report)
        assert payload["status"] == "degraded"
        evaluated = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )
        assert evaluated[-1].payload["outcome"] == "degraded"

    def test_replaying_the_submitted_command_against_the_real_executor_is_duplicate(
        self, tmp_path: Path
    ) -> None:
        """The test above proves this module reports a mocked ``DUPLICATE``
        correctly; it never submits the same idempotency key to a real
        ``MutationExecutor`` twice, so it cannot prove the executor's own
        idempotency-check stage is what produces that status. A second
        sequential run of this module does not reach that stage either --
        by the time it re-scans, ``p1`` already reads ``demoted`` and the
        evidence gate filters it out before a command is ever built.

        So this captures the exact ``Command`` the first, real run submits
        (same target, same properties, same ``idempotency_key``) and
        replays that same object against the same executor a second time,
        with nothing re-scanned in between. The executor's own stage 3 is
        what must answer ``DUPLICATE`` here, and the graph/event state
        after the replay must show one demotion, not two.
        """
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            registry.knowledge.graph_store.upsert_node(
                node_id="p1", node_type="precedent", properties={}
            )
            _emit_promote(
                registry.operational.event_log,
                "p1",
                at=_T0,
                requested_by=AUTO_PROMOTE_REQUESTED_BY,
            )
            _give_two_unhelpful_citations(
                registry.operational.event_log, "p1", promoted_at=_T0
            )
            policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")

            submitted: list[Command] = []
            real_execute = executor.execute

            def _capturing_execute(command: Command) -> CommandResult:
                submitted.append(command)
                return real_execute(command)

            executor.execute = _capturing_execute  # type: ignore[method-assign]

            first_report = run_precedent_auto_demotion(
                policy=policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )

            assert first_report.demoted_count == 1
            assert len(submitted) == 1
            replayed_command = submitted[0]

            second_result = real_execute(replayed_command)

            assert second_result.status == CommandStatus.DUPLICATE

            demote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                payload_filters={
                    "operation": Operation.PRECEDENT_DEMOTE.value,
                    "requested_by": AUTO_DEMOTE_REQUESTED_BY,
                    "status": "success",
                },
            )
            assert len(demote_events) == 1

            node = registry.knowledge.graph_store.get_node("p1")
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        finally:
            registry.close()


class TestEmitFailureEvent:
    def test_config_error_is_recorded_as_refused(self, event_log: EventLog) -> None:
        event_id = emit_precedent_auto_demote_failure_event(
            event_log, error=ConfigError("bad config", setting="x")
        )
        assert event_id is not None
        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )[0]
        assert event.payload["outcome"] == "refused"
        assert event.payload["error"]["type"] == "ConfigError"
        assert "bad config" not in str(event.payload)

    def test_other_exception_is_recorded_as_failed(self, event_log: EventLog) -> None:
        event_id = emit_precedent_auto_demote_failure_event(
            event_log, error=RuntimeError("super secret detail")
        )
        assert event_id is not None
        event = event_log.get_events(
            event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
        )[0]
        assert event.payload["outcome"] == "failed"
        assert event.payload["error"]["type"] == "RuntimeError"
        assert "super secret detail" not in str(event.payload)

    def test_emission_failure_is_swallowed_not_raised(self) -> None:
        broken_log = MagicMock(spec=EventLog)
        broken_log.emit.side_effect = RuntimeError("store is down")
        event_id = emit_precedent_auto_demote_failure_event(
            broken_log, error=RuntimeError("original")
        )
        assert event_id is None


class TestSerializationHelpers:
    def test_report_to_dict_shape(
        self, event_log: EventLog, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node(node_id="p1", node_type="precedent", properties={})
        _emit_promote(event_log, "p1", at=_T0, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        _give_two_unhelpful_citations(event_log, "p1", promoted_at=_T0)

        policy = PrecedentAutoDemotePolicy()
        report = run_precedent_auto_demotion(
            policy=policy, event_log=event_log, graph_store=graph_store
        )
        data = report_to_dict(report)

        assert data["would_demote_count"] == 1
        assert data["demoted_count"] == 0
        assert data["mode"] == "report_only"
        assert len(data["outcomes"]) == 1
        assert data["outcomes"][0]["entity_id"] == "p1"


class TestDefaultPostureBothPoliciesInert:
    """Acceptance test: with BOTH policies at default ``enabled=False``, a
    run changes nothing and emits only a report-only event — the default
    posture is inert end to end, not just for one policy in isolation."""

    def test_default_posture_changes_nothing_and_reports_only(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            promote_policy = LearningCandidateAutoPromotePolicy()
            demote_policy = PrecedentAutoDemotePolicy()
            assert promote_policy.enabled is False
            assert demote_policy.enabled is False

            candidate = _candidate("cand-inert")
            promote_report = run_candidate_auto_promotion(
                candidates=[candidate],
                policy=promote_policy,
                event_log=registry.operational.event_log,
                executor=executor,
                graph_store=registry.knowledge.graph_store,
            )
            assert promote_report.mode == "report_only"
            assert promote_report.promoted_count == 0

            demote_report = run_precedent_auto_demotion(
                policy=demote_policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert demote_report.mode == "report_only"
            assert demote_report.demoted_count == 0

            mutation_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED
            )
            assert mutation_events == []

            evaluated_events = registry.operational.event_log.get_events(
                event_type=EventType.LEARNING_AUTO_PROMOTE_EVALUATED
            )
            assert len(evaluated_events) == 1
            demote_evaluated_events = registry.operational.event_log.get_events(
                event_type=EventType.LEARNING_AUTO_DEMOTE_EVALUATED
            )
            assert len(demote_evaluated_events) == 1
        finally:
            registry.close()


class TestEndToEndLoopClosure:
    """Acceptance test: auto-promote, accumulate unhelpful citations past
    ``promoted_at``, auto-demote commits, then re-running auto-promotion
    on the *same* candidate still reports ``already_promoted`` — the
    candidate is never re-proposed just because its precedent was
    demoted."""

    def test_promote_then_demote_then_repromote_attempt_stays_already_promoted(
        self, tmp_path: Path
    ) -> None:
        registry = StoreRegistry(stores_dir=tmp_path / "stores")
        try:
            executor = build_curate_executor(registry)
            promote_policy = LearningCandidateAutoPromotePolicy(
                enabled=True, mode="commit", demotion_enabled=True
            )
            candidate = _candidate("cand-cycle")

            promote_report = run_candidate_auto_promotion(
                candidates=[candidate],
                policy=promote_policy,
                event_log=registry.operational.event_log,
                executor=executor,
                graph_store=registry.knowledge.graph_store,
            )
            assert promote_report.promoted_count == 1
            entity_id = promote_report.outcomes[0].entity_id

            promote_events = registry.operational.event_log.get_events(
                event_type=EventType.MUTATION_EXECUTED,
                entity_id=entity_id,
                payload_filters={
                    "operation": Operation.PRECEDENT_PROMOTE.value,
                    "status": "success",
                },
            )
            assert len(promote_events) == 1
            promoted_at = promote_events[0].occurred_at

            _give_two_unhelpful_citations(
                registry.operational.event_log, entity_id, promoted_at=promoted_at
            )

            demote_policy = PrecedentAutoDemotePolicy(enabled=True, mode="commit")
            demote_report = run_precedent_auto_demotion(
                policy=demote_policy,
                event_log=registry.operational.event_log,
                graph_store=registry.knowledge.graph_store,
                executor=executor,
            )
            assert demote_report.demoted_count == 1
            assert demote_report.outcomes[0].entity_id == entity_id
            assert demote_report.outcomes[0].disposition == "demoted"

            repromote_report = run_candidate_auto_promotion(
                candidates=[candidate],
                policy=promote_policy,
                event_log=registry.operational.event_log,
                executor=executor,
                graph_store=registry.knowledge.graph_store,
            )
            assert repromote_report.promoted_count == 0
            assert repromote_report.outcomes[0].disposition == "already_promoted"

            node = registry.knowledge.graph_store.get_node(entity_id)
            assert node is not None
            assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        finally:
            registry.close()
