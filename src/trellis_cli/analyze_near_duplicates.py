"""``trellis analyze near-duplicates``: paraphrases ingest's lexical dedup misses.

Mounted by one call to :func:`register` at the foot of ``analyze.py``, as
``session-repeats`` is. It reads the document and vector stores and writes
nothing but the meta-Activity every analyze command records; ``--no-meta-trace``
skips that, leaving the stores byte-for-byte as they were. It is a measurement
and changes no serving (:mod:`trellis.analyze.near_duplicates`).

The exit code does not depend on ``--format``: nothing here exits non-zero on
one surface and not the other. Both surfaces carry document ids in the pair
sample (never document text); the text report escapes them for Rich.
"""

from __future__ import annotations

import typer
from rich.markup import escape

from trellis.analyze.near_duplicates import (
    DEFAULT_MAX_DOCS,
    DEFAULT_NEIGHBORS,
    DEFAULT_THRESHOLDS,
    STATUS_EMPTY,
    NearDuplicatesReport,
    summarize_near_duplicates,
)
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()

#: Pairs printed per threshold in the text report (JSON carries the full sample).
_TEXT_SAMPLE = 3


def _render_text(report: NearDuplicatesReport) -> None:
    console.print(
        f"[bold]Near-duplicates[/bold]: {report.documents_considered} whole "
        f"documents considered, {report.embedded} embedded. Measurement only.",
        soft_wrap=True,
    )
    if report.status == STATUS_EMPTY:
        console.print(f"  empty: {escape(report.empty_reason)}", soft_wrap=True)
    skipped = report.skipped
    console.print(
        f"  skipped: {skipped.archived} archived, {skipped.superseded} superseded, "
        f"{skipped.noise} noise, {skipped.unreadable} unreadable; "
        f"{report.no_embedding} without an embedding, "
        f"{report.embedding_unusable} with an unusable one",
        soft_wrap=True,
    )
    if report.cap_hit:
        console.print(
            f"  cap hit: {report.documents_considered} of "
            f"{report.documents_in_store} documents read (--max-docs "
            f"{report.max_docs})",
            soft_wrap=True,
        )
    basis = report.basis
    console.print(
        f"  lexical predicate: ingest MinHash threshold {basis.minhash_threshold}, "
        f"{basis.minhash_shingle_size}-char shingles; neighbours per probe "
        f"{basis.neighbors}",
        soft_wrap=True,
    )
    for row in report.thresholds:
        console.print(
            f"  cosine >= {row.threshold}: {row.pairs} pairs, "
            f"{row.lexical_pairs} lexical (MinHash already catches), "
            f"{row.residue_pairs} paraphrase residue; "
            f"{row.documents_in_residue} documents in residue, "
            f"{row.clusters} clusters, largest {row.largest_cluster}",
            soft_wrap=True,
        )
        for group in row.by_group:
            console.print(
                f"    {escape(group.group)}: {group.documents_in_residue} of "
                f"{group.documents_embedded} embedded documents in residue",
                soft_wrap=True,
            )
        for pair in row.residue_sample[:_TEXT_SAMPLE]:
            console.print(
                f"    residue {pair.cosine}: {escape(pair.doc_id)} ~ "
                f"{escape(pair.other_doc_id)}",
                soft_wrap=True,
            )
    for limit in report.cannot_tell:
        console.print(f"  [dim]cannot tell: {escape(limit)}[/dim]", soft_wrap=True)
    for note in report.notes:
        console.print(f"  [dim]note: {escape(note)}[/dim]", soft_wrap=True)


def near_duplicates_command(
    threshold: list[float] | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--threshold",
        min=0.0,
        max=1.0,
        help="Cosine floor to report; repeat for several (default 0.90 and 0.95).",
    ),
    max_docs: int = typer.Option(
        DEFAULT_MAX_DOCS,
        "--max-docs",
        min=2,
        help="Most whole documents read, newest first; a hit cap is reported.",
    ),
    neighbors: int = typer.Option(
        DEFAULT_NEIGHBORS,
        "--neighbors",
        min=1,
        help="top_k of each document's similarity probe.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """Count stored documents that say the same thing in different words.

    Ingest dedup is an exact content hash plus character-trigram MinHash, so a
    reworded fact passes it. This pairs whole documents whose stored
    embeddings are close (cosine at or above each threshold) and splits the
    pairs into those the shipped MinHash predicate already catches and the
    paraphrase residue, with clusters and a per-source breakdown.

    Read-only. Archived, superseded and noise documents are skipped and
    counted; so are documents with no embedding. Thresholds are uncalibrated
    and a pair may be an update or a contradiction rather than a repeat.
    """
    registry = _get_registry()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.near_duplicates",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = summarize_near_duplicates(
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
            thresholds=tuple(threshold) if threshold else DEFAULT_THRESHOLDS,
            max_docs=max_docs,
            neighbors=neighbors,
        )
        if _meta_record.enabled and report.embedded > 0:
            _meta_record.produced_finding(
                f"near-duplicates-report-n{max_docs}-k{neighbors}",
                finding_type="NearDuplicatesReport",
            )

    if output_format == "json":
        emit_json(report.model_dump(mode="json"))
    else:
        _render_text(report)


def register(analyze_app: typer.Typer) -> None:
    """Mount ``near-duplicates`` onto the ``analyze`` Typer app."""
    analyze_app.command("near-duplicates")(near_duplicates_command)


__all__ = ["near_duplicates_command", "register"]
