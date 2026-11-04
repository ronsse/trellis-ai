"""``trellis curate evaluate-precedent-gate``: the two-stage gate, report only.

Stage 1 is the mint (dedup only): the distiller run dry over the window's
traces, reusing :func:`~trellis_cli.worker_distill_precedents.distill_window`,
and, beside it under ``stage1.advisory_source``, the advisory source run dry over
the stored advisories
(:func:`~trellis_cli.worker_distill_advisory_precedents.distill_advisory_window`).
Stage 2 is activation: the pure test in
:mod:`trellis.learning.precedent_activation`, run beside the gate
``auto-promote-candidates`` applies today, over the same candidates.

This command writes nothing. It emits no event (not even on failure), builds no
executor and records no candidate. It exists so the two gates can be counted
night by night before any serving code reads either.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.learning.precedent_activation import ActivationThresholds
from trellis.learning.precedent_distill import DISTILLER_VERSION
from trellis.learning.precedent_gate_report import (
    GradedWindow,
    build_graded_window,
    evaluate_gate,
    recorded_candidates_in_packs,
)
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    resolve_scan_window,
    scan_events,
)
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry, get_event_log
from trellis_cli.worker_distill_advisory_precedents import (
    distill_advisory_window,
    stage1_block,
)
from trellis_cli.worker_distill_precedents import distill_window

console = build_console()
logger = structlog.get_logger(__name__)

DEFAULT_DAYS = 30
DEFAULT_TRACE_LIMIT = 500


def _scan_block(coverage: ScanCoverage) -> dict[str, Any]:
    return {
        "limit": coverage.limit,
        "scanned": coverage.scanned,
        "matched": coverage.matched,
        "truncated": coverage.truncated,
        "covered_since": coverage.covered_since or None,
        "note": coverage.note,
    }


def _stage1(
    *, since: datetime, until: datetime | None, trace_limit: int, domain: str | None
) -> dict[str, Any]:
    window = distill_window(
        _get_registry(), start=since, end=until, limit=trace_limit, domain=domain
    )
    tally = window.tally
    return {
        "distiller_version": DISTILLER_VERSION,
        "traces_read": tally.traces_read,
        "trace_limit": trace_limit,
        "truncated": window.truncated,
        "candidates": len(tally.records),
        "would_mint": len(window.new),
        "duplicates": window.duplicates,
        "by_rule": dict(sorted(tally.by_rule.items())),
        "skipped": dict(sorted(tally.skips_by_reason.items())),
        "dropped": dict(sorted(tally.dropped.items())),
        # Beside the trace source's counts, which are unchanged: the same stage
        # run over the stored ANTI_PATTERN and APPROACH advisories.
        "advisory_source": stage1_block(distill_advisory_window(_get_registry())),
    }


def _packs_block(window: GradedWindow) -> dict[str, Any]:
    return {
        "scanned": window.packs_scanned,
        "held_out_dropped": window.packs_held_out,
        "with_item_rows": window.packs_with_rows,
        "graded": window.packs_graded,
        "feedback_scanned": window.feedback_scanned,
        "feedback_held_out_dropped": window.feedback_held_out,
    }


def _evaluate(
    *,
    candidates_path: Path | None,
    days: int,
    scan_limit: int,
    trace_limit: int,
    domain: str | None,
) -> dict[str, Any]:
    """Run both stages and return the payload. Reads only; writes nothing."""
    # Private to curate and imported late: curate registers this module at its
    # foot, so a top-level import would be circular.
    from trellis_cli.curate import (  # noqa: PLC0415
        _build_candidate_auto_promote_policy,
        _load_candidates_file,
        _resolve_candidate_auto_promote_overrides_for_cli,
    )

    registry = _get_registry()
    event_log = get_event_log()
    resolved_path, candidates = _load_candidates_file(
        candidates_path, registry.stores_dir
    )
    # Only the floors are read; the stored mode is irrelevant to a report that
    # never promotes, so a refused stored mode is ignored here.
    overrides, _held = _resolve_candidate_auto_promote_overrides_for_cli(
        registry.stores_dir
    )
    policy = _build_candidate_auto_promote_policy(
        overrides, force_report_only=True, demotion_enabled=False
    )

    since, until = resolve_scan_window(days)
    packs = scan_events(
        event_log,
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        until=until,
        limit=scan_limit,
    )
    feedback = scan_events(
        event_log,
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        until=until,
        limit=scan_limit,
    )
    recorded = scan_events(
        event_log,
        event_type=EventType.PRECEDENT_CANDIDATE_MINED,
        limit=scan_limit,
    )
    window = build_graded_window(packs.events, feedback.events)
    recorded_ids = {e.entity_id for e in recorded.events if e.entity_id}

    stage2 = evaluate_gate(
        candidates,
        window,
        policy=policy,
        thresholds=ActivationThresholds(),
    )
    notes = [
        "report only: nothing was served, promoted, demoted or written",
        (
            "recorded precedent candidates are event-only and never served, so "
            "they have no citations until serving reads them"
        ),
    ]
    for label, coverage in (
        ("PACK_ASSEMBLED", packs.coverage),
        ("FEEDBACK_RECORDED", feedback.coverage),
        ("PRECEDENT_CANDIDATE_MINED", recorded.coverage),
    ):
        if coverage.truncated:
            notes.append(f"{label} scan truncated: {coverage.note}")
    return {
        "status": "ok",
        "mode": "dry-run",
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "days": days,
            "domain": domain,
            "scan_limit": scan_limit,
        },
        "candidates_file": resolved_path.name,
        "stage1": _stage1(
            since=since, until=until, trace_limit=trace_limit, domain=domain
        ),
        "stage2": stage2,
        "candidates_recorded": len(recorded_ids),
        "candidates_with_servings": recorded_candidates_in_packs(recorded_ids, window),
        "packs": _packs_block(window),
        "scan": {
            "pack_assembled": _scan_block(packs.coverage),
            "feedback_recorded": _scan_block(feedback.coverage),
            "precedent_candidate_mined": _scan_block(recorded.coverage),
            "truncated": any(
                c.truncated
                for c in (packs.coverage, feedback.coverage, recorded.coverage)
            ),
        },
        "notes": notes,
    }


def _render(payload: dict[str, Any]) -> None:
    stage1, stage2, packs = payload["stage1"], payload["stage2"], payload["packs"]
    console.print("[bold]Evaluate precedent gate[/bold] (report only)")
    for note in payload["notes"]:
        if "truncated" in note:
            console.print(f"  [yellow]{escape(note)}[/yellow]", soft_wrap=True)
    console.print(
        f"  stage 1 (mint): read {stage1['traces_read']} trace(s), "
        f"would mint {stage1['would_mint']}, {stage1['duplicates']} duplicate(s)"
    )
    if stage1["truncated"]:
        console.print(
            "  [yellow]the window holds more traces than --trace-limit; "
            "older ones were not read.[/yellow]"
        )
    for rule, count in stage1["by_rule"].items():
        console.print(f"    rule {escape(rule)}: {count}")
    for reason, count in stage1["skipped"].items():
        console.print(f"    skipped {escape(reason)}: {count}")
    for reason, count in stage1["dropped"].items():
        console.print(f"    dropped {escape(reason)}: {count}")
    advisory = stage1["advisory_source"]
    console.print(
        f"  stage 1 (mint, advisory source): read {advisory['advisories_read']} "
        f"advisory(ies), would mint {advisory['would_mint']}, "
        f"{advisory['duplicates']} duplicate(s), "
        f"{advisory['finding_recorded']} finding(s) already recorded"
    )
    for rule, count in advisory["by_rule"].items():
        console.print(f"    rule {escape(rule)}: {count}")
    for reason, count in advisory["skipped"].items():
        console.print(f"    skipped {escape(reason)}: {count}")
    console.print(
        f"  stage 2 (activate): evaluated {stage2['evaluated']}, "
        f"promotable {stage2['promotable']}, "
        f"current_gate_pass {stage2['current_gate_pass']}, "
        f"new_gate_pass {stage2['new_gate_pass']}"
    )
    console.print(
        f"    both {stage2['both']}, only_new {stage2['only_new']}, "
        f"only_current {stage2['only_current']}, "
        f"without_graded_servings {stage2['without_graded_servings']}"
    )
    for name, count in stage2["failed_at"].items():
        console.print(f"    failed at {escape(name)}: {count}")
    console.print(
        f"  candidates_recorded {payload['candidates_recorded']}, "
        f"candidates_with_servings {payload['candidates_with_servings']}"
    )
    console.print(
        f"  packs: {packs['scanned']} scanned, {packs['held_out_dropped']} held out "
        f"and dropped, {packs['graded']} graded; feedback: "
        f"{packs['feedback_scanned']} scanned, "
        f"{packs['feedback_held_out_dropped']} dropped"
    )
    for row in stage2["targets"]:
        console.print(
            f"    - {escape(row['candidate_id'])}: current_gate={row['current_gate']} "
            f"new_gate={row['new_gate']}"
        )


def evaluate_precedent_gate(
    candidates_path: Path | None = typer.Option(  # noqa: B008 - typer option default
        None,
        "--candidates",
        help=(
            "Path to intent_learning_candidates.json. Defaults to the "
            "learning-artifacts directory, as auto-promote-candidates does."
        ),
    ),
    days: int = typer.Option(
        DEFAULT_DAYS, "--days", min=1, help="Look-back window for packs and traces."
    ),
    scan_limit: int = typer.Option(
        DEFAULT_SCAN_LIMIT,
        "--scan-limit",
        min=1,
        help="Most events read per event type, newest first; truncation is stated.",
    ),
    trace_limit: int = typer.Option(
        DEFAULT_TRACE_LIMIT, "--trace-limit", min=1, help="Most traces to distill."
    ),
    domain: str | None = typer.Option(
        None, "--domain", help="Distill only traces of this domain."
    ),
    dry_run: bool = typer.Option(  # noqa: ARG001 - the only mode, accepted
        False,
        "--dry-run",
        help=(
            "Accepted for symmetry with auto-promote-candidates. This command "
            "writes nothing with or without it."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format"),
) -> None:
    """Count what the two-stage precedent gate would mint and activate. Report only.

    Stage 1 (mint) runs the distiller dry over the window's traces. Stage 2
    (activate) tests each learning candidate against h >= 3 helpful, u < h,
    >= 2 distinct sessions, a within-pack permutation P < 0.05 and lift >= 1.2
    over the pack baseline, beside the gate ``auto-promote-candidates`` applies
    today. Counts and candidate ids only. Held-out packs and their feedback
    reach no count; the output says how many were dropped. Nothing is served,
    promoted, demoted or written, and no event is emitted.
    """
    code = EXIT_OK
    try:
        payload = _evaluate(
            candidates_path=candidates_path,
            days=days,
            scan_limit=scan_limit,
            trace_limit=trace_limit,
            domain=domain,
        )
    except ValueError as exc:
        # A resolver ValueError can embed the raw value it rejected; say only
        # the type, in either format, and let the exit code below be the same.
        message = f"invalid candidate auto-promote configuration ({type(exc).__name__})"
        payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": message,
        }
        code = EXIT_VALIDATION
    if output_format == "json":
        emit_json(payload)
    elif code == EXIT_OK:
        _render(payload)
    else:
        console.print(f"[red]{escape(payload['message'])}[/red]", soft_wrap=True)
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def register(curate_app: typer.Typer) -> None:
    """Mount ``evaluate-precedent-gate`` onto the ``curate`` Typer app."""
    curate_app.command("evaluate-precedent-gate")(evaluate_precedent_gate)


__all__ = ["evaluate_precedent_gate", "register"]
