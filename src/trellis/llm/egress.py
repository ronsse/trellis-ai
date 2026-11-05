"""May this content reach this LLM route or embedder? One predicate, everywhere.

A document labelled above ``internal`` (``confidential`` or ``restricted``;
see :mod:`trellis.schemas.classification`) must not be sent to a model whose
route is not declared local. The declaration lives on the route
(:attr:`trellis.llm.routing.LLMRoute.local`, set with ``local: true`` in the
``llm:`` block or a tier) and is never inferred, so a route that says nothing
is treated as cloud. An embedder is declared the same way, with
``local: true`` in the ``embeddings:`` block
(:attr:`trellis.stores.registry.StoreRegistry.embedding_local`), and is never
inferred either: a ``localhost`` ``base_url`` without the declaration is cloud.

Two functions answer the question and every call site that sends stored
content to an LLM or an embedder goes through one of them:

* :func:`may_reach_route` takes a label and a locality.
* :class:`EgressGate` takes a stored document (id plus metadata through
  :meth:`~EgressGate.permits`, or just the id through
  :meth:`~EgressGate.stored_verdict`), so a chunk is judged by its parent's
  label as the retrieval gate judges it, and fails closed: a label that cannot
  be read, a parent that cannot be fetched, or a chunk-shaped id whose parent
  row is absent, answers "no" (retrieval judges that last case by the chunk's
  own label; egress does not, because an unlabelled orphan is exactly what a
  deleted labelled parent leaves behind). ``stored_verdict`` keeps two kinds
  of "no" apart, :class:`EgressVerdict`: ``WITHHELD`` is a permanent fact about
  what is stored (a label, a row that will not parse, an orphan chunk, a pack
  event with no readable item list) and holds on a retry; ``UNREADABLE`` is a
  failure to read that a later run can clear (a store that could not answer, or
  a gate given no store), plus an exception nobody anticipated, which stays loud.
  A gate over a local route reads nothing and answers "yes" for everything, so
  a declared-local route behaves as it always did.

**Scope: chat and generate routes, and stored content sent to an embedder.**
:meth:`EgressGate.for_route` gates what is sent to an LLM's ``generate`` call.
:meth:`EgressGate.for_embedder` gates what
:func:`trellis.retrieve.embed_ingest_hook.build_vector_row` sends to the
embedder: a document withheld there is stored as usual and gets no vector row,
so keyword and graph retrieval still find it and only the semantic axis cannot.
The *query* embedding of a caller's intent at retrieval time is not stored
content and is not gated. A document that already has a vector row and is later
relabelled keeps it: nothing here drops a row, and ``trellis admin
reindex-vectors`` only upserts.

Content that is not itself a stored document but names some, a trace's
evidence refs or the packs a session was served, is judged by the documents it
names: one above ``internal`` withholds the whole trace or session, and one a
store could not be asked about keeps it off the route as ``UNREADABLE``, which
the job reports apart from a label. Neither carries a label of its own, so a
trace or session that names no stored document reads as unlabelled and is sent
as before. There are no exempt senders: every site that sends stored content
to a model or an embedder is rostered in the tripwires below.

``tests/unit/test_llm_egress_rule.py`` and ``tests/unit/test_embed_egress_rule.py``
roster the call sites and require each to reference this module; they are
tripwires for the common shapes, and the behaviour tests beside each sender are
the guard. A skip is counted in the job's own report under
:data:`EGRESS_WITHHELD_REASON`, never only logged; an inline write, which has no
report, emits :attr:`~trellis.stores.base.event_log.EventType.EMBED_WITHHELD`
for ``trellis analyze health`` to count.

This module reads labels; it does not edit them and sits outside
``trellis.retrieve`` so serving is untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from trellis.llm.routing import LLMRoutingError
from trellis.schemas.classification import sensitivity_of, sensitivity_rank
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.llm.routing import LLMConsumer
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.registry import StoreRegistry

__all__ = [
    "EGRESS_UNREADABLE_REASON",
    "EGRESS_WITHHELD_REASON",
    "EgressGate",
    "EgressVerdict",
    "egress_reason",
    "failed_read_verdict",
    "may_reach_route",
]

#: The named reason a report, event or note gives for an egress skip.
EGRESS_WITHHELD_REASON = "egress_withheld"

#: The named reason for a skip because a store could not be asked
#: (:attr:`EgressVerdict.UNREADABLE`), counted apart from
#: :data:`EGRESS_WITHHELD_REASON`: a label is a fact about what is stored, this
#: is a fault a later run can clear.
EGRESS_UNREADABLE_REASON = "egress_unreadable"

#: Content labelled above this never reaches a route that is not local.
_EGRESS_CEILING: Sensitivity = "internal"


class EgressVerdict(StrEnum):
    """What :meth:`EgressGate.stored_verdict` answers for one stored document.

    ``WITHHELD`` and ``UNREADABLE`` both keep the content off the route, and
    they are not the same fact. ``WITHHELD`` is a permanent fact about what is
    stored: a label above ``internal``, a label that is malformed, a row whose
    own bytes do not parse, a chunk id whose parent row is gone or does not
    parse, or (for a served pack, judged by the capture sweep) an immutable
    event that records no readable item list or whose payload does not parse.
    It holds until someone edits the row, so a job counting it must not read as
    a fault. ``UNREADABLE`` is a failure to read that a later run can clear: a
    store that could not answer, or a gate that was given no document store to
    ask. An operator must see it, and a retry may pass.
    """

    PERMITTED = "permitted"
    WITHHELD = "withheld"
    UNREADABLE = "unreadable"


def _verdict(permitted: bool) -> EgressVerdict:
    return EgressVerdict.PERMITTED if permitted else EgressVerdict.WITHHELD


def egress_reason(verdict: EgressVerdict) -> str:
    """The reason a report gives for a skip, unreadable apart from withheld.

    ``PERMITTED`` has no reason and is a caller's bug to ask about; it reads as
    ``egress_withheld`` so a mistake fails toward the cautious label.
    """
    if verdict is EgressVerdict.UNREADABLE:
        return EGRESS_UNREADABLE_REASON
    return EGRESS_WITHHELD_REASON


def failed_read_verdict(exc: Exception) -> EgressVerdict:
    """``WITHHELD`` when ``exc`` is a stored row's own bytes, else ``UNREADABLE``.

    A row whose bytes do not parse does not parse on a retry, so it is a fact
    about what is stored. That covers a document row (``JSONDecodeError``,
    ``RecursionError``, an undecodable SQLite text column) and an event row
    whose payload is valid JSON but not an object, which the event model
    refuses with a ``ValidationError``. Any other exception is a store that
    could not answer, or a defect, and is ``UNREADABLE``.
    """
    # Deferred for the reason given in ``EgressGate.__init__``.
    from trellis.retrieve.sensitivity import _row_cannot_be_parsed  # noqa: PLC0415

    if isinstance(exc, ValidationError) or _row_cannot_be_parsed(exc):
        return EgressVerdict.WITHHELD
    return EgressVerdict.UNREADABLE


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

            # The older judgement, on purpose: an egress verdict is pinned, and the
            # direct reads' fail-closed one would add a read of every chunk's
            # parent to the one this gate's chunk verdict makes through the cache.
            self._clearance = DocumentClearance(
                document_store,
                _EGRESS_CEILING,
                fail_closed_on_unreadable_label=False,
            )

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

    @classmethod
    def for_route_lazy(
        cls,
        registry: StoreRegistry,
        consumer: LLMConsumer,
        open_store: Callable[[], DocumentStore | None],
    ) -> EgressGate:
        """:meth:`for_route`, opening the document store only for a route that reads it.

        A declared-local route reads nothing, so a command on one must not need
        the store it would otherwise open for the gate.
        """
        gate = cls.for_route(registry, consumer, None)
        return gate if gate.local else cls.for_route(registry, consumer, open_store())

    @classmethod
    def for_embedder(
        cls,
        registry: StoreRegistry,
        document_store: DocumentStore | None,
    ) -> EgressGate:
        """A gate over the embedder ``registry.embedding_fn`` resolves.

        Local only when ``embeddings.local: true`` says so
        (:attr:`~trellis.stores.registry.StoreRegistry.embedding_local`); an
        embedder that says nothing is cloud. A malformed declaration reads as
        not local here; ``registry.embedding_fn`` raises on the same defect, so
        nothing is sent either way.
        """
        try:
            local = registry.embedding_local is True
        except LLMRoutingError:
            local = False
        return cls(document_store, local=local)

    @classmethod
    def for_embedder_lazy(
        cls,
        registry: StoreRegistry,
        open_store: Callable[[], DocumentStore | None],
    ) -> EgressGate:
        """:meth:`for_embedder`, opening the document store only for a cloud embedder.

        A declared-local embedder reads nothing, so a command on one must not
        need the store it would otherwise open for the gate.
        """
        gate = cls.for_embedder(registry, None)
        return gate if gate.local else cls.for_embedder(registry, open_store())

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

    def stored_verdict(self, doc_id: str) -> EgressVerdict:
        """Whether the document stored under ``doc_id`` may be sent, by its row.

        For content that names a document without carrying it: a trace's
        evidence ref, or an item a context pack served to a session. An id
        with no stored row names nothing that carries a label and is permitted
        (a graph entity, a trace id), except a chunk-shaped id whose parent row
        is absent, which :meth:`permits` withholds as an orphan and this method
        counts as ``WITHHELD``, because that does not pass on a retry.

        A store that cannot answer, for the row or for a chunk's parent, is
        ``UNREADABLE``, and so is a non-local gate that holds no store at all. So is
        an exception nobody anticipated: a defect should be loud, not filed under
        a label. A row whose own bytes cannot be parsed is ``WITHHELD``: it will
        not parse on a retry either. A local gate answers before it reads anything.
        """
        if self.local:
            return EgressVerdict.PERMITTED
        if self._documents is None:
            # A gate with no store to ask cannot say a stored row is unlabelled,
            # and "no row" must not read as "no label". Not ``WITHHELD``: the
            # row was never consulted, and a gate built with a store can answer.
            return EgressVerdict.UNREADABLE
        try:
            row = self._documents.get(doc_id)
            metadata = row.get("metadata") if row else None
        except Exception as exc:
            # GRACEFUL-DEGRADATION: fail closed, as in ``permits``, and say which
            # kind of closed.
            return failed_read_verdict(exc)
        return self.verdict(doc_id, metadata)

    def verdict(self, doc_id: str, metadata: Mapping[str, Any] | None) -> EgressVerdict:
        """Whether the document ``doc_id`` carrying ``metadata`` may be sent.

        :meth:`stored_verdict` for a caller that already holds the metadata (an
        embed hook has just written it, a backfill has just listed it), so the
        row is not fetched again. A plain document is judged by that metadata's
        own label and a chunk by its parent's, through the same cache.

        A gate with no document store can still judge a plain document by its
        own label; it cannot judge a chunk, whose parent it cannot read, and
        answers ``UNREADABLE`` for that. A metadata value that is not a mapping
        is ``WITHHELD``. A local gate answers before it reads anything. Never
        raises: a failure to read is ``WITHHELD`` for a row whose own bytes do
        not parse and ``UNREADABLE`` otherwise (:func:`failed_read_verdict`).
        """
        if self.local:
            return EgressVerdict.PERMITTED
        if metadata is not None and not isinstance(metadata, Mapping):
            return EgressVerdict.WITHHELD
        try:
            if self._clearance is None:
                return self._verdict_without_store(doc_id, metadata)
            chunk = self._chunk_verdict(doc_id, metadata)
            if chunk is not EgressVerdict.PERMITTED:
                return chunk
            return _verdict(bool(self._clearance.permits(doc_id, metadata)))
        except Exception as exc:
            # GRACEFUL-DEGRADATION: fail closed, as in ``permits``, and say which
            # kind of closed.
            return failed_read_verdict(exc)

    def _verdict_without_store(
        self, doc_id: str, metadata: Mapping[str, Any] | None
    ) -> EgressVerdict:
        """A non-local gate holding no store: a plain document by its own label.

        A chunk's label is its parent's, and with no store the parent cannot be
        read, so a chunk is ``UNREADABLE`` rather than judged by its own label.
        """
        # Deferred for the reason given in ``__init__``.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

        if is_chunk_doc_id(doc_id):
            return EgressVerdict.UNREADABLE
        return _verdict(self.permits_metadata(metadata))

    def _chunk_verdict(
        self, doc_id: str, metadata: Mapping[str, Any] | None
    ) -> EgressVerdict:
        """``PERMITTED`` unless ``doc_id`` is a chunk whose parent cannot be read.

        ``WITHHELD`` when the parent row is absent. When the parent fetch
        raised (the cache records which), the parent is asked again directly
        through :meth:`stored_verdict`, so a parent whose own bytes do not parse
        is ``WITHHELD`` for its chunk as it is for the parent, and a store that
        could not answer is ``UNREADABLE``. The shape is the only signal, as in
        retrieval: a plain document whose id merely resembles a chunk id is
        treated as one. A parent that reads costs no extra read, because the
        lookup goes through the clearance's own cache.
        """
        # Deferred for the reason given in ``__init__``.
        from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415
        from trellis.retrieve.concentration import (  # noqa: PLC0415
            resolve_parent_id,
        )

        if not is_chunk_doc_id(doc_id):
            return EgressVerdict.PERMITTED
        item = PackItem(
            item_id=doc_id, item_type="document", metadata=dict(metadata or {})
        )
        cache = self._clearance._cache
        if cache.resolve(item) is not None:
            return EgressVerdict.PERMITTED
        parent_id = resolve_parent_id(item)
        if parent_id in cache.failed_parent_ids:
            return self.stored_verdict(parent_id)
        return EgressVerdict.WITHHELD

    def _is_orphan_chunk(self, doc_id: str, metadata: Mapping[str, Any] | None) -> bool:
        """A chunk-shaped id whose parent row cannot be read back."""
        return self._chunk_verdict(doc_id, metadata) is not EgressVerdict.PERMITTED

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
