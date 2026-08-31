"""The noise boundary — demoted items must not reach a pack, on any axis.

:mod:`trellis.retrieve.servable` answers "which stored metadata *keys* may
reach a pack"; :mod:`trellis.retrieve.lifecycle` answers the same question
for whole items on the ``Lifecycle`` axis. This module answers it on the
``ContentTags.signal_quality`` axis: an item the feedback loop demoted to
``signal_quality="noise"`` (:func:`trellis.classify.feedback.apply_noise_tags`)
correlates with task failure, and ``docs/PRD.md`` and ``CLAUDE.md`` both
state it is excluded from packs by default.

**It was not.** The exclusion was expressed only as a store-side predicate
built by ``PackBuilder._build_filters``, and that predicate reached exactly
one axis under exactly one calling convention:

* ``SemanticSearch`` **strips** ``content_tags`` from the filters before
  calling the vector store (``strategies.py``), because the vector backends
  offer only hard-equality scalar metadata filters — passing the facet bag
  through matches nothing at all and would empty every pack (#254). So the
  noise predicate never reached the semantic axis, whatever the vector row
  said.
* ``_build_filters`` returns early when ``tag_filters is None``, so the
  default was never even *constructed* for a caller that passes none — and
  MCP ``get_context``, the primary production consumer, passes none unless
  a ``domain`` is supplied. On that path noise exclusion did not hold on the
  keyword axis either.

Both are the recurring shape this repo keeps finding: **a filter whose
precondition quietly stopped holding, while the code kept running and
reporting success.**

**Enforced where PackBuilder collects, not per strategy.** Same reasoning as
the serving and lifecycle boundaries: ``PackBuilder`` takes its strategies
by injection and exposes ``add_strategy``, so a rule applied inside the
built-in strategies would silently not hold for a fourth added later or out
of tree. The store-side pushdown is *left in place* — where it works
(keyword) it is strictly cheaper, since a row filtered in SQL never spends
the strategy's ``limit`` budget. This is the backstop that makes the
guarantee true everywhere rather than a replacement for it.

**Default-pass, exactly like the store-side filter it backstops.** An item
with no ``content_tags``, or with tags carrying no ``signal_quality``,
passes every operator — verified against ``SQLiteDocumentStore``'s tag
filter, which admits untagged rows under ``not_in``, ``in``, ``eq`` and
``ne`` alike. Only an explicit, mismatching value excludes. A malformed or
unreadable spec passes everything: a bad filter must never silently shrink
a pack.

**Caller-supplied specs win.** Curation and review tooling legitimately
wants to *see* noise — ``tag_filters={"signal_quality": {"in": ["noise"]}}``
inverts the boundary, and ``{"not_in": []}`` disables it. The default is a
default, not a wall.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.pack import PackItem

logger = structlog.get_logger(__name__)

#: The demoted value itself.
NOISE_SIGNAL_QUALITY = "noise"

#: ``RejectedItem.reason`` recorded when this gate removes an item, so the
#: withholding report (:mod:`trellis.retrieve.withholding`) can name it.
#: Deliberately the facet *value* the pipeline already writes rather than a
#: freshly-coined ``noise_filter``: a second word for the same fact is how
#: ``content_type`` and ``document_form`` drifted apart in #325/#326.
NOISE_REJECTION_REASON = NOISE_SIGNAL_QUALITY

#: The facet the boundary acts on.
SIGNAL_QUALITY_FACET = "signal_quality"

#: Applied when the caller names no ``signal_quality`` spec of its own.
#: Expressed as a negation rather than an allowlist of the acceptable
#: values so a ``SignalQuality`` value added later is servable by default
#: instead of silently dropped — the same reasoning ``_build_filters``
#: gives for the store-side default it mirrors.
DEFAULT_SIGNAL_QUALITY_SPEC: dict[str, Any] = {"not_in": [NOISE_SIGNAL_QUALITY]}


def resolve_signal_quality_spec(
    tag_filters: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``signal_quality`` spec in force for one pack build.

    Mirrors ``PackBuilder._build_filters``' default so the post-filter and
    the store-side pushdown cannot disagree about what "by default" means —
    except that this one is resolved even when ``tag_filters`` is ``None``,
    which is the gap that made the default unreachable from MCP.
    """
    spec = (tag_filters or {}).get(SIGNAL_QUALITY_FACET)
    if isinstance(spec, dict):
        return spec
    return DEFAULT_SIGNAL_QUALITY_SPEC


