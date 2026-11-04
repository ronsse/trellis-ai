"""Helpers shared by more than one handler family."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.vector_metadata import VectorSyncOutcome, sync_vector_metadata_outcome
from trellis.errors import StoreError
from trellis.mutate.commands import Command
from trellis.schemas.classification import LIFECYCLE_KEY, lifecycle_state_of
from trellis.stores.base.graph import GraphStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable


_REWRITE_ATTEMPTS = 5


def _rewrite_current_node(
    store: GraphStore,
    node_id: str,
    build: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Write a new version of ``node_id`` computed from its current one.

    ``build`` receives the current row and returns the fields to change
    (``properties``, optionally ``node_type`` or ``document_ids``), or
    ``None`` to decline. Every other field is carried forward from the row:
    ``node_role`` and ``generation_spec`` are immutable across versions, and
    ``document_ids`` is normalised from ``[]`` to ``None`` ("no link").

    The write is a compare-and-set on the row's ``valid_from``, so it never
    re-creates a node purged after the read. When another writer replaced
    the version in between, the node is re-read and ``build`` re-applied, at
    most ``_REWRITE_ATTEMPTS`` times in all.

    Returns the fields written, or ``None`` when the node is absent (never
    existed, or purged mid-command) or ``build`` declined. Raises
    :class:`StoreError` when every attempt lost to a concurrent writer.
    """
    for _attempt in range(_REWRITE_ATTEMPTS):
        node = store.get_node(node_id)
        if node is None:
            return None
        changes = build(node)
        if changes is None:
            return None
        fields: dict[str, Any] = {
            "node_type": node["node_type"],
            "node_role": node.get("node_role", "semantic"),
            "generation_spec": node.get("generation_spec"),
            "document_ids": node.get("document_ids") or None,
            **changes,
        }
        if store.update_node_if_current(node_id, node["valid_from"], **fields):
            return fields
    msg = f"Node {node_id} changed during each of {_REWRITE_ATTEMPTS} rewrite attempts"
    raise StoreError(msg, store="graph")


def _current_lifecycle_state(node: dict[str, Any]) -> str:
    """``node``'s current ``Lifecycle.state``, fail-open to ``CURRENT_STATE``.

    A freshly-promoted precedent's entity
    (``trellis.learning.scoring.submit_learning_promotion``) never writes
    ``LIFECYCLE_KEY`` at all, and a malformed record is no more
    informative than an absent one — both read as current, matching
    ``retrieve.lifecycle``'s own fail-open convention for this key: a
    bad or missing lifecycle record is never by itself a reason to
    refuse. An empty or unknown ``state`` is malformed, not a state of its
    own (:func:`~trellis.schemas.classification.lifecycle_state_of`).
    """
    return lifecycle_state_of(node["properties"])


#: Cap on the linked observation / measurement id lists in the
#: ``REDACTION_APPLIED`` payload (they are follow-up pointers, not an
#: exhaustive index).
_LINKED_SIGNAL_LIMIT = 100


