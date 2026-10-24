"""Which document rows belong to a promoted precedent.

A promoted precedent writes a document row under the same id as its graph node
(``PrecedentPromoteHandler``) so the keyword and semantic axes can serve it. The
row's lifecycle belongs to the precedent handlers alone: a hand-promoted
precedent is exempt from automatic demotion, and a document-plane loop that
tagged, re-tagged or archived the row would reopen the exemption through the
side door. Every such loop asks :func:`is_promoted_precedent_document`.

**The predicate keys on the id, never on metadata.** A document's metadata is an
open bag that ``save_memory`` and ``POST /documents`` forward verbatim, so
``document_form="precedent"`` is something any caller can write, and a predicate
that read it would let a caller exempt their own memory from noise demotion and
retention. The id namespace is different: ``precedent://learning/`` is minted by
:func:`~trellis.learning.scoring.precedent_entity_id_for_candidate`, and
``EvidenceIngestHandler`` refuses a caller-chosen document id inside it, which is
the one funnel ``save_memory``, ``POST /documents`` and the generic mutation
surface share (``trellis ingest evidence`` refuses it the same way). That
refusal protects the *row*, which only ``precedent.promote`` writes (through
``put_document`` directly, so the ingest refusal does not stop it), and only for
a precedent-typed node whose id is in the namespace.

The *node* is a different matter. ``entity.create`` takes a caller-supplied
``entity_id`` and is the very call ``submit_learning_promotion`` makes to write
a promoted precedent's node, so it cannot refuse the namespace, and a node there
is skipped by ``retention.prune`` whoever created it. That grants nothing new:
``lifecycle.state=current`` already makes any node prune-immune (counted as
``skipped_restored``). So the promotion-authority argument holds for the row
alone, and the exemption is keyed on the id and nothing else.

This module imports nothing from the package, so ``classify``, ``mutate`` and
``learning`` can all import it without a cycle.
"""

from __future__ import annotations

#: The id namespace of a promoted learning precedent, graph node and document
#: row alike. Reserved for the *row*: ``EvidenceIngestHandler`` and
#: ``trellis ingest evidence`` reject a document id here.
PRECEDENT_ENTITY_ID_PREFIX = "precedent://learning/"

#: ``ValidationError.code`` of a caller-chosen document id inside the namespace,
#: shared by every surface that refuses one so they cannot disagree.
RESERVED_DOCUMENT_ID_CODE = "reserved_document_id"


def reserved_document_id_message(field: str, doc_id: str) -> str:
    """The refusal text for a caller-chosen ``doc_id`` in the reserved namespace.

    ``field`` names where the caller supplied it (``evidence.doc_id``,
    ``evidence_id``).
    """
    return (
        f"{field} {doc_id!r} is in the reserved precedent namespace; "
        "a precedent's document row is written by its promotion"
    )


def is_promoted_precedent_document(doc_id: object) -> bool:
    """Whether ``doc_id`` names the document row of a promoted precedent.

    Reads the id and nothing else; see the module docstring for why metadata,
    which a caller can write, is not consulted.
    """
    return isinstance(doc_id, str) and doc_id.startswith(PRECEDENT_ENTITY_ID_PREFIX)


__all__ = [
    "PRECEDENT_ENTITY_ID_PREFIX",
    "RESERVED_DOCUMENT_ID_CODE",
    "is_promoted_precedent_document",
    "reserved_document_id_message",
]
