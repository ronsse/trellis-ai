"""The ``retention.prune`` and ``retention.restore`` handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import ValidationError
from trellis.mutate.commands import Command
from trellis.mutate.handlers._common import (
    _LINKED_SIGNAL_LIMIT,
    _current_lifecycle_state,
    _rewrite_current_node,
    _sync_vector_lifecycle,
)
from trellis.mutate.retention import (
    ARCHIVED_STATE,
    CURRENT_STATE,
    MAX_DOCUMENTS_SCANNED,
    MAX_ENTITIES_SCANNED,
    ResolutionReport,
    RetentionCandidate,
    RetentionCriteria,
    resolve_candidates,
)
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.event_log import EventType
from trellis.stores.base.graph import GraphStore
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


MAX_RETENTION_REASON_CHARS = 2000


def _prune_message(
    report: ResolutionReport,
    by_reason: dict[str, int],
    *,
    dry_run: bool,
    archived: int,
    skipped: int,
    resynced: int,
) -> str:
    """Render the operator-facing summary for one prune run.

    Every qualifier here reports something that would otherwise be silent:
    a capped scan (the candidate set is a prefix), a requested lifecycle
    state this phase cannot select at all, items that vanished mid-run,
    vector rows repaired, and items protected because a human restored them.
    """
    verb = "would archive" if dry_run else "archived"
    counts = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())) or "none"
    message = (
        f"retention.prune ({'dry run' if dry_run else 'applied'}): "
        f"{verb} {len(report.candidates) if dry_run else archived} item(s) "
        f"[{counts}]"
    )
    if report.unselectable_lifecycle_states:
        # #419: without this the operator cannot tell "no rows are in that
        # state" from "this phase can never select that state".
        inert = ", ".join(report.unselectable_lifecycle_states)
        # Phrased as the rule, not the writer: "current items were explicitly
        # restored" would be the same provenance claim #419's dangerous half
        # was about. ``current`` is protected for what the value asserts,
        # whoever wrote it.
        message += (
            f"; WARNING lifecycle state(s) [{inert}] cannot be selected by "
            "phase one (archived is already archived; current asserts the "
            "item belongs in service) — they scanned nothing and their zero "
            "says nothing about the corpus"
        )
    if report.scan_truncated:
        # The document plane stops at its cap and nothing else, so it can name
        # one. The entity scan can also stop on the walk's read budget or on a
        # store whose total contradicts its pages, so "capped" would be false
        # for two of its three causes.
        causes = {
            "document": f"scan capped at {MAX_DOCUMENTS_SCANNED} documents",
            "entity": (
                f"entity scan stopped early (the {MAX_ENTITIES_SCANNED}-entity cap, "
                "the read budget, or store totals that disagreed)"
            ),
        }
        stopped = (
            " and ".join(causes[plane] for plane in report.scan_truncated_planes)
            or causes["document"]
        )
        message += (
            f"; WARNING {stopped} — candidate set is a prefix "
            "of the real population, not the whole of it"
        )
    if skipped:
        message += f"; {skipped} skipped (vanished between resolve and write)"
    if resynced:
        message += f"; {resynced} stale vector row(s) re-synced"
    if report.skipped_restored:
        # Not "(explicitly restored)": ``retention.restore`` is the writer
        # that motivated the guard, not the only one that can trip it — an
        # ``entity.update`` property merge or any bag-forwarding save writes
        # ``current`` too, and lands in this same counter (#419).
        message += (
            f"; {report.skipped_restored} protected (state=current — "
            "asserted in service)"
        )
    if report.skipped_precedent:
        message += (
            f"; {report.skipped_precedent} promoted-precedent item(s) (document "
            "rows and graph nodes) exempt (their lifecycle belongs to the "
            "precedent handlers)"
        )
    return message


class RetentionPruneHandler:
    """Resolve retention criteria and archive the resulting candidates.

    Wires :data:`Operation.RETENTION_PRUNE` into the governed pipeline. The
    enum verb shipped with an empty ``set()`` args schema and no handler, so
    the executor rejected every ``retention.prune`` command with "No handler
    registered" — the exact gap ``redaction.apply`` had before
    :class:`RedactionApplyHandler`, and the reason 24 noise-tagged captures
    had to be demoted-and-kept rather than removed. Decision record:
    ``docs/design/adr-retention-prune.md`` (Option A, phase one).

    **Phase one is archival, not purge.** The handler stamps
    :class:`~trellis.schemas.classification.Lifecycle`
    ``state="archived"`` and retrieval stops serving the item
    (:mod:`trellis.retrieve.lifecycle`). Physical removal is deliberately
    deferred until the archived population is real: retention is criteria-
    driven and a criteria bug is a *batch* mistake, so the version an
    operator can walk back is the one worth shipping first. This makes
    ``Lifecycle`` — schema-without-behaviour since it landed — both its
    first writer and its first enforcement point.

    **Dry-run by default.** ``dry_run`` defaults to ``True`` when the caller
    omits it. Destructive-by-default is right for a single named redaction
    target and wrong for a predicate that resolves to a set the caller has
    not seen; a dry run emits the same event flagged ``dry_run=True`` so the
    preview is as auditable as the real thing.

    **Criteria-driven batches tolerate a moving candidate set.** An item
    that vanishes between resolution and write is counted in ``skipped``,
    not raised — unlike redaction's single-target
    :class:`~trellis.errors.NotFoundError`, a batch whose population shifts
    underneath it has not failed. Items already archived are filtered during
    resolution, so a re-run is a no-op rather than a second version bump.
    Use ``Command.idempotency_key`` when at-most-once semantics are needed.

    **Never traces, never event rows.** Enforced by construction:
    :func:`~trellis.mutate.retention.resolve_candidates` reads the document
    and graph stores and nothing else. Confirmed entities are dropped on
    every criterion — confirmation is a human's judgment that the entity is
    real, and age is not evidence against it.

    ``PolicyType.RETENTION`` gets its first consumer here: a deny rule on
    ``retention.prune`` is evaluated by the executor's standard policy stage
    before this handler runs, which is the operator's kill switch.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw_criteria = command.args["criteria"]
        if not isinstance(raw_criteria, dict):
            msg = "retention.prune criteria must be an object"
            raise ValidationError(msg, code="retention_criteria_invalid")
        criteria = RetentionCriteria.model_validate(raw_criteria)

        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "retention.prune requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="retention_reason_required")
        if len(reason) > MAX_RETENTION_REASON_CHARS:
            msg = (
                f"retention.prune reason exceeds {MAX_RETENTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="retention_reason_too_long")

        dry_run = bool(command.args.get("dry_run", True))

        event_log = self._registry.operational.event_log
        if not dry_run and isinstance(event_log, NullEventLog):
            msg = (
                "retention.prune requires a persisting event log for a "
                "non-dry run: the RETENTION_PRUNED event is the only record "
                "that the archival happened, and this deployment's event_log "
                "backend is 'null'"
            )
            raise ValidationError(msg, code="retention_requires_event_log")

        report = resolve_candidates(criteria, self._registry)

        archived = 0
        skipped = 0
        resynced = 0
        unfinished: Exception | None = None
        if not dry_run:
            try:
                for candidate in report.candidates:
                    if self._archive(candidate, reason):
                        archived += 1
                    else:
                        skipped += 1
                # Self-healing: an item archived before the vector sync
                # existed still has a stale snapshot, and the semantic path
                # serves from that snapshot. Re-running the prune repairs
                # them — no new verb for what is a one-shot consequence of
                # shipping the sync late.
                resynced = self._resync_archived(report.already_archived_ids)
            except Exception as exc:
                # Re-raised after the event: what was archived stays archived.
                unfinished = exc

        by_reason: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for candidate in report.candidates:
            code = candidate.reason_code
            by_reason[code] = by_reason.get(code, 0) + 1
            by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1

        payload: dict[str, Any] = {
            "criteria": criteria.model_dump(mode="json"),
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "dry_run": dry_run,
            "phase": "archival",
            "candidates": len(report.candidates),
            "archived": archived,
            "skipped": skipped,
            "by_reason": by_reason,
            "by_kind": by_kind,
            "documents_scanned": report.documents_scanned,
            "entities_scanned": report.entities_scanned,
            "scan_truncated": report.scan_truncated,
            "scan_truncated_planes": report.scan_truncated_planes,
            "skipped_already_archived": report.skipped_already_archived,
            "skipped_confirmed": report.skipped_confirmed,
            "skipped_restored": report.skipped_restored,
            "skipped_precedent": report.skipped_precedent,
            # #419: which requested lifecycle states contributed a
            # guaranteed zero. Audited alongside the criteria that named
            # them so a reviewer reading this event later can tell an inert
            # criterion from an empty corpus.
            "unselectable_lifecycle_states": report.unselectable_lifecycle_states,
            "vector_rows_resynced": resynced,
            # Capped pointer, not an index — see _LINKED_SIGNAL_LIMIT.
            "item_ids": [c.item_id for c in report.candidates[:_LINKED_SIGNAL_LIMIT]],
        }

        message = _prune_message(
            report,
            by_reason,
            dry_run=dry_run,
            archived=archived,
            skipped=skipped,
            resynced=resynced,
        )

        try:
            event_log.emit(
                EventType.RETENTION_PRUNED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_run",
                payload=payload,
            )
        except Exception:
            # Same discipline as RedactionApplyHandler: for a non-dry run the
            # archival already happened, so raising would report FAILED for
            # completed work. Preserve the payload in operator logs.
            logger.warning(
                "retention_audit_emit_failed",
                command_id=command.command_id,
                retention_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: RETENTION_PRUNED audit emit failed; "
                "payload preserved in operator logs)"
            )
        if unfinished is not None:
            raise unfinished
        return command.command_id, message

    def _resync_archived(self, item_ids: list[str]) -> int:
        """Re-stamp vector rows for already-archived items whose row is stale.

        Returns the number actually repaired — a row that already carries the
        archived stamp is left alone, so a steady-state re-run reports zero.
        """
        if not item_ids:
            return 0
        lifecycle = Lifecycle(state=ARCHIVED_STATE).model_dump(mode="json")
        vector_store = self._registry.knowledge.vector_store
        repaired = 0
        for item_id in item_ids:
            try:
                row = vector_store.get(item_id)
            except Exception:
                logger.warning(
                    "retention_vector_resync_read_failed",
                    item_id=item_id,
                    exc_info=True,
                )
                continue
            if row is None:
                continue
            metadata = row.get("metadata") or {}
            record = metadata.get(LIFECYCLE_KEY)
            if isinstance(record, dict) and record.get("state") == ARCHIVED_STATE:
                continue
            _sync_vector_lifecycle(self._registry, item_id, lifecycle)
            repaired += 1
        return repaired

    def _archive(self, candidate: RetentionCandidate, reason: str) -> bool:
        """Stamp one candidate archived. Returns False if it vanished."""
        lifecycle = Lifecycle(
            state=ARCHIVED_STATE,
            valid_until=datetime.now(UTC),
            deprecation_reason=reason,
        ).model_dump(mode="json")

        if candidate.kind == "document":
            store = self._registry.knowledge.document_store
            doc = store.get(candidate.item_id)
            if doc is None:
                return False
            metadata = dict(doc.get("metadata") or {})
            metadata[LIFECYCLE_KEY] = lifecycle
            # Metadata-only: the content written back is the row's own, and
            # only the lifecycle stamp changes.
            #
            # Masked on the *pack* surfaces — ``retrieve.lifecycle``'s
            # ``exclude_archived`` drops the row at the collect seam — and
            # ``_restore`` is the operation that hands it back to retrieval
            # still carrying whatever stamp this write left. But that is not
            # the same as latent (#406): ``retrieve.file_context`` reads the
            # column straight off ``list_documents``, with no lifecycle
            # predicate, so an archived document's bump moves the
            # ``newest_item_at`` its path reports to the read hook's staleness
            # gate immediately.
            put_document(
                store,
                resolve_vector_store(self._registry),
                candidate.item_id,
                doc["content"],
                metadata,
                preserve_updated_at=True,
            )
            return True

        graph = self._registry.knowledge.graph_store

        def archive(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            props[LIFECYCLE_KEY] = lifecycle
            return {"properties": props}

        # SCD-2: the rewrite closes the current version and opens a new one,
        # so the pre-archival state stays readable via get_node_history and
        # ``as_of``. A node purged since the resolve is skipped, not re-created.
        return _rewrite_current_node(graph, candidate.item_id, archive) is not None


def _pre_archival_lifecycle(graph: GraphStore, item_id: str) -> dict[str, Any]:
    """Return the ``Lifecycle`` dict ``item_id`` held right before its most
    recent archival, read from SCD-2 history rather than assumed.

    A plain ``Lifecycle(state="current")`` here would restore a precedent
    that was *demoted*, then pruned, straight back to ``"current"`` --
    laundering the demotion through a prune/restore round-trip with no
    trace of it in the new version (the demoted one is still in history,
    but nothing about the restored version says to look). A promoted
    precedent in the ``precedent://learning/`` namespace is never pruned
    now, so that case is reachable only for a precedent outside the
    namespace or one archived before the exemption existed. Walks back past
    every consecutive archived-state version -- an intervening write that
    never touches ``lifecycle`` (e.g. ``entity.update``) carries
    ``"archived"`` forward through the property merge, so more than one
    version can read as archived -- to the version immediately before the
    most recent transition into it, and returns that version's whole
    ``Lifecycle`` record, not just its ``state``, so a demotion's
    ``deprecation_reason`` and ``valid_until`` come back too.

    Refuses rather than guesses when history cannot answer the question:
    every version on record reads as archived, including the degenerate
    case of no history at all (an id purged mid-command) — either way,
    no pre-archival version exists to read.
    """
    history = graph.get_node_history(item_id)
    idx = 0
    total = len(history)
    while idx < total and _current_lifecycle_state(history[idx]) == ARCHIVED_STATE:
        idx += 1
    if idx >= total:
        msg = (
            f"retention.restore cannot determine the lifecycle state "
            f"{item_id} held before archival: its SCD-2 history has no "
            "version that is not itself archived"
        )
        raise ValidationError(
            msg, errors=[msg], code="retention_restore_history_unavailable"
        )
    record = history[idx]["properties"].get(LIFECYCLE_KEY)
    if isinstance(record, dict):
        return dict(record)
    # Absent/malformed on the pre-archival version itself fails open to
    # "current", matching _current_lifecycle_state's own convention.
    return Lifecycle(state=CURRENT_STATE).model_dump(mode="json")


def _validate_restore_args(command: Command) -> tuple[list[str], str]:
    """``item_ids`` and ``reason``, validated for ``retention.restore``.

    Split out of ``RetentionRestoreHandler.handle`` to keep that method's
    branch count under the linter's ceiling.
    """
    raw_ids = command.args["item_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        msg = "retention.restore requires a non-empty item_ids list"
        raise ValidationError(msg, code="retention_restore_ids_required")
    item_ids = [str(i) for i in raw_ids]

    reason = str(command.args["reason"] or "").strip()
    if not reason:
        msg = "retention.restore requires a non-empty reason for the audit trail"
        raise ValidationError(msg, code="retention_reason_required")
    if len(reason) > MAX_RETENTION_REASON_CHARS:
        msg = f"retention.restore reason exceeds {MAX_RETENTION_REASON_CHARS} chars"
        raise ValidationError(msg, code="retention_reason_too_long")
    return item_ids, reason


def _raise_nothing_restored(refused: list[dict[str, str]], skipped: list[str]) -> None:
    """Escalate a wholly-unactioned ``retention.restore`` batch.

    "Skipped" alone is a legitimate success (a corrective batch may
    legitimately name items someone else already restored), but a batch
    that restored nothing *and* refused at least one id is not a no-op
    retry — it is reported to the caller as rejected, naming every
    refused id *and* every skipped one, not only the refused half, so an
    operator reading the rejection sees the whole batch rather than just
    the part that failed loudly. Split out of
    ``RetentionRestoreHandler.handle`` to keep that method's branch count
    under the linter's ceiling.
    """
    codes = {r["code"] for r in refused}
    code = (
        next(iter(codes)) if len(codes) == 1 else "retention_restore_nothing_restored"
    )
    detail = "; ".join(f"{r['item_id']} ({r['code']})" for r in refused)
    if skipped:
        detail += "; " + "; ".join(f"{item_id} (not archived)" for item_id in skipped)
    msg = f"retention.restore restored none of the requested items: {detail}"
    raise ValidationError(msg, errors=[r["message"] for r in refused], code=code)


class RetentionRestoreHandler:
    """Return archived items to ``Lifecycle.state="current"``.

    Wires :data:`Operation.RETENTION_RESTORE`. This is the half that makes
    :class:`RetentionPruneHandler`'s central claim true: phase one is
    archival rather than purge *because* "a wrong prune is walked back by
    re-stamping" — and re-stamping needs a governed path, or the claim is
    only rhetorical. Direct store writes are not an option
    (``CLAUDE.md``: all mutations go through the governed pipeline), so
    without this verb an operator who over-pruned had no sanctioned remedy
    at all.

    **Explicit ids, not a predicate.** ``retention.prune`` resolves criteria
    because it selects a population nobody has enumerated. Restore is the
    opposite situation: the operator knows exactly which items were wrong,
    because their ids ride the ``RETENTION_PRUNED`` payload. Re-deriving
    them from criteria would re-run the selection that was wrong the first
    time.

    Restoring a document sets ``state="current"`` and clears the
    archival's ``valid_until`` / ``deprecation_reason`` rather than
    deleting the lifecycle record — the item is current again, and the
    ``RETENTION_PRUNED`` → ``RETENTION_RESTORED`` event pair is the
    history. A graph node is restored to whatever state it held
    immediately *before* that archival instead — read from SCD-2 history
    via :func:`_pre_archival_lifecycle` rather than assumed to be
    ``"current"`` — so a precedent that was demoted and then pruned comes
    back demoted, not reinstated: prune-then-restore is not a second,
    unaudited way to undo a demotion. Only a precedent outside the
    ``precedent://learning/`` namespace, or one archived before the
    exemption existed, can have been pruned: a promoted precedent in the
    namespace is never a prune candidate.

    A vector row's metadata is an embed-time snapshot, not a live read of
    the graph store, so a graph-node restore also re-stamps the node's
    vector row with the lifecycle it was just given — the same
    ``_sync_vector_lifecycle`` call :class:`PrecedentDemoteHandler` and
    :class:`PrecedentReinstateHandler` already make. Without it, a
    restored precedent's row kept reading ``archived`` and stayed withheld
    on the semantic path even though the graph node itself was current
    again — wrong, though it failed closed rather than open. The document
    branch needs no equivalent call: ``put_document`` already mirrors the
    lifecycle onto the vector row for every document write. A sync failure
    does not undo the restore (the graph write already happened); it is
    logged and appended to ``CommandResult.warnings``, exactly like a
    demote/reinstate sync failure.

    An id that is not archived is counted in ``skipped``, not raised: a
    corrective batch assembled from an audit payload will legitimately name
    items someone else already restored.

    A precedent whose history cannot answer what it held before archival
    (:func:`_pre_archival_lifecycle`) is counted in ``refused``, not raised
    out of the loop: the ids in the same batch after it are independent
    restores and the ones already restored before it stay restored. Each
    refusal's id and code land in the ``RETENTION_RESTORED`` payload's
    ``refused_items`` and in ``CommandResult.warnings`` (via
    ``command.metadata["handler_warnings"]``, see ``_handler_warnings`` in
    ``executor.py``), so a caller sees exactly which ids need
    ``precedent.reinstate`` or a different remedy instead of a blanket
    retry. The command is rejected only when *nothing* in the batch was
    restored — some skipped and some refused, with nothing restored, still
    rejects, since "skipped" alone cannot turn a wholly-unactioned batch
    into a reported success. The rejection message names every skipped id
    alongside every refused one, not only the refused half, so an operator
    reading the rejection sees the whole batch rather than just the part
    that failed loudly. A genuinely unexpected exception (not a refusal)
    still aborts the remainder of the batch, as before.

    Two more refusals cover a document row that holds nothing servable
    (:meth:`_refuse_unrestorable_row`): a promoted precedent's row archived
    while its node no longer exists
    (``retention_restore_precedent_node_missing``), and the blank row a
    redaction leaves, including one whose purge failed so its node is still
    live (``retention_restore_redaction_tombstone``). They ride the same
    path, so each is named in ``refused_items`` and the warnings, and neither
    can be un-archived into a ``current`` row with no content or no node
    (#957).

    ``RETENTION_RESTORED`` is emitted unconditionally, including for a
    batch that restored nothing at all (all refused, all skipped, or a
    mix of the two) — the same audit-completeness posture as
    ``RETENTION_PRUNED``, which is emitted in both dry-run and apply mode
    even over zero candidates. Nothing in this codebase branches on the
    event's ``restored`` count, so there is no consumer to protect by
    suppressing it, and an audit trail with a gap for "requested but
    nothing happened" is a worse trail than one that records the attempt.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        item_ids, reason = _validate_restore_args(command)
        restored, skipped, refused, unfinished = self._restore_batch(item_ids, command)

        payload: dict[str, Any] = {
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "restored": len(restored),
            "skipped": len(skipped),
            "refused": len(refused),
            "restored_ids": restored,
            "skipped_ids": skipped,
            "refused_ids": [r["item_id"] for r in refused],
            "refused_items": refused,
        }
        message = f"retention.restore: restored {len(restored)} item(s)"
        if skipped:
            message += f", {len(skipped)} not archived (skipped)"
        if refused:
            codes = ", ".join(sorted({r["code"] for r in refused}))
            message += f", {len(refused)} refused ({codes})"
            command.metadata.setdefault("handler_warnings", []).extend(
                f"retention.restore: {r['item_id']} refused ({r['code']}): "
                f"{r['message']}"
                for r in refused
            )
        try:
            self._registry.operational.event_log.emit(
                EventType.RETENTION_RESTORED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_restore",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "retention_restore_audit_emit_failed",
                command_id=command.command_id,
                restore_payload=payload,
                exc_info=True,
            )
            message += " (WARNING: RETENTION_RESTORED audit emit failed)"
        if unfinished is not None:
            raise unfinished
        if not restored and refused:
            _raise_nothing_restored(refused, skipped)
        return command.command_id, message

    def _restore_batch(
        self, item_ids: list[str], command: Command
    ) -> tuple[list[str], list[str], list[dict[str, str]], Exception | None]:
        """Restore every id, collecting outcomes rather than aborting on a
        per-item history refusal (see ``_pre_archival_lifecycle``).
        Anything other than that specific, expected refusal still escapes
        to the outer ``except`` and aborts the remainder via the returned
        exception — what was restored before it stays restored. Split out
        of ``handle`` to keep that method's branch count under the
        linter's ceiling.
        """
        restored: list[str] = []
        skipped: list[str] = []
        refused: list[dict[str, str]] = []
        unfinished: Exception | None = None
        try:
            for item_id in item_ids:
                try:
                    if self._restore(item_id, command):
                        restored.append(item_id)
                    else:
                        skipped.append(item_id)
                except ValidationError as exc:
                    refused.append(
                        {"item_id": item_id, "code": exc.code, "message": str(exc)}
                    )
        except Exception as exc:
            unfinished = exc
        return restored, skipped, refused, unfinished

    def _refuse_unrestorable_row(self, item_id: str, doc: dict[str, Any]) -> None:
        """Refuse to un-archive a row that holds nothing servable (#957).

        Two refusals, each through the ordinary ``refused`` path. Refused
        rather than skipped: the row *is* archived, and reporting it "not
        archived" would tell the operator the wrong thing.

        * ``retention_restore_precedent_node_missing``: a promoted
          precedent's row whose graph node no longer exists. Only a row in the
          ``precedent://learning/`` namespace is paired with a node.
        * ``retention_restore_redaction_tombstone``: the blank row a
          redaction leaves, whatever the state of its node. It covers a
          redaction whose purge failed after the row was blanked
          (:func:`_tombstone_redacted_documents` runs first), where the node
          is still live and a restore would serve empty text as ``current``,
          and it covers a document outside the namespace.

        The node check runs first, so a redacted precedent reports the
        missing node.
        """
        if (
            is_promoted_precedent_document(item_id)
            and self._registry.knowledge.graph_store.get_node(item_id) is None
        ):
            msg = (
                f"retention.restore will not un-archive {item_id}: it is a "
                "promoted precedent's document row and its graph node no "
                "longer exists, so a current row would have no node behind it"
            )
            raise ValidationError(
                msg, errors=[msg], code="retention_restore_precedent_node_missing"
            )
        if is_redaction_tombstone(doc):
            msg = (
                f"retention.restore will not un-archive {item_id}: it is the "
                "blank row a redaction left, so there is no content to "
                "restore. If its graph node still exists the redaction did "
                "not finish; re-run redaction.apply on it"
            )
            raise ValidationError(
                msg, errors=[msg], code="retention_restore_redaction_tombstone"
            )

    def _restore(self, item_id: str, command: Command) -> bool:
        """Un-archive one item. Returns False if it was not archived."""
        current = Lifecycle(state="current").model_dump(mode="json")

        doc_store = self._registry.knowledge.document_store
        doc = doc_store.get(item_id)
        if doc is not None:
            metadata = dict(doc.get("metadata") or {})
            record = metadata.get(LIFECYCLE_KEY)
            if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
                return False
            self._refuse_unrestorable_row(item_id, doc)
            metadata[LIFECYCLE_KEY] = current
            # Metadata-only, and unmasked (#406). Restore exists to make the
            # item servable again, so unlike the archive it undoes there is
            # no downstream filter left to render a bumped ``updated_at``
            # moot: without the flag, un-archiving a two-year-old note hands
            # it back to ``KeywordSearch`` as the freshest document in the
            # corpus. An operator walking back a bad prune from the
            # ``RETENTION_PRUNED`` payload would thereby *promote* exactly
            # the items they meant only to restore.
            #
            # Scoped to the document branch, and that scoping is now
            # *correct* rather than merely pending. The entity branch below
            # re-opens an SCD-2 version whose ``updated_at`` is now — but
            # SCD-2 carries ``created_at`` forward, ``GraphStore.query`` is
            # ``ORDER BY created_at DESC LIMIT n`` on every shipped backend,
            # and since #420 ``GraphSearch`` decays off that same column
            # (``retrieve.strategies.GRAPH_RECENCY_CLOCK_FIELD``). So a
            # restored node neither re-enters the graph axis's candidate
            # window nor gains rank inside it, and the rewrite needs no
            # ``preserve_updated_at`` equivalent. Fixed reader-side because
            # the writers that actually move a node's ``updated_at`` are the
            # ordinary ones (``entity.update``, extraction upserts), which no
            # kwarg here would ever have been passed on.
            put_document(
                doc_store,
                resolve_vector_store(self._registry),
                item_id,
                doc["content"],
                metadata,
                preserve_updated_at=True,
            )
            return True

        graph = self._registry.knowledge.graph_store

        def restore(node: dict[str, Any]) -> dict[str, Any] | None:
            props = dict(node["properties"])
            record = props.get(LIFECYCLE_KEY)
            if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
                return None
            if node["node_type"] == "precedent":
                # Not ``current`` unconditionally: read the state the node
                # held just before this archival (SCD-2 history), so a
                # precedent that was demoted and then pruned comes back
                # demoted, not silently reinstated (see
                # _pre_archival_lifecycle). Scoped to precedents: every
                # other node type has no "demoted" concept to launder, and
                # requiring pre-archival history from them would refuse a
                # restore for a node that was simply minted archived.
                props[LIFECYCLE_KEY] = _pre_archival_lifecycle(graph, item_id)
            else:
                props[LIFECYCLE_KEY] = current
            return {"properties": props}

        written = _rewrite_current_node(graph, item_id, restore)
        if written is None:
            return False
        # A graph node has no document write to piggyback a vector mirror
        # onto (unlike the document branch above, which gets one for
        # free from put_document), so re-stamp the vector row directly --
        # the same gap PrecedentDemoteHandler/PrecedentReinstateHandler
        # close with this call. Without it, a restored precedent's row
        # kept reading archived and stayed withheld on the semantic path.
        _sync_vector_lifecycle(
            self._registry,
            item_id,
            written["properties"][LIFECYCLE_KEY],
            command=command,
            log_event="retention_restore_vector_lifecycle_sync_failed",
        )
        return True
