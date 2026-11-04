"""The ``redaction.apply`` handler and the precedent scrub it drives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from trellis.core.document_write import (
    REDACTION_TOMBSTONE_REASON,
    is_redaction_tombstone,
    put_document,
)
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import NotFoundError, StoreError, ValidationError
from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.mutate.commands import Command
from trellis.mutate.handlers._common import _LINKED_SIGNAL_LIMIT
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.schemas.well_known import MEASUREMENT, OBSERVATION, normalize_entity_name
from trellis.stores.base.event_log import EventType
from trellis.stores.base.graph import GraphStore
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


def _rewrite_precedent_document(
    registry: StoreRegistry, precedent_id: str, properties: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Re-derive a precedent's existing document row from its scrubbed properties.

    Keeps the row's other metadata (a noise tag, a lifecycle stamp) and
    replaces its text and title with what the node now says. ``None`` when
    the precedent has no document row, or its row is already a redaction
    tombstone (rewriting it would refuse, after the source rows were blanked);
    nothing is created.
    """
    from trellis.learning.scoring import (  # noqa: PLC0415
        precedent_document_content,
    )

    document_store = registry.knowledge.document_store
    document = document_store.get(precedent_id)
    if not document or is_redaction_tombstone(document):
        return None
    content = precedent_document_content(
        properties.get("name"), properties.get("description")
    )
    metadata = dict(document.get("metadata") or {})
    name = properties.get("name")
    if isinstance(name, str) and name.strip():
        metadata["title"] = name.strip()
    put_document(
        document_store,
        resolve_vector_store(registry),
        precedent_id,
        content,
        metadata,
        preserve_updated_at=False,
    )
    return content, metadata


def _tombstone_document(
    registry: StoreRegistry, doc_id: str, *, vector_gone: set[str]
) -> bool:
    """Blank one document row and drop its vector row; whether a row existed.

    The document plane has no governed delete, so the row keeps its id with no
    text, no title and an archived stamp. Every other metadata key is dropped
    (a title, a tag, a source excerpt can all hold the redacted text); only
    ``document_form`` (a class label) and ``chunk_count`` (the bookkeeping a
    re-run needs to find the chunks again) survive. Nothing is created for an
    id that never had a row.

    A content write (``preserve_updated_at=False``) because the row's content
    did change. What withholds the blanked row from readers is the archived
    lifecycle, not the clock. The restamp has a cost: the age gate of
    retention's prune reads ``updated_at``, so the tombstone is pruned later
    than its original row would have been. The vector row goes first, so the
    metadata mirror in ``put_document`` has nothing left to sync.

    ``vector_gone`` names the row between its vector delete and the end of its
    ``put``: if the write raises, the row is unblanked but no longer in vector
    search, and the id is left in the set for the caller's failure log.
    """
    document_store = registry.knowledge.document_store
    document = document_store.get(doc_id)
    if not document:
        return False
    old = document.get("metadata") or {}
    lifecycle = Lifecycle(
        state="archived",
        valid_until=datetime.now(UTC),
        deprecation_reason=REDACTION_TOMBSTONE_REASON,
    ).model_dump(mode="json")
    metadata: dict[str, Any] = {LIFECYCLE_KEY: lifecycle}
    if isinstance(old.get("document_form"), str):
        metadata["document_form"] = old["document_form"]
    if isinstance(old.get("chunk_count"), int):
        metadata["chunk_count"] = old["chunk_count"]
    registry.knowledge.vector_store.delete(doc_id)
    vector_gone.add(doc_id)
    put_document(
        document_store,
        resolve_vector_store(registry),
        doc_id,
        "",
        metadata,
        preserve_updated_at=False,
    )
    vector_gone.discard(doc_id)
    return True


