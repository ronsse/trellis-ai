"""GraphMigrator — copy nodes/edges/aliases between any two GraphStore backends.

Backend-agnostic: only uses the public ``GraphStore`` API, so SQLite ↔
Postgres ↔ Neo4j all work without backend-specific branches. POC scope
is current-versions-only — see :mod:`trellis.migrate` for the full
contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)


_DEFAULT_BATCH_SIZE = 1_000
_DEFAULT_MAX_NODES = 100_000


class MigrationCapacityExceededError(RuntimeError):
    """Raised when the source has more current nodes than ``max_nodes``.

    POC scope is an in-memory snapshot. When a deployment legitimately
    needs more, add a paginated iterator on the ``GraphStore`` ABC and
    teach the migrator to use it.
    """

    def __init__(self, observed: int, limit: int) -> None:
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"Source graph has at least {observed} current nodes, exceeding "
            f"max_nodes={limit}. Increase max_nodes (memory permitting), "
            "or wait for paginated iteration support."
        )


@dataclass
class MigrationReport:
    """Per-run counts + timing returned by :meth:`GraphMigrator.run`."""

    nodes_read: int = 0
    nodes_written: int = 0
    nodes_skipped: int = 0
    edges_read: int = 0
    edges_written: int = 0
    edges_skipped: int = 0
    aliases_read: int = 0
    aliases_written: int = 0
    aliases_skipped: int = 0
    elapsed_ms: int = 0
    dry_run: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        """One-line human-readable summary suitable for CLI output."""
        prefix = "[DRY RUN] " if self.dry_run else ""
        bits = [
            f"{prefix}migrated in {self.elapsed_ms}ms:",
            f"nodes={self.nodes_written}/{self.nodes_read}",
            f"edges={self.edges_written}/{self.edges_read}",
            f"aliases={self.aliases_written}/{self.aliases_read}",
        ]
        if self.nodes_skipped or self.edges_skipped or self.aliases_skipped:
            bits.append(
                f"skipped(idempotent): nodes={self.nodes_skipped} "
                f"edges={self.edges_skipped} aliases={self.aliases_skipped}"
            )
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        return " ".join(bits)


class GraphMigrator:
    """Walks ``source`` and replays its current graph state into ``dest``."""

    def __init__(
        self,
        source: GraphStore,
        dest: GraphStore,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        max_nodes: int = _DEFAULT_MAX_NODES,
    ) -> None:
        self._source = source
        self._dest = dest
        self._batch_size = batch_size
        self._max_nodes = max_nodes

    def run(self, *, dry_run: bool = False) -> MigrationReport:
        """Migrate everything in one pass. Returns the report."""
        report = MigrationReport(dry_run=dry_run)
        t0 = time.monotonic()

        nodes = self._read_all_current_nodes(report)
        if nodes is None:
            # Capacity exceeded — error already on the report; bail.
            report.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return report

        self._migrate_nodes(nodes, dry_run, report)
        self._migrate_aliases(nodes, dry_run, report)
        self._migrate_edges(nodes, dry_run, report)

        report.elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "graph_migration_complete",
            **{
                k: getattr(report, k)
                for k in (
                    "nodes_read",
                    "nodes_written",
                    "nodes_skipped",
                    "edges_read",
                    "edges_written",
                    "edges_skipped",
                    "aliases_read",
                    "aliases_written",
                    "aliases_skipped",
                    "elapsed_ms",
                    "dry_run",
                )
            },
            error_count=len(report.errors),
        )
        return report

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _read_all_current_nodes(
        self, report: MigrationReport
    ) -> list[dict[str, Any]] | None:
        # query(limit=N+1) — if we get back N+1 the source has more than
        # we can safely hold in memory.
        limit = self._max_nodes + 1
        try:
            nodes = self._source.query(limit=limit)
        except Exception as exc:
            report.errors.append(("read_nodes", f"{type(exc).__name__}: {exc}"))
            logger.warning("graph_migration_read_failed", error=str(exc))
            return None

        if len(nodes) > self._max_nodes:
            report.errors.append(
                ("capacity", f"source has > {self._max_nodes} current nodes")
            )
            raise MigrationCapacityExceededError(len(nodes), self._max_nodes)
        report.nodes_read = len(nodes)
        return nodes

    # ------------------------------------------------------------------
    # Migrate — nodes
    # ------------------------------------------------------------------

    def _migrate_nodes(
        self,
        nodes: list[dict[str, Any]],
        dry_run: bool,
        report: MigrationReport,
    ) -> None:
        for node in nodes:
            node_id = str(node["node_id"])
            try:
                existing = self._dest.get_node(node_id)
            except Exception as exc:
                report.errors.append(
                    (f"node:{node_id}", f"get_node failed: {type(exc).__name__}: {exc}")
                )
                continue

            if existing is not None:
                report.nodes_skipped += 1
                continue

            if dry_run:
                report.nodes_written += 1
                continue

            try:
                self._dest.upsert_node(
                    node_id,
                    node_type=str(node["node_type"]),
                    properties=dict(node.get("properties") or {}),  # type: ignore[arg-type]
                    node_role=str(node.get("node_role") or "semantic"),
                    generation_spec=node.get("generation_spec"),  # type: ignore[arg-type]
                    document_ids=list(node.get("document_ids") or []),  # type: ignore[arg-type]
                )
                report.nodes_written += 1
            except Exception as exc:
                report.errors.append(
                    (f"node:{node_id}", f"upsert failed: {type(exc).__name__}: {exc}")
                )

    # ------------------------------------------------------------------
    # Migrate — aliases
    # ------------------------------------------------------------------

    def _migrate_aliases(
        self,
        nodes: list[dict[str, Any]],
        dry_run: bool,
        report: MigrationReport,
    ) -> None:
        for node in nodes:
            entity_id = str(node["node_id"])
            try:
                aliases = self._source.get_aliases(entity_id)
            except Exception as exc:
                report.errors.append(
                    (
                        f"aliases:{entity_id}",
                        f"get_aliases failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            for alias in aliases:
                report.aliases_read += 1
                source_system = str(alias["source_system"])
                raw_id = str(alias["raw_id"])

                # Idempotency: if the destination already resolves
                # (source_system, raw_id) to anything, skip.
                try:
                    existing = self._dest.resolve_alias(source_system, raw_id)
                except Exception as exc:
                    report.errors.append(
                        (
                            f"alias:{source_system}:{raw_id}",
                            f"resolve failed: {type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                if existing is not None:
                    report.aliases_skipped += 1
                    continue

                if dry_run:
                    report.aliases_written += 1
                    continue

                try:
                    self._dest.upsert_alias(
                        entity_id,
                        source_system,
                        raw_id,
                        raw_name=alias.get("raw_name"),  # type: ignore[arg-type]
                        match_confidence=float(alias.get("match_confidence") or 1.0),  # type: ignore[arg-type]
                        is_primary=bool(alias.get("is_primary", False)),
                    )
                    report.aliases_written += 1
                except Exception as exc:
                    report.errors.append(
                        (
                            f"alias:{source_system}:{raw_id}",
                            f"upsert failed: {type(exc).__name__}: {exc}",
                        )
                    )

    # ------------------------------------------------------------------
    # Migrate — edges
    # ------------------------------------------------------------------

    def _migrate_edges(
        self,
        nodes: list[dict[str, Any]],
        dry_run: bool,
        report: MigrationReport,
    ) -> None:
        # Walk outgoing edges per source node so each edge is visited
        # exactly once (an edge X→Y is "outgoing" from X). get_edges
        # with direction="both" would double-count.
        seen_edge_ids: set[str] = set()
        for node in nodes:
            node_id = str(node["node_id"])
            try:
                edges = self._source.get_edges(node_id, direction="outgoing")
            except Exception as exc:
                report.errors.append(
                    (
                        f"edges:{node_id}",
                        f"get_edges failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            for edge in edges:
                edge_id = str(edge["edge_id"])
                if edge_id in seen_edge_ids:
                    continue
                seen_edge_ids.add(edge_id)
                report.edges_read += 1

                # Idempotency: a destination that already has any current
                # edge with the same (source_id, target_id, edge_type)
                # is treated as already-migrated. The migrator does not
                # try to be edge-id-stable across backends because edge
                # IDs are usually backend-generated.
                source_id = str(edge["source_id"])
                target_id = str(edge["target_id"])
                edge_type = str(edge["edge_type"])
                if self._destination_has_edge(source_id, target_id, edge_type):
                    report.edges_skipped += 1
                    continue

                if dry_run:
                    report.edges_written += 1
                    continue

                try:
                    self._dest.upsert_edge(
                        source_id,
                        target_id,
                        edge_type,
                        properties=dict(edge.get("properties") or {}),  # type: ignore[arg-type]
                    )
                    report.edges_written += 1
                except Exception as exc:
                    report.errors.append(
                        (
                            f"edge:{source_id}->{target_id}",
                            f"upsert failed: {type(exc).__name__}: {exc}",
                        )
                    )

    def _destination_has_edge(
        self, source_id: str, target_id: str, edge_type: str
    ) -> bool:
        try:
            existing = self._dest.get_edges(
                source_id, direction="outgoing", edge_type=edge_type
            )
        except Exception:
            return False
        return any(str(e.get("target_id")) == target_id for e in existing)
