"""``trellis worker distill-advisory-precedents``: advisory findings as candidates.

The sibling of :mod:`trellis_cli.worker_distill_precedents`, whose source is the
advisory store instead of traces. It reads the stored advisories, maps the
``ANTI_PATTERN`` category (:mod:`trellis.learning.advisory_candidates`) and
reports counts. By default it writes nothing. ``--apply`` records each new
candidate through the governed ``precedent.candidate`` operation, which emits one
``PRECEDENT_CANDIDATE_MINED`` event and writes to no document, graph or vector
store: a candidate is never served. It changes no advisory, and what the advisory
generator produces, stores, suppresses or serves is untouched.

A candidate is new only if neither its id nor its finding is recorded. The
advisory generator keys an advisory on what it is about, not on its numbers, so
the same finding returns every night with moved counts, and a record that hashes
the counts would be a new id each time. A finding (rule, distiller version,
advisory id, action kind) already recorded is therefore a ``duplicate`` too, and
the counts of the night it was first recorded stand; a later measurement is not
re-recorded. The finding is read from the candidate events themselves.

The output is counts and candidate ids. It carries no advisory text.

:func:`distill_advisory_window` is the read-and-map half, shared with
``trellis curate evaluate-precedent-gate``: it never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
import typer
from rich.markup import escape

from trellis.learning.advisory_candidates import (
    ADVISORY_DISTILLER_VERSION,
    AdvisoryTally,
    map_advisories,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers.precedent import precedent_candidate_recorded
from trellis.schemas.precedent_record import PrecedentRecord
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, refusal_exit_code
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

logger = structlog.get_logger(__name__)

console = build_console()

REQUESTED_BY = "cli:distill-advisory-precedents"
_SURFACE = "cli.worker.distill_advisory_precedents"

#: What names one finding: rule, distiller version, advisory id, action kind.
FindingKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class AdvisoryWindow:
    """One dry pass over the advisory store."""

    tally: AdvisoryTally
    #: Records neither a candidate event nor a recorded finding names yet.
    new: tuple[PrecedentRecord, ...]
    #: Records whose id an event already names.
    duplicates: int
    #: Records whose finding an event names under another id (moved counts).
    finding_recorded: int
    #: Whether the advisory file loaded only in part.
    store_degraded: bool


def finding_key(record: PrecedentRecord) -> FindingKey | None:
    """The finding a record states, or ``None`` when it cites no advisory."""
    if not record.evidence.advisory_ids:
        return None
    return (
        record.rule,
        record.distiller_version,
        record.evidence.advisory_ids[0],
        record.action.kind,
    )


def recorded_findings(event_log: EventLog) -> frozenset[FindingKey]:
    """The findings every recorded candidate event already states."""
    total = event_log.count(event_type=EventType.PRECEDENT_CANDIDATE_MINED)
    if total <= 0:
        return frozenset()
    keys: set[FindingKey] = set()
    for event in event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED, limit=total
    ):
        try:
            key = finding_key(PrecedentRecord.model_validate(event.payload["record"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key is not None:
            keys.add(key)
    return frozenset(keys)


def distill_advisory_window(registry: StoreRegistry) -> AdvisoryWindow:
    """Read the stored advisories and map them. Writes nothing."""
    store = load_advisory_store(registry.stores_dir, surface=_SURFACE)
    advisories = [] if store is None else store.list(include_suppressed=True)
    tally = map_advisories(advisories)
    event_log = registry.operational.event_log
    findings = recorded_findings(event_log) if tally.records else frozenset()
    new: list[PrecedentRecord] = []
    duplicates = finding_recorded = 0
    for record in tally.records:
        if precedent_candidate_recorded(event_log, record.candidate_id):
            duplicates += 1
        elif finding_key(record) in findings:
            finding_recorded += 1
        else:
            new.append(record)
    return AdvisoryWindow(
        tally=tally,
        new=tuple(new),
        duplicates=duplicates,
        finding_recorded=finding_recorded,
        store_degraded=bool(store is not None and store.is_degraded),
    )


def stage1_block(window: AdvisoryWindow) -> dict[str, Any]:
    """The window as the counts the gate report carries beside the trace source."""
    tally = window.tally
    return {
        "distiller_version": ADVISORY_DISTILLER_VERSION,
        "advisories_read": tally.advisories_read,
        "candidates": len(tally.records),
        "would_mint": len(window.new),
        "duplicates": window.duplicates,
        "finding_recorded": window.finding_recorded,
        "by_rule": dict(sorted(tally.by_rule.items())),
        "skipped": dict(sorted(tally.skips_by_reason.items())),
        "store_degraded": window.store_degraded,
    }


def _distill(*, apply: bool) -> tuple[dict[str, Any], int]:
    """Run one pass; return the payload and the exit code (format-blind)."""
    registry = _get_registry()
    window = distill_advisory_window(registry)
    new = list(window.new)
    duplicates = window.duplicates

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

    block = stage1_block(window)
    done_key = "recorded" if apply else "would_record"
    payload: dict[str, Any] = {
        "status": "ok" if code == EXIT_OK else "error",
        "mode": "apply" if apply else "dry-run",
        "distiller_version": block["distiller_version"],
        "advisories_read": block["advisories_read"],
        "candidates": block["candidates"],
        "by_rule": block["by_rule"],
        "skipped": block["skipped"],
        "duplicates": duplicates,
        "finding_recorded": block["finding_recorded"],
        done_key: recorded if apply else len(new),
        "rejected": rejected,
        "failed": failed,
        "store_degraded": block["store_degraded"],
        "candidate_ids": [r.candidate_id for r in new],
    }
    return payload, code


def _render(payload: dict[str, Any]) -> None:
    verb = "recorded" if payload["mode"] == "apply" else "would record"
    done = payload.get("recorded", payload.get("would_record"))
    console.print(
        f"[bold]Distill advisory precedents[/bold] ({escape(payload['mode'])}): "
        f"read {payload['advisories_read']} advisory(ies), "
        f"{payload['candidates']} candidate(s), {verb} {done}, "
        f"{payload['duplicates']} duplicate(s), "
        f"{payload['finding_recorded']} finding(s) already recorded"
    )
    if payload["store_degraded"]:
        console.print(
            "[yellow]  the advisory file loaded only in part; "
            "the rows that parsed were read.[/yellow]"
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


def distill_advisory_precedents_command(
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
    """Distill candidate precedents from stored advisories (dry run by default).

    Reads the stored advisories, maps the ANTI_PATTERN ones, and reports counts
    only: advisories read, candidates by rule, skips by reason, duplicates and
    findings already recorded. ``--apply`` records the new ones. It changes no
    advisory. The exit code does not depend on ``--format``.
    """
    payload, code = _distill(apply=apply)
    if output_format == "json":
        emit_json(payload)
    else:
        _render(payload)
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def register(worker_app: typer.Typer) -> None:
    """Mount ``distill-advisory-precedents`` onto the ``worker`` Typer app."""
    worker_app.command("distill-advisory-precedents")(
        distill_advisory_precedents_command
    )


__all__ = [
    "AdvisoryWindow",
    "distill_advisory_precedents_command",
    "distill_advisory_window",
    "finding_key",
    "recorded_findings",
    "register",
    "stage1_block",
]
