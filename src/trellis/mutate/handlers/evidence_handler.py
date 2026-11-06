"""The ``evidence.ingest`` handler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.core.document_write import (
    RedactionTombstoneError,
    _guard_tombstone,
    is_redaction_tombstone,
    put_document,
)
from trellis.core.precedent_document import (
    RESERVED_DOCUMENT_ID_CODE,
    is_promoted_precedent_document,
    reserved_document_id_message,
)
from trellis.core.reserved_doc_ids import (
    RESERVED_TRACE_SUMMARY_ID_CODE,
    TRACE_SUMMARY_DOC_ID_PREFIX,
    reserved_trace_summary_id_message,
)
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import PolicyViolationError, StoreError, ValidationError
from trellis.llm.egress import EgressGate
from trellis.mutate.commands import Command
from trellis.mutate.evidence_ingest import EvidenceEmbedMode
from trellis.mutate.sensitivity_floor import (
    SENSITIVITY_FLOOR_POLICY_ID,
    refuse_written_label,
)
from trellis.retrieve.sensitivity import OverwriteRefusedError, refuse_overwrite
from trellis.schemas.classification import sensitivity_of
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.llm.egress import EgressVerdict
    from trellis.schemas.classification import Sensitivity


def _evidence_document_id(
    value: object,
    reserved_prefixes: tuple[str, ...] = (TRACE_SUMMARY_DOC_ID_PREFIX,),
) -> str:
    """``evidence.doc_id`` as a caller-chosen document id, or a refusal.

    A promoted precedent's document row is written by the promote handler
    alone. Letting a caller choose an id in that namespace would let it exempt
    its own memory from noise demotion and retention (see
    :mod:`trellis.core.precedent_document`). A chunk id is written by corpus
    ingestion alone: an id in the minted ``<x>#chunk-<digits>`` shape cannot be
    told from one afterwards, so a caller may not choose it (#1008). An id
    under one of ``reserved_prefixes`` (the trace embed worker's summary
    namespace, by default) is refused too: a row there makes the trace it names
    look already embedded (#1054).
    """
    if not isinstance(value, str) or not value:
        msg = "evidence.doc_id must be a non-empty string"
        raise ValidationError(msg, code="evidence_ingest_shape")
    if is_promoted_precedent_document(value):
        msg = reserved_document_id_message("evidence.doc_id", value)
        raise ValidationError(msg, code=RESERVED_DOCUMENT_ID_CODE)
    # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
    from trellis.ingest_corpus.models import (  # noqa: PLC0415
        is_chunk_doc_id,
        reserved_chunk_doc_id_message,
    )

    if is_chunk_doc_id(value):
        msg = reserved_chunk_doc_id_message("evidence.doc_id", value)
        raise ValidationError(msg, code=RESERVED_DOCUMENT_ID_CODE)
    if any(value.startswith(prefix) for prefix in reserved_prefixes):
        msg = reserved_trace_summary_id_message("evidence.doc_id", value)
        raise ValidationError(msg, code=RESERVED_TRACE_SUMMARY_ID_CODE)
    return value


def _evidence_flag(evidence: dict[str, Any], key: str) -> bool:
    """An optional ``evidence`` flag, refused unless it is a real boolean."""
    value = evidence.get(key, False)
    if not isinstance(value, bool):
        msg = f"evidence.{key} must be a boolean"
        raise ValidationError(msg, code="evidence_ingest_shape")
    return value


class EvidenceIngestHandler:
    """Store a document and optionally create its vector row.

    Agent-facing writes use ``soft`` embedding because the document is
    authoritative and must survive an embedder outage. Repair workers use
    ``strict`` because their success contract is the vector row itself.
    ``none`` lets ``save_memory`` keep embedding outside its process-wide
    deduplication lock.
    """

    #: Document id prefixes this handler refuses from a caller (#1054). A class
    #: attribute, not a command field: only code that constructs the trace
    #: embed worker's handler can clear it, where ``Command.requested_by`` is a
    #: string any caller may write.
    reserved_doc_id_prefixes: tuple[str, ...] = (TRACE_SUMMARY_DOC_ID_PREFIX,)

    def __init__(
        self,
        registry: StoreRegistry,
        *,
        embed: EvidenceEmbedMode = "soft",
        embedding_fn: Callable[[str], list[float]] | None = None,
        empty_error_code: str = "evidence_ingest_empty",
        clearance: Sensitivity = "restricted",
    ) -> None:
        self._registry = registry
        self._embed = embed
        self._embedding_fn = embedding_fn
        self._empty_error_code = empty_error_code
        self._clearance = clearance
        #: Set by the last ``strict`` embed that the egress gate declined: the
        #: verdict, else ``None``. ``handle`` returns ``(id, message)`` only,
        #: so a caller that counts a withheld document (the trace-embed worker)
        #: reads it here after a ``SUCCESS``; reset at the start of each command.
        self.last_embed_withheld: EgressVerdict | None = None

    def _label_for_write(
        self,
        doc_id: str,  # noqa: ARG002
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """The metadata to write for ``doc_id``; a label it inherits is stamped here.

        Runs after the write-time classifiers and before the clearance rules,
        so a label added here is checked like any other. The core writer
        inherits nothing and returns ``metadata`` as it came.
        """
        return metadata

    def _enforce_clearance(self, doc_id: str, metadata: dict[str, Any]) -> None:
        """The save surfaces' write rules, run where the write is.

        Run here so the generic doors (``POST /commands/batch``,
        ``execute_mutation``) are covered too (#1023). Two refusals, both
        before any write:

        * the label being written is above this caller's clearance, for a new
          document and an existing one alike (the rule ``classification.set``
          and ``save_memory`` apply, in the same words);
        * a write at an existing id would replace the row, label included, and
          the stored label is above this caller's clearance or above the label
          being written. A trusted caller (``clearance="restricted"``) still may
          not lower a label.
        """
        try:
            refuse_written_label(
                doc_id, sensitivity_of(metadata), clearance=self._clearance
            )
            refuse_overwrite(
                self._registry.knowledge.document_store,
                doc_id,
                clearance=self._clearance,
                metadata=metadata,
            )
        except OverwriteRefusedError as exc:
            raise PolicyViolationError(
                exc.message, policy_id=SENSITIVITY_FLOOR_POLICY_ID
            ) from exc

    def replay_refusal(self, command: Command) -> ValidationError | None:
        """The tombstone refusal this write would get now, for a replayed key.

        The executor asks only when the command's idempotency key was already
        seen, and answers DUPLICATE otherwise (see ``CommandHandler``). A
        duplicate says the write is in place, which it is not once the id the
        first write put has since been redacted: nothing is written and the
        tombstone stays, so the caller is told the refusal rather than "ok".
        Read-only.

        Only a row that is a tombstone is asked. Over a live row, or an id with
        no row, nothing was lost and the replay is the duplicate it was, so the
        guard's stamp check (which refuses a forged stamp on any write) is not
        run there: a replay of a write that carried the row's own stamp, after
        a later write dropped it, is still a true duplicate. Over a tombstone
        the answer is :func:`~trellis.core.document_write._guard_tombstone`'s
        own, run over the stored row and this command's content and metadata,
        so the two cannot disagree about what a tombstone is or what a write
        over one may carry, in either refusal code
        (``tests/unit/mutate/test_replay_refusal.py`` pins it).

        A flagged command is never refused here: its key digests the flag, so it
        replays a write that already put content back over the tombstone, and
        a replay writes nothing. If that row was redacted again afterwards the
        replay is still a DUPLICATE with the tombstone in place; the flag says
        a deliberate write is allowed, not that a replay of one repeats it. A
        command ``handle`` would refuse for its shape, and a malformed one, are
        left to the executor's DUPLICATE as before. A store read that raises
        propagates, and the executor fails the command closed.
        """
        evidence = command.args.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("reingest_redacted") is True:
            return None
        doc_id = evidence.get("doc_id")
        content = evidence.get("content")
        if not isinstance(doc_id, str) or not doc_id or not isinstance(content, str):
            return None
        stored = self._registry.knowledge.document_store.get(doc_id)
        if stored is None or not is_redaction_tombstone(stored):
            return None
        metadata = evidence.get("metadata")
        try:
            _guard_tombstone(
                stored,
                content,
                dict(metadata) if isinstance(metadata, dict) else {},
                reingest_redacted=False,
            )
        except RedactionTombstoneError as exc:
            return exc
        return None

    def _embed_strict(
        self,
        doc_id: str,
        content: str,
        metadata: dict[str, Any],
        evidence: dict[str, Any],
        requested_by: str,
    ) -> bool:
        """Embed the stored document and upsert its vector; not fail-soft.

        ``True`` when the egress gate kept the document off the embedder: it is
        stored, has no vector row, and the verdict is left on
        :attr:`last_embed_withheld` for a caller that counts it. That is not a
        failure of the command. Every other failure raises.
        """
        from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
            EmbedWithheldError,
            build_vector_row,
            record_embed_withheld,
        )

        vector_store = getattr(self._registry.knowledge, "vector_store", None)
        if vector_store is None:
            msg = "no vector store configured"
            raise StoreError(msg, store="vector")
        if self._embedding_fn is None:
            msg = "no embedding function configured"
            raise StoreError(msg, store="vector")
        raw_created_at = evidence.get("created_at")
        created_at = raw_created_at if isinstance(raw_created_at, str) else None
        try:
            row = build_vector_row(
                doc_id,
                content,
                metadata,
                self._embedding_fn,
                egress=EgressGate.for_embedder(
                    self._registry, self._registry.knowledge.document_store
                ),
                created_at=created_at,
            )
        except EmbedWithheldError as exc:
            self.last_embed_withheld = exc.verdict
            record_embed_withheld(
                self._registry, doc_id, exc.verdict, source=requested_by
            )
            return True
        vector_store.upsert(
            item_id=row["item_id"],
            vector=row["vector"],
            metadata=row["metadata"],
        )
        return False

    def handle(self, command: Command) -> tuple[str | None, str]:
        self.last_embed_withheld = None
        evidence = command.args.get("evidence")
        if not isinstance(evidence, dict):
            msg = "evidence must be a mapping"
            raise ValidationError(msg, code="evidence_ingest_shape")

        doc_id = _evidence_document_id(
            evidence.get("doc_id"), self.reserved_doc_id_prefixes
        )
        content = evidence.get("content")
        uri = evidence.get("uri")
        if not isinstance(content, str):
            msg = "evidence.content must be a string"
            raise ValidationError(msg, code="evidence_ingest_shape")
        if not content.strip() and not (isinstance(uri, str) and bool(uri.strip())):
            msg = "evidence content or uri is required"
            raise ValidationError(msg, code=self._empty_error_code)

        raw_metadata = evidence.get("metadata")
        if raw_metadata is not None and not isinstance(raw_metadata, dict):
            msg = "evidence.metadata must be a mapping"
            raise ValidationError(msg, code="evidence_ingest_shape")
        metadata: dict[str, Any] = dict(raw_metadata or {})
        if isinstance(uri, str) and uri.strip():
            metadata.setdefault("uri", uri)

        preserve_updated_at = evidence.get("preserve_updated_at", False)
        if not isinstance(preserve_updated_at, bool):
            msg = "evidence.preserve_updated_at must be a boolean"
            raise ValidationError(msg, code="evidence_ingest_shape")
        reingest_redacted = _evidence_flag(evidence, "reingest_redacted")

        requested_mode = evidence.get("embed_mode")
        if requested_mode is not None and requested_mode != self._embed:
            msg = (
                f"evidence embed_mode {requested_mode!r} does not match "
                f"handler mode {self._embed!r}"
            )
            raise ValidationError(msg, code="evidence_ingest_mode")

        from trellis.classify.ingest import classify_metadata_on_write  # noqa: PLC0415

        metadata = classify_metadata_on_write(
            metadata,
            content,
            source_system=str(metadata.get("source_system") or ""),
            doc_id=doc_id,
            registry=self._registry,
        )
        metadata = self._label_for_write(doc_id, metadata)
        self._enforce_clearance(doc_id, metadata)
        # Through the seam even though both embed modes below rebuild the
        # whole row: ``run_embed_on_ingest`` is gated on
        # ``TRELLIS_ENABLE_EMBED_ON_INGEST``, so on a deployment with that
        # flag off the ``"soft"`` arm mirrors nothing at all — and an
        # evidence write may be metadata-only (``preserve_updated_at``),
        # where there is no embed to piggyback on in either mode.
        put_document(
            self._registry.knowledge.document_store,
            resolve_vector_store(self._registry),
            doc_id,
            content,
            metadata,
            preserve_updated_at=preserve_updated_at,
            reingest_redacted=reingest_redacted,
        )

        if self._embed == "soft":
            from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
                run_embed_on_ingest,
            )

            run_embed_on_ingest(
                self._registry,
                doc_id,
                content,
                metadata,
                source=command.requested_by,
            )
        elif self._embed == "strict":
            withheld = self._embed_strict(
                doc_id, content, metadata, evidence, command.requested_by
            )
            if withheld:
                return doc_id, f"Evidence stored without a vector: {doc_id}"

        return doc_id, f"Evidence stored: {doc_id}"
