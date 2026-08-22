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
* Conversation ingest reuses the same key for conversation *titles* —
  those simply never match a file path.
* ``save_memory`` metadata is caller-supplied; a caller that stamped
  ``source_path`` (relative or absolute) is matched by the same rule.

The rule: a stored value matches a query path when the two are equal or
when one is a ``/``-boundary suffix of the other — an absolute
``/home/n/vault/notes/foo.md`` finds the stored relpath ``notes/foo.md``
and vice versa. Nothing more: no case folding and no separator rewriting,
because stored values are POSIX already.

Graph anchoring
---------------
The doc→node direction has no store-side reverse index — the query DSL's
``contains`` operator reaches only ``properties.<key>`` paths, and
``document_ids`` is a top-level column — so linked entities are found by
scanning current nodes (capped at ``graph_scan_limit``) and intersecting
``document_ids`` client-side. That is the same over-fetch compromise as
:class:`~trellis.retrieve.strategies.GraphSearch`, with the same
client-side gating: structural nodes and unconfirmed extraction mints
(#301) are excluded unless explicitly requested.

Cost
----
Both lookups are scans — one paged pass over the document store (there is
no metadata-only listing on the ABC; ``search`` needs an FTS ``MATCH``)
and one capped node query — so a call is linear in corpus size, not in
the number of paths asked about. **Batch the paths**: one call for the
files a hook cares about, not one call per file.
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

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trellis.stores.base.document import DocumentStore

logger = structlog.get_logger(__name__)

#: Page size for the ``list_documents`` scan — same order as the corpus
#: prune scan (``ingest_corpus.sync._PRUNE_PAGE_SIZE``).
_DOC_PAGE_SIZE = 500

#: Cap on the client-side node scan used for the doc→node reverse lookup.
#: The SQLite backend returns newest-first, so when a graph outgrows the
#: cap it is the oldest doc-linked nodes that fall off.
DEFAULT_GRAPH_SCAN_LIMIT = 2000


def source_path_matches(stored: Any, query: str) -> bool:
    """``True`` when a stored ``source_path`` names the queried file path.

    Equality, or a ``/``-boundary suffix match in either direction — the
    shapes that actually co-exist: stored relpaths queried by absolute
    path, and (less commonly) stored absolute paths queried by relpath.
    """
    if not isinstance(stored, str) or not stored or not query:
        return False
    if stored == query:
        return True
    return stored.endswith("/" + query) or query.endswith("/" + stored)


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


def _linked_entities(
    graph_store: Any,
    anchors_by_path: dict[str, set[str]],
    *,
    include_unconfirmed: bool,
    include_structural: bool,
    graph_scan_limit: int,
) -> dict[str, list[dict[str, Any]]]:
    """Nodes whose ``document_ids`` intersect a path's anchor documents."""
    entities_by_path: dict[str, list[dict[str, Any]]] = {p: [] for p in anchors_by_path}
    all_anchor_ids = set().union(*anchors_by_path.values())
    if not all_anchor_ids:
        return entities_by_path

    nodes = graph_store.query(node_type=None, properties=None, limit=graph_scan_limit)
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
    return entities_by_path


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
        graph_scan_limit: Cap on the client-side node scan used for the
            doc→node reverse lookup.

    Returns:
        ``{"paths": [{"path", "documents", "entities", "newest_item_at"}]}``
        — one entry per queried path, in query order. ``newest_item_at``
        is the ISO timestamp of the most recently written matching item
        (document or entity), or ``None`` when nothing matched; it is the
        value a client staleness gate compares against the file's mtime.
    """
    deduped: list[str] = []
    for raw in paths:
        path = raw.strip()
        if path and path not in deduped:
            deduped.append(path)

    docs_by_path, anchors_by_path = _matching_documents(document_store, deduped)
    entities_by_path = _linked_entities(
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
    return {"paths": results}


__all__ = [
    "DEFAULT_GRAPH_SCAN_LIMIT",
    "build_file_context",
    "source_path_matches",
]
