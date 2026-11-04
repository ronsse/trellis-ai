"""The typed reader for ``PACK_ASSEMBLED`` / ``FEEDBACK_RECORDED`` rows.

Pins the two policies every served-row reader has to state (sectioned packs,
withheld packs), the shape of a row from the real writers, and both
``FEEDBACK_RECORDED`` payload families.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from trellis.feedback.models import PackFeedback
from trellis.feedback.pack_rows import (
    FeedbackFamily,
    FeedbackRow,
    ServedRow,
    feedback_row,
    has_served_rows,
    iter_feedback_rows,
    iter_served_rows,
    served_rows_from_payload,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.schemas.pack import PackItem, PackSection
from trellis.stores.base.event_log import Event, EventType


def _item(item_id: str, rank: int = 1, strategy: str = "keyword") -> dict[str, Any]:
    return {
        "item_id": item_id,
        "item_type": "document",
        "rank": rank,
        "estimated_tokens": 40,
        "strategy_source": strategy,
    }


def _pack_event(payload: dict[str, Any], entity_id: str | None = "pk-1") -> Event:
    return Event(
        event_type=EventType.PACK_ASSEMBLED,
        source="test",
        entity_id=entity_id,
        payload=payload,
    )


def _feedback_event(payload: dict[str, Any], entity_id: str | None = None) -> Event:
    return Event(
        event_type=EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id=entity_id,
        payload=payload,
    )


def _full_feedback(**fields: Any) -> dict[str, Any]:
    """A payload from the real ``PackFeedback`` writer (the full family)."""
    base: dict[str, Any] = {
        "run_id": "r1",
        "phase": "",
        "intent": "",
        "outcome": "success",
        "items_served": [],
        "items_referenced": [],
    }
    return PackFeedback(**{**base, **fields}).to_event_payload(pack_id="pk-1")


def _read(
    payload: dict[str, Any], *, sectioned: bool, holdout: bool
) -> list[ServedRow]:
    return list(
        served_rows_from_payload(
            "pk-1", payload, include_sectioned=sectioned, include_holdout=holdout
        )
    )


class TestPolicyIsExplicit:
    def test_no_policy_kwarg_has_a_default(self) -> None:
        """A caller must state both policies; neither may be inherited."""
        for fn in (iter_served_rows, served_rows_from_payload):
            params = inspect.signature(fn).parameters
            for name in ("include_sectioned", "include_holdout"):
                assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
                assert params[name].default is inspect.Parameter.empty

    def test_omitting_a_policy_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            iter_served_rows(_pack_event({}), include_sectioned=True)  # type: ignore[call-arg]


class TestFlatPack:
    def test_rows_are_typed_and_in_payload_order(self) -> None:
        payload = {"injected_items": [_item("a", 1), _item("b", 2, "semantic")]}
        rows = _read(payload, sectioned=False, holdout=False)
        assert [r.item_id for r in rows] == ["a", "b"]
        first = rows[0]
        assert first.pack_id == "pk-1"
        assert first.item_type == "document"
        assert first.rank == 1
        assert first.section is None
        assert first.strategy_source == "keyword"
        assert first.estimated_tokens == 40
        assert first.row["item_id"] == "a"
        assert rows[1].strategy_source == "semantic"

    def test_event_form_stamps_entity_id_as_pack_id(self) -> None:
        rows = list(
            iter_served_rows(
                _pack_event({"injected_items": [_item("a")]}, entity_id="pk-9"),
                include_sectioned=False,
                include_holdout=False,
            )
        )
        assert [(r.pack_id, r.item_id) for r in rows] == [("pk-9", "a")]

    def test_event_without_entity_id_stamps_empty_pack_id(self) -> None:
        rows = list(
            iter_served_rows(
                _pack_event({"injected_items": [_item("a")]}, entity_id=None),
                include_sectioned=False,
                include_holdout=False,
            )
        )
        assert [r.pack_id for r in rows] == [""]

    @pytest.mark.parametrize("sectioned", [True, False])
    @pytest.mark.parametrize("holdout", [True, False])
    def test_a_flat_served_pack_reads_the_same_under_every_policy(
        self, sectioned: bool, holdout: bool
    ) -> None:
        payload = {"injected_items": [_item("a")], "holdout": False}
        assert [
            r.item_id for r in _read(payload, sectioned=sectioned, holdout=holdout)
        ] == ["a"]


class TestSectionedPack:
    """#958: sectioned rows add ``section`` and a section-local ``rank``."""

    PAYLOAD: dict[str, Any] = {  # noqa: RUF012
        "sections": [
            {"name": "reference", "item_ids": ["a", "b"]},
            {"name": "domain", "item_ids": ["c"]},
        ],
        "injected_items": [
            {**_item("a", 1), "section": "reference"},
            {**_item("b", 2), "section": "reference"},
            {**_item("c", 1), "section": "domain"},
        ],
    }

    def test_included_rows_carry_section_and_section_local_rank(self) -> None:
        rows = _read(self.PAYLOAD, sectioned=True, holdout=True)
        assert [(r.item_id, r.section, r.rank) for r in rows] == [
            ("a", "reference", 1),
            ("b", "reference", 2),
            ("c", "domain", 1),
        ]

    def test_excluded_yields_nothing(self) -> None:
        assert _read(self.PAYLOAD, sectioned=False, holdout=True) == []

    def test_an_empty_sections_list_still_marks_a_sectioned_pack(self) -> None:
        payload = {"sections": [], "injected_items": [_item("a")]}
        assert _read(payload, sectioned=False, holdout=True) == []
        assert len(_read(payload, sectioned=True, holdout=True)) == 1

    def test_round_trips_the_real_writer(self) -> None:
        """Rows from ``_sectioned_injected_item_rows`` parse as written."""
        sections = [
            PackSection(
                name="reference",
                items=[
                    PackItem(
                        item_id="a",
                        item_type="document",
                        rank=1,
                        estimated_tokens=12,
                        strategy_source="keyword",
                    ),
                    PackItem(item_id="b", item_type="document", rank=2),
                ],
            ),
            PackSection(
                name="domain",
                items=[PackItem(item_id="c", item_type="trace", rank=1)],
            ),
        ]
        payload = {
            "sections": [],
            "injected_items": PackBuilder._sectioned_injected_item_rows(sections),
        }
        rows = _read(payload, sectioned=True, holdout=False)
        assert [(r.item_id, r.section, r.rank, r.item_type) for r in rows] == [
            ("a", "reference", 1, "document"),
            ("b", "reference", 2, "document"),
            ("c", "domain", 1, "trace"),
        ]
        assert rows[0].strategy_source == "keyword"
        assert rows[0].estimated_tokens == 12
        assert rows[1].strategy_source is None
        assert rows[1].estimated_tokens is None

    def test_round_trips_the_real_flat_writer(self) -> None:
        payload = {
            "injected_items": PackBuilder._injected_item_rows(
                [PackItem(item_id="a", item_type="document", rank=1)]
            )
        }
        rows = _read(payload, sectioned=False, holdout=False)
        assert [(r.item_id, r.section, r.rank) for r in rows] == [("a", None, 1)]