def _sync_vector_lifecycle(
    registry: StoreRegistry,
    item_id: str,
    lifecycle: dict[str, Any],
    *,
    command: Command | None = None,
    log_event: str = "retention_vector_lifecycle_sync_failed",
) -> VectorSyncOutcome:
    """Stamp a lifecycle record onto a vector row that has no document write.

    Four call sites share this: ``RetentionPruneHandler._resync_archived``
    (a row that diverged *before* the ``put_document`` mirror seam existed),
    ``RetentionRestoreHandler._restore``'s graph-node branch (a graph node
    has no document write to piggyback a mirror onto, unlike the document
    branches of ``_archive``/``_restore``, which go through
    :func:`~trellis.core.document_write.put_document` instead, so the
    mirror there is not a step a future edit could forget), and
    ``PrecedentDemoteHandler``/``PrecedentReinstateHandler``, neither of
    which is a retention operation at all — pass ``log_event`` so the log
    line names the actual caller rather than defaulting to "retention" for
    a demotion or reinstatement.

    **A vector row's metadata is a snapshot taken at embed time**, and the
    semantic strategy builds its :class:`~trellis.schemas.pack.PackItem`
    from that snapshot rather than from the document store. So an archival
    written only through ``document_store.put`` leaves the semantic path
    serving the item as though nothing happened —
    :func:`~trellis.retrieve.lifecycle.exclude_archived` reads
    ``item.metadata`` and simply never sees a lifecycle key.

    That is not hypothetical: the first production prune archived 35
    documents and every one of them kept a vector row whose metadata still
    read ``signal_quality="standard"``. The keyword path honoured the
    archival immediately (it reads the document store); the semantic path
    did not, and a pack-level test written against
    :class:`~trellis.retrieve.strategies.KeywordSearch` alone could not see
    the difference.

    Metadata-only update: the existing embedding is re-``upsert``-ed
    unchanged, so nothing is re-embedded and no cost is incurred. A missing
    vector row is normal (structural nodes, un-embedded documents) and is a
    no-op rather than an error — retention must not fail because an item was
    never embedded.

    **Visibility on failure.** The document/graph stamp is already written
    by the time this runs, so a vector backend outage must not roll back the
    transition — it is logged, not raised. Logging alone leaves a JSON
    caller with no way to see that the semantic path went stale, so when
    ``command`` is given the same condition is also appended to
    ``command.metadata["handler_warnings"]``, which ``MutationExecutor``
    folds into the returned ``CommandResult.warnings`` (see
    ``_handler_warnings`` in ``executor.py``). The :data:`VectorSyncOutcome`
    is also returned so a caller that emits its own domain event (
    :class:`PrecedentDemoteHandler`, :class:`PrecedentReinstateHandler`) can
    fold a ``"failed"`` outcome into that event's payload too — the
    ``CommandResult`` warning and the event are two independent readers of
    the same fact, and a JSON caller may see either one without replaying
    the log.

    The actual get/merge/upsert is delegated to
    :func:`~trellis.core.vector_metadata.sync_vector_metadata_outcome` —
    the same mirror the classify layer uses for ``content_tags`` and
    ``auto_importance`` — rather than repeated here: two writers maintaining
    the same "read the row, merge one key, re-upsert unchanged" sequence is
    exactly how ``MIRRORED_METADATA_KEYS`` split into two disjoint sets
    before (see that module's docstring). ``keys=(LIFECYCLE_KEY,)`` narrows
    the mirror to the one key this caller owns, so a row that also carries
    ``content_tags``/``auto_importance`` is left untouched. Resolving the
    store itself can still raise (a misconfigured backend) outside anything
    ``sync_vector_metadata_outcome`` catches, since that function takes an
    already-resolved store; it is folded into the same "failed" handling
    here rather than treated as the separate, silent ``"no_store"`` outcome
    :func:`~trellis.core.vector_metadata.resolve_vector_store` reports for a
    *deployment with no vector store at all* — this function has never had
    that quiet case, and keeps not having it.
    """
    outcome: VectorSyncOutcome
    try:
        store = registry.knowledge.vector_store
    except Exception:
        # Fail soft and loud: the document-store stamp is the authoritative
        # record and has already been written. A vector backend outage
        # must not roll back an archival, but it does leave the semantic
        # path stale, so it is logged rather than swallowed. ``exc_info``
        # is valid here because we are still inside the handler for the
        # exception it reports.
        logger.warning(log_event, item_id=item_id, exc_info=True)
        outcome = "failed"
    else:
        outcome = sync_vector_metadata_outcome(
            store, item_id, {LIFECYCLE_KEY: lifecycle}, keys=(LIFECYCLE_KEY,)
        )
        if outcome == "failed":
            # No ``exc_info`` here: unlike the branch above, there is no
            # exception live at this point to attach -- ``outcome`` is a
            # sentinel the callee returned after already logging the real
            # traceback itself (``vector_metadata_sync_failed`` in
            # ``sync_vector_metadata_outcome``). This call records the
            # verb-specific ``log_event`` the tests and
            # ``handler_warnings`` below key on, not a second copy of the
            # same traceback.
            logger.warning(log_event, item_id=item_id)
    if outcome != "failed":
        # "synced" (re-stamped), "unchanged" (already agreed) and "absent"
        # (no vector row -- structural nodes, never-embedded documents) are
        # all non-errors here, same as the missing-row branch before this
        # was delegated.
        return outcome
    if command is not None:
        command.metadata.setdefault("handler_warnings", []).append(
            f"{log_event}: vector row for {item_id} was not re-stamped; "
            "the semantic path may continue serving a stale snapshot "
            "until the next resync"
        )
    return outcome
