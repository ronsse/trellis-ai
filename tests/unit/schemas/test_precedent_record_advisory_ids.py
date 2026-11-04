"""``evidence.advisory_ids``: hashed once it names an advisory, absent from old ids."""

from __future__ import annotations

from trellis.schemas.precedent_record import (
    OMIT_WHEN_EMPTY_EVIDENCE,
    PrecedentEvidence,
    derive_candidate_id,
)

#: ``derive_candidate_id`` of ``_OLD_SHAPE`` as computed by the code before the
#: field existed (origin/main at bbcb8c70). A record that cannot name an advisory
#: must keep this id, or every candidate already recorded would mint again.
OLD_SHAPE_ID = "pcand_35da6a727a395a1ea92d2c75"

_OLD_SHAPE = {
    "candidate_id": "x",
    "rule": "failed_step_avoid",
    "situation": {
        "intent_family": "general_context",
        "domain": "data",
        "entity_ids": ["e1"],
        "trigger": "schema mismatch",
    },
    "action": {"kind": "avoid", "text": "Avoid load_export with mode=strict"},
    "because": "It failed first.",
    "evidence": {
        "trace_ids": ["t1"],
        "step_indexes": [0, 1],
        "feedback_ids": [],
        "pack_ids": [],
    },
    "confidence": 0.6,
    "provenance_tier": "deterministic",
    "distiller_version": "deterministic-1",
    "lifecycle": "candidate",
}


def test_a_record_written_before_the_field_keeps_its_id() -> None:
    assert derive_candidate_id(_OLD_SHAPE) == OLD_SHAPE_ID


def test_an_empty_advisory_list_is_the_same_as_no_list() -> None:
    with_empty = {
        **_OLD_SHAPE,
        "evidence": {**_OLD_SHAPE["evidence"], "advisory_ids": []},
    }

    assert derive_candidate_id(with_empty) == OLD_SHAPE_ID


def test_a_named_advisory_changes_the_id_and_its_order_does_not() -> None:
    def named(*ids: str) -> str:
        evidence = {**_OLD_SHAPE["evidence"], "advisory_ids": list(ids)}
        return derive_candidate_id({**_OLD_SHAPE, "evidence": evidence})

    assert named("a") != OLD_SHAPE_ID
    assert named("a") != named("b")
    assert named("a", "b") == named("b", "a")
    assert named("a") != named("a", "b")


def test_advisory_ids_and_merged_from_are_left_out_while_empty() -> None:
    assert frozenset({"advisory_ids", "merged_from"}) == OMIT_WHEN_EMPTY_EVIDENCE
    assert "advisory_ids" in PrecedentEvidence.model_fields
