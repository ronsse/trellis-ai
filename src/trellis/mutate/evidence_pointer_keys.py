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
  a ``lifecycle`` is accepted when it says ``{"state": "current"}`` with every
  other field null or at its default (the record ``Lifecycle()`` dumps), and an
  ``evidence_ref`` when it is the value the document the command will write
  already holds, so a round-tripped bag keeps working. Anything else is refused:
  a ``current`` record that also carries a ``deprecation_reason`` would forge the
  tombstone a redaction leaves, which blocks later writes to the id.

``POST /evidence`` needs neither: it never stores the body's own ``metadata``.

**A graph node's ``evidence_ref``** (:func:`node_evidence_ref_refusal`) reaches
the same place from the other side: a node that carries one is the item another
item's ``superseded_by`` resolves to. ``entity.create``, ``entity.update`` and
``observation.record`` keep the field, since they are its governed writers, but
a value a caller supplies must name a document that exists and that the caller
may read, and a node that already holds one keeps it: a different value is
refused, the unchanged value (a read-back node sent again) is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from trellis.mutate.sensitivity_floor import visible_document
from trellis.retrieve.lifecycle import EVIDENCE_REF_KEY
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.mutate.commands import Command
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
    '. A caller may write a lifecycle only as {"state": "current"}, with any '
    "other field null or at its default: that restores a document a "
    "supersession stamp withheld"
)

POINTER_KEYS: Final[frozenset[str]] = frozenset(DOCUMENT_POINTER_REFUSALS)

#: Lifecycle fields the restore form may carry, but only as null.
_NULL_ONLY_FIELDS: Final[frozenset[str]] = frozenset(
    {"valid_from", "valid_until", "superseded_by", "deprecation_reason"}
)
_DEFAULT_CLASSIFICATION_VERSION: Final[str] = Lifecycle().classification_version


#: ``ValidationError.code`` of a node ``evidence_ref`` refusal, one per reason.
NODE_EVIDENCE_REF_UNREADABLE: Final[str] = "node_evidence_ref_unreadable"
NODE_EVIDENCE_REF_CHANGED: Final[str] = "node_evidence_ref_changed"
NODE_EVIDENCE_REF_NOT_A_STRING: Final[str] = "node_evidence_ref_not_a_string"

#: Longest caller-supplied id a refusal message echoes back. The message becomes
#: the immutable ``mutation.rejected`` audit event, so a value is never copied
#: into it whole.
_ECHO_LIMIT: Final[int] = 120


@dataclass(frozen=True)
class PointerKeyRefusal:
    """Why a document write was refused: the offending key and the message."""

    key: str
    message: str


@dataclass(frozen=True)
class NodeEvidenceRefRefusal:
    """Why a graph node's ``evidence_ref`` was refused: the audit code and message."""

    code: str
    message: str


def _is_restore_form(value: object) -> bool:
    """Whether ``value`` is the documented restore record.

    ``{"state": "current"}``, optionally with the other :class:`Lifecycle`
    fields at their defaults, so the record ``Lifecycle().model_dump()``
    produces passes too. A set value in any other field is refused, not merely
    the withholding ones: ``deprecation_reason`` set to ``redacted`` on a
    document with empty content is the tombstone a redaction leaves
    (:func:`~trellis.core.document_write.is_redaction_tombstone`), which blocks
    later writes to the id, corpus sync and promotion. An unknown key is
    refused, and a record that leaves the state to its default is not the
    documented form either.
    """
    if not isinstance(value, dict) or value.get("state") != "current":
        return False
    for key, field_value in value.items():
        if key == "state":
            continue
        if key in _NULL_ONLY_FIELDS and field_value is None:
            continue
        if (
            key == "classification_version"
            and field_value == _DEFAULT_CLASSIFICATION_VERSION
        ):
            continue
        return False
    return True


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


