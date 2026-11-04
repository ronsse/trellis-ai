"""``trellis worker distill-precedents``: candidate precedents, dry-run first.

Lives beside ``worker.py`` the way ``analyze_pack_judge`` lives beside
``analyze.py``: ``worker.py`` calls :func:`register` once at its foot.

It reads traces in a window, runs the deterministic distiller
(:mod:`trellis.learning.precedent_distill`) and reports counts. By default it
writes nothing. ``--apply`` records each new candidate through the governed
``precedent.candidate`` operation, which emits one ``PRECEDENT_CANDIDATE_MINED``
event and writes to no document, graph or vector store: a candidate is never
served. A candidate already recorded is a duplicate, counted and not rewritten,
so a rerun over the same traces records nothing new.

The output is counts and candidate ids. It carries no trace text.
"""

from __future__ import annotations

from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.learning.pack_judge import PackJudgeError, parse_instant
from trellis.learning.precedent_distill import DISTILLER_VERSION, distill_traces
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers.precedent import precedent_candidate_recorded
from trellis_cli.exit_codes import EXIT_OK, refusal_exit_code
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

logger = structlog.get_logger(__name__)

console = build_console()

REQUESTED_BY = "cli:distill-precedents"


def _distill(
    *,
    since: str | None,
    until: str | None,
    limit: int,
    domain: str | None,
    apply: bool,
) -> tuple[dict[str, Any], int]:
    """Run one pass; return the payload and the exit code (format-blind)."""
    try:
        start = parse_instant(since, option="--since")
        end = parse_instant(until, option="--until")
    except PackJudgeError as exc:
        return {"status": "error", "message": str(exc)}, 2

    registry = _get_registry()
    trace_store = registry.operational.trace_store
    event_log = registry.operational.event_log

    # One row past the limit says whether the window has more behind it.
    rows = trace_store.query(domain=domain, since=start, until=end, limit=limit + 1)
    truncated = len(rows) > limit
    tally = distill_traces(rows[:limit])

    new = []
    duplicates = 0
    for record in tally.records:
        if precedent_candidate_recorded(event_log, record.candidate_id):
            duplicates += 1
        else:
            new.append(record)

    recorded = rejected = failed = 0
    code = EXIT_OK
    if apply and new:
        executor = build_curate_executor(
            registry,
            clearance="restricted",  # trusted: operator CLI
        )
        for record in new:
            result = executor.execute(
                Command(
                    operation=Operation.PRECEDENT_CANDIDATE,
                    args={"record": record.model_dump(mode="json")},
                    requested_by=REQUESTED_BY,
                    idempotency_key=record.candidate_id,
                )
            )
            if result.status == CommandStatus.SUCCESS:
                recorded += 1
            elif result.status == CommandStatus.DUPLICATE:
                duplicates += 1
            else:
                if result.status == CommandStatus.REJECTED:
                    rejected += 1
                else:
                    failed += 1
                if code == EXIT_OK:
                    code = refusal_exit_code(result)

    done_key = "recorded" if apply else "would_record"
    done = recorded if apply else len(new)
    payload: dict[str, Any] = {
        "status": "ok" if code == EXIT_OK else "error",
        "mode": "apply" if apply else "dry-run",
        "distiller_version": DISTILLER_VERSION,
        "window": {
            "since": start.isoformat() if start else None,
            "until": end.isoformat() if end else None,
            "domain": domain,
            "limit": limit,
        },
        "traces_read": tally.traces_read,
        "truncated": truncated,
        "candidates": len(tally.records),
        "by_rule": dict(sorted(tally.by_rule.items())),
        "skipped": dict(sorted(tally.skips_by_reason.items())),
        "duplicates": duplicates,
        done_key: done,
        "rejected": rejected,
        "failed": failed,
        "candidate_ids": [r.candidate_id for r in new],
    }
    return payload, code


def _render(payload: dict[str, Any]) -> None:
    if payload["status"] == "error" and "message" in payload:
        console.print(
            f"[red]worker distill-precedents: {escape(payload['message'])}[/red]",
            soft_wrap=True,
        )
        return
    verb = "recorded" if payload["mode"] == "apply" else "would record"
    done = payload.get("recorded", payload.get("would_record"))
    console.print(
        f"[bold]Distill precedents[/bold] ({escape(payload['mode'])}): "
        f"read {payload['traces_read']} trace(s), "
        f"{payload['candidates']} candidate(s), {verb} {done}, "
        f"{payload['duplicates']} duplicate(s)"
    )
    if payload["truncated"]:
        console.print(
            "[yellow]  the window holds more traces than --limit; "
            "older ones were not read.[/yellow]"
        )
    for rule, count in payload["by_rule"].items():
        console.print(f"  rule {escape(rule)}: {count}")
    for reason, count in payload["skipped"].items():
        console.print(f"  skipped {escape(reason)}: {count}")
    if payload["rejected"] or payload["failed"]:
        console.print(
            f"[red]  {payload['rejected']} rejected, {payload['failed']} failed[/red]"
        )
    if payload["mode"] == "dry-run":
        console.print("  dry run: nothing was written. Pass --apply to record.")


def distill_precedents_command(
    since: str | None = typer.Option(
        None, "--since", help="Read traces created at or after this ISO-8601 instant."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Read traces created at or before this ISO-8601 instant."
    ),
    limit: int = typer.Option(
        500, "--limit", min=1, help="Most traces to read, newest first."
    ),
    domain: str | None = typer.Option(
        None, "--domain", help="Restrict the read to this trace domain."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Record the new candidates as PRECEDENT_CANDIDATE_MINED events "
            "through the governed pipeline. Without it nothing is written. "
            "A candidate is never served."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Distill candidate precedents from traces (deterministic; dry run by default).

    Reports counts only: traces read, candidates by rule, skips by reason,
    and duplicates of candidates already recorded. ``--apply`` records the new
    ones. The exit code does not depend on ``--format``.
    """
    payload, code = _distill(
        since=since, until=until, limit=limit, domain=domain, apply=apply
    )
    if output_format == "json":
        emit_json(payload)
    else:
        _render(payload)
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def register(worker_app: typer.Typer) -> None:
    """Mount ``distill-precedents`` onto the ``worker`` Typer app."""
    worker_app.command("distill-precedents")(distill_precedents_command)


__all__ = ["distill_precedents_command", "register"]
