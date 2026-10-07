"""Validate and apply an agent's explicit ``supersedes=`` declaration (#613).

:func:`~trellis.retrieve.lifecycle.partition_superseded` withholds an item
whose ``lifecycle`` stamp names a successor present in the same pool. Until
this module nothing produced that stamp deterministically — the reference
deployment held no stamped document on 2026-09-26 — so the gate had nothing
to act on. ``save_memory(supersedes=T)`` and ``save_knowledge(supersedes=N)``
are its first deterministic producer: the agent that wrote the replacement
says what it replaces, and the stamp is written through the governed
pipeline — ``evidence.ingest`` for a document, ``entity.update`` for a node —
never by a direct store write.

The module has two halves with different return conventions, because the
caller has to decide different things with each.

* **Checks** return a :class:`Refusal` (or ``None``) instead of raising one,
  and emit nothing. The tool records the ``WRITE_REJECTED`` event and raises,
  so every rejection site stays in ``server.py`` with a literal ``tool=`` —
  the form the capture-surface roster scans for.
* **Stamps** return an error string, or ``None`` on success, and never
  raise. Whether a failed stamp is an error or a warning depends on what the
  call has already written, and only the call site knows that.

Stamps carry no idempotency key and check state instead: a stamp already in
place writes nothing, so a resend is free, and a stamp an operator has
undone is re-applied by the next resend. A key derived from the write would
answer that resend with a silent ``DUPLICATE`` and leave the undo standing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from trellis.core.error_sanitize import render_exception_detail
from trellis.core.hashing import content_hash
from trellis.mutate import (
    Command,
    CommandStatus,
    MutationExecutor,
    Operation,
    build_curate_executor,
    build_evidence_ingest_command,
)
from trellis.retrieve.lifecycle import (
    EVIDENCE_REF_KEY,
    declared_successor,
    is_archived,
)
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.document import DocumentStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Refusal:
    """Why a ``supersedes=`` call is refused before anything is written.

    ``kind`` and ``loc`` are the ``WRITE_REJECTED`` row's classification;
    ``msg`` is the caller-facing message and ends by saying nothing was
    written, because only the message text reaches an MCP client.
    """

    kind: str
    loc: str
    msg: str

    def row(self) -> dict[str, str]:
        """The rejection row ``record_write_rejection`` carries."""
        return {"kind": self.kind, "loc": self.loc, "msg": self.msg}


@dataclass(frozen=True)
class EntityPlan:
    """What ``save_knowledge(supersedes=N)`` will stamp besides ``N``.

    ``t_doc`` is ``N``'s evidence document (``None`` when it has none).
    ``t_doc_skip`` is why that document is left unchanged, or ``None`` when
    it is stamped too.
    """

    t_doc: str | None
    t_doc_skip: str | None


def _stamp(successor: str) -> dict[str, Any]:
    return Lifecycle(state="superseded", superseded_by=successor).model_dump(
        mode="json"
    )


def _refused(kind: str, loc: str, message: str) -> Refusal:
    return Refusal(kind, loc, f"{message}; nothing was written")


def _ineligibility(doc: dict[str, Any], *, allowed: set[str]) -> str | None:
    """Why ``doc`` cannot be stamped, or ``None`` if it can.

    A chunked document is refused rather than half-stamped: its chunks are
    separate rows the gate would keep serving. Corpus sync writes
    ``chunk_count`` on the parent and on every chunk row, and it is read the
    way sync reads it back. An existing stamp naming a successor in
    ``allowed`` is this call's own earlier stamp, not a conflict.
    """
    metadata = doc.get("metadata") or {}
    chunk_count = metadata.get("chunk_count")
    if isinstance(chunk_count, int) and chunk_count > 0:
        return "it is part of a chunked document, which supersedes= does not support"
    if is_archived(metadata):
        return "it is archived"
    prior = declared_successor(metadata)
    if prior is not None and prior not in allowed:
        return f"it is already superseded by {prior}"
    return None


def check_memory_target(
    registry: StoreRegistry,
    target: str,
    *,
    doc_id: str | None,
    existing: dict[str, Any] | None,
) -> Refusal | None:
    """Refuse ``save_memory(supersedes=target)`` before the memory is stored.

    ``existing`` is the document already holding this call's exact content
    (the exact-hash hit), and ``doc_id`` the id the caller asked for; either
    could be the successor, so either naming ``target`` would declare a
    memory superseding itself.
    """
    doc = registry.knowledge.document_store.get(target)
    if doc is None:
        return _refused(
            "dangling_reference",
            "supersedes",
            f"supersedes does not reference an existing document: {target}",
        )
    existing_id = existing["doc_id"] if existing is not None else None
    own = {i for i in (existing_id, doc_id) if i is not None}
    if target in own:
        return _refused(
            "value",
            "supersedes",
            f"supersedes={target} names the document this call would write or "
            "match, and a memory cannot supersede itself",
        )
    reason = _ineligibility(doc, allowed=own)
    if reason is not None:
        return _refused(
            "value", "supersedes", f"supersedes cannot target {target}: {reason}"
        )
    # A revert: the matching document already names the target as its own
    # successor, so the stamp would close a cycle the gate keeps whole.
    prior = declared_successor(existing.get("metadata")) if existing else None
    if prior is not None:
        return _refused(
            "value",
            "content",
            f"content is identical to {existing_id}, which is itself superseded "
            f"by {prior}",
        )
    return None


def plan_entity_supersession(
    registry: StoreRegistry,
    target: str,
    *,
    content: str | None,
    evidence_ref: str | None,
) -> EntityPlan | Refusal | None:
    """Check ``save_knowledge(supersedes=target)`` before anything is written.

    The successor is the new entity's evidence document, which is not
    written yet, so it is looked up the way ``save_knowledge`` will resolve
    it: ``evidence_ref`` as given, or ``content`` by its content hash.

    Returns ``None`` when ``evidence_ref`` names no document, before any
    check on the target's state, so the tool's existing ``evidence_ref``
    rejection fires once, unchanged, whatever state the target is in.
    """
    node = registry.knowledge.graph_store.get_node(target)
    if node is None:
        return _refused(
            "dangling_reference",
            "supersedes",
            f"supersedes does not reference an existing entity: {target}",
        )
    docs = registry.knowledge.document_store
    if evidence_ref is not None:
        z_doc = docs.get(evidence_ref)
        if z_doc is None:
            return None
    elif content is not None and content.strip():
        z_doc = docs.get_by_hash(content_hash(content))
    else:
        return _refused(
            "missing",
            "content",
            "supersedes requires content or evidence_ref, so the replacement has "
            f"an evidence document to be served in place of {target}",
        )
    properties = node.get("properties") or {}
    if is_archived(properties):
        return _refused(
            "value", "supersedes", f"supersedes cannot target {target}: it is archived"
        )
    z_pre = z_doc["doc_id"] if z_doc is not None else None
    prior = declared_successor(properties)
    if prior is not None and prior != z_pre:
        return _refused(
            "value",
            "supersedes",
            f"supersedes cannot target {target}: it is already superseded by {prior}",
        )
    loc = "evidence_ref" if evidence_ref is not None else "content"
    raw_ref = properties.get(EVIDENCE_REF_KEY)
    t_doc = raw_ref if isinstance(raw_ref, str) and raw_ref else None
    if z_pre is not None and z_pre == t_doc:
        return _refused(
            "value",
            loc,
            f"{loc} resolves to {z_pre}, the evidence document of {target}; an "
            "entity cannot supersede itself",
        )
    z_prior = declared_successor(z_doc.get("metadata")) if z_doc is not None else None
    if z_prior is not None:
        return _refused(
            "value",
            loc,
            f"{loc} resolves to {z_pre}, which is itself superseded by {z_prior}",
        )
    return EntityPlan(t_doc=t_doc, t_doc_skip=_t_doc_skip(docs, t_doc, z_pre))


def _t_doc_skip(
    docs: DocumentStore, t_doc: str | None, successor: str | None
) -> str | None:
    """Why the old entity's evidence document is left unchanged, if it is.

    Leaving it is not a refusal: the entity is what the caller named, and
    its evidence may be shared, chunked, archived or already replaced by
    something else. The tool reports the document it left.
    """
    if t_doc is None:
        return None
    doc = docs.get(t_doc)
    if doc is None:
        return "no such document"
    allowed = {successor} if successor is not None else set()
    return _ineligibility(doc, allowed=allowed)


def supersede_document(
    registry: StoreRegistry, *, doc_id: str, successor: str, requested_by: str
) -> str | None:
    """Stamp document ``doc_id`` superseded by ``successor``.

    Returns ``None`` on success (including a stamp already in place) and an
    error string otherwise; never raises.
    """
    try:
        return _stamp_document(
            registry, doc_id=doc_id, successor=successor, requested_by=requested_by
        )
    except Exception as exc:
        logger.exception(
            "supersession_stamp_failed", target=doc_id, successor=successor
        )
        # The error string can reach an MCP caller verbatim (save_knowledge's
        # ``_raise_if_supersede_failed``, save_memory's two callers) — render
        # it the way ``trellis.mcp.server._exception_detail`` does rather
        # than embedding ``exc`` raw, so a non-Trellis exception (a store
        # driver's, not Trellis's own) can't leak its text (trellis-ai#793
        # follow-up 5).
        return f"{type(exc).__name__}: {render_exception_detail(exc)}"


def _stamp_document(
    registry: StoreRegistry, *, doc_id: str, successor: str, requested_by: str
) -> str | None:
    doc = registry.knowledge.document_store.get(doc_id)
    if doc is None:
        return f"document {doc_id} no longer exists"
    metadata = dict(doc.get("metadata") or {})
    if declared_successor(metadata) == successor:
        return None
    metadata[LIFECYCLE_KEY] = _stamp(successor)
    raw_uri = metadata.get("uri")
    # Metadata-only: the content goes back unchanged, the write clock is
    # kept, and nothing is re-embedded — ``put_document`` mirrors the
    # lifecycle key onto an existing vector row (#338).
    command = build_evidence_ingest_command(
        doc_id=doc_id,
        content=doc["content"],
        metadata=metadata,
        uri=raw_uri if isinstance(raw_uri, str) and raw_uri.strip() else None,
        requested_by=requested_by,
        embed_mode="none",
        preserve_updated_at=True,
        derive_idempotency=False,
    )
    return _execute(build_curate_executor(registry, evidence_embed="none"), command)


def supersede_entity(
    registry: StoreRegistry, *, node_id: str, successor: str, requested_by: str
) -> str | None:
    """Stamp graph node ``node_id`` superseded by ``successor``.

    Returns ``None`` on success (including a stamp already in place) and an
    error string otherwise; never raises.
    """
    try:
        return _stamp_entity(
            registry, node_id=node_id, successor=successor, requested_by=requested_by
        )
    except Exception as exc:
        logger.exception(
            "supersession_stamp_failed", target=node_id, successor=successor
        )
        # See the matching comment in supersede_document above.
        return f"{type(exc).__name__}: {render_exception_detail(exc)}"


def _stamp_entity(
    registry: StoreRegistry, *, node_id: str, successor: str, requested_by: str
) -> str | None:
    node = registry.knowledge.graph_store.get_node(node_id)
    if node is None:
        return f"entity {node_id} no longer exists"
    if declared_successor(node.get("properties")) == successor:
        return None
    command = Command(
        operation=Operation.ENTITY_UPDATE,
        target_id=node_id,
        target_type=node.get("node_type"),
        args={"entity_id": node_id, "properties": {LIFECYCLE_KEY: _stamp(successor)}},
        requested_by=requested_by,
    )
    return _execute(build_curate_executor(registry), command)


def _execute(executor: MutationExecutor, command: Command) -> str | None:
    result = executor.execute(command)
    if result.status != CommandStatus.SUCCESS:
        return f"{result.status.value}: {result.message}"
    return None
