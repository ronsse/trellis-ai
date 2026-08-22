"""Explicit elision markers for capped text (issue #310).

Several paths bound how much text a model or a pack consumer sees — the
excerpt truncator, the distillation prompt cap, the enrichment content
cap. A silent cut invites an LLM to confabulate the missing middle and
gives a pack consumer no basis for judging whether the full source is
worth fetching. This module is the one home for how a cut announces
itself:

* :func:`elide_text` caps prompt-bound text and marks any cut with an
  explicit ``<elided … />`` tag carrying dropped size, original size,
  and reason — the claude-mem convention the 2026-08 comparison audit
  recommended adopting.
* :func:`format_char_count` renders a character count compactly
  (``"734"``, ``"2.3k"``, ``"1.2M"``) for the excerpt truncator's
  dropped-size note in :mod:`trellis.retrieve.excerpts`.
"""

from __future__ import annotations

#: Reason recorded when text was cut purely for exceeding a size cap.
ELISION_REASON_OVERSIZE = "oversize"

_THOUSAND = 1_000
_MILLION = 1_000_000


def format_char_count(count: int) -> str:
    """Render a character count compactly: ``"734"``, ``"2.3k"``, ``"1.2M"``.

    One decimal place above 1k, with a trailing ``.0`` dropped — the
    number exists to support a fetch-the-full-source judgment, not
    byte-exact accounting.
    """
    if count >= _MILLION:
        return f"{count / _MILLION:.1f}".removesuffix(".0") + "M"
    if count >= _THOUSAND:
        return f"{count / _THOUSAND:.1f}".removesuffix(".0") + "k"
    return str(count)


def elide_text(
    text: str,
    max_chars: int,
    *,
    reason: str = ELISION_REASON_OVERSIZE,
) -> str:
    """Cap ``text`` at ``max_chars``, marking any cut with size + reason.

    Text at or under the cap is returned verbatim. Otherwise the kept
    head is exactly ``text[:max_chars]`` followed by a marker line::

        <elided chars="5234" original_size_chars="13234" reason="oversize" />

    so the model reading the capped payload knows material was removed —
    and how much — instead of treating the cut as the end of the text.
    The marker rides *on top of* the cap: ``max_chars`` bounds the
    payload, and a fixed ~60-char annotation must not silently vary that
    budget (this matches the pre-#310 enrichment behaviour, where the
    ``[Content truncated...]`` marker was likewise not charged).
    """
    if max_chars < 0:
        msg = f"max_chars must be >= 0; got {max_chars!r}"
        raise ValueError(msg)
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    marker = (
        f'\n<elided chars="{dropped}" original_size_chars="{len(text)}" '
        f'reason="{reason}" />'
    )
    return text[:max_chars] + marker


__all__ = [
    "ELISION_REASON_OVERSIZE",
    "elide_text",
    "format_char_count",
]
