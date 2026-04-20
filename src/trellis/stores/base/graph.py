"""GraphStore — abstract interface for graph storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

VALID_NODE_ROLES = frozenset({"structural", "semantic", "curated"})


def validate_node_role_args(
    node_role: str,
    generation_spec: dict[str, Any] | None,
) -> None:
    """Enforce node_role / generation_spec invariants at the store boundary.

    Raises:
        ValueError: if ``node_role`` is unknown, if a curated node is
            missing ``generation_spec``, or if ``generation_spec`` is set
            on a non-curated node.
    """
    if node_role not in VALID_NODE_ROLES:
        msg = (
            f"Invalid node_role {node_role!r}; expected one of "
            f"{sorted(VALID_NODE_ROLES)}"
        )
        raise ValueError(msg)
    if node_role == "curated" and generation_spec is None:
        msg = (
            "generation_spec is required when node_role is 'curated' "
            "(identify which generator produced the node)"
        )
        raise ValueError(msg)
    if node_role != "curated" and generation_spec is not None:
        msg = (
            "generation_spec must be None unless node_role is 'curated' "
            f"(got node_role={node_role!r})"
        )
        raise ValueError(msg)


def validate_document_ids(document_ids: list[str] | None) -> None:
    """Enforce ``document_ids`` invariants at the store boundary.

    ``document_ids`` is the Phase-4 cross-plane link introduced by
    ADR planes-and-substrates: an entity node may reference the
    ``DocumentStore`` rows that sourced it, so graph traversal can
    materialize original content without a separate FTS hop. See
    ``docs/design/adr-planes-and-substrates.md`` §2.4.

    Rules:

    * ``None`` is valid and means "no link" — equivalent to an
      empty list on the read side.
    * Each element must be a non-empty string.
    * No duplicate entries — reject rather than silently dedup so
      callers notice the mistake.

    Raises:
        ValueError: on any rule violation.
    """
    if document_ids is None:
        return
    if not isinstance(document_ids, list):
        msg = (
            f"document_ids must be a list of strings or None, "
            f"got {type(document_ids).__name__}"
        )
        raise TypeError(msg)
    seen: set[str] = set()
    for i, doc_id in enumerate(document_ids):
        if not isinstance(doc_id, str) or not doc_id:
            msg = (
                f"document_ids[{i}] must be a non-empty string, "
                f"got {doc_id!r}"
            )
            raise ValueError(msg)
        if doc_id in seen:
            msg = f"document_ids contains duplicate entry {doc_id!r}"
            raise ValueError(msg)
        seen.add(doc_id)


def check_node_role_immutable(
    node_id: str,
    existing: dict[str, Any],
    requested_role: str,
) -> None:
    """Raise if an existing node's role would change.

    Called by store backends before closing an old version to ensure
    ``node_role`` is immutable across SCD Type 2 versions.
    """
    existing_role = existing.get("node_role", "semantic")
    if existing_role != requested_role:
        msg = (
            f"Cannot change node_role of {node_id!r}: existing "
            f"{existing_role!r} -> requested {requested_role!r}. "
            "node_role is immutable across versions; delete and "
            "recreate the node if you need to change it."
        )
        raise ValueError(msg)


class GraphStore(ABC):
    """Abstract interface for graph storage.

    Stores nodes (entities) and edges (relationships) with
    metadata and provenance tracking.

    Supports SCD Type 2 temporal versioning via ``valid_from``/``valid_to``
    columns.  Pass ``as_of`` to read methods to time-travel.
    """

    @abstractmethod
    def upsert_node(
        self,
        node_id: str | None,
        node_type: str,
        properties: dict[str, Any],
        *,
        node_role: str = "semantic",
        generation_spec: dict[str, Any] | None = None,
        document_ids: list[str] | None = None,
        commit: bool = True,
    ) -> str:
        """Insert or update a node.

        Auto-generates an ID if *node_id* is ``None``.

        When updating an existing (current) node, the old version is
        closed (``valid_to`` set) and a new version row is inserted.

        Args:
            node_id: Logical entity ID, or ``None`` to auto-generate.
            node_type: Domain entity type (free-form string).
            properties: Arbitrary JSON-serialisable property bag.
            node_role: Graph-invariant role — ``"structural"``,
                ``"semantic"`` (default), or ``"curated"``. Structural nodes
                are excluded from retrieval by default; curated nodes must
                carry a *generation_spec*.
            generation_spec: Required for curated nodes, forbidden for
                structural/semantic nodes. Captures the generator name,
                version, inputs, and parameters so the node can be
                regenerated or audited.
            document_ids: Optional list of ``DocumentStore`` IDs that
                sourced this entity. Introduced by Phase 4 of ADR
                planes-and-substrates as the first-class graph↔document
                link. ``None`` means "no link" and is equivalent to an
                empty list on the read side. When set, each element must
                be a non-empty string with no duplicates.
            commit: When ``False`` the caller is responsible for committing
                the surrounding transaction.

        Raises:
            ValueError: If ``node_role`` is invalid, if a curated node is
                missing ``generation_spec``, if ``generation_spec`` is set
                on a non-curated node, if ``node_role`` would change
                between versions of an existing node, or if
                ``document_ids`` contains duplicates or non-string entries.
            TypeError: If ``document_ids`` is provided but not a list.

        Returns:
            The node ID.
        """

    @abstractmethod
    def get_node(
        self,
        node_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Get a node by ID.

        Args:
            node_id: Logical entity ID.
            as_of: If set, return the version that was valid at this time.
                   If ``None``, return the current (``valid_to IS NULL``)
                   version.

        Returns:
            Node dict ``{node_id, node_type, node_role, generation_spec,
            document_ids, properties, created_at, updated_at,
            valid_from, valid_to}`` or ``None``. ``document_ids`` is
            always a ``list[str]`` (possibly empty) so consumers can
            iterate unconditionally.
        """

    @abstractmethod
    def get_nodes_bulk(
        self,
        node_ids: list[str],
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Batch get nodes by IDs.

        Args:
            node_ids: Logical entity IDs.
            as_of: Optional point-in-time filter.
        """

    @abstractmethod
    def upsert_alias(
        self,
        entity_id: str,
        source_system: str,
        raw_id: str,
        *,
        raw_name: str | None = None,
        match_confidence: float = 1.0,
        is_primary: bool = False,
    ) -> str:
        """Insert or update an alias bound to a canonical entity.

        Returns:
            The logical alias ID.
        """

    @abstractmethod
    def resolve_alias(
        self,
        source_system: str,
        raw_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an external system identifier to its current entity mapping."""

    @abstractmethod
    def get_aliases(
        self,
        entity_id: str,
        source_system: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """List aliases currently bound to a canonical entity."""

    @abstractmethod
    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
        *,
        commit: bool = True,
    ) -> str:
        """Insert or update an edge.

        Returns:
            The edge ID.
        """

    @abstractmethod
    def get_edges(
        self,
        node_id: str,
        direction: str = "both",
        edge_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get edges for a node.

        Args:
            node_id: The node ID.
            direction: ``"outgoing"``, ``"incoming"``, or ``"both"``.
            edge_type: Optional filter by edge type.
            as_of: Optional point-in-time filter.
        """

    @abstractmethod
    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Get subgraph via BFS traversal.

        Args:
            seed_ids: Starting node IDs.
            depth: Max traversal depth.
            edge_types: Optional edge type filter.
            as_of: Optional point-in-time filter.

        Returns:
            Dict with ``nodes`` and ``edges`` lists.
        """

    @abstractmethod
    def query(
        self,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Query nodes by type and/or properties.

        Args:
            node_type: Optional node type filter.
            properties: Optional property filters.
            limit: Max results.
            as_of: Optional point-in-time filter.
        """

    @abstractmethod
    def get_node_history(self, node_id: str) -> list[dict[str, Any]]:
        """Retrieve all versions of a node, ordered by valid_from DESC.

        Returns:
            List of node version dicts, newest first.
        """

    @abstractmethod
    def delete_node(self, node_id: str) -> bool:
        """Delete a node and cascade to its edges.

        Returns ``True`` if the node existed.
        """

    @abstractmethod
    def delete_edge(self, edge_id: str) -> bool:
        """Delete an edge.

        Returns ``True`` if the edge existed.
        """

    @abstractmethod
    def count_nodes(self) -> int:
        """Total current node count (valid_to IS NULL)."""

    @abstractmethod
    def count_edges(self) -> int:
        """Total current edge count (valid_to IS NULL)."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
