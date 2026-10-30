"""Document metadata keys a caller may not use to withhold or redirect an item.

The pack builder withholds items *by id*. A document whose ``metadata.lifecycle``
says ``superseded`` withholds every candidate under the document's id, so a
document written at a graph node's id hides that node too, and a document that
carries ``metadata.evidence_ref`` becomes the successor another item's
``superseded_by`` resolves to, which withholds that item. Both keys therefore
reach an item other than the document, so a caller does not get to set them.
Each has a governed writer, named in its refusal message.

The handler cannot hold this rule for every door:
``save_memory(supersedes=)`` stamps a document's ``lifecycle`` through
``evidence.ingest`` on purpose, so ``EvidenceIngestHandler`` accepts both
keys. The rule sits at the agent-facing doors instead, in two strengths:

* **Strict** (``POST /documents``): the key is refused whatever its value.
* **Restore form** (``POST /commands/batch``, MCP ``execute_mutation``): the
  documented operator undo of a supersession stamp sends the document's metadata
  bag back through ``evidence.ingest`` with ``lifecycle`` set to ``current``, so
  a ``lifecycle`` is accepted when it is a record that withholds nothing
  (``state`` ``current``, no ``superseded_by``, no ``valid_until``), and an
  ``evidence_ref`` when it is the value the same document already holds, so a
  round-tripped bag keeps working. Anything else is refused.

``POST /evidence`` needs neither: it never stores the body's own ``metadata``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from trellis.mutate.sensitivity_floor import visible_document
from trellis.retrieve.lifecycle import EVIDENCE_REF_KEY
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore

#: ``metadata`` keys a document write may not carry, with the refusal each gets.
DOCUMENT_POINTER_REFUSALS: Final[dict[str, str]] = {
    LIFECYCLE_KEY: (
        "metadata.lifecycle may not be set on a document write: it withholds "
        "every item under the document's id, a graph node's included. Supersede "
        "a document with MCP save_memory(supersedes=...); bring an archived one "
        "back with the retention.restore command. Nothing was written"
    ),
    EVIDENCE_REF_KEY: (
        "metadata.evidence_ref may not be set on a document write: it names the "
        "document another item is superseded by. MCP "
        "save_knowledge(evidence_ref=...) sets it on a graph node. "
        "Nothing was written"
    ),
}

#: Added to the ``lifecycle`` refusal at a door that accepts the restore form.
LIFECYCLE_RESTORE_NOTE: Final[str] = (
    '. A caller may write a lifecycle only as {"state": "current"}, with no '
    "superseded_by or valid_until: that restores a document a supersession "
    "stamp withheld"
)

POINTER_KEYS: Final[frozenset[str]] = frozenset(DOCUMENT_POINTER_REFUSALS)


@dataclass(frozen=True)
class PointerKeyRefusal:
    """Why a document write was refused: the offending key and the message."""

    key: str
    message: str


def _is_restore_form(value: object) -> bool:
    """Whether ``value`` is a lifecycle record that withholds nothing.

    A record that says ``state: current`` and carries no ``superseded_by`` and no
    ``valid_until``. The state is spelled out (a record that leaves it to the
    default is not the documented form), and the record must be a valid
    :class:`~trellis.schemas.classification.Lifecycle` (a non-object, an unknown
    key or a mistyped field is not), so what the door accepts is what the pack
    builder's readers can parse.
    """
    if not isinstance(value, dict) or value.get("state") != "current":
        return False
    try:
        record = Lifecycle.model_validate(value)
    except ValidationError:
        return False
    return record.superseded_by is None and record.valid_until is None


def pointer_key_refusal(
    metadata: Mapping[str, Any],
    *,
    stored_metadata: Mapping[str, Any] | None = None,
    strict: bool = False,
) -> PointerKeyRefusal | None:
    """The refusal for the first pointer key in ``metadata``, or ``None``.

    ``strict`` refuses either key whatever its value. Without it a ``lifecycle``
    is accepted in the restore form, and an ``evidence_ref`` when
    ``stored_metadata`` (the metadata of the row the write replaces, or ``None``
    when there is none) holds the same value under the same key.
    """
    for key, message in DOCUMENT_POINTER_REFUSALS.items():
        if key not in metadata:
            continue
        value = metadata[key]
        if strict:
            return PointerKeyRefusal(key, message)
        if key == LIFECYCLE_KEY:
            if not _is_restore_form(value):
                return PointerKeyRefusal(key, message + LIFECYCLE_RESTORE_NOTE)
        elif not (
            stored_metadata is not None
            and key in stored_metadata
            and stored_metadata[key] == value
        ):
            return PointerKeyRefusal(key, message)
    return None


def evidence_ingest_pointer_refusal(
    args: Mapping[str, Any],
    *,
    target_id: str | None,
    document_store: DocumentStore,
    clearance: Sensitivity,
) -> PointerKeyRefusal | None:
    """The refusal for a generic-door ``evidence.ingest`` command, or ``None``.

    ``args`` is the command's argument map as the caller sent it, and
    ``target_id`` the id the door passes to
    :func:`~trellis.mutate.evidence_ingest.build_evidence_ingest_command_from_args`
    (so the same document id is judged that the command will write). Only the
    caller's own ``evidence.metadata`` is read. The stored row is read at the
    caller's ``clearance``: a row above it reads as absent, so the comparison
    for ``evidence_ref`` cannot say what a document it may not read holds.
    """
    evidence = args.get("evidence")
    if not isinstance(evidence, dict):
        return None
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict) or POINTER_KEYS.isdisjoint(metadata):
        return None
    doc_id = evidence.get("doc_id") or target_id
    stored: dict[str, Any] | None = None
    if isinstance(doc_id, str) and doc_id:
        stored = visible_document(document_store, doc_id, clearance=clearance)
    stored_metadata = stored.get("metadata") if stored is not None else None
    return pointer_key_refusal(
        metadata,
        stored_metadata=stored_metadata if isinstance(stored_metadata, dict) else None,
    )


__all__ = [
    "DOCUMENT_POINTER_REFUSALS",
    "LIFECYCLE_RESTORE_NOTE",
    "POINTER_KEYS",
    "PointerKeyRefusal",
    "evidence_ingest_pointer_refusal",
    "pointer_key_refusal",
]
