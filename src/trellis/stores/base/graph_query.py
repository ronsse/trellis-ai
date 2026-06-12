"""Canonical graph query DSL.

Per ``docs/design/adr-canonical-graph-layer.md`` Phase 1, this module
defines the small typed value-object DSL that `GraphStore` backends
compile to their native query dialect.

Operator surface:

Phase 1 (node + edge):

* ``eq`` — exact equality on a scalar value (str / int / float / bool)
* ``in`` — membership in a tuple of scalar values
* ``exists`` — the property path resolves to a non-null value

Phase 2 — range comparisons (added for provenance filtering on edges,
``plan-provenance-columns.md``):

* ``lt`` / ``lte`` / ``gt`` / ``gte`` — numeric inequality.  Values are
  not validated for range here (e.g. ``confidence < 2.0`` is allowed —
  the DSL just translates cleanly; over-range filters return every row
  the inequality matches).  Operator vocabulary is short-form
  (``lt`` etc.) for parity with the Phase 1 ops.

Phase 3 — list-membership (added for searchability over list-typed
properties such as ``properties.column_names``, Track G ADR
``adr-searchability-recipe.md``):

* ``contains`` — the scalar value is a member of a list-typed property
  at the given path.  The DSL value is a single scalar; the property at
  ``field`` is expected to be a JSON array.  If the property is scalar,
  missing, or any non-list value, the predicate evaluates ``False`` for
  that row (no exception).  Inverted from ``in``: ``in`` asks "is the
  property's scalar value in this set?"; ``contains`` asks "is this
  scalar value an element of the property's list?".

Operators still out of scope: regex match, full-text search (the
``DocumentStore`` owns that), nested-path traversal beyond one level.

Field paths in :class:`FilterClause` use dotted notation:

* ``"node_type"`` — top-level column on the node row
* ``"properties.team"`` — JSON property nested one level inside
  ``properties``
* ``"node_role"`` — ``"semantic"`` / ``"structural"`` / ``"curated"``

Edge field paths (used with :class:`EdgeQuery`):

* ``"edge_type"`` / ``"source_id"`` / ``"target_id"`` — edge columns
* ``"source_trace_id"`` / ``"agent_id"`` / ``"confidence"`` /
  ``"evidence_ref"`` / ``"extractor_tier"`` — the five provenance
  columns promoted in Phase 3 of ``adr-graph-ontology.md``
* ``"properties.<key>"`` — JSON property nested one level inside the
  edge's ``properties``

Backends are responsible for parsing these paths into their native
form (e.g., Postgres ``properties->>'team'``; SQLite
``json_extract(properties_json, '$.team')``; Neo4j
``n.properties.team``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# The set of operators a backend MUST support to be contract-compliant.
# Range ops (``lt`` / ``lte`` / ``gt`` / ``gte``) were added in Phase 2
# alongside provenance-column filtering on edges; backends may reject
# them on string-typed fields, but the DSL itself does not gate by
# dtype — translation is the backend's responsibility.
# ``contains`` (Phase 3, Track G) asks list-membership: the scalar
# value is an element of the list-typed property at ``field``.
FilterOp = Literal["eq", "in", "exists", "lt", "lte", "gt", "gte", "contains"]

#: Range operators — used by backends to branch SQL/Cypher emission and
#: by the FilterClause invariant check.  Kept as a module constant so
#: backend compilers can ``in _RANGE_OPS`` without re-listing them.
_RANGE_OPS: frozenset[str] = frozenset({"lt", "lte", "gt", "gte"})

#: Canonical mapping from DSL range op to the glyph used by both SQL
#: and openCypher (they share the same set of inequality operators).
#: Shared by every backend compiler so the SQLite, Postgres, and
#: Bolt-path compilers never drift on the mapping.
RANGE_OP_GLYPH: dict[str, str] = {
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


@dataclass(frozen=True)
class FilterClause:
    """A single filter predicate over a field path.

    Args:
        field: Dotted path. Top-level columns or ``properties.<key>``.
        op: One of ``"eq"``, ``"in"``, ``"exists"``, ``"lt"``, ``"lte"``,
            ``"gt"``, ``"gte"``, ``"contains"``.
        value: Scalar for ``eq``, range ops, and ``contains``; tuple of
            scalars for ``in``; ignored for ``exists`` (pass ``None``).

    ``contains`` semantics: "the scalar ``value`` is a member of the
    list-typed property at ``field``."  The property is expected to be
    a JSON array; if it's scalar, missing, or a non-list value, the
    predicate evaluates ``False`` for that row (no exception is raised).
    """

    field: str
    op: FilterOp
    value: str | int | float | bool | tuple[str | int | float | bool, ...] | None = None

    def __post_init__(self) -> None:
        if self.op == "in" and not isinstance(self.value, tuple):
            msg = (
                f"FilterClause op='in' requires a tuple value, "
                f"got {type(self.value).__name__}"
            )
            raise TypeError(msg)
        if self.op == "eq" and isinstance(self.value, tuple):
            msg = "FilterClause op='eq' must use a scalar value, not a tuple"
            raise TypeError(msg)
        if self.op == "exists" and self.value is not None:
            msg = "FilterClause op='exists' must have value=None"
            raise ValueError(msg)
        if self.op == "contains":
            # ``contains`` takes a single scalar.  Tuples, lists, dicts,
            # and ``None`` are nonsense for "is this element a member of
            # the list-typed property?" — reject early so the backend
            # compiler never has to second-guess.  Unlike the range ops,
            # ``bool`` is accepted here (a list of booleans is a legal
            # property shape; ``contains True`` is a meaningful query).
            if isinstance(self.value, tuple):
                msg = "FilterClause op='contains' must use a scalar value, not a tuple"
                raise TypeError(msg)
            if self.value is None:
                msg = "FilterClause op='contains' must have a scalar value, not None"
                raise ValueError(msg)
            if not isinstance(self.value, str | int | float | bool):
                msg = (
                    f"FilterClause op='contains' value must be a scalar "
                    f"(str / int / float / bool), got {type(self.value).__name__}"
                )
                raise TypeError(msg)
        if self.op in _RANGE_OPS:
            # Range ops take a scalar.  Tuples and ``None`` are nonsense
            # for ``confidence < ?``-style predicates; reject early so
            # the backend compiler never has to second-guess.
            if isinstance(self.value, tuple):
                msg = (
                    f"FilterClause op={self.op!r} must use a scalar value, not a tuple"
                )
                raise TypeError(msg)
            if self.value is None:
                msg = f"FilterClause op={self.op!r} must have a scalar value, not None"
                raise ValueError(msg)
            # bool is an int subclass — reject explicitly so a
            # ``confidence < True`` typo doesn't silently land as 1.
            if isinstance(self.value, bool) or not isinstance(
                self.value, int | float | str
            ):
                msg = (
                    f"FilterClause op={self.op!r} value must be a numeric or "
                    f"string scalar, got {type(self.value).__name__}"
                )
                raise TypeError(msg)


@dataclass(frozen=True)
class NodeQuery:
    """A typed read query over the node table.

    Args:
        filters: Conjunction (AND) of :class:`FilterClause` predicates.
            Empty tuple matches every current node.
        limit: Maximum rows to return.
        as_of: Optional point-in-time filter. ``None`` reads the
            current version of every matching node.
    """

    filters: tuple[FilterClause, ...] = ()
    limit: int = 50
    as_of: datetime | None = None


@dataclass(frozen=True)
class EdgeQuery:
    """A typed read query over the edge table.

    Mirrors :class:`NodeQuery` but resolves field paths against the
    edge row.  Added in Phase 2 of ``plan-provenance-columns.md`` so
    callers can ask "edges with ``confidence < 0.7``" / "edges minted
    by trace X" without falling back to ``properties.<key>`` JSON
    extraction.

    Args:
        filters: Conjunction (AND) of :class:`FilterClause` predicates.
            Empty tuple matches every current edge.
        limit: Maximum rows to return.
        as_of: Optional point-in-time filter. ``None`` reads the
            current version of every matching edge.
    """

    filters: tuple[FilterClause, ...] = ()
    limit: int = 50
    as_of: datetime | None = None


@dataclass(frozen=True)
class SubgraphQuery:
    """A typed BFS subgraph query.

    Args:
        seed_ids: Seed nodes for traversal.
        depth: Maximum hops from any seed. ``0`` returns just the seeds.
        edge_type_filter: Optional whitelist of edge types to traverse.
            ``None`` traverses every type.
        as_of: Optional point-in-time filter applied to both nodes and
            edges during traversal.
    """

    seed_ids: tuple[str, ...]
    depth: int = 2
    edge_type_filter: tuple[str, ...] | None = None
    as_of: datetime | None = None


@dataclass(frozen=True)
class SubgraphResult:
    """Typed return shape for :class:`SubgraphQuery` execution.

    Mirrors the existing ``get_subgraph`` dict shape (``{"nodes": [...],
    "edges": [...]}``) but as a typed value object so consumers don't
    rely on dict-key strings.
    """

    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
