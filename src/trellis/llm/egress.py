"""May this content reach this LLM route? One predicate, applied at every sender.

A document labelled above ``internal`` (``confidential`` or ``restricted``;
see :mod:`trellis.schemas.classification`) must not be sent to a model whose
route is not declared local. The declaration lives on the route
(:attr:`trellis.llm.routing.LLMRoute.local`, set with ``local: true`` in the
``llm:`` block or a tier) and is never inferred, so a route that says nothing
is treated as cloud.

Two functions answer the question and every call site that sends stored
content to an LLM goes through one of them:

* :func:`may_reach_route` takes a label and a locality.
* :class:`EgressGate` takes a stored document (id plus metadata, or just the id
  through :meth:`~EgressGate.permits_stored`), so a chunk is
  judged by its parent's label as the retrieval gate judges it, and fails
  closed: a label that cannot be read, a parent that cannot be fetched, or a
  chunk-shaped id whose parent row is absent, answers "no" (retrieval judges
  that last case by the chunk's own label; egress does not, because an
  unlabelled orphan is exactly what a deleted labelled parent leaves behind).
  A gate over a local route reads nothing and answers "yes" for everything, so
  a declared-local route behaves as it always did.

**Scope: chat and generate routes only.** This gates what is sent to an LLM's
``generate`` call. Embedding is not gated: stored content, labelled content
included, still goes to the configured embedder, and an operator with a hosted
embedder must treat that as egress.

Content that is not itself a stored document but names some, a trace's
evidence refs or the packs a session was served, is judged by the documents it
names: one above ``internal`` withholds the whole trace or session. Neither
carries a label of its own, so a trace or session that names no stored document
reads as unlabelled and is sent as before.

``tests/unit/test_llm_egress_rule.py`` rosters the call sites and requires each
to reference this module; it is a tripwire for the common shapes, and the
behaviour tests beside each sender are the guard. A skip is counted in the
job's own report under :data:`EGRESS_WITHHELD_REASON`, never only logged.

This module reads labels; it does not edit them and sits outside
``trellis.retrieve`` so serving is untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from trellis.llm.routing import LLMRoutingError
from trellis.schemas.classification import sensitivity_of, sensitivity_rank
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.llm.routing import LLMConsumer
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.registry import StoreRegistry

__all__ = [
    "EGRESS_WITHHELD_REASON",
    "EgressGate",
    "may_reach_route",
]

#: The named reason a report, event or note gives for an egress skip.
EGRESS_WITHHELD_REASON = "egress_withheld"

#: Content labelled above this never reaches a route that is not local.
_EGRESS_CEILING: Sensitivity = "internal"


def may_reach_route(label: Sensitivity, *, local: bool) -> bool:
    """Whether content labelled ``label`` may be sent to a route.

    ``local`` is the route's explicit declaration
    (:attr:`~trellis.llm.routing.LLMRoute.local`): ``True`` admits any label,
    ``False`` (the default for an undeclared route) admits ``public`` and
    ``internal`` only. ``internal`` is the label an unlabelled document reads
    as, so unlabelled content is admitted everywhere, as before.
    """
    return local or sensitivity_rank(label) <= sensitivity_rank(_EGRESS_CEILING)


class EgressGate:
    """:func:`may_reach_route` over stored documents, failing closed.

    One instance per job: it carries a per-instance parent cache, and a
    ``local`` route skips the label read entirely. Build it from the same
    route the job builds its client from, :meth:`for_route`, so the two cannot
    disagree about which route the content is going to.
    """

    def __init__(self, document_store: DocumentStore | None, *, local: bool) -> None:
        self.local = local is True
        self._documents = document_store
        self._clearance: Any = None
        if not self.local and document_store is not None:
            # Deferred: ``trellis.retrieve`` is a heavy package and
            # ``trellis.ingest_corpus`` imports back into it, so a
            # module-level import here would couple every sender to it.
            from trellis.retrieve.sensitivity import (  # noqa: PLC0415
                DocumentClearance,
            )

            self._clearance = DocumentClearance(document_store, _EGRESS_CEILING)

    @classmethod
    def for_route(
        cls,
        registry: StoreRegistry,
        consumer: LLMConsumer,
        document_store: DocumentStore | None,
    ) -> EgressGate:
        """A gate over the route ``consumer`` builds its client from.

        An unresolvable routing block reads as not local; the client build
        raises on the same defect, so nothing is sent either way.
        """
        try:
            local = registry.llm_route(consumer).local is True
        except LLMRoutingError:
            local = False
        return cls(document_store, local=local)

    def permits(self, doc_id: str, metadata: Mapping[str, Any] | None) -> bool:
        """Whether the stored document ``doc_id`` may be sent to the route.

        Never raises: any failure to read the label is a refusal, and so is a
        chunk-shaped id whose parent row is absent.
        """
        if self.local:
            return True
        if metadata is not None and not isinstance(metadata, Mapping):
            return False
        try:
            if self._clearance is None:
                return self.permits_metadata(metadata)
            if self._is_orphan_chunk(doc_id, metadata):
                return False
            return bool(self._clearance.permits(doc_id, metadata))
        except Exception:
            # GRACEFUL-DEGRADATION: fail closed. A label that cannot be read
            # (malformed metadata, a parent the store cannot answer for) must
            # not read as permission to send the content off the host; the
            # job counts the skip under EGRESS_WITHHELD_REASON.
            return False

    def permits_stored(self, doc_id: str) -> bool:
        """Whether the document stored under ``doc_id`` may be sent, by its row.

        For content that names a document without carrying it: a trace's
        evidence ref, or an item a context pack served to a session. An id
        with no stored row names nothing that carries a label and is
        permitted (a graph entity, a trace id), except a chunk-shaped id,
        which :meth:`permits` withholds as an orphan. A row that cannot be
        read is a refusal. A local gate answers before it reads anything.
        """
        if self.local:
            return True
        row = None
        if self._documents is not None:
            try:
                row = self._documents.get(doc_id)
            except Exception:
                # GRACEFUL-DEGRADATION: fail closed, as in ``permits``.
                return False
        return self.permits(doc_id, row.get("metadata") if row else None)

    def _is_orphan_chunk(self, doc_id: str, metadata: Mapping[str, Any] | None) -> bool:
        """A chunk-shaped id whose parent row cannot be read back.

        The shape is the only signal, as in retrieval: a plain document whose
        id merely resembles a chunk id is treated as one. The parent lookup
        goes through the clearance's own cache, so it costs no extra read.
        """
        # Deferred for the reason given in ``__init__``.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

        if not is_chunk_doc_id(doc_id):
            return False
        item = PackItem(
            item_id=doc_id, item_type="document", metadata=dict(metadata or {})
        )
        return self._clearance._cache.resolve(item) is None

    def permits_metadata(self, metadata: Mapping[str, Any] | None) -> bool:
        """Whether content carrying ``metadata`` may be sent, by its own label.

        For content with no stored row yet (a memory being saved), where there
        is no parent to inherit from. Never raises.
        """
        if self.local:
            return True
        if metadata is not None and not isinstance(metadata, Mapping):
            return False
        try:
            return may_reach_route(sensitivity_of(metadata), local=False)
        except Exception:
            # GRACEFUL-DEGRADATION: fail closed, as in ``permits``.
            return False