def _tombstone_redacted_documents(
    registry: StoreRegistry,
    command: Command,
    target_id: str,
    *,
    vector_deleted: bool,
) -> list[str]:
    """Blank the target's own document row and its chunks; the ids blanked.

    Redaction covers the Document Plane for the document the target *is*: a
    graph node whose id is a document id (memory extraction mints one per
    document, and a promoted precedent has one). Documents a node merely links
    through ``document_ids`` are not touched, since one may back many
    entities. The target's ``<target>#chunk-N`` fragments go with it, found
    from its ``chunk_count`` and then probed past it until an id is missing,
    because chunks are separate rows that serve the same text. That holds
    whatever the target's own id looks like: a document named ``x#chunk-5``
    has chunks of its own that nothing in its id gives away (#1008), and a
    minted chunk, having none, costs a few absent-row reads.

    Chunks go first and the parent last: the parent's ``chunk_count`` is how a
    re-run after a partial failure finds the chunks, so it must outlive them.
    Nothing is purged until this has finished, so a failure leaves the target
    in place and the redaction re-runnable; the rows blanked before it are
    logged (:func:`_log_blanked_before_failure`), with ``vector_deleted``,
    the target's own vector row the caller already removed. A row whose write
    fails after its vector delete is in neither list of blanked rows, so the
    log names it apart (``documents_vector_deleted_not_blanked``).
    """
    # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
    from trellis.ingest_corpus.models import chunk_doc_id  # noqa: PLC0415

    document_store = registry.knowledge.document_store
    tombstoned: list[str] = []
    vector_gone: set[str] = set()
    try:
        parent = document_store.get(target_id)
        declared = ((parent or {}).get("metadata") or {}).get("chunk_count")
        index = 0
        while True:
            chunk_id = chunk_doc_id(target_id, index)
            exists = _tombstone_document(registry, chunk_id, vector_gone=vector_gone)
            if exists:
                tombstoned.append(chunk_id)
            elif not isinstance(declared, int) or index >= declared:
                break
            index += 1
        if _tombstone_document(registry, target_id, vector_gone=vector_gone):
            tombstoned.append(target_id)
    except Exception:
        _log_blanked_before_failure(
            command,
            tombstoned,
            vector_deleted=vector_deleted,
            vector_gone=vector_gone,
        )
        raise
    return tombstoned


def _log_blanked_before_failure(
    command: Command,
    blanked: list[str],
    *,
    vector_deleted: bool,
    vector_gone: set[str] | None = None,
) -> None:
    """Record document rows and the target vector a failed redaction removed.

    A blanked row stays, so a re-run lists it again; this is for the operator
    reading the failure, who otherwise cannot tell which rows already stopped
    serving their text. The target's vector row does not stay: a re-run's
    ``REDACTION_APPLIED`` reads ``vector_deleted: false``, so this line is the
    only record that this run deleted it. ``vector_gone`` is the row whose
    write failed after its vector was deleted: it is not blanked, so it is not
    in ``blanked``, yet it already left vector search.
    """
    if blanked or vector_deleted or vector_gone:
        logger.warning(
            "redaction.documents_blanked_before_failure",
            command_id=command.command_id,
            documents_blanked=sorted(blanked),
            documents_vector_deleted_not_blanked=sorted(vector_gone or ()),
            target_vector_deleted=vector_deleted,
        )


#: Upper bound on ``redaction.apply``'s ``reason`` arg. The reason is
#: written verbatim into the append-only audit log — the one payload
#: field that *could* re-contain the redacted content — so it is kept
#: short and rejected loudly rather than silently truncated.
MAX_REDACTION_REASON_CHARS = 2000


#: Page size of the precedent walk below. Not a cap: the walk continues
#: until it has read every precedent.
_PRECEDENT_PAGE_SIZE = 500


