"""File-scoped context retrieval — the server-side half of read-time file context.

Given one or more file paths, surface what the memory system already knows
about those files: documents whose ``metadata.source_path`` names the path,
and graph entities doc-linked (``document_ids``) to those documents. The
client half of #307 (a Claude Code ``PreToolUse`` Read hook with an mtime
staleness gate) consumes this, so every item carries its store timestamps
and each path carries ``newest_item_at`` — the client compares that against
the file's mtime and skips injection when the file changed after everything
known about it was written.

Path matching
-------------
Matching follows how paths are actually stored today; no normalization
scheme is invented for data that does not exist:

* Corpus ingest (:mod:`trellis.ingest_corpus`) stores POSIX relpaths
  relative to the corpus root in ``metadata.source_path`` (the walker
  yields ``path.relative_to(root).as_posix()``); chunk documents repeat
  the parent's value.
* Conversation ingest reuses the same key for conversation *titles*,
  which are free text and can look exactly like a bare filename
  (``server.py`` is a legal chat title).
* ``save_memory`` metadata is caller-supplied; a caller that stamped
  ``source_path`` (relative or absolute) is matched by the same rule.

The rule: a stored value matches a query path when the two are equal, or
when one is a ``/``-boundary suffix of the other **and the suffix itself
spans a directory** — an absolute ``/home/n/vault/notes/foo.md`` finds
the stored relpath ``notes/foo.md``. A single-segment value matches by
equality only. That last clause is the whole reason the rule is not
plain ``endswith``: ``README.md`` / ``CLAUDE.md`` / ``TODO.md`` sit at
the root of the vault *and* of every repo, so a bare basename identifies
a file no better than a coin flip — and those are exactly the files a
read-time hook asks about most. Nothing more: no case folding and no
separator rewriting, because stored values are POSIX already.

Graph anchoring
---------------
The doc→node direction has no per-document reverse index — the query
DSL's ``contains`` operator reaches only ``properties.<key>`` paths —
so linked entities are found by intersecting ``document_ids``
client-side. The scan it intersects over is narrowed store-side to the
population that can possibly match: ``FilterClause(DOC_LINK_FIELD,
"exists")`` asks the backend for doc-linked nodes only, so unlinked
nodes (the bulk of a mature graph) never enter the cap. The cap still
exists, and a saturated scan is reported as ``graph_scan_truncated``
rather than passed off as "nothing linked" — a client can tell "no
entities" from "couldn't look". Gating matches
:class:`~trellis.retrieve.strategies.GraphSearch`: structural nodes and
unconfirmed extraction mints (#301) are excluded unless explicitly
requested.

Cost
----
The document side is a scan: one paged pass over the document store,
because there is no metadata-only listing on the ABC (``search`` needs
an FTS ``MATCH``, so it cannot serve a bare ``source_path`` lookup). The
graph side is one filtered query. Either way a call costs the same
whether it asks about one path or twenty, so **batch the paths**: one
call for the files a hook cares about, not one call per file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.stores.base.graph_query import DOC_LINK_FIELD, FilterClause, NodeQuery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)

#: Page size for the ``list_documents`` scan — same order as the corpus
#: prune scan (``ingest_corpus.sync._PRUNE_PAGE_SIZE``).
_DOC_PAGE_SIZE = 500

#: Cap on the doc-linked node scan backing the doc→node reverse lookup.
#: The scan is filtered to doc-linked nodes store-side, so the cap bites
#: only on a graph carrying more than this many linked nodes — and when
#: it does, the result says so (``graph_scan_truncated``) rather than
#: reporting the shortfall as an absence.
DEFAULT_GRAPH_SCAN_LIMIT = 2000


def source_path_matches(stored: Any, query: str) -> bool:
    """``True`` when a stored ``source_path`` names the queried file path.

    Equality, or a ``/``-boundary suffix match in either direction where
    the suffix spans at least one directory — the shapes that actually
    co-exist: stored relpaths queried by absolute path, and (less
    commonly) stored absolute paths queried by relpath.

    A single-segment value matches by equality only. ``TODO.md`` is a
    real stored ``source_path`` for a vault-root file *and* a real
    basename in every repo on the machine, so honouring it as a suffix
    would answer a read of one project's ``TODO.md`` with another's
    notes.
    """
    if not isinstance(stored, str) or not stored or not query:
        return False
    if stored == query:
        return True
    if "/" in stored and query.endswith("/" + stored):
        return True
    return "/" in query and stored.endswith("/" + query)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a store timestamp (ISO string or datetime); ``None`` if unparseable."""
    if isinstance(value, datetime):
        ts = value
    else:
        if not value:
            return None
        try:
            ts = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _newest_timestamp(items: list[dict[str, Any]]) -> str | None:
    """ISO timestamp of the most recently written item, or ``None``."""
    newest: datetime | None = None
    for item in items:
        ts = _parse_timestamp(item.get("updated_at") or item.get("created_at"))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return newest.isoformat() if newest is not None else None


