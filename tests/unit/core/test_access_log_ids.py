"""Which ids an access-log row keeps, and how they are bounded."""

from __future__ import annotations

from trellis.core import access_log
from trellis.core.access_log import (
    MAX_LOGGED_ITEM_IDS,
    ServedItems,
    begin_served_items,
    build_payload,
    clip_item_ids,
    end_served_items,
    note_printed_items,
    served_ids_in,
)

LONG = "x" * 300


class TestServedIdsIn:
    def test_only_ids_the_text_prints_in_backticks_are_kept(self) -> None:
        text = "## [doc] `a`\n- `b` (doc): excerpt\nand c is only mentioned"
        assert served_ids_in(text, ["a", "b", "c", "d"]) == ["a", "b"]

    def test_an_id_counts_once_and_order_is_the_pack_s(self) -> None:
        assert served_ids_in("`b` `a` `b`", ["a", "b", "a", "b"]) == ["a", "b"]

    def test_an_empty_id_is_never_served(self) -> None:
        assert served_ids_in("`` ``", ["", "a"]) == []

    def test_known_bound_a_body_that_quotes_a_pack_id_counts_it(self) -> None:
        # Item a's body quotes `b`; b's own entry was cut. b still reads as
        # printed. Documented in operations.md; never an id outside the pack.
        text = "## [doc] `a`\nsee also `b` and `not-in-the-pack`"
        assert served_ids_in(text, ["a", "b"]) == ["a", "b"]
        assert "not-in-the-pack" not in served_ids_in(text, ["a", "b"])


class TestNotePrintedItems:
    def test_it_fills_the_holder_with_the_printed_ids(self) -> None:
        holder, token = begin_served_items()
        try:
            note_printed_items("`a` `b`", iter(["a", "c", "b"]))
            assert holder.result() == (["a", "b"], 2)
        finally:
            end_served_items(token)

    def test_a_pack_with_nothing_printed_is_a_real_zero(self) -> None:
        holder, token = begin_served_items()
        try:
            note_printed_items("no ids here", ["a"])
            assert holder.result() == ([], 0)
        finally:
            end_served_items(token)

    def test_it_is_a_no_op_with_no_holder_and_never_raises(self) -> None:
        failure = RuntimeError("the pack failed while it was read")

        def broken():
            yield "a"
            raise failure

        note_printed_items("`a`", ["a"])
        holder, token = begin_served_items()
        try:
            note_printed_items("`a`", broken())
        finally:
            end_served_items(token)
        assert holder is not None


class TestIdsAreClipped:
    def test_the_holder_clips_an_id_to_200_characters(self) -> None:
        holder = ServedItems()
        holder.add([LONG])
        assert holder.ids == [LONG[:200]]
        assert len(holder.ids[0]) == 200

    def test_clip_item_ids_clips_each_id_and_caps_the_list(self) -> None:
        kept = clip_item_ids([LONG] + [str(n) for n in range(100)])
        assert len(kept) == MAX_LOGGED_ITEM_IDS
        assert kept[0] == LONG[:200]

    def test_a_payload_clips_the_id_and_keeps_the_count(self) -> None:
        payload = build_payload(
            key_id="k", surface="rest", route="GET /x", item_ids=[LONG], item_count=1
        )
        assert payload["item_ids"] == [LONG[:200]]
        assert payload["item_count"] == 1
        assert access_log.MAX_LOGGED_ITEM_IDS == 25