class TestHeldOutPack:
    #: What the builder writes for a withheld pack: the empty pack the caller
    #: saw, and the would-be rows under ``holdout_items``.
    BUILDER_SHAPE: dict[str, Any] = {  # noqa: RUF012
        "holdout": True,
        "holdout_rate": 0.5,
        "injected_items": [],
        "holdout_items": [_item("hidden")],
    }
    #: A payload the builder never writes, to separate the switch from the data.
    HAND_BUILT: dict[str, Any] = {  # noqa: RUF012
        "holdout": True,
        "injected_items": [_item("a")],
    }

    def test_excluded_yields_nothing_whatever_the_payload_holds(self) -> None:
        assert _read(self.BUILDER_SHAPE, sectioned=True, holdout=False) == []
        assert _read(self.HAND_BUILT, sectioned=True, holdout=False) == []

    def test_included_reads_injected_items_as_written(self) -> None:
        assert _read(self.HAND_BUILT, sectioned=True, holdout=True)[0].item_id == "a"

    def test_the_would_be_rows_are_never_read(self) -> None:
        """``holdout_items`` is the blind arm; no policy turns it into a serving."""
        assert _read(self.BUILDER_SHAPE, sectioned=True, holdout=True) == []

    def test_only_a_literal_true_withholds(self) -> None:
        payload = {"holdout": "yes", "injected_items": [_item("a")]}
        assert len(_read(payload, sectioned=True, holdout=False)) == 1


class TestMalformedRows:
    def test_non_mapping_rows_are_skipped(self) -> None:
        payload = {"injected_items": ["a", None, 3, _item("b")]}
        assert [r.item_id for r in _read(payload, sectioned=True, holdout=True)] == [
            "b"
        ]

    def test_a_row_without_a_usable_item_id_is_still_a_row(self) -> None:
        """Whether it counts is the caller's call; several count it."""
        payload = {"injected_items": [{"item_type": "document"}, {"item_id": 7}]}
        rows = _read(payload, sectioned=True, holdout=True)
        assert [r.item_id for r in rows] == [None, None]
        assert rows[0].item_type == "document"

    @pytest.mark.parametrize("value", [None, "abc", b"abc", 5, {"item_id": "a"}])
    def test_a_non_list_is_no_rows(self, value: Any) -> None:
        assert _read({"injected_items": value}, sectioned=True, holdout=True) == []

    def test_absent_key_is_no_rows(self) -> None:
        assert _read({}, sectioned=True, holdout=True) == []

    def test_wrongly_typed_fields_read_as_none(self) -> None:
        row = _read(
            {
                "injected_items": [
                    {
                        "item_id": "a",
                        "item_type": 1,
                        "rank": True,
                        "section": 2,
                        "strategy_source": ["x"],
                        "estimated_tokens": "40",
                    }
                ]
            },
            sectioned=True,
            holdout=True,
        )[0]
        assert (
            row.item_type,
            row.rank,
            row.section,
            row.strategy_source,
            row.estimated_tokens,
        ) == (None, None, None, None, None)


