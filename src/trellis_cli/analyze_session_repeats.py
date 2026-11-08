"""``trellis analyze session-repeats``: how often a session is re-served a sibling.

Lives beside ``analyze.py`` and is mounted by one call to :func:`register` at its
foot, as ``intent-families`` is. It is a read of the event log and the document
store. It writes nothing but the meta-Activity every analyze command records,
and ``--no-meta-trace`` skips that, leaving the stores byte-for-byte as they
were. It is a measurement and changes no serving
(:mod:`trellis.analyze.session_repeats`).

The exit code does not depend on ``--format``: nothing here exits non-zero on
one surface and not the other. A truncated scan is stated in the output on both.
The text report carries counts and an opaque session handle only, never a
session id, an item id or any document text; the JSON may carry item ids.
"""

from __future__ import annotations

import typer
from rich.markup import escape

from trellis.analyze.session_repeats import (
    STATUS_EMPTY,
    SessionRepeatsReport,
    summarize_session_repeats,
)
from trellis.retrieve.pack_builder import DEFAULT_SESSION_DEDUP_WINDOW_MINUTES
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

console = build_console()


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _render_text(report: SessionRepeatsReport) -> None:
    console.print(
        f"[bold]Session repeats[/bold] (last {report.window_days} days, "
        f"{report.window_minutes}-minute window): "
        f"{report.sessions_multi_pack} sessions with 2 or more packs, "
        f"{report.later_pack_items} later-pack items served. Measurement only.",
        soft_wrap=True,
    )
    if report.status == STATUS_EMPTY:
        console.print(f"  empty: {escape(report.empty_reason)}", soft_wrap=True)
    classes = (
        ("exact repeat", report.exact_repeat),
        ("near-duplicate sibling", report.near_duplicate),
        ("chunk sibling", report.chunk_sibling),
        ("fresh", report.fresh),
        ("unreadable", report.unreadable),
    )
    for label, tally in classes:
        console.print(
            f"  {escape(label)}: {tally.items} items, "
            f"{tally.estimated_tokens} estimated tokens"
        )
    console.print(
        f"  near-duplicate share of later-pack items: "
        f"{_pct(report.near_duplicate_share)}"
    )
    basis = report.basis
    console.print(
        f"  predicate: MinHash threshold {basis.threshold}, "
        f"{basis.shingle_size}-char shingles, {basis.num_perm} permutations, "
        f"{basis.min_shingles} shingle floor, {basis.excerpt_max_chars}-char excerpt",
        soft_wrap=True,
    )
    console.print(
        f"  packs: {report.packs_scanned} scanned, {report.packs_held_out} held out, "
        f"{report.packs_without_session} without a session, "
        f"{report.packs_without_rows} without item rows; "
        f"{report.outside_window_items} items had no earlier pack in the window"
    )
    if report.top_sessions:
        console.print("  top sessions by near-duplicate count (opaque handle, counts):")
        for row in report.top_sessions:
            console.print(
                f"    {escape(row.session)}: {row.near_duplicates} near-duplicates "
                f"of {row.later_pack_items} later-pack items over {row.packs} packs"
            )
    for note in report.notes:
        console.print(f"  [dim]note: {escape(note)}[/dim]", soft_wrap=True)


def session_repeats_command(
    days: int = typer.Option(30, "--days", min=1, help="Days of history to analyze."),
    window_minutes: int = typer.Option(
        DEFAULT_SESSION_DEDUP_WINDOW_MINUTES,
        "--window-minutes",
        min=1,
        help="How far back from a pack its session's earlier packs count "
        "(default: the session-dedup window).",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
    no_meta_trace: bool = typer.Option(
        False,
        "--no-meta-trace",
        help="Skip recording this run as a meta-Activity (Item 6 Phase 2).",
    ),
) -> None:
    """How often a later pack of a session serves a sibling of an earlier item.

    Session dedup suppresses by item id and content hash, and MinHash semantic
    dedup collapses near-duplicates within one pack. A later pack of the same
    session can therefore serve a different member of a near-duplicate cluster
    that an earlier pack already served (#1208). This reads ``PACK_ASSEMBLED``
    and the document store and classifies every later-pack item as an exact
    repeat, a near-duplicate sibling (the pack builder's own MinHash predicate),
    a chunk sibling, fresh, or unreadable, with counts and estimated tokens per
    class, so a fix can be priced before anyone builds one.

    Read-only; held-out packs are dropped. The text report prints counts and an
    opaque session handle, never a session id, item id or document text.
    """
    registry = _get_registry()
    with wrap_cli_meta_analysis(
        agent_suffix="analyze",
        analyzer_name="cli.analyze.session_repeats",
        disabled=no_meta_trace,
    ) as _meta_record:
        report = summarize_session_repeats(
            registry.operational.event_log,
            registry.knowledge.document_store,
            days=days,
            window_minutes=window_minutes,
        )
        if _meta_record.enabled and report.later_pack_items > 0:
            _meta_record.produced_finding(
                f"session-repeats-report-d{days}-w{window_minutes}",
                finding_type="SessionRepeatsReport",
            )

    if output_format == "json":
        emit_json(report.model_dump(mode="json"))
    else:
        _render_text(report)


def register(analyze_app: typer.Typer) -> None:
    """Mount ``session-repeats`` onto the ``analyze`` Typer app."""
    analyze_app.command("session-repeats")(session_repeats_command)


__all__ = ["register", "session_repeats_command"]
