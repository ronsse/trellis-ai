"""The report layer of the two-stage precedent gate: windows in, counts out."""

from __future__ import annotations

from typing import Any

from trellis.learning.candidate_auto_promote import LearningCandidateAutoPromotePolicy
from trellis.learning.precedent_gate_report import (
    GENERAL_FAMILY,
    build_graded_window,
    evaluate_gate,
    recorded_candidates_in_packs,
)
from trellis.learning.scoring import PROMOTE_RECOMMENDATIONS
from trellis.stores.base.event_log import Event, EventType

TARGET = "doc-target"
PROMOTE = min(PROMOTE_RECOMMENDATIONS)


def _pack(
    pack_id: str,
    items: list[str],
    *,
    session: str | None = "s1",
    holdout: bool = False,
    sectioned: bool = False,
) -> Event:
    payload: dict[str, Any] = {
        "injected_items": [
            {"item_id": item, "item_type": "document", "rank": n + 1}
            for n, item in enumerate(items)
        ],
    }
    if session is not None:
        payload["session_id"] = session
    if holdout:
        payload["holdout"] = True
    if sectioned:
        payload["sections"] = ["lessons"]
        for row in payload["injected_items"]:
            row["section"] = "lessons"
    return Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=pack_id,
        entity_type="pack",
        payload=payload,
    )


def _feedback(
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
    family: str | None = None,
) -> Event:
    payload: dict[str, Any] = {
        "pack_id": pack_id,
        "helpful_item_ids": list(helpful),
        "unhelpful_item_ids": list(unhelpful),
    }
    if family:
        payload["intent_family"] = family
    return Event(
        event_type=EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id=f"fb-{pack_id}",
        payload=payload,
    )


def _items(pack_id: str, n: int = 12) -> list[str]:
    return [TARGET, *(f"{pack_id}-x{i}" for i in range(n - 1))]


# ------------------------------------------------------------ the window


def test_a_graded_pack_carries_its_verdicts_session_and_rows() -> None:
    window = build_graded_window(
        [_pack("p1", ["a", "b", "c"], session=" s9 ")],
        [_feedback("p1", helpful=("a",), unhelpful=("b",))],
    )

    (pack,) = window.packs
    assert pack.pack_id == "p1"
    assert pack.session_id == "s9"
    assert pack.helpful == frozenset({"a"})
    assert pack.unhelpful == frozenset({"b"})
    assert pack.served_ids == frozenset({"a", "b", "c"})
    assert pack.intent_family == GENERAL_FAMILY


def test_two_graders_on_one_pack_are_one_pack_with_unioned_verdicts() -> None:
    window = build_graded_window(
        [_pack("p1", ["a", "b"])],
        [
            _feedback("p1", helpful=("a",)),
            _feedback("p1", helpful=("a",), unhelpful=("a", "b")),
        ],
    )

    (pack,) = window.packs
    assert pack.helpful == frozenset({"a"})
    assert pack.verdict("a") == "helpful"  # helpful wins on the same item
    assert pack.verdict("b") == "unhelpful"


def test_an_ungraded_pack_is_served_but_not_judged() -> None:
    window = build_graded_window([_pack("p1", ["a", "b"])], [])

    assert window.packs == ()
    assert window.served_item_ids == frozenset({"a", "b"})
    assert window.packs_with_rows == 1
    assert window.packs_graded == 0


def test_a_pack_with_no_item_rows_is_not_attributable() -> None:
    bare = Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="old",
        payload={"session_id": "s1"},
    )

    window = build_graded_window([bare], [_feedback("old", helpful=("a",))])

    assert window.packs == ()
    assert window.packs_with_rows == 0
    assert window.packs_scanned == 1


def test_held_out_packs_and_their_feedback_reach_nothing_and_are_counted() -> None:
    # A defective writer could leave rows on a withheld pack; none may count.
    held = _pack("held", _items("held"), holdout=True)
    served = _pack("ok", _items("ok"))

    window = build_graded_window(
        [held, served],
        [
            _feedback("held", helpful=(TARGET,)),
            _feedback("ok", helpful=(TARGET,)),
        ],
    )

    assert [p.pack_id for p in window.packs] == ["ok"]
    assert window.packs_held_out == 1
    assert window.feedback_held_out == 1
    assert window.packs_scanned == 2
    assert window.feedback_scanned == 2
    assert "held-x0" not in window.served_item_ids


def test_a_sectioned_pack_is_judged_like_a_flat_one() -> None:
    window = build_graded_window(
        [_pack("sec", ["a", "b"], sectioned=True)],
        [_feedback("sec", helpful=("a",))],
    )

    (pack,) = window.packs
    assert {row.section for row in pack.served} == {"lessons"}
    assert pack.size == 2


