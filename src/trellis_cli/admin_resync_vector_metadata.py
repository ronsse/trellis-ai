"""``trellis admin resync-vector-metadata`` — repair stale vector snapshots.

A vector row's metadata is a snapshot taken at embed time. Until #338 no
post-embed tag writer refreshed it, so every document tagged after it was
embedded left a row advertising its pre-tag state to the semantic axis. The
write-through added in #338 stops the divergence growing; this command is
the one-time repair for rows that diverged before it shipped.

Measured in production on 2026-08-25, joining ``documents`` to ``vectors``:
**45 noise-tagged documents, none of whose vector rows agreed** — 28 carried
no ``signal_quality`` at all and 17 still read ``"standard"``.

**Not ``reindex-vectors --force``.** That path exists and would also fix the
divergence, by re-embedding the whole corpus — one paid embedding call per
document to correct metadata that is sitting in the document store for free.
This command calls no embedder, needs none configured, and rewrites only
:data:`~trellis.core.vector_metadata.SYNCED_METADATA_KEYS`; the existing
embedding is carried through untouched.

Idempotent by construction: a row already in agreement is not rewritten, so
a second run reports zero repaired. That makes it safe to schedule, and
makes "did the write-through hold?" a question with an answer — a steady-
state run that reports non-zero means a writer is still bypassing it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
import typer

from trellis.core.vector_metadata import (
    SYNCED_METADATA_KEYS,
    sync_vector_metadata,
    vector_metadata_diverges,
)
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

console = build_console()
logger = structlog.get_logger(__name__)

#: Documents fetched per ``list_documents`` round-trip.
DEFAULT_BATCH_SIZE = 500


def run_resync_vector_metadata(
    registry: StoreRegistry,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Programmatic entry point — page documents, repair divergent rows.

    Per-row failures are counted rather than fatal: one unreadable vector
    row must not abort a corpus-wide repair, and re-running is always safe
    because the operation is idempotent.

    Raises:
        ValueError: when no vector store is configured — there is nothing to
            resync, and reporting "0 repaired" would be indistinguishable
            from a clean corpus.
    """
    document_store = registry.knowledge.document_store
    try:
        vector_store = registry.knowledge.vector_store
    except Exception as exc:
        msg = f"resync-vector-metadata requires a vector store: {exc}"
        raise ValueError(msg) from exc

    scanned = divergent = repaired = missing_row = errors = 0
    offset = 0
    while True:
        page_size = batch_size if limit == 0 else min(batch_size, limit - scanned)
        if page_size <= 0:
            break
        # ``include_chunks`` is named rather than defaulted (#396): this
        # walker repairs vector rows, and chunk rows are the ones that
        # have them.
        page = document_store.list_documents(
            limit=page_size, offset=offset, include_chunks=True
        )
        if not page:
            break
        offset += len(page)
        scanned += len(page)

        for doc in page:
            doc_id = doc.get("doc_id") or doc.get("id")
            if not doc_id:
                continue
            metadata = doc.get("metadata") or {}
            try:
                row = vector_store.get(str(doc_id))
            # AGGREGATE: a per-row read failure is counted and logged so one
            # bad row doesn't abort the repair; the summary surfaces it.
            except Exception:
                logger.warning(
                    "resync_vector_metadata_read_failed", doc_id=doc_id, exc_info=True
                )
                errors += 1
                continue
            if row is None:
                # Never embedded — nothing to disagree with. Not an error:
                # embed-on-ingest is opt-in and structural rows have no
                # document at all.
                missing_row += 1
                continue
            if not vector_metadata_diverges(metadata, row.get("metadata")):
                continue
            divergent += 1
            if dry_run:
                continue
            if sync_vector_metadata(vector_store, str(doc_id), metadata):
                repaired += 1
            else:
                # sync_vector_metadata is fail-soft and already logged the
                # reason; count it so a partial repair is never reported as
                # a complete one.
                errors += 1

    summary = {
        "status": "ok",
        "scanned": scanned,
        "divergent": divergent,
        "repaired": repaired,
        "no_vector_row": missing_row,
        "errors": errors,
        "keys": list(SYNCED_METADATA_KEYS),
        "dry_run": dry_run,
    }
    logger.info("resync_vector_metadata_completed", **summary)
    return summary


def register(admin_app: typer.Typer) -> None:
    """Attach the ``resync-vector-metadata`` command to the admin Typer app."""

    @admin_app.command("resync-vector-metadata")
    def resync_vector_metadata(
        batch_size: int = typer.Option(
            DEFAULT_BATCH_SIZE,
            "--batch-size",
            min=1,
            help="Documents per list_documents page.",
        ),
        limit: int = typer.Option(
            0,
            "--limit",
            min=0,
            help="Stop after scanning this many documents (0 = all).",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Count divergent rows without rewriting any.",
        ),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json"
        ),
    ) -> None:
        """Re-sync stale vector-row metadata from the document store."""
        try:
            summary = run_resync_vector_metadata(
                _get_registry(),
                batch_size=batch_size,
                limit=limit,
                dry_run=dry_run,
            )
        except ValueError as exc:
            if output_format == "json":
                print(json.dumps({"status": "error", "message": str(exc)}))
            else:
                console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=EXIT_INTERNAL) from exc

        if output_format == "json":
            print(json.dumps(summary))
        else:
            verb = "would repair" if dry_run else "repaired"
            console.print(
                f"[green]Vector metadata resync:[/green] {verb} "
                f"{summary['divergent'] if dry_run else summary['repaired']} of "
                f"{summary['scanned']} scanned "
                f"({summary['no_vector_row']} not embedded, "
                f"{summary['errors']} errors)"
            )
        raise typer.Exit(code=EXIT_OK)