def _document_entry(doc: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": doc.get("doc_id"),
        "source_path": metadata.get("source_path"),
        "source_system": metadata.get("source_system"),
        "title": metadata.get("title"),
        "excerpt": truncate_excerpt(str(doc.get("content") or "")),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def _entity_entry(node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("properties") or {}
    entry = {
        "entity_id": node.get("node_id"),
        "name": props.get("name"),
        "entity_type": node.get("node_type"),
        "node_role": node.get("node_role") or "semantic",
        "description": truncate_excerpt(str(props.get("description") or "")) or None,
        "document_ids": list(node.get("document_ids") or []),
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
    }
    status = props.get(EXTRACTION_STATUS_PROPERTY)
    if status is not None:
        entry[EXTRACTION_STATUS_PROPERTY] = status
    return entry


def _matching_documents(
    document_store: DocumentStore,
    paths: Sequence[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    """Scan the document store once for ``source_path`` matches.

    Returns per-path document entries (parent rows only — chunk rows
    repeat the parent's content and path) and per-path anchor doc-id sets
    (parents *and* chunks, because an entity's ``document_ids`` link may
    name either).
    """
    # Deferred: ``trellis.ingest_corpus.__init__`` pulls ``sync``, which
    # imports back into ``trellis.retrieve`` (the embed hook). At module
    # level this would make the two packages mutually importing — legal
    # today only because of submodule ordering, and one added line at the
    # other end turns it into a hard cycle.
    from trellis.ingest_corpus.models import is_chunk_doc_id  # noqa: PLC0415

    docs_by_path: dict[str, list[dict[str, Any]]] = {p: [] for p in paths}
    anchors_by_path: dict[str, set[str]] = {p: set() for p in paths}

    offset = 0
    while True:
        page = document_store.list_documents(limit=_DOC_PAGE_SIZE, offset=offset)
        if not page:
            break
        offset += len(page)
        for doc in page:
            metadata = doc.get("metadata") or {}
            stored = metadata.get("source_path")
            for path in paths:
                if not source_path_matches(stored, path):
                    continue
                doc_id = str(doc.get("doc_id") or "")
                anchors_by_path[path].add(doc_id)
                if not is_chunk_doc_id(doc_id):
                    docs_by_path[path].append(_document_entry(doc, metadata))
        if len(page) < _DOC_PAGE_SIZE:
            break

    return docs_by_path, anchors_by_path


def _scan_doc_linked_nodes(graph_store: Any, limit: int) -> list[dict[str, Any]]:
    """Current nodes carrying any doc link, newest first, capped at ``limit``.

    The ``exists`` filter is what keeps the cap meaningful: without it
    the scan spends its budget on nodes that cannot match, and a graph
    with more than ``limit`` nodes of *any* kind answers every file with
    zero entities.
    """
    query = NodeQuery(filters=(FilterClause(DOC_LINK_FIELD, "exists"),), limit=limit)
    try:
        return list(graph_store.execute_node_query(query))
    except NotImplementedError:
        # A backend without a Phase-2 DSL compiler (the ABC's default
        # routing rejects ``exists``). Degrade to the unfiltered scan
        # rather than failing the whole lookup — the saturation check
        # downstream still reports what the caller got.
        logger.debug(
            "file_context_doc_link_filter_unsupported",
            backend=type(graph_store).__name__,
        )
        return list(graph_store.query(node_type=None, properties=None, limit=limit))


def _linked_entities(
    graph_store: Any,
    anchors_by_path: dict[str, set[str]],
    *,
    include_unconfirmed: bool,
    include_structural: bool,
    graph_scan_limit: int,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Nodes whose ``document_ids`` intersect a path's anchor documents.

    Returns the per-path entity lists and whether the node scan hit its
    cap — a saturated scan means the entity half of the answer may be
    incomplete, which the caller reports rather than swallows.
    """
    entities_by_path: dict[str, list[dict[str, Any]]] = {p: [] for p in anchors_by_path}
    all_anchor_ids: set[str] = set().union(*anchors_by_path.values())
    if not all_anchor_ids:
        return entities_by_path, False

    nodes = _scan_doc_linked_nodes(graph_store, graph_scan_limit)
    truncated = len(nodes) >= graph_scan_limit
    if truncated:
        logger.warning(
            "file_context_graph_scan_truncated",
            scanned=len(nodes),
            limit=graph_scan_limit,
        )
    excluded_unconfirmed = 0
    for node in nodes:
        linked = set(node.get("document_ids") or [])
        if not linked & all_anchor_ids:
            continue
        if not include_structural and node.get("node_role") == "structural":
            continue
        if (
            not include_unconfirmed
            and (node.get("properties") or {}).get(EXTRACTION_STATUS_PROPERTY)
            == EXTRACTION_STATUS_UNCONFIRMED
        ):
            excluded_unconfirmed += 1
            continue
        entry = _entity_entry(node)
        for path, anchor_ids in anchors_by_path.items():
            if linked & anchor_ids:
                entities_by_path[path].append(entry)
    if excluded_unconfirmed:
        logger.debug("file_context_unconfirmed_excluded", excluded=excluded_unconfirmed)
    return entities_by_path, truncated


def build_file_context(
    document_store: DocumentStore,
    graph_store: Any,
    paths: Sequence[str],
    *,
    include_unconfirmed: bool = False,
    include_structural: bool = False,
    graph_scan_limit: int = DEFAULT_GRAPH_SCAN_LIMIT,
) -> dict[str, Any]:
    """Assemble stored context for one or more file paths.

    Args:
        document_store: Knowledge-plane document store.
        graph_store: Knowledge-plane graph store (open ABC, hence ``Any``
            — same convention as :class:`GraphSearch`).
        paths: File paths to look up. Matched against stored
            ``metadata.source_path`` values as described in the module
            docstring; duplicates are collapsed, order is preserved.
        include_unconfirmed: Surface unconfirmed extraction mints (#301).
            Off by default — extraction attests *mention*, not fact.
        include_structural: Surface structural plumbing nodes. Off by
            default, mirroring :class:`GraphSearch`.
        graph_scan_limit: Cap on the doc-linked node scan backing the
            doc→node reverse lookup.

    Returns:
        ``{"paths": [{"path", "documents", "entities", "newest_item_at"}],
        "graph_scan_truncated": bool}`` — one path entry per queried
        path, in query order. ``newest_item_at`` is the ISO timestamp of
        the most recently written matching item (document or entity), or
        ``None`` when nothing matched; it is the value a client staleness
        gate compares against the file's mtime. ``graph_scan_truncated``
        is ``True`` when the graph carries more doc-linked nodes than the
        scan cap, i.e. the entity lists may be short.
    """
    deduped: list[str] = []
    for raw in paths:
        path = raw.strip()
        if path and path not in deduped:
            deduped.append(path)
    if not deduped:
        return {"paths": [], "graph_scan_truncated": False}

    docs_by_path, anchors_by_path = _matching_documents(document_store, deduped)
    entities_by_path, graph_scan_truncated = _linked_entities(
        graph_store,
        anchors_by_path,
        include_unconfirmed=include_unconfirmed,
        include_structural=include_structural,
        graph_scan_limit=graph_scan_limit,
    )

    results = []
    for path in deduped:
        documents = docs_by_path[path]
        entities = entities_by_path[path]
        results.append(
            {
                "path": path,
                "documents": documents,
                "entities": entities,
                "newest_item_at": _newest_timestamp(documents + entities),
            }
        )
    return {"paths": results, "graph_scan_truncated": graph_scan_truncated}


__all__ = [
    "DEFAULT_GRAPH_SCAN_LIMIT",
    "build_file_context",
    "source_path_matches",
]