def _facet_value(metadata: dict[str, Any] | None) -> str | None:
    """Read ``content_tags.signal_quality`` out of a pack item's metadata."""
    if not metadata:
        return None
    tags = metadata.get("content_tags")
    if not isinstance(tags, dict):
        return None
    value = tags.get(SIGNAL_QUALITY_FACET)
    return value if isinstance(value, str) else None


def passes_signal_quality(
    metadata: dict[str, Any] | None,
    spec: dict[str, Any],
) -> bool:
    """Whether one item's metadata satisfies a ``signal_quality`` spec.

    Supports the four operators the document store's tag-filter parser
    accepts — ``in`` / ``not_in`` / ``eq`` / ``ne`` — with the same
    default-pass semantics. An unrecognised or malformed operator passes
    (and says so in the log) rather than excluding on a spec nobody can
    evaluate.
    """
    value = _facet_value(metadata)
    if value is None:
        return True
    if "in" in spec:
        allowed = spec["in"]
        return value in allowed if isinstance(allowed, list) else True
    if "not_in" in spec:
        denied = spec["not_in"]
        return value not in denied if isinstance(denied, list) else True
    if "eq" in spec:
        return bool(value == spec["eq"])
    if "ne" in spec:
        return bool(value != spec["ne"])
    logger.debug("signal_quality_spec_unrecognised", spec=sorted(spec))
    return True


def partition_by_signal_quality(
    items: Iterable[PackItem],
    spec: dict[str, Any] | None = None,
) -> tuple[list[PackItem], list[PackItem]]:
    """Split ``items`` into ``(kept, withheld)`` under ``spec``.

    The same decision :func:`exclude_noise` makes, with the losing side
    returned instead of counted. ``PackBuilder`` needs the withheld items
    themselves, not a tally: this boundary is the one gate whose only
    observable was a ``logger.debug`` line — a no-op under the CLI's
    ``WARNING`` default — so a noise-demoted item left no trace in the pack,
    the ``PACK_ASSEMBLED`` payload or the log. Handing the items back lets
    the builder record them like every other gate (see
    :mod:`trellis.retrieve.withholding`).
    """
    effective = DEFAULT_SIGNAL_QUALITY_SPEC if spec is None else spec
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        if passes_signal_quality(item.metadata, effective):
            kept.append(item)
        else:
            withheld.append(item)
    return kept, withheld


def exclude_noise(
    items: Iterable[PackItem],
    spec: dict[str, Any] | None = None,
) -> list[PackItem]:
    """Drop every item failing ``spec``, passing the rest through unchanged.

    The survivors-only form of :func:`partition_by_signal_quality`, and the
    name the docs and the retention ADR use for this boundary. **Not** the
    pack path any more: ``PackBuilder`` calls the partition directly,
    because a drop it cannot see is a drop it cannot report (#404). Kept
    for callers that only need "what would survive" — and note that the
    ``logger.debug`` below is exactly the observability #404 found
    insufficient, so do not reach for this on a serving path.
    """
    effective = DEFAULT_SIGNAL_QUALITY_SPEC if spec is None else spec
    kept, withheld = partition_by_signal_quality(items, effective)
    if withheld:
        logger.debug(
            "noise_items_excluded", dropped=len(withheld), spec=sorted(effective)
        )
    return kept


__all__ = [
    "DEFAULT_SIGNAL_QUALITY_SPEC",
    "NOISE_REJECTION_REASON",
    "NOISE_SIGNAL_QUALITY",
    "SIGNAL_QUALITY_FACET",
    "exclude_noise",
    "partition_by_signal_quality",
    "passes_signal_quality",
    "resolve_signal_quality_spec",
]