def test_the_family_comes_from_the_feedback_else_the_default() -> None:
    window = build_graded_window(
        [_pack("p1", ["a"]), _pack("p2", ["a"])],
        [
            _feedback("p1", helpful=("a",), family="debugging"),
            _feedback("p2", helpful=("a",)),
        ],
    )

    assert {p.pack_id: p.intent_family for p in window.packs} == {
        "p1": "debugging",
        "p2": GENERAL_FAMILY,
    }


# ------------------------------------------------------------ the gates


def _policy() -> LearningCandidateAutoPromotePolicy:
    return LearningCandidateAutoPromotePolicy()


def _candidate(candidate_id: str, **evidence: int) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "intent_family": GENERAL_FAMILY,
        "recommendation_type": PROMOTE,
        "item_id": TARGET,
        "citation_evidence": {
            "helpful_count": evidence.get("helpful", 0),
            "unhelpful_count": evidence.get("unhelpful", 0),
            "distinct_helpful_sessions": evidence.get("sessions", 0),
        },
    }


def _window_citing_target(n: int, *, sessions: int):
    packs = [
        _pack(f"p{i}", _items(f"p{i}", 60), session=f"s{i % sessions}")
        for i in range(n)
    ]
    feedback = [_feedback(f"p{i}", helpful=(TARGET,)) for i in range(n)]
    return build_graded_window(packs, feedback)


def test_the_two_gates_are_counted_side_by_side() -> None:
    window = _window_citing_target(4, sessions=2)
    candidates = [
        _candidate("both", helpful=6, sessions=4),  # current gate passes
        _candidate("neither", helpful=1, sessions=1),
        {**_candidate("odd", helpful=1), "recommendation_type": "investigate_noise"},
        {**_candidate(""), "item_id": TARGET},  # no usable id
        "not a mapping",
    ]

    report = evaluate_gate(candidates, window, policy=_policy())

    assert report["evaluated"] == 3
    assert report["unusable_candidates"] == 2
    assert report["not_promotable"] == 1
    assert report["promotable"] == 2
    # Both promotable rows name the same item, so the new gate passes both.
    assert report["new_gate_pass"] == 2
    assert report["current_gate_pass"] == 1
    assert (report["both"], report["only_new"], report["only_current"]) == (1, 1, 0)
    assert report["failed_at"] == {
        "helpful": 0,
        "unhelpful": 0,
        "sessions": 0,
        "p_value": 0,
        "lift": 0,
    }
    assert {row["candidate_id"]: row["current_gate"] for row in report["targets"]} == {
        "both": True,
        "neither": False,
    }


def test_attrition_counts_the_first_failed_criterion_once_per_candidate() -> None:
    window = _window_citing_target(4, sessions=1)  # one session only

    report = evaluate_gate([_candidate("c1")], window, policy=_policy())

    assert report["new_gate_pass"] == 0
    assert report["failed_at"]["sessions"] == 1
    assert sum(report["failed_at"].values()) == 1
    assert report["targets"] == []


def test_a_candidate_served_nowhere_fails_at_helpful_and_is_counted() -> None:
    report = evaluate_gate(
        [_candidate("c1")], build_graded_window([], []), policy=_policy()
    )

    assert report["failed_at"]["helpful"] == 1
    assert report["without_graded_servings"] == 1


def test_a_candidate_is_judged_over_its_own_family_only() -> None:
    packs = [_pack(f"p{i}", _items(f"p{i}", 60), session=f"s{i}") for i in range(4)]
    feedback = [
        _feedback(f"p{i}", helpful=(TARGET,), family="debugging") for i in range(4)
    ]
    window = build_graded_window(packs, feedback)

    elsewhere = evaluate_gate([_candidate("c1")], window, policy=_policy())
    at_home = evaluate_gate(
        [{**_candidate("c1"), "intent_family": "debugging"}], window, policy=_policy()
    )

    assert elsewhere["new_gate_pass"] == 0
    assert at_home["new_gate_pass"] == 1


def test_the_report_names_the_thresholds_and_the_differences() -> None:
    report = evaluate_gate([], build_graded_window([], []), policy=_policy())

    assert report["new_gate"]["min_helpful"] == 3
    assert report["new_gate"]["max_p_value"] == 0.05
    assert report["new_gate"]["min_lift"] == 1.2
    assert report["current_gate"]["require_zero_unhelpful"] is True
    assert report["differences"]


def test_recorded_candidates_found_in_packs_are_counted() -> None:
    window = build_graded_window([_pack("p1", ["pcand_a", "doc"])], [])

    assert recorded_candidates_in_packs({"pcand_a", "pcand_b"}, window) == 1
    assert recorded_candidates_in_packs(set(), window) == 0