def _precedents_sourced_from(graph: GraphStore, target_id: str) -> list[dict[str, Any]]:
    """Current precedent nodes that copied text from ``target_id``.

    The graph API has no index on ``source_item_id`` and its property filter
    is equality only, which cannot express "a chunk of this document", so
    this reads every precedent-typed node, page by page through
    :meth:`~trellis.stores.base.graph.GraphStore.search_nodes` (the one node
    read every backend pages, with a stable order), and filters here. The
    walk finishes before anything is scrubbed, so a scrub's re-created node
    cannot move under it. A read that raises propagates before anything is
    purged: a partial scan never reaches success.

    A precedent's ``source_item_id`` names a document or one of its
    ``<parent>#chunk-N`` fragments. A target matches itself and its chunks
    (the chunk id must start with ``<target>#chunk-``, so a sibling document
    sharing a prefix does not match), whatever its own id looks like: a
    document named ``x#chunk-5`` has chunks ``x#chunk-5#chunk-N`` (#1008), and
    a minted chunk, having none, matches only itself. Returns the matching
    rows.
    """
    # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
    from trellis.ingest_corpus.models import CHUNK_ID_SEPARATOR  # noqa: PLC0415

    chunk_prefix = f"{target_id}{CHUNK_ID_SEPARATOR}"
    matched: list[dict[str, Any]] = []
    offset = 0
    first_total: int | None = None
    while True:
        rows, total = graph.search_nodes(
            node_type="precedent",
            sort="node_type",
            descending=False,
            limit=_PRECEDENT_PAGE_SIZE,
            offset=offset,
        )
        # Offset paging skips a row when an earlier one disappears between
        # pages, so a set that changed size mid-scan refuses rather than
        # risk leaving an excerpt behind. Nothing has been purged yet.
        if first_total is None:
            first_total = total
        elif total != first_total:
            msg = (
                "precedent set changed during the redaction scan "
                f"({first_total} -> {total} nodes); re-run the redaction"
            )
            raise StoreError(msg, store="graph")
        for row in rows:
            source = (row.get("properties") or {}).get("source_item_id")
            if not isinstance(source, str) or not source:
                continue
            if source == target_id or source.startswith(chunk_prefix):
                matched.append(row)
        offset += len(rows)
        if not rows or offset >= total:
            return matched


def _reembed_scrubbed_precedent(
    registry: StoreRegistry,
    precedent_id: str,
    scrubbed_document: tuple[str, dict[str, Any]] | None,
    *,
    vector_removed: bool,
) -> bool:
    """Re-embed a scrubbed precedent's document; whether its vector row is lost.

    Lost means a row existed and was deleted and nothing wrote one back:
    embed-on-ingest is off, no embedder resolves, the embed failed, or the
    precedent has no document row to embed. It is logged here and named in
    the redaction event, so the precedent's absence from semantic search is
    a record.
    """
    reembedded = False
    if scrubbed_document is not None:
        from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
            run_embed_on_ingest,
        )

        outcome = run_embed_on_ingest(
            registry,
            precedent_id,
            scrubbed_document[0],
            scrubbed_document[1],
            source="redaction.apply",
        )
        reembedded = bool(outcome and outcome.get("embedded"))
    lost = vector_removed and not reembedded
    if lost:
        logger.warning(
            "redaction.precedent_vector_not_reembedded", precedent_id=precedent_id
        )
    return lost


@dataclass(frozen=True)
class _PrecedentScrub:
    """What one scrub did that the redaction event has to say."""

    precedent_id: str
    #: A vector row existed, was deleted, and was not written back (embed-on-
    #: ingest off, no embedder, or the embed failed): the precedent is
    #: unreachable by semantic search until something re-embeds it.
    vector_lost: bool


def _binds_a_name_in(alias: dict[str, Any], name_keys: set[str]) -> bool:
    """Whether ``alias`` is a ``name`` alias bound for one of ``name_keys``."""
    if alias["source_system"] != NAME_ALIAS_SOURCE_SYSTEM:
        return False
    raw_name = alias.get("raw_name")
    return alias["raw_id"] in name_keys or (
        isinstance(raw_name, str) and normalize_entity_name(raw_name) in name_keys
    )


