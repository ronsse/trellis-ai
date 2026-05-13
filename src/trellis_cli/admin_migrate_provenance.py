"""``trellis admin migrate-provenance`` — lift legacy edge provenance.

Phase 2 of ``plan-provenance-columns.md`` shipped the five typed
provenance columns and validated writes against them.  Legacy edges
written before that schema landed kept their provenance keys inside
the free-form ``properties`` JSON blob.  This CLI walks the graph
once, finds those legacy edges, and rewrites them with the canonical
typed columns so retrieval surfaces (Item 6 dogfooding, Observation
queries, etc.) can filter without falling through to JSON extract.

Behaviour (mirrors ``plan-provenance-columns.md`` §4 + plan brief):

* **Scope.** Each edge with all five typed columns NULL is a
  candidate.  If any typed column is already populated the row is
  skipped — the migration is *additive*, not authoritative.
* **Validation.** Lifted values pass through
  :func:`validate_edge_provenance` before re-write.  Malformed legacy
  values (``confidence="high"``, ``extractor_tier="hybrid"`` in
  lowercase, …) emit an ``EXTRACTION_FAILED`` event with
  ``failure_kind="parse_error"`` and skip the edge — they do not
  silently drop the row.
* **Drift threshold.** When more than 1% of scanned edges fail
  validation, :class:`MigrationDriftError` is raised after the scan
  completes.  Operators want a loud signal that the legacy corpus
  has structural issues; silently logging hundreds of per-row
  warnings is the wrong default.
* **Batching.**  Re-writes commit every ``--batch-size`` edges so a
  crash mid-run doesn't lose the prefix that already landed.  Default
  1000 (matches the program-wide convention from earlier migrations).
* **Idempotency.** Re-running the CLI is a no-op for rows already
  migrated — the "all-NULL" filter naturally skips them.
* **JSON output.**  Per the project's hard rule, ``--format json``
  emits a single JSON object on stdout suitable for piping to ``jq``.

Exit codes follow the project map established in PR #123:

* ``0`` — success (including no-op runs).
* ``1`` — unexpected runtime error.
* ``5`` — store error during scan / write.

Drift detection raises :class:`MigrationDriftError` which surfaces as
exit code ``1`` (parallel to ``RetentionDriftError`` from PR #116;
both signal "the data is in a state operators need to look at").
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import typer
from rich.console import Console

from trellis.extract.telemetry import emit_extraction_failure
from trellis.stores.base.edge_provenance import (
    EDGE_PROVENANCE_FIELDS,
    validate_edge_provenance,
)
from trellis_cli.stores import get_event_log, get_graph_store

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)
console = Console()


#: Drift threshold — fraction of edges scanned whose legacy
#: ``properties`` JSON blob carries malformed provenance values.
#: Above this fraction the run raises :class:`MigrationDriftError`
#: rather than logging quietly.  Mirrors the loudness contract from
#: PR #116's retention-drift gate.
_DRIFT_THRESHOLD: float = 0.01

#: Cap on how many per-row error messages the text report prints in
#: full before truncating with a "… and N more" line.  Keeps the
#: console output finite when a misconfigured store rains errors.
_ERROR_PREVIEW_LIMIT = 10

#: Exit code map.  Kept narrow — anything store-shaped goes to ``5``,
#: anything else to ``1``.
_EXIT_OK = 0
_EXIT_RUNTIME = 1
_EXIT_STORE = 5


class MigrationDriftError(RuntimeError):
    """Raised when too many edges have malformed legacy provenance.

    Parallel to ``RetentionDriftError`` (PR #116) — signals that the
    corpus has structural issues operators need to investigate.  The
    CLI surfaces this as exit code ``1`` so CI catches it loudly.
    """

    def __init__(
        self,
        *,
        malformed_count: int,
        scanned: int,
        threshold: float,
    ) -> None:
        rate = malformed_count / scanned if scanned else 0.0
        super().__init__(
            f"{malformed_count}/{scanned} edges have malformed legacy "
            f"provenance ({rate:.1%} >= {threshold:.1%} threshold); "
            "investigate the source data before migrating."
        )
        self.malformed_count = malformed_count
        self.scanned = scanned
        self.threshold = threshold
        self.rate = rate


@dataclass
class MigrateProvenanceReport:
    """Tallies for one ``migrate-provenance`` run.

    All counts are post-scan totals (not per-batch).  ``dry_run``
    reports the count that *would* have migrated.
    """

    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    dry_run: bool = False
    batch_size: int = 1000
    edges_scanned: int = 0
    edges_migrated: int = 0
    edges_already_migrated: int = 0
    edges_no_legacy_provenance: int = 0
    edges_malformed: int = 0
    drift_rate: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe payload for the CLI's ``--format json`` output."""
        return asdict(self)


def _all_provenance_columns_null(edge: dict[str, Any]) -> bool:
    """Edge is a candidate only when every typed column is unset."""
    return all(edge.get(field) is None for field in EDGE_PROVENANCE_FIELDS)


def _extract_legacy_provenance(properties: dict[str, Any]) -> dict[str, Any]:
    """Pull provenance keys out of the legacy ``properties`` JSON blob.

    Missing keys default to ``None`` — same shape as
    :func:`trellis.stores.base.edge_provenance.extract_edge_provenance`.
    Separate function so callers can distinguish "no legacy data"
    (every value None) from "validation failure" (one or more values
    rejected).
    """
    return {field: properties.get(field) for field in EDGE_PROVENANCE_FIELDS}


def _has_any_legacy_provenance(legacy: dict[str, Any]) -> bool:
    """``True`` when at least one legacy key is populated."""
    return any(value is not None for value in legacy.values())


def _migrate_one_edge(
    store: GraphStore,
    edge: dict[str, Any],
    *,
    dry_run: bool,
    event_log: EventLog | None,
    report: MigrateProvenanceReport,
) -> None:
    """Process one candidate edge in-place; update ``report`` counters."""
    properties = dict(edge.get("properties") or {})
    legacy = _extract_legacy_provenance(properties)
    if not _has_any_legacy_provenance(legacy):
        report.edges_no_legacy_provenance += 1
        return

    # Validate legacy values up-front — surface malformed values as a
    # parse_error event rather than swallowing them on the way to the
    # write path.  emit-then-skip mirrors the dispatcher's "loud on
    # bad data" contract.
    try:
        validate_edge_provenance(**legacy)
    except (TypeError, ValueError) as exc:
        report.edges_malformed += 1
        emit_extraction_failure(
            event_log=event_log,
            extractor_id="trellis.admin.migrate_provenance",
            extractor_tier="deterministic",
            failure_kind="parse_error",
            source_hint=edge.get("edge_type"),
            error_class=type(exc).__name__,
            error_excerpt=str(exc),
        )
        logger.warning(
            "migrate_provenance_malformed_legacy",
            edge_id=edge.get("edge_id"),
            error=str(exc),
        )
        return

    if dry_run:
        report.edges_migrated += 1
        return

    # Strip the legacy keys out of properties on rewrite so the JSON
    # blob isn't carrying duplicate state.  Greenfield directive: the
    # typed columns are the source of truth post-migration.
    for key in EDGE_PROVENANCE_FIELDS:
        properties.pop(key, None)

    try:
        store.upsert_edge(
            edge["source_id"],
            edge["target_id"],
            edge["edge_type"],
            properties=properties,
            **legacy,
        )
        report.edges_migrated += 1
    except Exception as exc:
        report.errors.append(
            f"edge:{edge.get('edge_id')}: upsert failed: "
            f"{type(exc).__name__}: {exc}"
        )
        logger.exception(
            "migrate_provenance_upsert_failed",
            edge_id=edge.get("edge_id"),
        )


def _iter_all_edges(store: GraphStore) -> list[dict[str, Any]]:
    """Walk every current edge in the store.

    Uses outgoing-edges-per-node so each edge is visited exactly once
    (a→b is outgoing from a, incoming to b — counting both would
    double).  Mirrors the strategy in
    :class:`trellis.migrate.GraphMigrator`.
    """
    nodes = store.query(limit=10_000_000)  # operational cap; same as graph_migrator
    seen: set[str] = set()
    edges: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node["node_id"])
        for edge in store.get_edges(node_id, direction="outgoing"):
            edge_id = str(edge.get("edge_id"))
            if edge_id in seen:
                continue
            seen.add(edge_id)
            edges.append(edge)
    return edges


