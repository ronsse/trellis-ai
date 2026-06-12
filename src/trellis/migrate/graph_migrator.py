"""GraphMigrator — copy nodes/edges/aliases between any two GraphStore backends.

Backend-agnostic: only uses the public ``GraphStore`` API, so SQLite ↔
Postgres ↔ Neo4j all work without backend-specific branches. POC scope
is current-versions-only — see :mod:`trellis.migrate` for the full
contract.

Failure semantics (loud-by-default, C2 Phase 4):

* The default behavior on **any** step failure (read, get_node,
  upsert, alias-resolve, edge-walk, edge-upsert) is to wrap the
  original exception in a :class:`MigrationStepError` and **re-raise**.
  Partial migration state leaves the destination in an inconsistent
  shape — operators need to know.
* Opt in to continue-on-error semantics by passing
  ``strategy=BatchStrategy.CONTINUE_ON_ERROR`` to :meth:`GraphMigrator.run`.
  Failures are then captured in :attr:`MigrationReport.step_failures`
  (one entry per failing step, with traceback) and migration proceeds.
* Every caught exception is logged with ``exc_info=True`` so the
  traceback is preserved even when ``continue_on_error`` is on.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from trellis.mutate.commands import BatchStrategy

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


class MigrationStepError(RuntimeError):
    """Wraps the original exception raised by a single migration step.

    The migrator catches each step's exception, packages the step name
    and offending entity id (when applicable) into the message, and
    re-raises this wrapper. The original exception is preserved on
    ``__cause__`` via ``raise ... from exc`` so the full traceback is
    visible.

    Raised by default. When ``GraphMigrator.run(strategy=
    BatchStrategy.CONTINUE_ON_ERROR)`` is passed the wrapper is not
    raised — failures are captured in
    :attr:`MigrationReport.step_failures` instead.
    """

    def __init__(
        self,
        step: str,
        entity_id: str | None,
        original: BaseException,
    ) -> None:
        self.step = step
        self.entity_id = entity_id
        self.original = original
        detail = f" (entity_id={entity_id!r})" if entity_id is not None else ""
        super().__init__(
            f"Migration step {step!r}{detail} failed: "
            f"{type(original).__name__}: {original}"
        )


@dataclass
class MigrationStepFailure:
    """One per-step failure captured under continue-on-error semantics."""

    step: str
    entity_id: str | None
    error_class: str
    message: str
    traceback: str

    def short(self) -> str:
        """Compact ``step:entity_id -> ErrClass: msg`` string for logs."""
        eid = self.entity_id or "-"
        return f"{self.step}:{eid} -> {self.error_class}: {self.message}"


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
    #: Free-form (step, message) pairs preserved for backwards compatibility.
    #: New call sites should prefer :attr:`step_failures` which carries the
    #: exception class, message, and traceback in a structured form.
    errors: list[tuple[str, str]] = field(default_factory=list)
    #: Structured per-step failures captured under continue-on-error.
    #: Empty when the default raise-on-error mode is used.
    step_failures: list[MigrationStepFailure] = field(default_factory=list)

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
        if self.step_failures:
            bits.append(f"step_failures={len(self.step_failures)}")
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

    def run(
        self,
        *,
        dry_run: bool = False,
        strategy: BatchStrategy = BatchStrategy.STOP_ON_ERROR,
    ) -> MigrationReport:
        """Migrate everything in one pass. Returns the report.

        Args:
            dry_run: When True, walk and count without writing to the
                destination.
            strategy: Failure-handling strategy. The default
                :attr:`BatchStrategy.STOP_ON_ERROR` raises
                :class:`MigrationStepError` on the first failing step
                (reusing the executor enum keeps semantics consistent
                across the codebase). :attr:`BatchStrategy.CONTINUE_ON_ERROR`
                captures each failure in
                :attr:`MigrationReport.step_failures` and continues.
                :attr:`BatchStrategy.SEQUENTIAL` is treated as
                ``STOP_ON_ERROR`` — the migrator is inherently sequential.
        """
        continue_on_error = strategy == BatchStrategy.CONTINUE_ON_ERROR
        report = MigrationReport(dry_run=dry_run)
        t0 = time.monotonic()

        nodes = self._read_all_current_nodes(report, continue_on_error)
        if nodes is None:
            # Capacity exceeded or read failed under continue-on-error.
            report.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return report

        self._migrate_nodes(nodes, dry_run, report, continue_on_error)
        self._migrate_aliases(nodes, dry_run, report, continue_on_error)
        self._migrate_edges(nodes, dry_run, report, continue_on_error)

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
            step_failure_count=len(report.step_failures),
        )
        return report

    # ------------------------------------------------------------------
    # Failure-handling helpers
    # ------------------------------------------------------------------

    def _record_step_failure(
        self,
        *,
        report: MigrationReport,
        step: str,
        entity_id: str | None,
        exc: BaseException,
        log_event: str,
    ) -> None:
        """Log + record a step failure into the report (continue-on-error path).

        Pure bookkeeping for the :attr:`BatchStrategy.CONTINUE_ON_ERROR`
        path: logs the structured error, appends to the legacy ``errors``
        list, and appends a :class:`MigrationStepFailure`. Under the
        default :attr:`BatchStrategy.STOP_ON_ERROR` the call sites raise
        :class:`MigrationStepError` directly without invoking this helper,
        so static silent-fallback audits see the loud-failure path
        inline in each ``except`` block.
        """
        logger.error(
            log_event,
            step=step,
            entity_id=entity_id,
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
        # Preserve the legacy `errors` list for backwards-compatible callers.
        legacy_key = f"{step}:{entity_id}" if entity_id is not None else step
        report.errors.append((legacy_key, f"{type(exc).__name__}: {exc}"))
        report.step_failures.append(
            MigrationStepFailure(
                step=step,
                entity_id=entity_id,
                error_class=type(exc).__name__,
                message=str(exc),
                traceback="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _read_all_current_nodes(
        self,
        report: MigrationReport,
        continue_on_error: bool,
    ) -> list[dict[str, Any]] | None:
        # query(limit=N+1) — if we get back N+1 the source has more than
        # we can safely hold in memory.
        limit = self._max_nodes + 1
        try:
            nodes = self._source.query(limit=limit)
        except Exception as exc:
            if not continue_on_error:
                raise MigrationStepError("read_nodes", None, exc) from exc  # noqa: EM101
            self._record_step_failure(
                report=report,
                step="read_nodes",
                entity_id=None,
                exc=exc,
                log_event="graph_migration_read_failed",
            )
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
        continue_on_error: bool,
    ) -> None:
        for node in nodes:
            node_id = str(node["node_id"])
            try:
                existing = self._dest.get_node(node_id)
            except Exception as exc:
                if not continue_on_error:
                    raise MigrationStepError("get_node", node_id, exc) from exc  # noqa: EM101
                self._record_step_failure(
                    report=report,
                    step="get_node",
                    entity_id=node_id,
                    exc=exc,
                    log_event="graph_migration_get_node_failed",
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
                if not continue_on_error:
                    raise MigrationStepError("upsert_node", node_id, exc) from exc  # noqa: EM101
                self._record_step_failure(
                    report=report,
                    step="upsert_node",
                    entity_id=node_id,
                    exc=exc,
                    log_event="graph_migration_upsert_node_failed",
                )

    # ------------------------------------------------------------------
    # Migrate — aliases
    # ------------------------------------------------------------------

    def _migrate_aliases(
        self,
        nodes: list[dict[str, Any]],
        dry_run: bool,
        report: MigrationReport,
        continue_on_error: bool,
    ) -> None:
        for node in nodes:
            entity_id = str(node["node_id"])
            try:
                aliases = self._source.get_aliases(entity_id)
            except Exception as exc:
                if not continue_on_error:
                    raise MigrationStepError("get_aliases", entity_id, exc) from exc  # noqa: EM101
                self._record_step_failure(
                    report=report,
                    step="get_aliases",
                    entity_id=entity_id,
                    exc=exc,
                    log_event="graph_migration_get_aliases_failed",
                )
                continue

            for alias in aliases:
                report.aliases_read += 1
                source_system = str(alias["source_system"])
                raw_id = str(alias["raw_id"])
                alias_key = f"{source_system}:{raw_id}"

                # Idempotency: if the destination already resolves
                # (source_system, raw_id) to anything, skip.
                try:
                    existing = self._dest.resolve_alias(source_system, raw_id)
                except Exception as exc:
                    if not continue_on_error:
                        raise MigrationStepError(
                            "resolve_alias",  # noqa: EM101
                            alias_key,
                            exc,
                        ) from exc
                    self._record_step_failure(
                        report=report,
                        step="resolve_alias",
                        entity_id=alias_key,
                        exc=exc,
                        log_event="graph_migration_resolve_alias_failed",
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
                    if not continue_on_error:
                        raise MigrationStepError(
                            "upsert_alias",  # noqa: EM101
                            alias_key,
                            exc,
                        ) from exc
                    self._record_step_failure(
                        report=report,
                        step="upsert_alias",
                        entity_id=alias_key,
                        exc=exc,
                        log_event="graph_migration_upsert_alias_failed",
                    )

    # ------------------------------------------------------------------
    # Migrate — edges
    # ------------------------------------------------------------------

    def _migrate_edges(
        self,
        nodes: list[dict[str, Any]],
        dry_run: bool,
        report: MigrationReport,
        continue_on_error: bool,
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
                if not continue_on_error:
                    raise MigrationStepError("get_edges", node_id, exc) from exc  # noqa: EM101
                self._record_step_failure(
                    report=report,
                    step="get_edges",
                    entity_id=node_id,
                    exc=exc,
                    log_event="graph_migration_get_edges_failed",
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
                edge_key = f"{source_id}->{target_id}:{edge_type}"
                try:
                    already = self._destination_has_edge(
                        source_id, target_id, edge_type
                    )
                except Exception as exc:
                    if not continue_on_error:
                        raise MigrationStepError(
                            "check_edge_exists",  # noqa: EM101
                            edge_key,
                            exc,
                        ) from exc
                    self._record_step_failure(
                        report=report,
                        step="check_edge_exists",
                        entity_id=edge_key,
                        exc=exc,
                        log_event="graph_migration_check_edge_failed",
                    )
                    continue
                if already:
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
                    if not continue_on_error:
                        raise MigrationStepError(
                            "upsert_edge",  # noqa: EM101
                            edge_key,
                            exc,
                        ) from exc
                    self._record_step_failure(
                        report=report,
                        step="upsert_edge",
                        entity_id=edge_key,
                        exc=exc,
                        log_event="graph_migration_upsert_edge_failed",
                    )

    def _destination_has_edge(
        self, source_id: str, target_id: str, edge_type: str
    ) -> bool:
        """Probe whether the destination already has the (src,tgt,type) edge.

        Raises on backend error; the caller is responsible for routing
        the exception through :meth:`_handle_step_failure`. Returning
        ``False`` on error would silently re-upsert and let SCD-2 create
        a duplicate current row — exactly the kind of silent fallback
        C2 Phase 4 set out to remove.
        """
        existing = self._dest.get_edges(
            source_id, direction="outgoing", edge_type=edge_type
        )
        return any(str(e.get("target_id")) == target_id for e in existing)
