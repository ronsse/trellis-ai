"""``trellis admin reindex-vectors`` — backfill document embeddings.

The embed-on-ingest hook (``TRELLIS_ENABLE_EMBED_ON_INGEST``) only covers
documents written *after* it was enabled. This command is the explicit,
operator-driven backfill for everything already in the document store:
it walks ``list_documents`` pages, embeds each document through the
registry's configured ``embedding_fn``, and bulk-upserts vectors keyed
by ``doc_id`` — the same row shape the live hook writes, via the same
:func:`~trellis.retrieve.embed_ingest_hook.build_vector_row` core, so
the two paths cannot drift.

Like ``trellis extract traces``, this command does **not** require the
feature flag — invoking it is the opt-in. It *does* require an
``embeddings:`` block (or ``TRELLIS_EMBEDDING_FN``) and a configured
vector store, and exits loudly when either is missing rather than
silently indexing nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
import typer
from rich.markup import escape

from trellis.errors import StoreError, TrellisError
from trellis.retrieve.embed_ingest_hook import build_vector_row
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK
from trellis_cli.output import build_console
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry

console = build_console()
logger = structlog.get_logger(__name__)

#: Documents fetched (and vectors upserted) per round-trip.
DEFAULT_BATCH_SIZE = 100


def _resolve_vector_store(registry: StoreRegistry) -> VectorStore | None:
    """``registry.knowledge.vector_store``, describing an untyped resolve failure.

    A real registry's ``vector_store`` property always exists (it is a
    normal ``@property`` on ``_KnowledgePlane`` — see
    ``trellis.stores.registry``), so the ``getattr(..., None)`` this
    replaced never actually absorbed an ``AttributeError`` here; it only
    hid whatever OTHER exception the property's own backend
    instantiation raised (a bad backend config, an unreachable store).

    A typed :class:`~trellis.errors.TrellisError` (a
    :class:`~trellis.errors.ConfigError`, say) already reaches the root
    CLI boundary (:class:`trellis_cli.main._BoundaryGroup`) unharmed on
    base — that boundary maps it to exit 5 (``EXIT_STORE``) with the
    standard sanitized JSON envelope, keeping the error's own
    ``error_code`` and, for a ``ConfigError``, its ``setting`` — so it is
    re-raised unchanged here rather than re-wrapped. Only an *untyped*
    exception (one the boundary cannot already describe) escaped as a
    bare traceback with exit 1 instead of this command's own error path;
    it is wrapped in a :class:`~trellis.errors.StoreError` naming the
    cause's type, never its message text, so it reaches the same boundary
    and exits 5 too.

    Raises:
        TrellisError: unchanged, when resolving the property raises one.
        StoreError: when resolving the property raises anything else.
    """
    try:
        return registry.knowledge.vector_store
    except TrellisError:
        raise
    except Exception as exc:
        msg = (
            f"reindex-vectors could not resolve the vector store ({type(exc).__name__})"
        )
        raise StoreError(msg, store="vector") from exc


def run_reindex_vectors(
    registry: StoreRegistry,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Programmatic entry point — page, embed, bulk-upsert.

    Skips content-less documents and (unless ``force``) documents that
    already have a vector. Per-document embed failures are counted and
    logged, never fatal — rerunning is always safe because rows are
    keyed by ``doc_id``. ``dry_run`` counts candidates without calling
    the embedder.

    Raises:
        ValueError: when the embedder or vector store is unconfigured.
        TrellisError: when resolving the vector store itself raises one
            (a broken backend config, not simply "none configured"),
            unchanged; see :func:`_resolve_vector_store`.
        StoreError: when resolving the vector store raises anything
            else.
    """
    embedding_fn = registry.embedding_fn
    vector_store = _resolve_vector_store(registry)
    document_store = registry.knowledge.document_store

    if embedding_fn is None or vector_store is None:
        missing = []
        if embedding_fn is None:
            missing.append(
                "embeddings config (embeddings: block or TRELLIS_EMBEDDING_FN)"
            )
        if vector_store is None:
            missing.append("vector store")
        msg = f"reindex-vectors requires: {', '.join(missing)}"
        raise ValueError(msg)

    scanned = embedded = skipped_existing = skipped_empty = errors = 0
    offset = 0
    while True:
        page_size = batch_size if limit == 0 else min(batch_size, limit - scanned)
        if page_size <= 0:
            break
        # ``include_chunks`` is named rather than defaulted (#396): this
        # walker embeds, and chunk rows are what carry the embeddings.
        page = document_store.list_documents(
            limit=page_size, offset=offset, include_chunks=True
        )
        if not page:
            break
        offset += len(page)
        scanned += len(page)

        rows: list[dict[str, Any]] = []
        for doc in page:
            doc_id = doc["doc_id"]
            content = doc.get("content") or ""
            if not content.strip():
                skipped_empty += 1
                continue
            if not force and vector_store.get(doc_id) is not None:
                skipped_existing += 1
                continue
            if dry_run:
                embedded += 1
                continue
            try:
                rows.append(
                    build_vector_row(
                        doc_id,
                        content,
                        doc.get("metadata"),
                        embedding_fn,
                        created_at=doc.get("created_at"),
                    )
                )
            # AGGREGATE: per-document embed failures are counted and
            # logged so one unembeddable document doesn't abort the
            # backfill; the summary surfaces the error count.
            except Exception:
                logger.warning(
                    "reindex_vectors_embed_failed", doc_id=doc_id, exc_info=True
                )
                errors += 1

        if rows:
            vector_store.upsert_bulk(rows)
            embedded += len(rows)

    summary = {
        "status": "ok",
        "scanned": scanned,
        "embedded": embedded,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
        "errors": errors,
        "dry_run": dry_run,
    }
    logger.info("reindex_vectors_completed", **summary)
    return summary