def _commit_batch_if_supported(store: GraphStore) -> None:  # noqa: ARG001
    """Flush pending writes when the backend exposes a transaction handle.

    The SQLite/Postgres ``upsert_edge`` already commits per call (the
    ``commit=True`` default).  This function is a no-op for those
    backends — kept as a hook so future backends with batch semantics
    can override.  Intentionally permissive; we don't want migration
    to fail because a backend lacks an explicit flush hook.

    Args:
        store: Retained for the future signature; unused today.
    """
    # Nothing portable today.  Future: detect a ``flush()`` /
    # ``transaction()`` method and call it.
    return


def run_migrate_provenance(
    store: GraphStore,
    *,
    dry_run: bool,
    batch_size: int,
    event_log: EventLog | None = None,
) -> MigrateProvenanceReport:
    """Programmatic entry point for ``trellis admin migrate-provenance``.

    Pulled out of the Typer wrapper so unit tests can exercise the
    full state machine without touching the CLI parser.  Returns the
    final report; raises :class:`MigrationDriftError` when the
    malformed-edge rate exceeds :data:`_DRIFT_THRESHOLD`.
    """
    report = MigrateProvenanceReport(dry_run=dry_run, batch_size=batch_size)
    all_edges = _iter_all_edges(store)

    pending_in_batch = 0
    for edge in all_edges:
        report.edges_scanned += 1
        if not _all_provenance_columns_null(edge):
            report.edges_already_migrated += 1
            continue
        _migrate_one_edge(
            store,
            edge,
            dry_run=dry_run,
            event_log=event_log,
            report=report,
        )
        pending_in_batch += 1
        if pending_in_batch >= batch_size:
            _commit_batch_if_supported(store)
            pending_in_batch = 0

    if pending_in_batch:
        _commit_batch_if_supported(store)

    if report.edges_scanned:
        report.drift_rate = report.edges_malformed / report.edges_scanned

    report.completed_at = time.time()

    if (
        report.edges_scanned > 0
        and report.drift_rate > _DRIFT_THRESHOLD
    ):
        raise MigrationDriftError(
            malformed_count=report.edges_malformed,
            scanned=report.edges_scanned,
            threshold=_DRIFT_THRESHOLD,
        )

    return report