def _scrub_precedent_excerpt(
    registry: StoreRegistry, row: dict[str, Any]
) -> _PrecedentScrub | None:
    """Remove a precedent's copied source text from every version it has.

    The text is the excerpt in its description and, when the name was derived
    from the source's first line (``name_source``, see
    :func:`~trellis.learning.scoring.name_may_hold_source_text`), the name:
    that becomes :func:`~trellis.learning.scoring.neutral_precedent_name`,
    marked ``neutral``. The source's own title counts too (``item_title``):
    the redaction blanks the document row's title (:func:`_tombstone_document`),
    so a precedent name copied from it is the same text. A name that came from
    a reviewer or is the bare id stays.

    Matches how ``redaction.apply`` treats its own target: a purge, not an
    SCD-2 close. A new version would leave the excerpt readable through
    ``get_node_history`` and ``as_of``, and the graph API cannot rewrite a
    closed version, so the node is purged and re-created from its current
    version with the excerpt removed and ``source_excerpt_status`` set to
    :data:`~trellis.learning.scoring.SOURCE_EXCERPT_REDACTED`. Everything
    else on the current version survives: properties, role, document links,
    live edges (provenance included) and aliases. What does not survive is
    the precedent's own earlier versions (which is the point), the ids and
    timestamps of its edge rows, and any ``name`` alias bound for a
    source-derived name, which holds that text as its key. The neutral name
    is not bound in its place.

    Returns ``None`` when no version carries an excerpt or a source-derived
    name (nothing to scrub, and the history is left alone). The precedent's
    document row, when it has one, is rewritten from the scrubbed name and
    description before anything is purged (a failure there leaves the
    redaction re-runnable) and re-embedded after the node is back. The vector
    row is deleted first: it was embedded from the description. When no
    embedder writes it back, ``vector_lost`` says so and the redaction event
    names the id, so the loss is a record rather than a silence. If the
    re-create fails after the purge, the precedent is gone, never
    half-redacted, and the error names its id.

    **``valid_from`` is not carried forward.** The graph API has no way to set
    it: ``upsert_node`` stamps now, and the only way to drop the old versions
    (which is the point) is a purge. The re-created node therefore reads as
    new, and a recency window can rank it as such. Every other field the old
    version held, ``created_at`` aside, is restored.
    """
    from trellis.learning.scoring import (  # noqa: PLC0415
        NAME_SOURCE_NEUTRAL,
        SOURCE_EXCERPT_REDACTED,
        carries_source_excerpt,
        name_may_hold_source_text,
        neutral_precedent_name,
        strip_source_excerpt,
    )

    graph = registry.knowledge.graph_store
    precedent_id = row["node_id"]
    versions = graph.get_node_history(precedent_id) or [row]
    version_props = [v.get("properties") or {} for v in versions]
    has_excerpt = any(carries_source_excerpt(p) for p in version_props)
    has_source_name = any(
        name_may_hold_source_text(p, precedent_id) for p in version_props
    )
    if not (has_excerpt or has_source_name):
        return None

    properties = dict(row.get("properties") or {})
    if has_excerpt:
        description = properties.get("description")
        if isinstance(description, str):
            properties["description"] = strip_source_excerpt(
                description, str(properties.get("source_item_id") or "")
            )
        properties["source_excerpt_status"] = SOURCE_EXCERPT_REDACTED
    if name_may_hold_source_text(properties, precedent_id):
        properties["name"] = neutral_precedent_name(
            precedent_id, properties.get("intent_family")
        )
        properties["name_source"] = NAME_SOURCE_NEUTRAL

    edges = {e["edge_id"]: e for e in graph.get_edges(precedent_id, direction="both")}
    # A ``name`` alias is bound for the normalized name, so one bound for a
    # source-derived name holds that text in raw_id and raw_name. A rename
    # leaves the old alias, so the names of every version count, not only the
    # current one. Such an alias is dropped rather than restored.
    source_name_keys = {
        normalize_entity_name(p["name"])
        for p in version_props
        if isinstance(p.get("name"), str) and name_may_hold_source_text(p, precedent_id)
    } - {""}
    aliases = [
        a
        for a in graph.get_aliases(precedent_id)
        if not _binds_a_name_in(a, source_name_keys)
    ]

    scrubbed_document = _rewrite_precedent_document(registry, precedent_id, properties)
    vector_removed = registry.knowledge.vector_store.delete(precedent_id)
    if not graph.delete_node(precedent_id):
        return None  # a concurrent purge already removed it
    try:
        graph.upsert_node(
            precedent_id,
            row["node_type"],
            properties,
            node_role=row.get("node_role") or "semantic",
            generation_spec=row.get("generation_spec"),
            document_ids=row.get("document_ids") or None,
        )
    except Exception as exc:
        # The listing withholds the promoted excerpt for a precedent with no
        # node (see ``trellis.retrieve.precedents``), so the copy in
        # ``PRECEDENT_PROMOTED`` stays unserved; what is lost is the node and
        # its links, which the log names so they can be rebuilt.
        logger.warning(
            "redaction.precedent_recreate_failed",
            precedent_id=precedent_id,
            edge_ids=sorted(edges),
            alias_count=len(aliases),
        )
        msg = (
            f"precedent {precedent_id} was purged to scrub its source excerpt "
            f"but could not be re-created ({type(exc).__name__}); its node, "
            f"{len(edges)} edge(s) and {len(aliases)} alias(es) are gone and it "
            "must be re-promoted"
        )
        raise StoreError(msg, store="graph") from exc

    vector_lost = _reembed_scrubbed_precedent(
        registry, precedent_id, scrubbed_document, vector_removed=bool(vector_removed)
    )

    # Each link is restored on its own, so one failure does not strand the
    # rest. A re-run cannot finish the job (no version carries an excerpt
    # any more, so the precedent no longer matches), which is why every
    # unrestored link is named rather than summarised.
    unrestored_edges: list[str] = []
    for edge in edges.values():
        try:
            graph.upsert_edge(
                edge["source_id"],
                edge["target_id"],
                edge["edge_type"],
                edge.get("properties"),
                source_trace_id=edge.get("source_trace_id"),
                agent_id=edge.get("agent_id"),
                confidence=edge.get("confidence"),
                evidence_ref=edge.get("evidence_ref"),
                extractor_tier=edge.get("extractor_tier"),
            )
        except Exception:
            unrestored_edges.append(
                f"{edge['source_id']} -{edge['edge_type']}-> {edge['target_id']}"
            )
    unrestored_aliases: list[str] = []
    for alias in aliases:
        try:
            graph.upsert_alias(
                precedent_id,
                alias["source_system"],
                alias["raw_id"],
                raw_name=alias.get("raw_name"),
                match_confidence=alias.get("match_confidence", 1.0),
                is_primary=bool(alias.get("is_primary", False)),
            )
        except Exception:
            unrestored_aliases.append(f"{alias['source_system']}:{alias['raw_id']}")
    if unrestored_edges or unrestored_aliases:
        logger.warning(
            "redaction.precedent_links_not_restored",
            precedent_id=precedent_id,
            edges=unrestored_edges,
            aliases=unrestored_aliases,
        )
        msg = (
            f"precedent {precedent_id} was scrubbed and re-created, but "
            f"{len(unrestored_edges)} edge(s) and {len(unrestored_aliases)} "
            "alias(es) could not be restored: "
            f"{'; '.join(unrestored_edges + unrestored_aliases)}"
        )
        raise StoreError(msg, store="graph")
    return _PrecedentScrub(precedent_id, vector_lost=vector_lost)


