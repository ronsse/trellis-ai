"""The lifecycle boundary — archived items, and superseded items beside their
successor, must not reach a pack.

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

**Supersession is pairwise, not per-item** (#613). A ``superseded`` item is
*not* excluded on its own: ``docs/design/plan-memory-lifecycle.md`` §4 keeps
the losing version retrievable on demand, and
``tests/unit/mcp/test_reconcile.py::test_superseded_is_not_excluded_from_retrieval``
pins that :func:`is_archived` stays blind to it. What §4 forbids is serving
*both sides* in one pack, which is a property of the candidate pool rather
than of one item — so :func:`partition_superseded` runs over the whole pool
after every strategy has been collected (the successor may arrive on a
different axis), not at the per-strategy collect seam above.
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

#: The ``Lifecycle.state`` value ``mcp.reconcile.mark_document_superseded``
#: writes, reused verbatim as the ``RejectedItem.reason`` of the pairwise
#: supersession gate — the same no-second-vocabulary rule as
#: :data:`ARCHIVED_REJECTION_REASON`.
SUPERSEDED_STATE = "superseded"
SUPERSEDED_REJECTION_REASON = SUPERSEDED_STATE

#: The node property that points a ``save_knowledge`` graph node at its
#: evidence document. ``GraphSearch`` spreads node properties into item
#: metadata, so this is how a successor named by its *document* id is found
#: when the graph axis served its *node*.
EVIDENCE_REF_KEY = "evidence_ref"


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


def declared_successor(metadata: dict[str, Any] | None) -> str | None:
    """The ``superseded_by`` id of an item stamped ``state == "superseded"``.

    ``None`` for anything else, including every malformed record — a
    missing, empty or non-string ``superseded_by``, or a lifecycle record
    that is not a dict. The :func:`is_archived` rule: a bad record is a
    reason to keep serving an item, never a reason to hide it.
    """
    if not metadata:
        return None
    record = metadata.get(LIFECYCLE_KEY)
    if not isinstance(record, dict) or record.get("state") != SUPERSEDED_STATE:
        return None
    successor = record.get("superseded_by")
    if not isinstance(successor, str) or not successor:
        return None
    return successor


def _on_cycle(start: str, next_of: dict[str, str]) -> bool:
    """Whether following ``next_of`` from ``start`` returns to ``start``."""
    seen: set[str] = set()
    current = start
    while current in next_of and current not in seen:
        seen.add(current)
        current = next_of[current]
    return current == start


def partition_superseded(
    items: Iterable[PackItem],
) -> tuple[list[PackItem], list[PackItem]]:
    """Split a collected pool into ``(kept, withheld)`` on declared supersession.

    An item id is withheld when some copy of it carries a
    :func:`declared_successor` that is **present in the same pool** —
    either as another item's ``item_id`` or as another item's
    ``metadata["evidence_ref"]`` (the ``save_knowledge`` seam: the graph axis
    serves the node, the other two serve the document it points at). A loser
    whose successor is absent is kept; that is §4's "retrievable on demand".

    Withholding is **by id**, so every copy of a loser goes, including one
    an axis returned without the stamp: a vector row's metadata is an
    embed-time snapshot (#338), and keeping the unstamped copy would serve
    both sides after all.

    Two shapes are kept deliberately. A **self-reference** (the successor
    resolves only to the item itself) is malformed. A **cycle** (A→B→A, or
    longer) names no winner, and withholding every member would hide the
    whole claim — the ``No context found`` failure #404 exists to prevent —
    so its members are kept and one ``supersession_cycle_kept`` warning is
    logged. An item that merely *leads into* a cycle is withheld.

    Order is preserved in both halves, for the reason
    ``PackBuilder._partition`` gives: ``withheld`` order becomes the order
    of ``rejected_items`` and of the served record's ``withheld_item_ids``.
    """
    pool = list(items)
    by_id: set[str] = {item.item_id for item in pool}
    by_evidence: dict[str, str] = {}
    for item in pool:
        ref = (item.metadata or {}).get(EVIDENCE_REF_KEY)
        if isinstance(ref, str) and ref:
            by_evidence.setdefault(ref, item.item_id)

    next_of: dict[str, str] = {}
    for item in pool:
        successor = declared_successor(item.metadata)
        if successor is None or item.item_id in next_of:
            continue
        resolved = successor if successor in by_id else by_evidence.get(successor)
        if resolved is not None and resolved != item.item_id:
            next_of[item.item_id] = resolved

    cycle = sorted(i for i in next_of if _on_cycle(i, next_of))
    if cycle:
        logger.warning("supersession_cycle_kept", item_ids=cycle)
    losers = set(next_of) - set(cycle)

    kept: list[PackItem] = []
    withheld: list[PackItem] = []
    for item in pool:
        (withheld if item.item_id in losers else kept).append(item)
    return kept, withheld


__all__ = [
    "ARCHIVED_REJECTION_REASON",
    "EVIDENCE_REF_KEY",
    "SUPERSEDED_REJECTION_REASON",
    "SUPERSEDED_STATE",
    "declared_successor",
    "exclude_archived",
    "is_archived",
    "partition_archived",
    "partition_superseded",
]
