"""Graph entity search, withheld the same way on every surface that offers it.

``GET /graph/search``, ``GET /graph/search/facets`` and the MCP ``find_entity``
tool all answer "which entities match this string?". They share this module so
a match one of them withholds is withheld by all of them, and counted by none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.retrieve.sensitivity import conceal_node_pointers

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.retrieve.precedents import PrecedentClearance
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.graph import GraphStore

#: Rows per store call when walking the matches for a caller below restricted.
PRECEDENT_SCAN_PAGE = 500


def readable_matches(
    store: GraphStore,
    precedents: PrecedentClearance,
    q: str | None,
    node_type: str | None = None,
    *,
    sort: str = "created_at",
    descending: bool = True,
) -> Iterator[dict[str, Any]]:
    """Every match of ``q`` (and ``node_type``) the caller may read, in sort order.

    The one predicate a search page, its ``total`` and its facets share, so
    they cannot disagree: ``precedents.verdict_of`` is ``None``. That withholds
    a node labelled above the caller (#1029, #1264) and any node in the
    promoted-precedent namespace the caller may not read (#1080), whatever type
    it is stored under. Walks every match once, in store pages of
    :data:`PRECEDENT_SCAN_PAGE`; ``search_nodes`` ends each sort key with
    ``node_id``, so the pages neither repeat nor skip a row while the graph
    does not change. A label is read from the row in hand, and only a node in
    the precedent namespace costs a document read.
    """
    offset = 0
    while True:
        rows, total = store.search_nodes(
            search=q,
            node_type=node_type,
            sort=sort,
            descending=descending,
            limit=PRECEDENT_SCAN_PAGE,
            offset=offset,
        )
        for r in rows:
            if precedents.verdict_of(r["node_id"], r) is None:
                yield r
        offset += len(rows)
        if not rows or offset >= total:
            return


def search_readable_nodes(
    store: GraphStore,
    document_store: DocumentStore | None,
    precedents: PrecedentClearance,
    clearance: Sensitivity,
    *,
    q: str | None,
    node_type: str | None = None,
    sort: str = "created_at",
    descending: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """A page of the matches of ``q`` a caller at ``clearance`` may read, and a count.

    A ``restricted`` caller reads the stored page with one store call. For a
    caller below it, ``offset`` and the count both range over the matches
    :func:`readable_matches` yields, so a withheld match shortens no page and
    is counted nowhere; the cost is one walk of every match of ``q``. Every
    served row then goes through ``conceal_node_pointers``: a pointer naming a
    document the caller may not read is dropped from it.
    """
    if clearance == "restricted":
        rows, total = store.search_nodes(
            search=q,
            node_type=node_type,
            sort=sort,
            descending=descending,
            limit=limit,
            offset=offset,
        )
    else:
        rows, total = [], 0
        for row in readable_matches(
            store, precedents, q, node_type, sort=sort, descending=descending
        ):
            if offset <= total < offset + limit:
                rows.append(row)
            total += 1
    concealed = conceal_node_pointers(
        rows, document_store, clearance, precedents=precedents
    )
    return concealed, total