def _scrub_precedents(
    registry: StoreRegistry, command: Command, rows: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Scrub each precedent in ``rows``.

    Returns the sorted ids scrubbed, and the sorted subset whose vector row
    was deleted and not re-embedded.
    """
    scrubbed: list[str] = []
    vector_lost: list[str] = []
    try:
        for row in rows:
            done = _scrub_precedent_excerpt(registry, row)
            if done is not None:
                scrubbed.append(done.precedent_id)
                if done.vector_lost:
                    vector_lost.append(done.precedent_id)
    except Exception:
        # A scrub purges history irreversibly and REDACTION_APPLIED is never
        # emitted on this path, so the earlier scrubs are recorded here or
        # nowhere.
        _log_scrubbed_before_failure(command, scrubbed)
        raise
    return sorted(scrubbed), sorted(vector_lost)


def _log_scrubbed_before_failure(command: Command, scrubbed: list[str]) -> None:
    """Record precedents a failed redaction had already scrubbed."""
    if scrubbed:
        logger.warning(
            "redaction.precedents_scrubbed_before_failure",
            command_id=command.command_id,
            scrubbed_precedent_ids=sorted(scrubbed),
        )


def _scrub_then_purge(
    registry: StoreRegistry,
    command: Command,
    target_id: str,
    blanked: list[str],
    *,
    vector_deleted: bool,
) -> tuple[list[str], list[str]]:
    """Scrub the precedents sourced from ``target_id``, then purge it.

    Returns what :func:`_scrub_precedents` does. Precedents that copied the
    document's text are scrubbed *before* the target is purged: a failure here
    leaves the target in place, so the redaction can simply be re-run (a
    scrubbed precedent no longer matches), where the other order would strand a
    scrub that has nothing left to be triggered by. No precedent sourced from
    the target is a no-op.

    No ``REDACTION_APPLIED`` follows a failure, so what this run had already
    done is recorded here or nowhere: the scrubs, and the document rows
    ``blanked`` before it. The scrubs a failing :func:`_scrub_precedents`
    completed are logged by that function.
    """
    graph = registry.knowledge.graph_store
    scrubbed: list[str] = []
    try:
        precedent_rows = _precedents_sourced_from(graph, target_id)
        scrubbed, vector_lost = _scrub_precedents(registry, command, precedent_rows)
        purged = graph.delete_node(target_id)
    except Exception:
        _log_scrubbed_before_failure(command, scrubbed)
        _log_blanked_before_failure(command, blanked, vector_deleted=vector_deleted)
        raise
    if not purged:
        # Lost a race: another writer purged the node between our read and
        # the delete. Fail rather than emit a second REDACTION_APPLIED
        # carrying counts this command did not purge.
        _log_scrubbed_before_failure(command, scrubbed)
        _log_blanked_before_failure(command, blanked, vector_deleted=vector_deleted)
        raise NotFoundError(entity_type="entity", entity_id=target_id)
    return scrubbed, vector_lost


class RedactionApplyHandler:
    """Hard-purge a graph entity, emitting ``REDACTION_APPLIED``.

    Wires :data:`Operation.REDACTION_APPLY` into the governed pipeline. The
    enum verb shipped with no handler (the same gap ``entity.update`` had
    before :class:`EntityUpdateHandler`), so the executor rejected every
    ``redaction.apply`` command with "No handler registered" — defect
    cleanups had to fall back to *neutralizing* entities via
    ``entity.update`` because no governed deletion path existed.

    **Redaction is a purge, not an SCD-2 close.** ``delete_node`` physically
    removes *all* version rows, cascades to every edge version touching the
    node, and drops its alias rows (pinned by the graph-store contract
    tests). After redaction the entity is unreachable through ``get_node``,
    ``as_of`` time-travel, and ``get_node_history`` — a redaction that
    time-travel can resurrect would not be a redaction. The vector entry is
    deleted *before* the graph purge (``item_id == node_id`` is the
    shape-#2 contract on the bolt backends; the standalone stores key
    vectors by document id, so the delete is a recorded no-op there): a
    vector-backend failure therefore aborts before anything irreversible
    happens. The reverse failure — vector gone, graph purge raises — leaves
    the entity intact minus its re-derivable embedding, never the reverse.

    **The EventLog is the audit trail, and it never re-contains content.**
    The ``REDACTION_APPLIED`` payload carries the ``reason``, counts, and id
    pointers only — no name, no properties (see
    :attr:`~trellis.stores.base.event_log.EventType.REDACTION_APPLIED` for
    the schema). Because that event is the only record that survives the
    purge, the handler refuses to run against a ``NullEventLog``
    (``code="redaction_requires_event_log"``), and the emit itself is
    guarded: if it fails after the purge, the content-free payload is
    preserved in operator logs rather than reporting FAILED for a redaction
    that already happened. Scope is the Knowledge Plane: prior
    Operational-Plane events (e.g. the original ``ENTITY_CREATED`` payload)
    and traces are immutable by design and are not rewritten.

    **Precedents that copied the target's text are scrubbed (#927).** A
    promoted precedent's description opens with up to 600 chars of its
    source document (``source_item_id``), and a precedent with no readable
    name takes up to 96 chars of the source's first line as its name. When
    the target is that document or a ``<document>#chunk-N`` fragment of it,
    the excerpt is removed from the precedent (every version, see
    :func:`_scrub_precedent_excerpt`), ``source_excerpt_status`` becomes
    ``source_redacted``, a source-derived name becomes a neutral one, and
    the rest of the precedent stays. The lookup reads every precedent, page
    by page, so no precedent is missed; a read that fails refuses the
    redaction before anything is purged. The ids ride the payload as
    ``scrubbed_precedent_ids``. Only a *graph node* can be a target, so
    this runs for a document that has one (memory extraction mints a
    node whose id is the document id); a document with no node is refused
    like any other missing target. The ``PRECEDENT_PROMOTED`` event keeps
    its copy of the description and title by design — the log is immutable —
    so ``list_precedents`` serves the node's text, or a neutral title, for a
    scrubbed precedent instead.

    **The target's own document row is blanked, not deleted (#935).** A node
    whose id is a document id (memory extraction, a promoted precedent) keeps
    that document id, with the text, title and every other metadata key
    emptied and an archived stamp (:func:`_tombstone_document`), since the
    document plane has no governed delete. A parent's ``<parent>#chunk-N``
    fragments go with it, and each blanked row's vector row is deleted. Linked
    documents (``document_ids``) are not touched. The ids blanked ride the
    payload as ``documents_tombstoned``; a precedent whose vector row was
    deleted and not re-embedded rides it as ``precedent_vectors_to_reembed``.

    **Stage 2 sees the precedents the scrub rewrites.** The executor asks
    :meth:`policy_scope_commands` and puts one ``precedent`` command per
    candidate to the gate (see ``MutationExecutor``).

    **Scope: graph entities only; linked records become pointers.**
    Documents linked from any purged version are *not* cascaded — a
    document may back many entities — and their union across versions rides
    the payload so a future document-level redaction can locate them.
    Observations and Measurements *about* the subject are independent
    governed nodes and are likewise not cascaded, but they carry
    ``subject_entity_id`` as a property, so property-based queries keep
    serving them after the purge; their ids ride the payload so the
    operator can redact each one individually (they are graph nodes — this
    same verb applies). A ``target_id`` that is not a graph node is refused
    (``code="target_not_found"`` → ``CommandStatus.REJECTED``).

    Idempotency: re-redacting a purged id is refused the same way, and a
    concurrent-purge race is detected via ``delete_node``'s return value:
    the loser raises :class:`~trellis.errors.NotFoundError`
    (→ ``CommandStatus.FAILED``) and emits no second
    ``REDACTION_APPLIED``. Both succeed, each emitting one, only when
    ``delete_node`` returns ``True`` to both (see ``GraphStore.delete_node``).
    Use ``Command.idempotency_key`` when at-most-once semantics are
    required. A blank ``reason`` is rejected
    (``code="redaction_reason_required"``) and an over-long one too
    (``code="redaction_reason_too_long"``) — the recorded justification is
    the point of governed redaction, and it must stay short and
    content-free.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def policy_scope_commands(self, command: Command) -> list[Command]:
        """One command per precedent the scrub may rewrite, for Stage 2.

        The gate matches on a command's ``target_type`` and metadata, so a
        policy scoped to ``precedent`` never saw a redaction aimed at a
        document, though the scrub rewrites precedents. The executor puts each
        command returned here to the gate before :meth:`handle` runs. A
        superset of what the scrub rewrites (every precedent sourced from the
        target, not only those still carrying an excerpt), which errs toward
        refusing. A target that is no node returns nothing: :meth:`handle`
        refuses it. A graph read that raises propagates and the executor
        refuses the command.

        The precedent set is read again in :meth:`handle`; one promoted from
        the target in between is scrubbed without having been gated.
        """
        target_id = command.args.get("target_id")
        graph = self._registry.knowledge.graph_store
        if not isinstance(target_id, str) or graph.get_node(target_id) is None:
            return []
        # A copy of the command re-aimed at each precedent: the gate reads its
        # operation, ``target_type`` and metadata, and nothing runs from it.
        return [
            command.model_copy(
                update={
                    "target_id": row["node_id"],
                    "target_type": row["node_type"],
                    "args": {
                        "target_id": row["node_id"],
                        "reason": command.args.get("reason"),
                    },
                    "metadata": dict(command.metadata),
                }
            )
            for row in _precedents_sourced_from(graph, target_id)
        ]

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "redaction.apply requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="redaction_reason_required")
        if len(reason) > MAX_REDACTION_REASON_CHARS:
            msg = (
                f"redaction.apply reason exceeds {MAX_REDACTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="redaction_reason_too_long")

        event_log = self._registry.operational.event_log
        if isinstance(event_log, NullEventLog):
            msg = (
                "redaction.apply requires a persisting event log: the "
                "REDACTION_APPLIED event is the only record that survives "
                "the purge, and this deployment's event_log backend is 'null'"
            )
            raise ValidationError(msg, code="redaction_requires_event_log")

        graph = self._registry.knowledge.graph_store
        node = graph.get_node(target_id)
        if node is None:
            # Input to fix, not a store failure: refused like a label on a
            # missing node (REJECTED, ``code`` the audit reason, CLI exit 2).
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        entity_type = node["node_type"]
        history = graph.get_node_history(target_id)
        node_versions = len(history)
        # Union across ALL versions: the purge takes every version's rows
        # with it, and the payload is the only place the graph→document
        # pointers survive — the current version alone under-reports when
        # a later version replaced the link.
        document_ids = sorted(
            {d for version in history for d in version.get("document_ids") or []}
        )
        edges = len(graph.get_edges(target_id, direction="both"))
        aliases = len(graph.get_aliases(target_id))
        linked_observation_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=OBSERVATION,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]
        linked_measurement_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=MEASUREMENT,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]

        # Vector entry first: if the vector backend raises, the command
        # fails with the graph untouched — nothing irreversible has
        # happened yet. It also precedes the tombstone below, which deletes
        # the same row, so ``vector_deleted`` reports it.
        vector_deleted = self._registry.knowledge.vector_store.delete(target_id)
        # The target's own document row (and its chunks) serves the same text
        # as the node, through keyword search and ``get_items``. It runs ahead
        # of the precedent scrub because a document write can fail and a scrub
        # cannot be undone: the other order purges a precedent's history, then
        # fails with no event naming it. It reads no graph state.
        documents_tombstoned = _tombstone_redacted_documents(
            self._registry, command, target_id, vector_deleted=vector_deleted
        )

        scrubbed_precedent_ids, vector_lost_ids = _scrub_then_purge(
            self._registry,
            command,
            target_id,
            documents_tombstoned,
            vector_deleted=vector_deleted,
        )

        # ``target_id`` can carry the exact hazard this handler exists to
        # purge: ``redaction.apply`` is deliberately absent from
        # ID_BEARING_ARG_KEYS (see OperationRegistry.validate in
        # mutate/commands.py), so a raw NUL byte or lone surrogate reaches
        # here unscrubbed, and REDACTION_APPLIED is "the only record that
        # survives the purge" (class docstring above). ``EventLog.append``
        # scrubs ``entity_id`` and ``payload`` on every ``emit()`` call
        # before any backend sees them, so this record lands on Postgres
        # the same as any other -- no local scrub needed here.
        payload: dict[str, Any] = {
            "target_id": target_id,
            "target_kind": "entity",
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "node_versions_purged": node_versions,
            "edges_purged": edges,
            "aliases_purged": aliases,
            "vector_deleted": vector_deleted,
            "document_ids": document_ids,
            "linked_observation_ids": linked_observation_ids,
            "linked_measurement_ids": linked_measurement_ids,
        }
        message = (
            f"Entity redacted: {target_id} "
            f"({node_versions} version(s), {edges} edge(s), "
            f"{aliases} alias(es), vector_deleted={vector_deleted})"
        )
        # Keys only when there is something to say, so a redaction that
        # reaches no precedent emits the payload it always did.
        if documents_tombstoned:
            payload["documents_tombstoned"] = documents_tombstoned
            message += f"; {len(documents_tombstoned)} document row(s) blanked"
        if vector_lost_ids:
            # Their vector rows were deleted with the excerpt and nothing
            # re-embedded them; the ids are what a later re-embed needs.
            payload["precedent_vectors_to_reembed"] = vector_lost_ids
            message += (
                f"; {len(vector_lost_ids)} precedent vector row(s) deleted and "
                "not re-embedded"
            )
        if scrubbed_precedent_ids:
            payload["scrubbed_precedent_ids"] = scrubbed_precedent_ids
            payload["scrubbed_precedent_count"] = len(scrubbed_precedent_ids)
            message += (
                f"; source excerpt scrubbed from {len(scrubbed_precedent_ids)} "
                "precedent(s)"
            )
        try:
            event_log.emit(
                EventType.REDACTION_APPLIED,
                source="mutation_executor",
                entity_id=target_id,
                entity_type=entity_type,
                payload=payload,
            )
        except Exception:
            # Same discipline as ops/write_health.py: the purge already
            # happened, so raising would report FAILED for a completed
            # redaction and (via the executor's own emit against the same
            # log) lose the record entirely. Preserve the content-free
            # payload in operator logs instead.
            logger.warning(
                "redaction_audit_emit_failed",
                target_id=target_id,
                entity_type=entity_type,
                redaction_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: REDACTION_APPLIED audit emit failed; "
                "payload preserved in operator logs)"
            )
        return target_id, message