class TestHasServedRows:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"injected_items": [_item("a")]}, True),
            ({"injected_items": []}, False),
            ({}, False),
            ({"injected_items": None}, False),
            ({"sections": [], "injected_items": [_item("a")]}, True),
        ],
    )
    def test_is_truthiness_of_the_raw_list(
        self, payload: dict[str, Any], expected: bool
    ) -> None:
        assert has_served_rows(payload) is expected


class TestFeedbackRows:
    def test_full_family_from_the_real_writer(self) -> None:
        payload = _full_feedback(
            intent_family="debug",
            rating=0.75,
            items_served=["a", "b", "c"],
            items_referenced=["a"],
            unhelpful_item_ids=["b"],
            followed_advisory_ids=["adv-1"],
            ignored_item_ids=["c"],
        )
        row = feedback_row(_feedback_event(payload, entity_id="pk-1"))
        assert row.family is FeedbackFamily.PACK_FEEDBACK
        assert row.pack_id == "pk-1"
        assert row.feedback_id == payload["feedback_id"]
        assert row.rating == 0.75
        assert row.success is True
        assert row.intent_family == "debug"
        assert row.helpful_item_ids == ("a",)
        assert row.unhelpful_item_ids == ("b",)
        assert row.followed_advisory_ids == ("adv-1",)
        assert row.ignored_item_ids == ("c",)
        assert row.target_id is None

    def test_full_family_without_an_ignored_claim_reads_none_not_empty(self) -> None:
        payload = _full_feedback(items_referenced=["a"])
        assert "ignored_item_ids" not in payload
        assert feedback_row(_feedback_event(payload)).ignored_item_ids is None

    def test_governed_family_carries_no_attribution(self) -> None:
        row = feedback_row(
            _feedback_event(
                {
                    "target_id": "trace-1",
                    "rating": 0.0,
                    "comment": "bad",
                    "success": False,
                    "pack_id": " pk-2 ",
                },
                entity_id="trace-1",
            )
        )
        assert row.family is FeedbackFamily.GOVERNED
        assert row.pack_id == "pk-2"
        assert row.target_id == "trace-1"
        assert row.success is False
        assert row.feedback_id is None
        assert row.intent_family is None
        assert row.helpful_item_ids == ()
        assert row.unhelpful_item_ids == ()
        assert row.followed_advisory_ids == ()
        assert row.ignored_item_ids is None

    def test_governed_family_names_no_pack_unless_the_payload_does(self) -> None:
        """The entity id is a target here and must not be read as a pack."""
        row = feedback_row(
            _feedback_event(
                {"target_id": "pk-looking-id", "rating": 1.0, "success": True},
                entity_id="pk-looking-id",
            )
        )
        assert row.pack_id is None

    def test_a_payload_with_no_discriminator_is_unknown(self) -> None:
        row = feedback_row(_feedback_event({"rating": 1.0}))
        assert row.family is FeedbackFamily.UNKNOWN

    def test_ids_are_cleaned_and_deduplicated_in_order(self) -> None:
        row = feedback_row(
            _feedback_event(
                {
                    "feedback_id": "f1",
                    "helpful_item_ids": ["b", "a", "b", "", None, 3],
                    "unhelpful_item_ids": "not-a-list",
                }
            )
        )
        assert row.helpful_item_ids == ("b", "a")
        assert row.unhelpful_item_ids == ()

    def test_wrongly_typed_scalars_read_as_none(self) -> None:
        row = feedback_row(
            _feedback_event(
                {"feedback_id": "f1", "rating": "high", "success": 1, "pack_id": 9}
            )
        )
        assert (row.rating, row.success, row.pack_id) == (None, None, None)

    def test_iter_feedback_rows_is_one_row_per_event_in_order(self) -> None:
        events = [
            _feedback_event({"feedback_id": "f1", "pack_id": "p1"}),
            _feedback_event({"target_id": "t", "pack_id": "p2"}),
        ]
        rows = list(iter_feedback_rows(events))
        assert all(isinstance(r, FeedbackRow) for r in rows)
        assert [r.pack_id for r in rows] == ["p1", "p2"]
        assert [r.family for r in rows] == [
            FeedbackFamily.PACK_FEEDBACK,
            FeedbackFamily.GOVERNED,
        ]
