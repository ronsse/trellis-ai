"""``PrecedentRecord`` ids: every content field is hashed, found from the model.

The variants below are generated from ``model_fields``, so a field added to the
record or to a nested model is covered the day it lands; a hand-kept list of
variants is how a new field escapes the hash unnoticed.
"""

from __future__ import annotations

import typing
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel, create_model

from tests.precedent_candidates import recovering_trace
from trellis.learning.precedent_distill import distill_trace
from trellis.schemas.precedent_record import (
    ID_EXCLUDED_FIELDS,
    PrecedentRecord,
    derive_candidate_id,
)

#: The leaf fields a hand count of the schema finds: rule, situation (4),
#: action (2), because, evidence (5), confidence, provenance_tier,
#: distiller_version. Fewer means the walk below lost a branch.
HAND_READ_LEAF_FLOOR = 16


def _real() -> PrecedentRecord:
    return distill_trace(recovering_trace()).records[0]


def _other(value: Any, annotation: Any) -> Any:
    """A value of the same field that differs from ``value``."""
    args = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) is typing.Literal:
        return next(a for a in get_args(annotation) if a != value)
    if value is None:
        return "other-domain"
    if isinstance(value, str):
        return value + "-other"
    if isinstance(value, float):
        return 0.25 if value != 0.25 else 0.75
    if isinstance(value, list):
        item = args[0] if args else str
        return [*value, 99 if item is int else "other-item"]
    message = f"no variant rule for {annotation!r}"
    raise AssertionError(message)


def _leaves(model: BaseModel, path: tuple[str, ...] = ()):
    for name, field in type(model).model_fields.items():
        if not path and name in ID_EXCLUDED_FIELDS:
            continue
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            yield from _leaves(value, (*path, name))
        else:
            yield (*path, name), value, field.annotation


def _with(model: BaseModel, path: tuple[str, ...], new: Any) -> BaseModel:
    head, *rest = path
    if rest:
        new = _with(getattr(model, head), tuple(rest), new)
    return model.model_copy(update={head: new})


def test_the_leaf_walk_finds_every_content_field() -> None:
    assert len(list(_leaves(_real()))) >= HAND_READ_LEAF_FLOOR


def test_changing_any_one_content_field_changes_the_id() -> None:
    real = _real()
    ids: dict[tuple[str, ...], str] = {}
    for path, value, annotation in _leaves(real):
        changed = _with(real, path, _other(value, annotation))
        assert changed != real, path
        ids[path] = changed.expected_candidate_id()

    assert real.expected_candidate_id() == real.candidate_id
    assert real.candidate_id not in ids.values(), [
        p for p, i in ids.items() if i == real.candidate_id
    ]
    assert len(set(ids.values())) == len(ids)


def test_the_re_gate_s_partial_drops_are_all_visible_in_the_id() -> None:
    # The shapes of mutant that dropped a group of fields from the old hand list.
    real = _real()
    groups = [
        [("evidence", "feedback_ids"), ("evidence", "pack_ids")],
        [("situation", "domain"), ("situation", "entity_ids")],
        [("situation", "intent_family")],
    ]
    for group in groups:
        for path in group:
            value = getattr(getattr(real, path[0]), path[1])
            changed = _with(
                real, path, _other(value, list[str] if isinstance(value, list) else str)
            )
            assert changed.expected_candidate_id() != real.candidate_id, path


def test_only_the_id_and_the_one_valued_lifecycle_are_left_out() -> None:
    assert {"candidate_id", "lifecycle"} == ID_EXCLUDED_FIELDS
    assert len(get_args(PrecedentRecord.model_fields["lifecycle"].annotation)) == 1

    real = _real()
    renamed = real.model_copy(update={"candidate_id": "pcand_something_else"})
    assert renamed.expected_candidate_id() == real.candidate_id


def test_a_field_added_to_the_record_is_hashed_without_touching_the_encoder() -> None:
    wider = create_model("WiderRecord", __base__=PrecedentRecord, note=(str, ""))
    real = _real()
    one = wider(**real.model_dump(), note="a")
    other = wider(**real.model_dump(), note="b")

    assert one.expected_candidate_id() != other.expected_candidate_id()
    assert one.expected_candidate_id() != real.expected_candidate_id()


def test_a_key_added_to_the_dump_is_hashed() -> None:
    dump = _real().model_dump(mode="json")

    assert derive_candidate_id({**dump, "new_key": 1}) != derive_candidate_id(dump)
    nested = {**dump, "evidence": {**dump["evidence"], "new_list": ["a"]}}
    assert derive_candidate_id(nested) != derive_candidate_id(dump)


def test_derive_builds_a_record_whose_id_is_its_content_id() -> None:
    real = _real()
    content = {
        k: v for k, v in real.model_dump().items() if k not in ID_EXCLUDED_FIELDS
    }

    built = PrecedentRecord.derive(**content)

    assert built == real
    assert built.candidate_id.startswith("pcand_")


@pytest.mark.parametrize("field", ["feedback_ids", "pack_ids", "merged_from"])
def test_evidence_lists_are_order_free_but_membership_counts(field: str) -> None:
    real = _real()
    base = real.evidence.model_copy(update={field: ["a", "b"]})
    swapped = real.evidence.model_copy(update={field: ["b", "a"]})
    fewer = real.evidence.model_copy(update={field: ["a"]})

    def with_evidence(evidence):
        return real.model_copy(update={"evidence": evidence}).expected_candidate_id()

    assert with_evidence(base) == with_evidence(swapped)
    assert with_evidence(base) != with_evidence(fewer)


def test_an_empty_merged_from_leaves_every_existing_id_unchanged() -> None:
    """``merged_from`` is additive: a record that merged nothing hashes as it did."""
    real = _real()
    assert real.evidence.merged_from == []
    dumped = real.model_dump(mode="json")
    legacy_evidence = {
        k: v for k, v in dumped["evidence"].items() if k != "merged_from"
    }
    assert "merged_from" in dumped["evidence"]
    assert derive_candidate_id({**dumped, "evidence": legacy_evidence}) == (
        real.candidate_id
    )


def test_a_merged_record_never_shares_the_id_of_either_parent() -> None:
    real = _real()
    merged = real.model_copy(
        update={
            "evidence": real.evidence.model_copy(
                update={"merged_from": [real.candidate_id, "pcand_other"]}
            )
        }
    )
    assert merged.expected_candidate_id() != real.candidate_id
