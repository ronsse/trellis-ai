"""``trellis admin generate-proposals`` / ``list-proposals`` / ``show-proposal``.

Item 7 Phase 1 of the self-improvement program — surfaces the
:class:`~trellis_workers.code_authoring.ProposalGenerator` (Phase 0,
PR #134) through the CLI so an operator can:

* ``generate-proposals`` — kick off a generator run over a rolling
  window. Each proposal that was newly drafted (not just updated) is
  reported. ``--format json`` emits a machine-readable payload per the
  project hard rule.
* ``list-proposals`` — query the EventLog for recent
  ``PROPOSAL_DRAFTED`` events (most recent ``--limit`` rows, or rows
  ``--since`` a given ISO-8601 timestamp).
* ``show-proposal`` — look up a single proposal by ``proposal_id`` and
  print its markdown. NOTE: Phase 0 persists only the
  ``markdown_preview`` (500-char slice) on the EventLog payload —
  full markdown rendering lands when the on-disk
  ``agent-proposals/<proposal_id>/proposal.md`` directory does
  (cohort 2). Until then ``show-proposal`` prints the preview and a
  banner noting the truncation so operators aren't surprised.

Exit codes follow :mod:`trellis_cli.exit_codes`:

* :data:`EXIT_OK` (0) — success, including no-op runs and "no rows" for
  ``list-proposals``.
* :data:`EXIT_INTERNAL` (1) — uncaught exception. Operators should
  file a bug.
* :data:`EXIT_STORE` (5) — backend / EventLog failure during read.

This module is the first CLI surface to actually consume the
:mod:`trellis_cli.exit_codes` constants rather than literal ints
(see PR #123 which introduced them).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
import typer
from rich.console import Console

from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_OK, EXIT_STORE
from trellis_cli.stores import _get_registry, get_event_log

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event
    from trellis_workers.code_authoring import Proposal

logger = structlog.get_logger(__name__)
console = Console()

#: Default rolling window (in hours) for ``generate-proposals``. Matches
#: :data:`trellis_workers.code_authoring.DEFAULT_WINDOW` but expressed in
#: hours for the CLI surface — operators reason in hours / days, not
#: ``timedelta``s. Keep the default loud (24h) so a no-args invocation
#: produces a sensible window.
_DEFAULT_WINDOW_HOURS: int = 24

#: Default cap on ``list-proposals`` rows. Mirrors the EventLog default
#: (100) so an operator running with no flags gets a useful window.
_DEFAULT_LIST_LIMIT: int = 50

#: Hard upper bound on ``list-proposals --limit``. Stops accidental
#: ``--limit 10_000_000`` from streaming the whole EventLog into a
#: ``rich.Table`` render.
_LIST_LIMIT_CEILING: int = 1000

#: Title-line prefix used by ``render_markdown``. Used to extract the
#: ``source_file`` for the JSON output of ``list-proposals`` — Phase 0
#: doesn't put ``source_file`` on the event payload, but the rendered
#: markdown title is deterministic. Format is::
#:
#:     # Proposal: address <failure_class> in <source_file>
#:
#: Falls back to ``None`` when the marker isn't present (e.g., a future
#: WELL_KNOWN_CANDIDATE-derived proposal whose title shape diverges).
_MARKDOWN_TITLE_PREFIX: str = "# Proposal: address "
_MARKDOWN_TITLE_INFIX: str = " in "


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_source_file_from_preview(preview: str) -> str | None:
    """Extract the ``source_file`` token from a rendered markdown preview.

    The first line of every Phase 0 proposal renders as
    ``# Proposal: address <failure_class> in <source_file>``. We split
    on the well-known markers rather than regex to keep the parse cheap
    and predictable. Returns ``None`` when the marker isn't present so
    callers can fall through to ``"(unknown)"`` for display.
    """
    first_line = preview.split("\n", 1)[0] if preview else ""
    if not first_line.startswith(_MARKDOWN_TITLE_PREFIX):
        return None
    body = first_line[len(_MARKDOWN_TITLE_PREFIX) :]
    if _MARKDOWN_TITLE_INFIX not in body:
        return None
    return body.split(_MARKDOWN_TITLE_INFIX, 1)[1].strip() or None


def _event_to_listing_row(event: Event) -> dict[str, Any]:
    """Project a ``PROPOSAL_DRAFTED`` event to the listing-row shape."""
    payload = event.payload or {}
    preview = str(payload.get("markdown_preview") or "")
    return {
        "proposal_id": str(payload.get("proposal_id") or event.entity_id or ""),
        "cluster_signature": str(payload.get("cluster_signature") or ""),
        "source_file": _parse_source_file_from_preview(preview),
        "source_event_count": int(payload.get("source_event_count") or 0),
        "generated_at": event.occurred_at.isoformat(),
        "event_id": event.event_id,
    }


def _proposal_to_summary_row(proposal: Proposal) -> dict[str, Any]:
    """Project a freshly-drafted :class:`Proposal` to the summary row shape.

    Used by ``generate-proposals --format json`` so each newly drafted
    proposal carries enough identity for an operator to look it up with
    ``show-proposal`` in a follow-up call.
    """
    source_file = _parse_source_file_from_preview(proposal.markdown)
    return {
        "proposal_id": proposal.proposal_id,
        "cluster_signature": proposal.cluster_signature,
        "source_file": source_file,
        "source_event_count": len(proposal.source_event_ids),
        "generated_at": proposal.generated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# generate-proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GenerateOutcome:
    """Result of one ``generate-proposals`` run.

    Carries enough state for both the text and JSON renderers.
    ``drafted_proposals`` is the subset of ``proposals_returned`` that
    triggered a fresh ``PROPOSAL_DRAFTED`` event (vs.
    ``PROPOSAL_UPDATED`` on an already-drafted ID).
    """

    proposals_returned: int
    drafted_proposals: list[dict[str, Any]]
    window_hours: float


def _run_generate_proposals(
    *,
    window_hours: int,
    event_log_before: list[Event],
) -> _GenerateOutcome:
    """Programmatic entry point — pulled out for testability.

    ``event_log_before`` is the list of ``PROPOSAL_DRAFTED`` events
    present *before* this run starts. We diff the post-run event set
    against this list to figure out which proposals were newly drafted
    (vs. PROPOSAL_UPDATED). Pulling the snapshot in a helper keeps the
    diff window deterministic in tests.
    """
    # Imported lazily so the CLI module doesn't pull workers deps at
    # import time — the same pattern admin.py uses for analyze imports.
    from trellis_workers.code_authoring import ProposalGenerator  # noqa: PLC0415

    registry = _get_registry()
    window = timedelta(hours=window_hours)
    generator = ProposalGenerator(registry, window=window)
    proposals = generator.run()

    # Diff against the pre-run snapshot to identify NEWLY drafted IDs.
    # A proposal whose ID was already in the snapshot fired
    # PROPOSAL_UPDATED instead of PROPOSAL_DRAFTED — surface only the
    # truly new ones in the "drafted" tally so operators can see what
    # changed.
    pre_drafted_ids = {
        str((e.payload or {}).get("proposal_id") or e.entity_id or "")
        for e in event_log_before
    }
    drafted_proposals = [
        _proposal_to_summary_row(p)
        for p in proposals
        if p.proposal_id not in pre_drafted_ids
    ]
    return _GenerateOutcome(
        proposals_returned=len(proposals),
        drafted_proposals=drafted_proposals,
        window_hours=float(window_hours),
    )


def _print_generate_outcome_text(outcome: _GenerateOutcome) -> None:
    """Human-readable summary for ``generate-proposals`` (text mode)."""
    drafted = len(outcome.drafted_proposals)
    updated = outcome.proposals_returned - drafted
    console.print(
        f"[bold]generate-proposals[/bold]: window={outcome.window_hours}h "
        f"proposals_returned={outcome.proposals_returned} "
        f"drafted={drafted} updated={updated}"
    )
    if not outcome.drafted_proposals:
        console.print(
            "[dim]No newly drafted proposals — re-runs of an already-"
            "surfaced cluster emit PROPOSAL_UPDATED.[/dim]"
        )
        return
    for row in outcome.drafted_proposals:
        source = row["source_file"] or "(unknown source)"
        console.print(
            f"  [green]drafted[/green] {row['proposal_id'][:16]}… "
            f"signature={row['cluster_signature'][:16]}… "
            f"source={source} events={row['source_event_count']}"
        )


def _print_generate_outcome_json(outcome: _GenerateOutcome) -> None:
    """JSON output for ``generate-proposals`` — single line per project rule."""
    payload = {
        "proposals": outcome.drafted_proposals,
        "proposals_returned": outcome.proposals_returned,
        "window_hours": outcome.window_hours,
    }
    # Plain ``print`` (not ``console.print``) so Rich's terminal-width
    # soft-wrap never splits the JSON across lines — see admin.py
    # for the existing convention.
    print(json.dumps(payload))


def generate_proposals_command(
    *,
    window_hours: int,
    output_format: str,
    dry_run: bool,
) -> None:
    """CLI body — wraps the generator with output + exit codes."""
    if dry_run:
        # Phase 0 generator has no dry-run knob. Documented in the
        # ``--help`` of the registered command; the flag remains
        # because the original Phase 1 brief asked for it. Refuse
        # rather than silently ignoring so operators know.
        console.print(
            "[yellow]--dry-run is not supported in Phase 1: the Phase 0 "
            "ProposalGenerator does not expose a dry-run knob. Re-run "
            "without --dry-run, or pin --window-hours 0 to short-circuit "
            "every cluster outside the window.[/yellow]"
        )
        raise typer.Exit(code=EXIT_OK)

    try:
        event_log = get_event_log()
        # Snapshot the pre-run PROPOSAL_DRAFTED set so the diff is
        # deterministic. Cap at the SQL limit ceiling — we only need
        # the proposal_ids, and a million-row history will fit in
        # memory but is wasteful to read every run.
        pre_run_events = event_log.get_events(
            event_type=EventType.PROPOSAL_DRAFTED,
            limit=10_000,
        )
        outcome = _run_generate_proposals(
            window_hours=window_hours,
            event_log_before=pre_run_events,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("generate_proposals_failed")
        message = f"{type(exc).__name__}: {exc}"
        if output_format == "json":
            print(json.dumps({"error": "store_error", "message": message}))
        else:
            console.print(f"[red]store error: {message}[/red]")
        raise typer.Exit(code=EXIT_STORE) from exc

    if output_format == "json":
        _print_generate_outcome_json(outcome)
    else:
        _print_generate_outcome_text(outcome)
    raise typer.Exit(code=EXIT_OK)


# ---------------------------------------------------------------------------
# list-proposals
# ---------------------------------------------------------------------------


def _parse_since(since: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string from the CLI; return ``None`` if blank.

    Accepts both naive and tz-aware ISO-8601. Naive values are coerced
    to UTC to keep the EventLog query predictable (the EventLog stores
    tz-aware datetimes).
    """
    if not since:
        return None
    parsed = datetime.fromisoformat(since)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def list_proposals_command(
    *,
    limit: int,
    since: str | None,
    output_format: str,
) -> None:
    """CLI body for ``list-proposals``."""
    if limit < 1:
        # Typer's min= validation handles this on the registered
        # wrapper, but the programmatic entry-point may also be
        # exercised in tests; be loud either way.
        console.print(
            "[red]--limit must be a positive integer[/red]"
        )
        raise typer.Exit(code=EXIT_INTERNAL)
    capped_limit = min(limit, _LIST_LIMIT_CEILING)

    try:
        event_log = get_event_log()
        since_dt = _parse_since(since)
        events = event_log.get_events(
            event_type=EventType.PROPOSAL_DRAFTED,
            since=since_dt,
            limit=capped_limit,
            order="desc",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("list_proposals_failed")
        message = f"{type(exc).__name__}: {exc}"
        if output_format == "json":
            print(json.dumps({"error": "store_error", "message": message}))
        else:
            console.print(f"[red]store error: {message}[/red]")
        raise typer.Exit(code=EXIT_STORE) from exc

    rows = [_event_to_listing_row(event) for event in events]

    if output_format == "json":
        print(json.dumps({"proposals": rows, "count": len(rows)}))
    elif not rows:
        console.print(
            "[dim]No PROPOSAL_DRAFTED events found. Run "
            "'trellis admin generate-proposals' to surface "
            "current clusters.[/dim]"
        )
    else:
        console.print(
            f"[bold]list-proposals[/bold] (showing {len(rows)} "
            f"most-recent of cap {capped_limit})"
        )
        for row in rows:
            source = row["source_file"] or "(unknown source)"
            console.print(
                f"  {row['proposal_id'][:16]}…  "
                f"{row['generated_at']}  "
                f"events={row['source_event_count']}  "
                f"source={source}"
            )
    raise typer.Exit(code=EXIT_OK)


# ---------------------------------------------------------------------------
# show-proposal
# ---------------------------------------------------------------------------


def show_proposal_command(
    *,
    proposal_id: str,
    output_format: str,
) -> None:
    """CLI body for ``show-proposal``."""
    if not proposal_id:
        console.print("[red]proposal_id is required[/red]")
        raise typer.Exit(code=EXIT_INTERNAL)

    try:
        event_log = get_event_log()
        # ``payload_filters`` pushes the predicate into the SQL so the
        # limit cap applies after the filter — the same pattern
        # ProposalGenerator._emit_proposal_event uses for the
        # idempotency check.
        matches = event_log.get_events(
            event_type=EventType.PROPOSAL_DRAFTED,
            payload_filters={"proposal_id": proposal_id},
            limit=1,
            order="desc",
        )
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("show_proposal_failed")
        message = f"{type(exc).__name__}: {exc}"
        if output_format == "json":
            print(json.dumps({"error": "store_error", "message": message}))
        else:
            console.print(f"[red]store error: {message}[/red]")
        raise typer.Exit(code=EXIT_STORE) from exc

    if not matches:
        # Unknown proposal_id — EXIT_INTERNAL (1) per the brief's
        # error-path contract. Operators can scope-down with
        # list-proposals to find a valid id.
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "error": "not_found",
                        "message": (
                            f"No PROPOSAL_DRAFTED event for "
                            f"proposal_id={proposal_id!r}"
                        ),
                    }
                )
            )
        else:
            console.print(
                f"[red]No PROPOSAL_DRAFTED event for "
                f"proposal_id={proposal_id!r}.[/red]"
            )
            console.print(
                "[dim]Run 'trellis admin list-proposals' to see "
                "available IDs.[/dim]"
            )
        raise typer.Exit(code=EXIT_INTERNAL)

    event = matches[0]
    payload = event.payload or {}
    markdown_preview = str(payload.get("markdown_preview") or "")

    if output_format == "json":
        # ``markdown`` is the preview only — Phase 0 doesn't persist
        # full markdown. Operators relying on the full document
        # should regenerate (cohort 2 will land the on-disk artefact
        # directory). The truncation is signalled explicitly so
        # consumers don't conflate preview with full.
        print(
            json.dumps(
                {
                    "proposal_id": str(
                        payload.get("proposal_id") or event.entity_id or ""
                    ),
                    "cluster_signature": str(
                        payload.get("cluster_signature") or ""
                    ),
                    "markdown": markdown_preview,
                    "markdown_truncated": True,
                    "source_event_count": int(
                        payload.get("source_event_count") or 0
                    ),
                    "generated_at": event.occurred_at.isoformat(),
                }
            )
        )
    else:
        console.print(
            "[dim](showing markdown_preview from PROPOSAL_DRAFTED "
            "event payload — Phase 0 does not persist full "
            "markdown.)[/dim]"
        )
        # ``console.print`` would interpret bracketed tokens as Rich
        # markup; the proposal markdown contains backticked tokens we
        # need to preserve verbatim. Print as plain string.
        print(markdown_preview)
    raise typer.Exit(code=EXIT_OK)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(admin_app: typer.Typer) -> None:
    """Wire the three subcommands onto an existing ``admin`` Typer app.

    Mirrors :mod:`trellis_cli.admin_migrate_provenance` — registration
    hook (rather than module-level decorator) so the import order in
    :mod:`trellis_cli.admin` stays explicit.
    """

    @admin_app.command("generate-proposals")
    def generate_proposals(  # pragma: no cover — Typer wrapper only
        window_hours: int = typer.Option(
            _DEFAULT_WINDOW_HOURS,
            "--window-hours",
            help=(
                "Rolling window over which to cluster EXTRACTION_FAILED and "
                "WELL_KNOWN_CANDIDATE events. Defaults to 24 hours."
            ),
            min=0,
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help=(
                "Currently unsupported — the Phase 0 generator does not "
                "expose a dry-run knob. Flag remains for forward "
                "compatibility; passing it short-circuits with a warning."
            ),
        ),
    ) -> None:
        """Run :class:`ProposalGenerator` over the given window.

        Emits PROPOSAL_DRAFTED for newly surfaced clusters and
        PROPOSAL_UPDATED for already-surfaced ones (idempotency via
        :class:`Proposal.proposal_id` lookup).
        """
        generate_proposals_command(
            window_hours=window_hours,
            output_format=output_format,
            dry_run=dry_run,
        )

    @admin_app.command("list-proposals")
    def list_proposals(  # pragma: no cover — Typer wrapper only
        limit: int = typer.Option(
            _DEFAULT_LIST_LIMIT,
            "--limit",
            help=(
                "Maximum number of PROPOSAL_DRAFTED rows to return "
                f"(capped at {_LIST_LIMIT_CEILING})."
            ),
            min=1,
        ),
        since: str = typer.Option(
            None,
            "--since",
            help=(
                "ISO-8601 datetime lower bound. Naive timestamps "
                "are coerced to UTC."
            ),
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        """List recent PROPOSAL_DRAFTED events.

        Returns the ``--limit`` most recent rows (descending by
        ``occurred_at``), optionally narrowed by ``--since``.
        """
        list_proposals_command(
            limit=limit,
            since=since,
            output_format=output_format,
        )

    @admin_app.command("show-proposal")
    def show_proposal(  # pragma: no cover — Typer wrapper only
        proposal_id: str = typer.Argument(
            ...,
            help="The proposal_id (SHA-256 hex) to look up.",
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        """Print the rendered markdown for a single proposal.

        Phase 0 persists only the 500-char ``markdown_preview`` on the
        EventLog payload; the full markdown will become available
        when cohort 2 lands the on-disk artefact directory.
        """
        show_proposal_command(
            proposal_id=proposal_id,
            output_format=output_format,
        )


__all__ = [
    "generate_proposals_command",
    "list_proposals_command",
    "register",
    "show_proposal_command",
]
