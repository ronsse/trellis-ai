"""Canonical withholding renderer and wire DTO contracts."""

from __future__ import annotations

import importlib
import importlib.util

import trellis_wire.dtos as wire_dtos
from trellis.retrieve import withholding as core_withholding
from trellis_sdk import _format as sdk_format
from trellis_sdk import async_client as async_sdk
from trellis_sdk import client as sync_sdk
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


def test_sdk_pack_surfaces_bind_the_canonical_withholding_renderer() -> None:
    wire_withholding = importlib.import_module("trellis_wire.withholding")

    assert (
        sdk_format.format_withholding_note is wire_withholding.format_withholding_note
    )
    assert (
        sync_sdk.withholding_from_payload is wire_withholding.withholding_from_payload
    )
    assert (
        async_sdk.withholding_from_payload is wire_withholding.withholding_from_payload
    )
    assert (
        sync_sdk.format_sectioned_pack_as_markdown
        is sdk_format.format_sectioned_pack_as_markdown
    )
    assert (
        async_sdk.format_sectioned_pack_as_markdown
        is sdk_format.format_sectioned_pack_as_markdown
    )


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
    # An older event payload carries no ``class_prior_detail`` key at all —
    # ``extra="forbid"`` on WireModel means the field must default rather
    # than be required, or this construction would raise.
    assert response.withholding.class_prior_detail == []


def test_withholding_response_accepts_class_prior_detail() -> None:
    """A payload naming which (form, axis) pair the gate withheld validates
    into typed rows, not a bare dict — ``extra="forbid"`` means a field the
    server emits that the DTO never declared would raise here instead."""
    response = PackResponse(
        pack_id="pack-1",
        intent="deploy",
        count=0,
        items=[],
        withholding={
            "total": 2,
            "by_reason": {"class_prior": 2},
            "withheld_item_ids": ["c1", "c2"],
            "non_absence_reasons": [],
            "section_filtered": 0,
            "served_count": 1,
            "class_prior_detail": [
                {"form": "conversation", "axis": "keyword", "count": 2}
            ],
        },
    )
    assert response.withholding is not None
    [detail] = response.withholding.class_prior_detail
    assert isinstance(detail, wire_dtos.ClassPriorWithheldDetail)
    assert detail.form == "conversation"
    assert detail.axis == "keyword"
    assert detail.count == 2
