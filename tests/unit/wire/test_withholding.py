"""Canonical withholding renderer and wire DTO contracts."""

from __future__ import annotations

import importlib
import importlib.util

import trellis_wire.dtos as wire_dtos
from trellis.retrieve import withholding as core_withholding
from trellis_wire.dtos import PackResponse, SectionedPackResponse


def test_wire_package_owns_the_canonical_withholding_renderer() -> None:
    assert importlib.util.find_spec("trellis_wire.withholding") is not None
    wire_withholding = importlib.import_module("trellis_wire.withholding")

    assert (
        core_withholding.format_withholding_note
        is wire_withholding.format_withholding_note
    )
    assert (
        core_withholding.withholding_from_payload
        is wire_withholding.withholding_from_payload
    )
    assert core_withholding.WithholdingSummary is wire_withholding.WithholdingSummary
    assert core_withholding.WithheldGroup is wire_withholding.WithheldGroup


def test_both_pack_response_dtos_declare_optional_withholding() -> None:
    assert hasattr(wire_dtos, "WithholdingResponse")
    assert PackResponse.model_fields["withholding"].is_required() is False
    assert SectionedPackResponse.model_fields["withholding"].is_required() is False
    assert PackResponse.model_fields["withholding"].default is None
    assert SectionedPackResponse.model_fields["withholding"].default is None

    response = PackResponse(
        pack_id="pack-1",
        intent="deploy",
        count=0,
        items=[],
        withholding={
            "total": 1,
            "by_reason": {"noise": 1},
            "withheld_item_ids": ["noise-id"],
            "non_absence_reasons": [],
            "section_filtered": 0,
            "served_count": 0,
        },
    )
    assert isinstance(response.withholding, wire_dtos.WithholdingResponse)
