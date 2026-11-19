"""A reviewer's decline moves the displayed agreement rate and nothing else.

``compute_agreement_metric`` is a display counter on the loop-health row.
A decline is the disagreement it could never see before; these tests pin
that it now counts, that the latest decision per candidate wins, and that
the auto-promote decision itself is identical with and without decline
events in the EventLog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning.candidate_auto_promote import (
    LearningCandidateAutoPromotePolicy,
    compute_agreement_metric,
    run_candidate_auto_promotion,
)
from trellis.learning.review_decisions import read_learning_decisions
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _would_promote(event_log: EventLog, *ids: str) -> None:
    event_log.emit(
        EventType.LEARNING_AUTO_PROMOTE_EVALUATED,
        source="candidate_auto_promote",
        payload={
            "dispositions": [
                {"candidate_id": i, "disposition": "would_promote"} for i in ids
            ]
        },
    )


def _decide(event_log: EventLog, action: str, *ids: str) -> None:
    event_log.emit(
        EventType.REVIEW_DECISION_RECORDED,
        source="trellis_api.review",
        payload={
            "surface": "learning_promotion",
            "action": action,
            "candidate_ids": list(ids),
        },
    )


def _candidate(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "recommendation_type": "promote_precedent",
        "precedent_name": f"Precedent for {candidate_id}",
        "citation_evidence": {
            "helpful_count": 5,
            "distinct_helpful_sessions": 3,
            "unhelpful_count": 0,
        },
        "target_entity_ids": [],
    }


class TestAgreementCountsDeclines:
    def test_a_decline_is_a_disagreement(self, event_log: EventLog) -> None:
        _would_promote(event_log, "cand-1", "cand-2", "cand-3")
        _decide(event_log, "promote", "cand-1")
        _decide(event_log, "decline", "cand-2")

        metric = compute_agreement_metric(event_log)

        assert metric.agree == 1
        assert metric.disagree == 1
        assert metric.undecided == 1
        assert metric.rate == 0.5

    def test_the_latest_decision_per_candidate_wins(self, event_log: EventLog) -> None:
        # Asymmetric on purpose: folding newest-first instead would read
        # (agree, disagree) as (1, 2) and flip every candidate's state.
        _would_promote(event_log, "cand-1", "cand-2", "cand-3")
        _decide(event_log, "decline", "cand-1")
        _decide(event_log, "promote", "cand-1")
        _decide(event_log, "promote", "cand-2")
        _decide(event_log, "decline", "cand-2")
        _decide(event_log, "decline", "cand-3")
        _decide(event_log, "promote", "cand-3")

        metric = compute_agreement_metric(event_log)
        states = read_learning_decisions(event_log)

        assert (metric.agree, metric.disagree, metric.undecided) == (2, 1, 0)
        assert {cid: s.last_action for cid, s in states.items()} == {
            "cand-1": "promote",
            "cand-2": "decline",
            "cand-3": "promote",
        }

    def test_other_surfaces_and_actions_are_ignored(self, event_log: EventLog) -> None:
        _would_promote(event_log, "cand-1")
        event_log.emit(
            EventType.REVIEW_DECISION_RECORDED,
            source="trellis_api.review",
            payload={
                "surface": "tuner_proposal",
                "action": "reject",
                "candidate_ids": ["cand-1"],
            },
        )
        metric = compute_agreement_metric(event_log)
        assert (metric.agree, metric.disagree, metric.undecided) == (0, 0, 1)
        assert metric.rate is None


class TestReadLearningDecisions:
    def test_last_declined_at_survives_a_later_promote(
        self, event_log: EventLog
    ) -> None:
        _decide(event_log, "decline", "cand-1")
        _decide(event_log, "promote", "cand-1")
        state = read_learning_decisions(event_log)["cand-1"]
        assert state.last_action == "promote"
        assert state.last_declined_at is not None


class TestAutoPromoteIgnoresDeclines:
    """Declines are display-only: the auto-promote decision never reads them."""

    def _run(self, event_log: EventLog) -> list[tuple[str, str, str]]:
        report = run_candidate_auto_promotion(
            candidates=[_candidate("cand-1"), _candidate("cand-2")],
            policy=LearningCandidateAutoPromotePolicy(enabled=True, mode="report_only"),
            event_log=event_log,
        )
        return [(o.candidate_id, o.disposition, o.reason) for o in report.outcomes]

    def test_decision_is_identical_with_and_without_decline_events(
        self, tmp_path: Path
    ) -> None:
        clean = SQLiteEventLog(tmp_path / "clean.db")
        declined = SQLiteEventLog(tmp_path / "declined.db")
        try:
            _decide(declined, "decline", "cand-1", "cand-2")
            _decide(declined, "decline", "cand-2")
            without = self._run(clean)
            with_declines = self._run(declined)
        finally:
            clean.close()
            declined.close()
        assert without == with_declines
        # Both candidates still qualify: a decline gates nothing.
        assert [d for _, d, _ in with_declines] == ["would_promote", "would_promote"]