def _print_text_report(report: MigrateProvenanceReport) -> None:
    """Human-readable summary.  JSON shape lives in :meth:`to_payload`."""
    mode = "dry-run" if report.dry_run else "applied"
    console.print(
        f"[bold]migrate-provenance ({mode})[/bold]: "
        f"scanned={report.edges_scanned} migrated={report.edges_migrated} "
        f"already-migrated={report.edges_already_migrated} "
        f"no-legacy={report.edges_no_legacy_provenance} "
        f"malformed={report.edges_malformed}"
    )
    if report.errors:
        console.print(f"[red]errors ({len(report.errors)}):[/red]")
        for err in report.errors[:_ERROR_PREVIEW_LIMIT]:
            console.print(f"  [red]{err}[/red]")
        if len(report.errors) > _ERROR_PREVIEW_LIMIT:
            extra = len(report.errors) - _ERROR_PREVIEW_LIMIT
            console.print(f"  [red]… and {extra} more[/red]")


def migrate_provenance_command(
    dry_run: bool,
    batch_size: int,
    output_format: str,
) -> None:
    """CLI body — wraps :func:`run_migrate_provenance` with output + exit codes."""
    store = get_graph_store()
    try:
        event_log: EventLog | None = get_event_log()
    except Exception:
        event_log = None

    try:
        report = run_migrate_provenance(
            store,
            dry_run=dry_run,
            batch_size=batch_size,
            event_log=event_log,
        )
    except MigrationDriftError as exc:
        if output_format == "json":
            # On drift we still emit the partial report so operators
            # can see exactly what tripped the gate.
            payload = {
                "error": "drift_threshold_exceeded",
                "message": str(exc),
                "malformed_count": exc.malformed_count,
                "scanned": exc.scanned,
                "threshold": exc.threshold,
                "rate": exc.rate,
            }
            console.print(json.dumps(payload, indent=2))
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=_EXIT_RUNTIME) from exc
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("migrate_provenance_failed")
        if output_format == "json":
            console.print(
                json.dumps(
                    {
                        "error": "store_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                    indent=2,
                )
            )
        else:
            console.print(f"[red]store error: {type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(code=_EXIT_STORE) from exc

    if output_format == "json":
        console.print(json.dumps(report.to_payload(), indent=2, default=str))
    else:
        _print_text_report(report)

    raise typer.Exit(code=_EXIT_OK)


def register(admin_app: typer.Typer) -> None:
    """Wire the command onto an existing ``admin`` Typer app.

    Kept as a registration hook (rather than module-level decorator)
    so the import order in :mod:`trellis_cli.admin` stays explicit.
    """

    @admin_app.command("migrate-provenance")
    def migrate_provenance(  # pragma: no cover — wrapper only
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Report what would be migrated without writing.",
        ),
        batch_size: int = typer.Option(
            1000,
            "--batch-size",
            help="Edges committed per batch.  Default 1000.",
            min=1,
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        """Lift provenance from legacy ``properties`` JSON into typed columns.

        Idempotent (re-runs are no-ops for already-migrated edges).
        Fail-loud on malformed legacy data: emits an
        ``EXTRACTION_FAILED`` event per offending row and raises a
        :class:`MigrationDriftError` when >1% of scanned edges fail.
        """
        migrate_provenance_command(dry_run, batch_size, output_format)


__all__ = [
    "MigrateProvenanceReport",
    "MigrationDriftError",
    "register",
    "run_migrate_provenance",
]
