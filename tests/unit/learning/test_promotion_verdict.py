"""The one reading of a promotion run's outcomes, shared by every surface."""

from __future__ import annotations

from typing import Any

import pytest

from trellis.learning.scoring import (
    promotion_is_refusal,
    promotion_landed,
    promotion_verdict,
    settle_promotion,
    unnamed_approved_count,
)

LANDED: dict[str, Any] = {
    "status": "promoted",
    "edges": [{"status": "success"}],
    "precedent_event_status": "success",
}
EDGE_REJECTED = {**LANDED, "edges": [{"status": "success"}, {"status": "rejected"}]}
EVENT_FAILED = {**LANDED, "precedent_event_status": "failed"}
ENTITY_FAILED = {"status": "entity_failed"}
REFUSED = {"status": "refused_demoted"}
MISSING = {"status": "missing_candidate"}
NON_PROMOTABLE = {"status": "skipped_non_promotable"}


class TestLanded:
    def test_a_promotion_with_no_edges_lands_when_its_event_did(self) -> None:
        assert promotion_landed({**LANDED, "edges": []})

    @pytest.mark.parametrize(
        "outcome",
        [EDGE_REJECTED, EVENT_FAILED, ENTITY_FAILED, REFUSED, {"status": "promoted"}],
        ids=["edge", "event", "entity", "refused", "no-event-status"],
    )
    def test_anything_short_of_whole_does_not(self, outcome: dict[str, Any]) -> None:
        assert not promotion_landed(outcome)

    def test_settle_relabels_only_a_half_landed_promotion(self) -> None:
        assert settle_promotion(dict(EDGE_REJECTED))["status"] == "promoted_incomplete"
        assert settle_promotion(dict(EVENT_FAILED))["status"] == "promoted_incomplete"
        assert settle_promotion(dict(LANDED))["status"] == "promoted"
        assert settle_promotion(dict(REFUSED))["status"] == "refused_demoted"


class TestRefusal:
    @pytest.mark.parametrize(
        "outcome", [REFUSED, {"status": "refused_not_current"}, MISSING, NON_PROMOTABLE]
    )
    def test_declined_before_anything_was_written(
        self, outcome: dict[str, Any]
    ) -> None:
        assert promotion_is_refusal(outcome)

    @pytest.mark.parametrize(
        "outcome", [ENTITY_FAILED, {"status": "promoted_incomplete"}, LANDED, {}]
    )
    def test_a_write_that_did_not_land_is_not_a_refusal(
        self, outcome: dict[str, Any]
    ) -> None:
        assert not promotion_is_refusal(outcome)


class TestVerdict:
    def test_no_outcomes_is_ok(self) -> None:
        assert promotion_verdict([]) == "ok"

    def test_all_landed_is_ok(self) -> None:
        assert promotion_verdict([LANDED, LANDED]) == "ok"

    @pytest.mark.parametrize("refusal", [REFUSED, MISSING, NON_PROMOTABLE])
    def test_a_refusal_is_refused(self, refusal: dict[str, Any]) -> None:
        assert promotion_verdict([refusal]) == "refused"
        assert promotion_verdict([LANDED, refusal]) == "refused"

    @pytest.mark.parametrize("failure", [ENTITY_FAILED, EDGE_REJECTED, EVENT_FAILED])
    def test_a_write_that_did_not_land_is_failed(self, failure: dict[str, Any]) -> None:
        assert promotion_verdict([failure]) == "failed"
        assert promotion_verdict([LANDED, failure]) == "failed"

    def test_promoted_incomplete_is_unlanded_not_refused(self) -> None:
        assert promotion_verdict([{"status": "promoted_incomplete"}]) == "failed"

    @pytest.mark.parametrize(
        "outcomes",
        [[REFUSED, ENTITY_FAILED], [ENTITY_FAILED, REFUSED], [MISSING, EDGE_REJECTED]],
    )
    def test_failed_outranks_refused_whatever_the_order(
        self, outcomes: list[dict[str, Any]]
    ) -> None:
        assert promotion_verdict(outcomes) == "failed"

    def test_the_answer_is_the_same_before_and_after_settling(self) -> None:
        run = [LANDED, REFUSED, EDGE_REJECTED]
        settled = [settle_promotion(dict(o)) for o in run]
        assert promotion_verdict(run) == promotion_verdict(settled) == "failed"


class TestUnnamedApprovedRows:
    """An approved row naming no candidate has no outcome but refuses the run."""

    def test_the_count_is_what_was_marked_less_what_was_named(self) -> None:
        assert unnamed_approved_count({"approved_count": 4, "named_count": 1}) == 3
        assert unnamed_approved_count({"approved_count": 2, "named_count": 2}) == 0

    def test_a_count_below_zero_is_none_not_a_credit(self) -> None:
        assert unnamed_approved_count({"approved_count": 0, "named_count": 1}) == 0

    def test_a_blank_id_alone_is_refused_not_ok(self) -> None:
        assert promotion_verdict([], unnamed_count=1) == "refused"

    def test_none_unnamed_changes_nothing(self) -> None:
        assert promotion_verdict([], unnamed_count=0) == "ok"
        assert promotion_verdict([LANDED], unnamed_count=0) == "ok"

    def test_a_blank_id_beside_landed_rows_refuses_the_run(self) -> None:
        assert promotion_verdict([LANDED, LANDED], unnamed_count=1) == "refused"

    @pytest.mark.parametrize("refusal", [REFUSED, MISSING, NON_PROMOTABLE])
    def test_a_blank_id_beside_a_refusal_is_still_refused(
        self, refusal: dict[str, Any]
    ) -> None:
        assert promotion_verdict([refusal], unnamed_count=2) == "refused"

    @pytest.mark.parametrize(
        "outcomes",
        [
            [ENTITY_FAILED],
            [LANDED, EDGE_REJECTED],
            [REFUSED, EVENT_FAILED],
            [EVENT_FAILED, REFUSED],
        ],
    )
    def test_failed_outranks_a_blank_id_refusal(
        self, outcomes: list[dict[str, Any]]
    ) -> None:
        assert promotion_verdict(outcomes, unnamed_count=1) == "failed"
