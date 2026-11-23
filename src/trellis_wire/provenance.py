"""The optional provenance line a pack item can carry under its body.

One pure renderer over plain values, so any surface that has the values can
word the line the same way without importing ``trellis`` core (the same
reason :mod:`trellis_wire.withholding` lives here). Which surfaces render it,
and when, is theirs to decide; today only the MCP pack formatters do, behind
``TRELLIS_PACK_PROVENANCE_LINE`` (off by default).

The line states **where an item came from and how it was reached**, not
whether it is right. It never carries a number that reads as confidence:
the fused relevance score is ordinal (see
:func:`trellis.retrieve.formatters.format_pack_as_markdown`), and an age is a
fact about a timestamp, not a grade.

Every string argument may come from stored metadata, so each is reduced to a
short run of letters, digits and a few punctuation marks before it is
rendered. A stored ``source_system`` cannot end the line, open a code span,
or forge a clause.
"""

from __future__ import annotations

import math

#: Longest label rendered for any one string value.
PROVENANCE_LABEL_MAX_CHARS = 40

#: Characters kept besides letters and digits. Everything else becomes a
#: space: no newline, backtick, ``;`` (the clause separator), brackets or
#: markdown emphasis survives.
_LABEL_PUNCTUATION = frozenset(" .-_/:@+")

#: Clock names :func:`trellis.retrieve.strategies.resolve_recency_stamp`
#: stamps for a usable stamp. Anything else is not echoed.
_KNOWN_CLOCKS = frozenset({"source", "row"})

_DAYS_PER_MONTH = 30
_DAYS_PER_YEAR = 365
_MONTHS_FROM_DAYS = 60
_YEARS_FROM_DAYS = 730
_MAX_AGE_DAYS = 365 * 200


def clean_provenance_label(
    value: object, limit: int = PROVENANCE_LABEL_MAX_CHARS
) -> str | None:
    """``value`` as a short single-line label, or ``None`` when nothing is left.

    Keeps letters, digits and ``. - _ / : @ +``; every other character,
    including every kind of line break and control character, becomes a
    space, and runs of spaces collapse. The result is cut to ``limit``
    characters. A non-string, or a string with nothing printable left, gives
    ``None`` so the caller omits the clause rather than rendering a blank.
    """
    if not isinstance(value, str):
        return None
    kept = "".join(
        ch if (ch.isalnum() or ch in _LABEL_PUNCTUATION) else " " for ch in value
    )
    cleaned = " ".join(kept.split())[:limit].strip()
    return cleaned or None


def _age_text(age_days: float | None) -> str | None:
    if age_days is None or isinstance(age_days, bool):
        return None
    # Past _MAX_AGE_DAYS the stamp is corrupt, not an age worth stating.
    if (
        not isinstance(age_days, int | float)
        or not math.isfinite(age_days)
        or age_days > _MAX_AGE_DAYS
    ):
        return None
    days = max(int(age_days), 0)
    if days < 1:
        return "under 1 day"
    if days < _MONTHS_FROM_DAYS:
        return "1 day" if days == 1 else f"{days} days"
    if days < _YEARS_FROM_DAYS:
        return f"{days // _DAYS_PER_MONTH} months"
    return f"{days // _DAYS_PER_YEAR} years"


def _origin_text(
    form: str | None,
    source_system: str | None,
    captured: bool,
    node_role: str | None,
) -> str | None:
    form_label = clean_provenance_label(form)
    if form_label:
        return f"{form_label} document"
    source_label = clean_provenance_label(source_system)
    if source_label:
        return f"from {source_label}"
    if captured is True:
        return "session capture"
    if node_role == "curated":
        return "curated graph node"
    return None


def format_provenance_line(
    *,
    form: str | None = None,
    source_system: str | None = None,
    captured: bool = False,
    node_role: str | None = None,
    age_days: float | None = None,
    clock: str | None = None,
    axis: str | None = None,
) -> str | None:
    """One ``Provenance: ...`` line for a pack item, or ``None``.

    Shape: ``Provenance: <origin>; <age> old (<clock> clock); via <axis>``,
    each clause omitted when its value is absent, and ``None`` when no
    clause is known.

    Args:
        form: The document form (``document_form``); rendered as
            ``<form> document``. Wins over every other origin.
        source_system: The ingest path's ``source_system``; rendered as
            ``from <source_system>`` when there is no form.
        captured: ``True`` for a session-capture memory; rendered as
            ``session capture`` when there is no form or source system.
        node_role: A graph node's role; only ``"curated"`` names an origin
            (``curated graph node``), and only as the last fallback.
        age_days: Age of the stamp recency scoring read, in days. ``None``
            or a non-finite value omits the clause; a slightly future stamp
            (negative age) reads as ``under 1 day``.
        clock: Which clock that stamp came from (``source`` / ``row``).
            Shown only beside an age, and only when it is a known name.
        axis: The retrieval axis that collected the item.
    """
    clauses: list[str] = []
    origin = _origin_text(form, source_system, captured, node_role)
    if origin:
        clauses.append(origin)
    age = _age_text(age_days)
    if age:
        clock_name = clock if clock in _KNOWN_CLOCKS else None
        clauses.append(
            f"{age} old ({clock_name} clock)" if clock_name else f"{age} old"
        )
    axis_label = clean_provenance_label(axis)
    if axis_label:
        clauses.append(f"via {axis_label}")
    if not clauses:
        return None
    return "Provenance: " + "; ".join(clauses)


__all__ = [
    "PROVENANCE_LABEL_MAX_CHARS",
    "clean_provenance_label",
    "format_provenance_line",
]
