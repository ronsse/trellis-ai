"""``trellis classify backfill`` — re-tag documents already in the store.

Classify-on-write (``TRELLIS_ENABLE_CLASSIFY_ON_INGEST``, see
:mod:`trellis.classify.ingest`) only covers documents written *after* it was
enabled, and tags written at any point drift as the keyword vocabulary and the
graph around a document change. This command is the explicit,
operator-driven backfill for everything already stored: it pages the
DocumentStore, re-runs the deterministic tagging pipeline over every item whose
``content_tags.classified_at`` is missing or older than ``--max-age-days``, and
writes the fresh tags back — the same
:func:`~trellis.classify.refresh.reclassify_item` core the programmatic path
uses, so the two cannot drift.

Like ``trellis extract traces`` and ``trellis admin reindex-vectors``, this
command does **not** require the ingest-time feature flag — invoking it *is*
the opt-in. It never deletes tags: an item the pipeline produces no signal for
keeps whatever it had.

``--include-domain`` is the one dangerous switch and is off by default. See
:func:`~trellis.classify.refresh.reclassify_item` for why re-deriving the
``domain`` facet deterministically can hide a document from domain-scoped
retrieval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
import typer
from rich.console import Console

from trellis.classify.refresh import DEFAULT_PAGE_SIZE, reclassify_stale
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK
from trellis_cli.output import emit_json
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.classify.refresh import BatchRefreshResult

logger = structlog.get_logger(__name__)

classify_app = typer.Typer(no_args_is_help=True)
console = Console()

#: Warnings go to stderr so ``--format json`` stdout stays parseable.
err_console = Console(stderr=True)


@classify_app.callback()
def _classify() -> None:
    """Backfill and refresh content tags.

    Typer folds a single-command group into a bare command unless the group
    declares a callback — this one keeps ``trellis classify backfill`` spelled
    the way the docs spell it, and leaves room for sibling commands.
    """


@classify_app.command("backfill")
def backfill(
    max_age_days: int = typer.Option(
        30,
        "--max-age-days",
        min=0,
        help=(
            "Re-tag items whose tags are older than this (0 = re-tag every "
            "scanned item regardless of freshness)."
        ),
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help="Stop after scanning this many documents (0 = all).",
    ),
    page_size: int = typer.Option(
        DEFAULT_PAGE_SIZE,
        "--page-size",
        min=1,
        help="Documents fetched per store round-trip.",
    ),
    include_domain: bool = typer.Option(
        False,
        "--include-domain",
        help=(
            "DANGEROUS: let the deterministic classifiers (re)assign the "
            "'domain' facet. 'domain' is the only facet that hard-excludes a "
            "document from a domain-scoped query on mismatch, so a wrong "
            "value hides content instead of just re-ranking it. Only use this "
            "with a pipeline you trust to compute domain."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would change without writing tags or emitting events.",
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: text or json"
    ),
) -> None:
    """Backfill / refresh content tags for stored documents."""
    registry = _get_registry()

    if include_domain:
        err_console.print(
            "[yellow]--include-domain is set: the deterministic classifiers "
            "may (re)assign the hard-excluding 'domain' facet.[/yellow]"
        )

    try:
        result = reclassify_stale(
            pipeline=registry.build_ingestion_pipeline(),
            document_store=registry.knowledge.document_store,
            # Dry runs stay audit-silent: TAGS_REFRESHED claims a write that
            # did not happen.
            event_log=None if dry_run else registry.operational.event_log,
            max_age_days=max_age_days,
            limit=limit,
            page_size=page_size,
            include_domain=include_domain,
            dry_run=dry_run,
        )
    except ValueError as exc:
        # Raised by the pipeline factory on a malformed classify: block in
        # config.yaml — an operator error, not a bug. Report and exit.
        if output_format == "json":
            emit_json({"status": "error", "message": str(exc)})
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=EXIT_INTERNAL) from exc

    summary = _summary(result, dry_run=dry_run, include_domain=include_domain)
    logger.info("classify_backfill_completed", **summary)

    if output_format == "json":
        emit_json(summary)
    else:
        _render_text(summary)
    raise typer.Exit(code=EXIT_OK)


def _summary(
    result: BatchRefreshResult,
    *,
    dry_run: bool,
    include_domain: bool,
) -> dict[str, object]:
    """Flat JSON-friendly view of a :class:`BatchRefreshResult`."""
    return {
        "status": "ok",
        "scanned": result.scanned,
        "refreshed": result.refreshed,
        "skipped_fresh": result.skipped_fresh,
        "skipped_no_signal": result.skipped_no_signal,
        "skipped_missing_content": result.skipped_missing_content,
        "dry_run": dry_run,
        "include_domain": include_domain,
        "item_ids_refreshed": list(result.item_ids_refreshed),
    }


def _render_text(summary: dict[str, object]) -> None:
    """Human-readable rendering of the backfill summary."""
    verb = "would re-tag" if summary["dry_run"] else "re-tagged"
    console.print(
        f"[green]Classify backfill:[/green] {verb} {summary['refreshed']} of "
        f"{summary['scanned']} scanned "
        f"({summary['skipped_fresh']} still fresh, "
        f"{summary['skipped_no_signal']} no signal, "
        f"{summary['skipped_missing_content']} empty)"
    )
    if summary["dry_run"]:
        console.print("  [yellow]dry-run — nothing written, no events[/yellow]")