def register(admin_app: typer.Typer) -> None:
    """Attach the ``reindex-vectors`` command to the admin Typer app."""

    @admin_app.command("reindex-vectors")
    def reindex_vectors(
        batch_size: int = typer.Option(
            DEFAULT_BATCH_SIZE,
            "--batch-size",
            min=1,
            help="Documents per page / vectors per bulk upsert.",
        ),
        limit: int = typer.Option(
            0,
            "--limit",
            min=0,
            help="Stop after scanning this many documents (0 = all).",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Re-embed documents that already have a vector.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Count what would be embedded without calling the embedder.",
        ),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json"
        ),
    ) -> None:
        """Backfill vector embeddings for stored documents."""
        try:
            summary = run_reindex_vectors(
                _get_registry(),
                batch_size=batch_size,
                limit=limit,
                force=force,
                dry_run=dry_run,
            )
        except ValueError as exc:
            # A ``StoreError`` from ``_resolve_vector_store`` (or any other
            # ``TrellisError``) is deliberately left uncaught here: it
            # propagates to the root CLI boundary
            # (``trellis_cli.main._BoundaryGroup``), which renders it with
            # the standard sanitized JSON envelope and exits ``EXIT_STORE``
            # (5) regardless of ``--format``. A local catch pre-empted that
            # boundary and printed the raw, unsanitized message (#839).
            if output_format == "json":
                print(json.dumps({"status": "error", "message": str(exc)}))
            else:
                console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
            raise typer.Exit(code=EXIT_INTERNAL) from exc

        if output_format == "json":
            print(json.dumps(summary))
        else:
            verb = "would embed" if dry_run else "embedded"
            console.print(
                f"[green]Reindex complete:[/green] {verb} {summary['embedded']} of "
                f"{summary['scanned']} scanned "
                f"({summary['skipped_existing']} already indexed, "
                f"{summary['skipped_empty']} empty, {summary['errors']} errors)"
            )
        raise typer.Exit(code=EXIT_OK)