def node_evidence_ref_refusal(
    properties: Mapping[str, Any],
    *,
    stored_properties: Mapping[str, Any] | None,
    document_store: DocumentStore,
    clearance: Sensitivity,
    writer: str,
) -> NodeEvidenceRefRefusal | None:
    """The refusal for the ``evidence_ref`` a node write carries, or ``None``.

    ``properties`` is the property bag the command writes (``entity.create``'s
    or ``entity.update``'s ``properties``, or the one key an observation sets);
    ``stored_properties`` is the node's current bag, or ``None`` for a node the
    write creates. ``writer`` names the command, for the message. A bag without
    the key is no verdict. With it:

    * A node that already holds an ``evidence_ref`` keeps it. The same value is
      accepted whether or not its document still exists (a node read back and
      sent again must keep working); any other value, ``null`` included, is
      refused.
    * On a node that holds none, a value must be a non-empty string naming a
      document the caller may read under ``clearance``. A document that does not
      exist and one above the caller's clearance are refused with the same
      message, so a refusal does not tell a key that a document it may not read
      exists. ``null`` is accepted there: it points nowhere.

    Neither refusal carries the stored value.
    """
    if EVIDENCE_REF_KEY not in properties:
        return None
    value = properties[EVIDENCE_REF_KEY]
    stored = (
        stored_properties.get(EVIDENCE_REF_KEY)
        if stored_properties is not None
        else None
    )
    refusal: NodeEvidenceRefRefusal | None = None
    if stored not in (None, ""):
        if value != stored:
            refusal = NodeEvidenceRefRefusal(
                NODE_EVIDENCE_REF_CHANGED,
                f"{writer} may not change a node's evidence_ref once it is set: "
                "it names the document another item is superseded by, so a node "
                "keeps the one it has. Sending the stored value again is "
                "accepted. Nothing was written",
            )
    elif value is not None:
        if not isinstance(value, str) or not value:
            refusal = NodeEvidenceRefRefusal(
                NODE_EVIDENCE_REF_NOT_A_STRING,
                f"{writer} needs evidence_ref to be the id of a document, a "
                "non-empty string. Nothing was written",
            )
        elif visible_document(document_store, value, clearance=clearance) is None:
            refusal = NodeEvidenceRefRefusal(
                NODE_EVIDENCE_REF_UNREADABLE,
                f"{writer} evidence_ref {value[:_ECHO_LIMIT]!r} does not name a "
                "document this credential can read: a node's evidence_ref is "
                "the document another item is superseded by, so it must exist. "
                "Save the document first (MCP save_knowledge(evidence_ref=...) "
                "checks this). Nothing was written",
            )
    return refusal


def evidence_ingest_pointer_refusal(
    command: Command,
    *,
    document_store: DocumentStore,
    clearance: Sensitivity,
) -> PointerKeyRefusal | None:
    """The refusal for a generic-door ``evidence.ingest`` command, or ``None``.

    ``command`` is the command the door built, so the check judges what the
    command will write: the document id is the one the builder resolved (the
    caller's ``doc_id``, else the batch item's ``target_id``, else the id derived
    from the idempotency key or a fresh one) and the metadata is the prepared
    ``evidence.metadata``. The stored row is read at the caller's ``clearance``:
    a row above it reads as absent, so the comparison for ``evidence_ref``
    cannot say what a document it may not read holds.
    """
    evidence = command.args.get("evidence")
    if not isinstance(evidence, dict):
        return None
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict) or POINTER_KEYS.isdisjoint(metadata):
        return None
    doc_id = evidence.get("doc_id")
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
    "NODE_EVIDENCE_REF_CHANGED",
    "NODE_EVIDENCE_REF_NOT_A_STRING",
    "NODE_EVIDENCE_REF_UNREADABLE",
    "POINTER_KEYS",
    "NodeEvidenceRefRefusal",
    "PointerKeyRefusal",
    "evidence_ingest_pointer_refusal",
    "node_evidence_ref_refusal",
    "pointer_key_refusal",
]
