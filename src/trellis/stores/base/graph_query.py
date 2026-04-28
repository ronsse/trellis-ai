"""Canonical graph query DSL.

Per ``docs/design/adr-canonical-graph-layer.md`` Phase 1, this module
defines the small typed value-object DSL that `GraphStore` backends
compile to their native query dialect.

Operator surface (Phase 1):

* ``eq`` — exact equality on a scalar value (str / int / float / bool)
* ``in`` — membership in a tuple of scalar values
* ``exists`` — the property path resolves to a non-null value

Operators explicitly **out of scope** for Phase 1: range comparisons
(``gt`` / ``lt``), regex match, full-text search (the `DocumentStore`
owns that), nested-path traversal beyond one level. Each can graduate
later when a consumer asks; the gate is an ADR amendment + a contract
test extension.

Field paths in :class:`FilterClause` use dotted notation:

* ``"node_type"`` — top-level column on the node row
* ``"properties.team"`` — JSON property nested one level inside
  ``properties``
* ``"node_role"`` — ``"semantic"`` / ``"structural"`` / ``"curated"``

Backends are responsible for parsing these paths into their native
form (e.g., Postgres ``properties->>'team'``; SQLite
``json_extract(properties_json, '$.team')``; Neo4j
``n.properties.team``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# The set of operators a backend MUST support to be contract-compliant
# in Phase 1. Extending this set is an ADR amendment.
FilterOp = Literal["eq", "in", "exists"]


@dataclass(frozen=True)
class FilterClause:
    """A single filter predicate over a field path.

    Args:
        field: Dotted path. Top-level columns or ``properties.<key>``.
        op: One of ``"eq"``, ``"in"``, ``"exists"``.
        value: Scalar for ``eq``; tuple of scalars for ``in``;
            ignored for ``exists`` (pass ``None``).
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
