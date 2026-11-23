"""What a served item's provenance line is built from. Values are made up."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trellis.retrieve.disclosure import POINTER_SELECTION_REASON
from trellis.retrieve.provenance import provenance_inputs
from trellis.schemas.pack import PackItem
from trellis_wire.provenance import format_provenance_line

_NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _item(metadata: dict[str, Any], **fields: Any) -> PackItem:
    return PackItem(
        item_id="doc-1",
        item_type="document",
        excerpt="body",
        relevance_score=0.02,
        metadata=metadata,
        **fields,
    )


def test_source_clock_age_comes_from_the_stamp_scoring_read() -> None:
    item = _item(
        {
            "document_form": "reference",
            # updated_at wins over created_at in resolve_recency_stamp.
            "updated_at": "2026-02-19T00:00:00+00:00",
            "created_at": "2025-01-01T00:00:00+00:00",
            "recency_clock": "source",
        },
        strategy_source="keyword",
    )
    inputs = provenance_inputs(item, now=_NOW)
    assert inputs is not None
    assert inputs["age_days"] == pytest.approx(10.0)
    assert format_provenance_line(**inputs) == (
        "Provenance: reference document; 10 days old (source clock); via keyword"
    )


def test_unusable_updated_at_falls_through_like_the_resolver() -> None:
    item = _item(
        {
            "updated_at": "not a date",
            "created_at": "2026-02-27T00:00:00",
            "recency_clock": "source",
        }
    )
    inputs = provenance_inputs(item, now=_NOW)
    assert inputs is not None
    assert inputs["age_days"] == pytest.approx(2.0)


def test_row_clock_age_is_omitted_not_read_from_the_store() -> None:
    # The keyword axis stamped "row": the winning stamp was a store column,
    # which the item does not carry. A metadata stamp present on the item
    # is not the one scoring read, so it must not be shown as the age.
    item = _item(
        {
            "source_system": "example-tracker",
            "created_at": "2020-01-01T00:00:00+00:00",
            "recency_clock": "row",
        },
        strategy_source="keyword",
    )
    inputs = provenance_inputs(item, now=_NOW)
    assert inputs is not None
    assert inputs["age_days"] is None
    assert format_provenance_line(**inputs) == (
        "Provenance: from example-tracker; via keyword"
    )


@pytest.mark.parametrize("clock", ["none", None])
def test_no_usable_stamp_has_no_age(clock: str | None) -> None:
    meta: dict[str, Any] = {"created_at": "2026-02-01T00:00:00+00:00"}
    if clock is not None:
        meta["recency_clock"] = clock
    inputs = provenance_inputs(_item(meta), now=_NOW)
    assert inputs is not None
    assert inputs["age_days"] is None


def test_capture_and_curated_node_and_axis_fallback() -> None:
    capture = provenance_inputs(_item({"distilled": True}), now=_NOW)
    assert capture is not None
    assert capture["captured"] is True

    truthy_string = provenance_inputs(_item({"distilled": "yes"}), now=_NOW)
    assert truthy_string is not None
    assert truthy_string["captured"] is False

    node = provenance_inputs(
        _item({"node_role": "curated", "source_strategy": "graph"}), now=_NOW
    )
    assert node is not None
    assert node["axis"] == "graph"
    assert format_provenance_line(**node) == (
        "Provenance: curated graph node; via graph"
    )


def test_legacy_flat_form_is_read_through_document_form_of() -> None:
    inputs = provenance_inputs(_item({"content_type": "conversation"}), now=_NOW)
    assert inputs is not None
    assert inputs["form"] == "conversation"


def test_pointer_gets_no_inputs() -> None:
    item = _item(
        {"document_form": "reference"},
        selection_reason=POINTER_SELECTION_REASON,
    )
    assert provenance_inputs(item, now=_NOW) is None


def test_keys_match_the_wire_renderer() -> None:
    inputs = provenance_inputs(_item({}), now=_NOW)
    assert inputs is not None
    assert set(inputs) == set(format_provenance_line.__kwdefaults__ or {})
