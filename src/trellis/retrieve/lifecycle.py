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


def exclude_archived(items: Iterable[PackItem]) -> list[PackItem]:
    """Drop every item stamped archived, passing the rest through unchanged."""
    kept: list[PackItem] = []
    dropped = 0
    for item in items:
        if is_archived(item.metadata):
            dropped += 1
            continue
        kept.append(item)
    if dropped:
        logger.debug("archived_items_excluded", dropped=dropped)
    return kept


__all__ = ["exclude_archived", "is_archived"]
