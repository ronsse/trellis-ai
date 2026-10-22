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
surface share. Another route also mints an id there: ``entity.create`` with such
an ``entity_id``, then ``precedent.promote``. That route needs promotion
authority, which is the authority the exemption is meant to protect, so it is
not a hole. The promote handler writes its row through ``put_document``
directly, so it is not stopped by the ingest refusal.

This module imports nothing from the package, so ``classify``, ``mutate`` and
``learning`` can all import it without a cycle.
"""

from __future__ import annotations

#: The id namespace of a promoted learning precedent, graph node and document
#: row alike. Reserved: ``EvidenceIngestHandler`` rejects a document id here.
PRECEDENT_ENTITY_ID_PREFIX = "precedent://learning/"


def is_promoted_precedent_document(doc_id: object) -> bool:
    """Whether ``doc_id`` names the document row of a promoted precedent.

    Reads the id and nothing else; see the module docstring for why metadata,
    which a caller can write, is not consulted.
    """
    return isinstance(doc_id, str) and doc_id.startswith(PRECEDENT_ENTITY_ID_PREFIX)


__all__ = ["PRECEDENT_ENTITY_ID_PREFIX", "is_promoted_precedent_document"]
