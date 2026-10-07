"""Pure retrieval-axis note formatting, shared by core and the SDK.

A pack assembled with a failed or misconfigured retrieval axis is a
materially different pack from a clean one, and
:func:`~trellis.retrieve.builder_factory.describe_axes` (core-only; it
needs a live ``PackBuilder``) reports the gap structurally. A markdown
surface has no JSON ``axes`` block for a caller to fall back on, so it
renders the same facts as one or two plain-text lines instead — same
shape and same reasoning as :mod:`trellis_wire.withholding`.

:mod:`trellis.retrieve.builder_factory` re-exports
:func:`format_failed_axes_note` and
:func:`format_misconfigured_semantic_note` from here, so MCP and the
SDK render the same wording from one source
(``tests/unit/wire/test_axes.py`` pins the identity).

:func:`axis_note_from_payload` is the SDK-specific half: it has no
``PackBuilder`` to call ``describe_axes`` on, only the wire-level
``axes`` dict a ``PackResponse``/``SectionedPackResponse`` JSON body
carries (``trellis_wire.dtos.AxisReportResponse``, serialized). It
parses that dict defensively, the same posture
:func:`trellis_wire.withholding.withholding_from_payload` takes: a
missing or malformed field contributes nothing to the note rather than
raising, so an older server with no ``axes`` block renders no note
instead of breaking the SDK caller.
"""

from __future__ import annotations

from typing import Any


def format_failed_axes_note(failed: list[str]) -> str:
    """One line naming axes that failed to run, or ``""`` when none did.

    Axis names only, never the exception text — that lives in
    ``PACK_ASSEMBLED.strategy_failures`` for offline analysis. This
    covers any axis (keyword, graph, semantic, ...), for a markdown
    surface that has no JSON ``axes`` block to fall back on: a reply
    that goes silent on a failed axis reads as a genuinely empty corpus
    (see :mod:`trellis_wire.withholding` for the same problem on
    withheld items), so the note renders unconditionally here rather
    than only in a structured response a human might think to check.
    """
    if not failed:
        return ""
    return f"**Retrieval axis failed:** {', '.join(failed)}."


def format_misconfigured_semantic_note(semantic_state: str) -> str:
    """One line for a ``misconfigured`` semantic axis, or ``""`` otherwise.

    :func:`format_failed_axes_note` only names axes in ``axes["failed"]``
    — axes that exist and raised during *this* build. A ``misconfigured``
    semantic axis never reaches that list: it is absent from
    ``axes["available"]`` altogether, because the embedder resolved but
    the vector backend never initialised. Same facts as the CLI's
    ``misconfigured`` sentence, no exception text, same markdown-note
    shape as :func:`format_failed_axes_note`.
    """
    if semantic_state != "misconfigured":
        return ""
    return (
        "**Semantic retrieval misconfigured:** the vector store did not"
        " initialise, so this pack has no semantic results."
    )


def axis_note_from_payload(axes: dict[str, Any] | None) -> str:
    """Every markdown axis note a wire-level ``axes`` block calls for.

    ``axes`` is the raw ``axes`` field of a decoded
    ``PackResponse``/``SectionedPackResponse`` JSON body — e.g.
    ``{"available": [...], "ran": [...], "failed": [...], "semantic":
    "ran"}`` — or ``None`` (a server older than the field). A failed
    axis and a misconfigured semantic axis are independent states one
    build can hit together, so both lines render when both apply — the
    same join :func:`trellis.mcp.server._axis_note` does from a live
    ``AxisReport``.

    Parses defensively because this dict crossed the wire: a missing or
    wrong-shaped field contributes nothing to the note rather than
    raising, the same posture
    :func:`trellis_wire.withholding.withholding_from_payload` takes.
    """
    if not isinstance(axes, dict):
        return ""
    failed = axes.get("failed")
    failed_names = [str(name) for name in failed] if isinstance(failed, list) else []
    semantic_state = axes.get("semantic")
    semantic_str = semantic_state if isinstance(semantic_state, str) else ""
    notes = [
        format_failed_axes_note(failed_names),
        format_misconfigured_semantic_note(semantic_str),
    ]
    return "\n\n".join(note for note in notes if note)


__all__ = [
    "axis_note_from_payload",
    "format_failed_axes_note",
    "format_misconfigured_semantic_note",
]
