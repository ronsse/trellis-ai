"""The lifecycle boundary — archived items must not reach a pack.

:mod:`trellis.retrieve.servable` answers "which stored metadata *keys* may
reach a pack". This module answers the adjacent question about whole
*items*: an item stamped ``Lifecycle.state="archived"`` by
``retention.prune`` has been judged to have stopped earning its storage, and
serving it would make the archival cosmetic.

**Enforced where PackBuilder collects, not per strategy.** Same reasoning as
the serving boundary: ``PackBuilder`` takes its strategies by injection and
exposes ``add_strategy``, so a rule applied inside the built-in strategies
would silently not hold for a fourth added later or out of tree. Filtering
at the collect seam covers every strategy and every store backend, including
ones that never learn what a lifecycle record is.

**Why a post-filter rather than a store-level predicate.** Noise exclusion
pushes down into SQL because ``signal_quality`` is a ``content_tags`` facet
and tag filters address ``$.content_tags.<facet>``. ``Lifecycle`` is
deliberately a *sibling* key on a separate axis (see
``docs/design/adr-tag-vocabulary-split.md``), so the tag-filter path cannot
address it without conflating the two vocabularies — which is exactly the
collision #325/#326 spent two PRs undoing. Post-filtering costs a fetch of
rows that are then dropped, so an archived item still consumes its
strategy's ``limit`` budget.

That trade is deliberate and bounded: it is correct for every backend on day
one, and it only starts to cost recall when the archived population is a
material fraction of the corpus. The size at which that happens is
observable rather than guessed — ``RETENTION_PRUNED.payload["archived"]``
counts it. A store-level pushdown is the follow-up if and when that count
says so; shipping it now would be optimising a population of 24.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.mutate.retention import ARCHIVED_STATE
from trellis.schemas.classification import LIFECYCLE_KEY

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.pack import PackItem

logger = structlog.get_logger(__name__)

#: ``RejectedItem.reason`` recorded when this gate removes an item, so the
#: withholding report (:mod:`trellis.retrieve.withholding`) can name it.
#: The ``Lifecycle.state`` value the retention pass already writes, reused
#: verbatim rather than re-labelled — see
#: :data:`trellis.retrieve.noise.NOISE_REJECTION_REASON`.
ARCHIVED_REJECTION_REASON = ARCHIVED_STATE


def is_archived(metadata: dict[str, Any] | None) -> bool:
    """Whether a metadata bag carries ``Lifecycle.state == "archived"``.

    Anything malformed reads as **not** archived: a bad lifecycle record is
    a reason to keep serving an item, never a reason to hide it. Excluding
    on a parse failure would let a typo silently shrink every pack.
    """
    if not metadata:
        return False
    record = metadata.get(LIFECYCLE_KEY)
    if not isinstance(record, dict):
        return False
    return record.get("state") == ARCHIVED_STATE


def partition_archived(
    items: Iterable[PackItem],
) -> tuple[list[PackItem], list[PackItem]]:
    """Split ``items`` into ``(kept, withheld)`` on the archived stamp.

    The same decision :func:`exclude_archived` makes, with the losing side
    returned instead of counted — for the reason given in
    :func:`trellis.retrieve.noise.partition_by_signal_quality`: this gate's
    only observable was a ``logger.debug`` line, so an archived item left no
    trace anywhere a caller or an analyzer could read.
    """
    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in items:
        if is_archived(item.metadata):
            withheld.append(item)
        else:
            kept.append(item)
    return kept, withheld


def exclude_archived(items: Iterable[PackItem]) -> list[PackItem]:
    """Drop every item stamped archived, passing the rest through unchanged.

    The survivors-only form of :func:`partition_archived` — see
    :func:`trellis.retrieve.noise.exclude_noise` for why ``PackBuilder`` no
    longer calls it.
    """
    kept, withheld = partition_archived(items)
    if withheld:
        logger.debug("archived_items_excluded", dropped=len(withheld))
    return kept


__all__ = [
    "ARCHIVED_REJECTION_REASON",
    "exclude_archived",
    "is_archived",
    "partition_archived",
]
