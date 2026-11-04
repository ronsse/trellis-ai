"""``trellis worker distill-precedents-local``: local-model precedents, dry-run first.

The sibling of ``worker distill-precedents`` for the local-model tier
(:mod:`trellis.learning.precedent_local`). It reads traces in a window and asks the
``precedent_distill`` consumer's model for the lessons the deterministic tier did
not already find; then, with ``--merge-limit`` above zero, it asks the
``precedent_merge`` consumer whether near-duplicate candidates are one lesson.

Both consumers refuse any route not declared ``local: true``, before the first
call. A refusal ends that phase, is counted under ``refusals`` and exits
``EXIT_POLICY``: the answer cannot change from one trace to the next.

By default nothing is recorded. ``--apply`` records each new candidate through
the governed ``precedent.candidate`` operation, which writes one
``PRECEDENT_CANDIDATE_MINED`` event and no document, graph or vector row. A
candidate is never served. Every model call emits one ``MEMORY_OP_JUDGED`` event in
both modes (that stream is the training data), and the output says how many.

The output is counts and candidate ids. It carries no trace or record text.
``--limit`` bounds the traces read, and so the distill calls; ``--merge-limit``
bounds the merge calls. Neither is unbounded, since each is a model call.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import typer
from pydantic import ValidationError as PydanticValidationError
from rich.markup import escape

from trellis.errors import TrellisError
from trellis.learning.pack_judge import (
    MAX_CONSECUTIVE_LLM_ERRORS,
    PackJudgeError,
    parse_instant,
)
from trellis.learning.precedent_local import (
    DISTILL_PROMPT_VERSION,
    MERGE_PROMPT_VERSION,
    LocalModelRunner,
    RouteNotLocalError,
    candidate_pairs,
    distill_trace_local,
    merge_pair_local,
)
from trellis.llm.routing import LLMConsumer
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers.precedent import precedent_candidate_recorded
from trellis.schemas.precedent_record import PrecedentRecord
from trellis.stores.base.event_log import EventType
from trellis_cli.exit_codes import (
    EXIT_OK,
    EXIT_POLICY,
    EXIT_STORE,
    EXIT_VALIDATION,
    exit_code_for,
    refusal_exit_code,
)
from trellis_cli.output import build_console, emit_json
from trellis_cli.stores import _get_registry

if TYPE_CHECKING:
    from datetime import datetime

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

console = build_console()

REQUESTED_BY = "cli:distill-precedents-local"

#: Most recorded candidates read as the merge pool. When more exist, the newest
#: ``MERGE_POOL_LIMIT`` are read and the older ones are not paired; the payload's
#: ``merge_pool`` block says so (``truncated``).
MERGE_POOL_LIMIT = 200


@dataclass
class _Tally:
    """What one run did, in counts and the records it made."""

    traces_read: int = 0
    truncated: bool = False
    merge_pool: _MergePool | None = None
    calls: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    refusals: Counter[str] = field(default_factory=Counter)
    llm_errors: int = 0
    distilled: list[PrecedentRecord] = field(default_factory=list)
    merged: list[PrecedentRecord] = field(default_factory=list)
    code: int = EXIT_OK


@dataclass(frozen=True)
class _MergePool:
    """The recorded candidates a run may pair, and whether older ones were left out."""

    records: list[PrecedentRecord]
    limit: int
    truncated: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "read": len(self.records),
            "limit": self.limit,
            "truncated": self.truncated,
        }


def _recorded_pool(registry: StoreRegistry) -> _MergePool:
    """The newest recorded candidates, newest first. A row that is not one is skipped.

    ``order="desc"`` makes the store's ``limit`` cut the oldest rows, not the
    newest; one extra row is read to know whether anything was cut. The pairing
    order does not depend on this one: :func:`candidate_pairs` sorts by id.
    """
    pool: list[PrecedentRecord] = []
    events = registry.operational.event_log.get_events(
        event_type=EventType.PRECEDENT_CANDIDATE_MINED,
        limit=MERGE_POOL_LIMIT + 1,
        order="desc",
    )
    truncated = len(events) > MERGE_POOL_LIMIT
    for event in events[:MERGE_POOL_LIMIT]:
        raw = (event.payload or {}).get("record")
        if not isinstance(raw, dict):
            continue  # an event from the older miner carries no governed record
        try:
            pool.append(PrecedentRecord.model_validate(raw))
        except PydanticValidationError:
            continue
    return _MergePool(records=pool, limit=MERGE_POOL_LIMIT, truncated=truncated)


def _label_for(registry: StoreRegistry, consumer: LLMConsumer, model_id: str) -> str:
    """The label a consumer's judged events carry: ``--model-id`` or its route's."""
    label = model_id or registry.llm_route(consumer).model
    if not label:
        message = (
            f"The {consumer.value} route names no model, so its judged events have "
            "no label. Pass --model-id <label>, or set model on the route."
        )
        raise PackJudgeError(message)
    return label


def _open_runners(
    registry: StoreRegistry, *, merge: bool, model_id: str
) -> dict[str, LocalModelRunner]:
    """One runner per consumer the run will use; each client built for its own."""
    from trellis_cli.worker import _require_llm_client_or_exit  # noqa: PLC0415

    command = "worker distill-precedents-local"
    document_store = registry.knowledge.document_store
    event_log = registry.operational.event_log
    runners: dict[str, LocalModelRunner] = {}
    distill_client = _require_llm_client_or_exit(
        LLMConsumer.PRECEDENT_DISTILL, command=command
    )
    runners["distill"] = LocalModelRunner.for_consumer(
        registry,
        LLMConsumer.PRECEDENT_DISTILL,
        client=distill_client,
        model=_label_for(registry, LLMConsumer.PRECEDENT_DISTILL, model_id),
        document_store=document_store,
        event_log=event_log,
    )
    if merge:
        merge_client = _require_llm_client_or_exit(
            LLMConsumer.PRECEDENT_MERGE, command=command
        )
        runners["merge"] = LocalModelRunner.for_consumer(
            registry,
            LLMConsumer.PRECEDENT_MERGE,
            client=merge_client,
            model=_label_for(registry, LLMConsumer.PRECEDENT_MERGE, model_id),
            document_store=document_store,
            event_log=event_log,
        )
    return runners


async def _distill_phase(
    runner: LocalModelRunner,
    registry: StoreRegistry,
    tally: _Tally,
    *,
    start: datetime | None,
    end: datetime | None,
    limit: int,
    domain: str | None,
) -> None:
    rows = registry.operational.trace_store.query(
        domain=domain, since=start, until=end, limit=limit + 1
    )
    tally.truncated = len(rows) > limit
    consecutive = 0
    for trace in rows[:limit]:
        tally.traces_read += 1
        try:
            result = await distill_trace_local(runner, trace)
        except RouteNotLocalError:
            tally.code = tally.code or EXIT_POLICY
            return  # counted on the runner; the next trace would be refused too
        except Exception as exc:
            # GRACEFUL-DEGRADATION: one failed model call must not end a bounded
            # run; it is counted, and the type (never the message, which can quote
            # a prompt) is logged. A run of failures in a row stops it.
            logger.warning("distill_local_call_failed", error_type=type(exc).__name__)
            tally.llm_errors += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_LLM_ERRORS:
                return
            continue
        consecutive = 0
        tally.skipped.update(result.skips)
        if result.called:
            tally.calls["distill"] += 1
            tally.outcomes[f"distill_{result.outcome}"] += 1
        tally.distilled.extend(result.records)


async def _merge_phase(
    runner: LocalModelRunner,
    tally: _Tally,
    pool: list[PrecedentRecord],
    *,
    merge_limit: int,
) -> None:
    consecutive = 0
    for first, second in candidate_pairs(pool, limit=merge_limit):
        try:
            result = await merge_pair_local(runner, first, second)
        except RouteNotLocalError:
            tally.code = tally.code or EXIT_POLICY
            return
        except Exception as exc:
            # GRACEFUL-DEGRADATION: as in the distill phase.
            logger.warning("merge_local_call_failed", error_type=type(exc).__name__)
            tally.llm_errors += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_LLM_ERRORS:
                return
            continue
        consecutive = 0
        tally.skipped.update(result.skips)
        if result.called:
            tally.calls["merge"] += 1
            tally.outcomes[f"merge_{result.verdict}"] += 1
        if result.merged is not None:
            tally.merged.append(result.merged)


def _record_new(
    registry: StoreRegistry, tally: _Tally, *, apply: bool
) -> tuple[list[PrecedentRecord], Counter[str], int, int, int]:
    """Split the run's records into new and duplicate; with ``apply``, record them.

    Returns ``(new, recorded by op, duplicates, rejected, failed)``.
    """
    event_log = registry.operational.event_log
    merged_ids = {r.candidate_id for r in tally.merged}
    new: list[PrecedentRecord] = []
    seen: set[str] = set()
    duplicates = 0
    for record in [*tally.distilled, *tally.merged]:
        if record.candidate_id in seen:
            continue
        seen.add(record.candidate_id)
        if precedent_candidate_recorded(event_log, record.candidate_id):
            duplicates += 1
        else:
            new.append(record)
    recorded: Counter[str] = Counter()
    rejected = failed = 0
    if not (apply and new):
        return new, recorded, duplicates, rejected, failed
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
            recorded["merge" if record.candidate_id in merged_ids else "distill"] += 1
        elif result.status == CommandStatus.DUPLICATE:
            duplicates += 1
        else:
            if result.status == CommandStatus.REJECTED:
                rejected += 1
            else:
                failed += 1
            if tally.code == EXIT_OK:
                tally.code = refusal_exit_code(result)
    return new, recorded, duplicates, rejected, failed


def _run(
    *,
    since: str | None,
    until: str | None,
    limit: int,
    merge_limit: int,
    domain: str | None,
    apply: bool,
    model_id: str,
) -> tuple[dict[str, Any], int]:
    """Run one pass; return the payload and the exit code (format-blind)."""
    try:
        start = parse_instant(since, option="--since")
        end = parse_instant(until, option="--until")
        registry = _get_registry()
        runners = _open_runners(registry, merge=bool(merge_limit), model_id=model_id)
    except PackJudgeError as exc:
        return {"status": "error", "message": str(exc)}, EXIT_VALIDATION
    tally = _Tally()

    async def go() -> None:
        await _distill_phase(
            runners["distill"],
            registry,
            tally,
            start=start,
            end=end,
            limit=limit,
            domain=domain,
        )
        if "merge" in runners:
            tally.merge_pool = _recorded_pool(registry)
            pool = [*tally.distilled, *tally.merge_pool.records]
            await _merge_phase(runners["merge"], tally, pool, merge_limit=merge_limit)

    asyncio.run(go())
    for runner in runners.values():
        tally.refusals.update(runner.refusals)
    new, recorded, duplicates, rejected, failed = _record_new(
        registry, tally, apply=apply
    )
    if tally.llm_errors and tally.code == EXIT_OK:
        tally.code = EXIT_STORE

    merged_ids = {r.candidate_id for r in tally.merged}
    judged = {
        "distillation": runners["distill"].judged["distillation"],
        "reconciliation": (
            runners["merge"].judged["reconciliation"] if "merge" in runners else 0
        ),
    }
    done = {
        "distill": sum(1 for r in new if r.candidate_id not in merged_ids),
        "merge": sum(1 for r in new if r.candidate_id in merged_ids),
    }
    payload: dict[str, Any] = {
        "status": "ok" if tally.code == EXIT_OK else "error",
        "mode": "apply" if apply else "dry-run",
        "prompt_versions": {
            "distill": DISTILL_PROMPT_VERSION,
            "merge": MERGE_PROMPT_VERSION if "merge" in runners else None,
        },
        "models": {name: r.model for name, r in sorted(runners.items())},
        "window": {
            "since": start.isoformat() if start else None,
            "until": end.isoformat() if end else None,
            "domain": domain,
            "limit": limit,
            "merge_limit": merge_limit,
        },
        "traces_read": tally.traces_read,
        "truncated": tally.truncated,
        "merge_pool": tally.merge_pool.as_payload() if tally.merge_pool else None,
        "model_calls": dict(sorted(tally.calls.items())),
        "outcomes": dict(sorted(tally.outcomes.items())),
        "refusals": dict(sorted(tally.refusals.items())),
        "skipped": dict(sorted(tally.skipped.items())),
        "llm_errors": tally.llm_errors,
        "judged_events": {**judged, "total": sum(judged.values())},
        "candidates": {"distill": len(tally.distilled), "merge": len(tally.merged)},
        "duplicates": duplicates,
        "recorded" if apply else "would_record": (
            {"distill": recorded["distill"], "merge": recorded["merge"]}
            if apply
            else done
        ),
        "rejected": rejected,
        "failed": failed,
        "candidate_ids": [r.candidate_id for r in new],
    }
    return payload, tally.code


def _render(payload: dict[str, Any]) -> None:
    if "message" in payload:
        console.print(
            f"[red]worker distill-precedents-local: {escape(payload['message'])}[/red]",
            soft_wrap=True,
        )
        return
    mode = payload["mode"]
    console.print(
        f"[bold]Distill precedents, local model[/bold] ({escape(mode)}): "
        f"read {payload['traces_read']} trace(s), "
        f"{payload['judged_events']['total']} judged event(s), "
        f"{payload['duplicates']} duplicate(s)"
    )
    for name, count in payload["model_calls"].items():
        console.print(f"  model calls {escape(name)}: {count}")
    for name, count in payload["outcomes"].items():
        console.print(f"  {escape(name)}: {count}")
    for reason, count in payload["refusals"].items():
        console.print(f"[red]  refused {escape(reason)}: {count}[/red]")
    for reason, count in payload["skipped"].items():
        console.print(f"  skipped {escape(reason)}: {count}")
    if payload["llm_errors"]:
        console.print(f"[red]  {payload['llm_errors']} model call(s) failed[/red]")
    if payload["truncated"]:
        console.print(
            "[yellow]  the window holds more traces than --limit; "
            "older ones were not read.[/yellow]"
        )
    pool = payload["merge_pool"]
    if pool is not None:
        console.print(
            f"  merge pool: read {pool['read']} recorded candidate(s) "
            f"(limit {pool['limit']})"
        )
        if pool["truncated"]:
            console.print(
                "[yellow]  more candidates are recorded than the merge pool holds; "
                "the oldest were not paired.[/yellow]"
            )
    if mode == "dry-run":
        would = sum(payload["would_record"].values())
        console.print(
            f"  would record {would}. "
            "Dry run: no candidate was written. Pass --apply to record."
        )
    else:
        console.print(f"  recorded {sum(payload['recorded'].values())}")


def distill_precedents_local_command(
    since: str | None = typer.Option(
        None, "--since", help="Read traces created at or after this ISO-8601 instant."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Read traces created at or before this ISO-8601 instant."
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        help="Most traces to read, newest first. Each can be one model call.",
    ),
    merge_limit: int = typer.Option(
        5,
        "--merge-limit",
        min=0,
        help="Most candidate pairs to ask the merge model about. 0 skips merging.",
    ),
    domain: str | None = typer.Option(
        None, "--domain", help="Restrict the read to this trace domain."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Record the new candidates as PRECEDENT_CANDIDATE_MINED events "
            "through the governed pipeline. Without it no candidate is written. "
            "Judged events are written either way. A candidate is never served."
        ),
    ),
    model_id: str = typer.Option(
        "",
        "--model-id",
        help="Label for the model in judged events. Defaults to the route's model.",
    ),
    output_format: str = typer.Option("text", "--format", help="Output format."),
) -> None:
    """Distill and merge candidate precedents with a local model (dry run by default).

    Both consumers (``precedent_distill``, ``precedent_merge``) must route to a
    tier declared ``local: true``; any other route is refused before a call is
    made. Reports counts and ids only. The exit code does not depend on ``--format``.
    """
    try:
        payload, code = _run(
            since=since,
            until=until,
            limit=limit,
            merge_limit=merge_limit,
            domain=domain,
            apply=apply,
            model_id=model_id,
        )
    except TrellisError as exc:
        payload, code = {"status": "error", "message": str(exc)}, exit_code_for(exc)
    if output_format == "json":
        emit_json(payload)
    else:
        _render(payload)
    if code != EXIT_OK:
        raise typer.Exit(code=code)


def register(worker_app: typer.Typer) -> None:
    """Mount ``distill-precedents-local`` onto the ``worker`` Typer app."""
    worker_app.command("distill-precedents-local")(distill_precedents_local_command)


__all__ = ["distill_precedents_local_command", "register"]
