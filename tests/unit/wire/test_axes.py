"""Canonical axis-note renderer and its SDK wire-payload parsing.

Mirrors ``tests/unit/wire/test_withholding.py``: the identity-pinning
tests prove ``trellis.retrieve.builder_factory`` re-exports the same
objects :mod:`trellis_wire.axes` defines (never a hand-copied
duplicate), and the behavior tests pin ``axis_note_from_payload``'s
defensive parsing of a wire-level ``axes`` dict against malformed and
missing input.
"""

from __future__ import annotations

import importlib
import importlib.util

from trellis.retrieve import builder_factory as core_builder_factory
from trellis_sdk import client as sync_sdk
from trellis_wire.axes import (
    axis_note_from_payload,
    format_failed_axes_note,
    format_misconfigured_semantic_note,
)


def test_wire_package_owns_the_canonical_axis_note_renderer() -> None:
    assert importlib.util.find_spec("trellis_wire.axes") is not None
    wire_axes = importlib.import_module("trellis_wire.axes")

    assert (
        core_builder_factory.format_failed_axes_note
        is wire_axes.format_failed_axes_note
    )
    assert (
        core_builder_factory.format_misconfigured_semantic_note
        is wire_axes.format_misconfigured_semantic_note
    )


def test_sdk_pack_surfaces_bind_the_canonical_axis_note_renderer() -> None:
    async_sdk = importlib.import_module("trellis_sdk.async_client")
    wire_axes = importlib.import_module("trellis_wire.axes")

    assert sync_sdk.axis_note_from_payload is wire_axes.axis_note_from_payload
    assert async_sdk.axis_note_from_payload is wire_axes.axis_note_from_payload


class TestFormatFailedAxesNote:
    def test_empty_list_renders_nothing(self) -> None:
        assert format_failed_axes_note([]) == ""

    def test_names_every_failed_axis(self) -> None:
        note = format_failed_axes_note(["keyword", "graph"])
        assert note == "**Retrieval axis failed:** keyword, graph."


class TestFormatMisconfiguredSemanticNote:
    def test_non_misconfigured_states_render_nothing(self) -> None:
        for state in ("ran", "not_configured", "failed", "", "bogus"):
            assert format_misconfigured_semantic_note(state) == ""

    def test_misconfigured_renders_the_sentence(self) -> None:
        note = format_misconfigured_semantic_note("misconfigured")
        assert note.startswith("**Semantic retrieval misconfigured:**")


class TestAxisNoteFromPayload:
    def test_none_renders_nothing(self) -> None:
        """An older server with no ``axes`` field, or a sectioned request
        with no sections to report one for — never raises, never notes."""
        assert axis_note_from_payload(None) == ""

    def test_wrong_shaped_payload_renders_nothing(self) -> None:
        """A payload that crossed the wire as something other than a dict
        (e.g. a stray list) is parsed defensively, not raised on."""
        assert axis_note_from_payload([]) == ""  # type: ignore[arg-type]

    def test_healthy_axes_render_nothing(self) -> None:
        axes = {
            "available": ["keyword", "graph"],
            "ran": ["keyword", "graph"],
            "failed": [],
            "semantic": "not_configured",
        }
        assert axis_note_from_payload(axes) == ""

    def test_failed_axis_renders_the_failed_note(self) -> None:
        axes = {
            "available": ["keyword", "graph"],
            "ran": ["graph"],
            "failed": ["keyword"],
            "semantic": "not_configured",
        }
        note = axis_note_from_payload(axes)
        assert note == "**Retrieval axis failed:** keyword."

    def test_misconfigured_semantic_renders_the_misconfigured_note(self) -> None:
        axes = {
            "available": ["keyword", "graph"],
            "ran": ["keyword", "graph"],
            "failed": [],
            "semantic": "misconfigured",
        }
        note = axis_note_from_payload(axes)
        assert note.startswith("**Semantic retrieval misconfigured:**")

    def test_both_failures_join_with_a_blank_line(self) -> None:
        axes = {
            "available": ["keyword", "graph"],
            "ran": ["graph"],
            "failed": ["keyword"],
            "semantic": "misconfigured",
        }
        note = axis_note_from_payload(axes)
        failed_line, _, misconfigured_line = note.partition("\n\n")
        assert failed_line == "**Retrieval axis failed:** keyword."
        assert misconfigured_line.startswith("**Semantic retrieval misconfigured:**")

    def test_malformed_failed_field_is_ignored_not_raised_on(self) -> None:
        """``failed`` sent as a non-list (e.g. a lone string) is treated as
        empty rather than iterated character-by-character or raised on."""
        axes = {"failed": "keyword", "semantic": "ran"}
        assert axis_note_from_payload(axes) == ""

    def test_malformed_semantic_field_is_ignored_not_raised_on(self) -> None:
        axes = {"failed": [], "semantic": 42}
        assert axis_note_from_payload(axes) == ""

    def test_missing_keys_render_nothing(self) -> None:
        assert axis_note_from_payload({}) == ""
