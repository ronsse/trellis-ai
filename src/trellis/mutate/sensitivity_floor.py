"""The sensitivity floor on the mutation side (#194).

:mod:`trellis.retrieve.sensitivity` withholds a document above the caller's
clearance from every read. This module is the same boundary for writes: a
mutation whose target is a document the caller may not read is refused, so a
key that cannot see a labelled document cannot overwrite, restore, redact or
relabel it either.

Two layers, one predicate (:class:`~trellis.retrieve.sensitivity.DocumentClearance`):

* **Stage 2.** :class:`SensitivityFloor` is injected into the policy gate
  (:class:`~trellis.mutate.policy_gate.DefaultPolicyGate`) and asked about every
  command before a handler runs. It reads only the target document's label and
  refuses above clearance, which is what the handlers cannot see for a verb
  they were never taught about.
* **Handlers.** ``evidence.ingest`` and ``classification.set`` run
  :func:`~trellis.retrieve.sensitivity.refuse_overwrite` / :func:`refuse_relabel`
  themselves, because they decide the label being *written*, which Stage 2
  cannot know (a cleared caller may not silently downgrade, #1023).

**Transparent by default.** A ``"restricted"`` clearance (trusted-local: the
CLI, stdio MCP, auth off, an admin key) reads nothing and refuses nothing. A
store with no labels refuses no one, so a command's result, payload and audit
fields are the ones it had before this module existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.mutate.commands import Command, Operation
from trellis.retrieve.sensitivity import (
    DocumentClearance,
    OverwriteRefusedError,
    permits,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore

#: ``PolicyViolationError.policy_id`` a handler-level refusal carries, so the
#: rejection names this floor rather than a policy file entry.
SENSITIVITY_FLOOR_POLICY_ID = "sensitivity_floor"


def refuse_above_clearance(
    document_store: DocumentStore,
    doc_id: str,
    *,
    clearance: Sensitivity,
) -> None:
    """Raise :class:`OverwriteRefusedError` when ``doc_id`` is above ``clearance``.

    A missing document is no verdict, and a chunk id is judged by the higher of
    its own label and its parent's, as every read surface does.
    """
    if clearance == "restricted":
        return
    existing = document_store.get(doc_id)
    if existing is None:
        return
    if not DocumentClearance(document_store, clearance).permits(
        doc_id, existing.get("metadata")
    ):
        msg = (
            f"document {doc_id!r} is above this credential's clearance; "
            "nothing was written"
        )
        raise OverwriteRefusedError(msg)


def refuse_relabel(
    document_store: DocumentStore,
    doc_id: str,
    new_sensitivity: Sensitivity,
    *,
    clearance: Sensitivity,
) -> None:
    """Refuse relabelling ``doc_id`` unless ``clearance`` covers old *and* new label.

    A caller may not touch a document it cannot read, and may not assign a
    label above its own clearance either: that would put the document out of
    its own reach, an escalation it could not undo. Lowering a label it clears
    is allowed, which is what relabelling is for.
    """
    refuse_above_clearance(document_store, doc_id, clearance=clearance)
    if not permits(clearance, new_sensitivity):
        msg = (
            "this credential's clearance does not cover the label requested "
            f"for document {doc_id!r}; nothing was written"
        )
        raise OverwriteRefusedError(msg)


def targeted_document_ids(command: Command) -> list[str]:
    """The document ids ``command`` names as its target, by operation.

    An explicit map rather than a scan of ``args``: an operation that does not
    write a caller-named document row (entity, link, label, trace and the
    record verbs) names none, and a scan would read a graph node id as a
    document id. ``entity.create``'s ``document_ids`` are pointers stored
    verbatim, not writes to those documents, so they are not targets.
    """
    args: Mapping[str, Any] = command.args
    op = command.operation
    if op == Operation.EVIDENCE_INGEST:
        evidence = args.get("evidence")
        doc_id = evidence.get("doc_id") if isinstance(evidence, dict) else None
        return [doc_id] if isinstance(doc_id, str) and doc_id else []
    if op == Operation.CLASSIFICATION_SET:
        doc_id = args.get("doc_id")
        return [doc_id] if isinstance(doc_id, str) and doc_id else []
    if op == Operation.RETENTION_RESTORE:
        item_ids = args.get("item_ids")
        if not isinstance(item_ids, list):
            return []
        return [i for i in item_ids if isinstance(i, str) and i]
    if op in (
        Operation.REDACTION_APPLY,
        Operation.PRECEDENT_DEMOTE,
        Operation.PRECEDENT_REINSTATE,
    ):
        target = args.get("target_id")
        return [target] if isinstance(target, str) and target else []
    return []


class SensitivityFloor:
    """Stage-2 predicate: refuse a command aimed at a document above clearance.

    Injected into :class:`~trellis.mutate.policy_gate.DefaultPolicyGate`, which
    asks :meth:`refusal` before it evaluates any policy. Returns the refusal
    text, or ``None`` to let the command through. A store read that raises
    propagates: an outage must not read as permission.
    """

    def __init__(self, document_store: DocumentStore, clearance: Sensitivity) -> None:
        self._document_store = document_store
        self._clearance = clearance

    def refusal(self, command: Command) -> str | None:
        if self._clearance == "restricted":
            return None
        for doc_id in targeted_document_ids(command):
            try:
                refuse_above_clearance(
                    self._document_store, doc_id, clearance=self._clearance
                )
            except OverwriteRefusedError as exc:
                return exc.message
        return None


__all__ = [
    "SENSITIVITY_FLOOR_POLICY_ID",
    "SensitivityFloor",
    "refuse_above_clearance",
    "refuse_relabel",
    "targeted_document_ids",
]
