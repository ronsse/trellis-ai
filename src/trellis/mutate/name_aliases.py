"""Governed, bounded backfill for normalized entity-name aliases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.schemas.well_known import normalize_entity_name

if TYPE_CHECKING:
    from trellis.mutate.commands import CommandResult
    from trellis.mutate.executor import MutationExecutor
    from trellis.stores.base.graph import GraphStore

FailureStage = Literal["snapshot", "bind", "verify"]
FailureReason = Literal["store", "policy", "validation", "protocol"]
BackfillOutcome = Literal["bound", "rebound", "already_bound", "contested"]


@dataclass(frozen=True)
class BackfillFailure:
    """Count-safe failure detail that never contains an entity name."""

    stage: FailureStage
    reason: FailureReason
    entity_id: str | None


@dataclass(frozen=True)
class BackfillReport:
    """Outcome of one bounded governed backfill pass."""

    bound: int = 0
    rebound: int = 0
    already_bound: int = 0
    contested: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[BackfillFailure] = field(default_factory=list)
    commands_submitted: int = 0
    truncated: bool = False


def _idempotency_key(entity_id: str, key: str) -> str:
    digest = hashlib.sha256(
        f"{NAME_ALIAS_SOURCE_SYSTEM}\0{key}\0{entity_id}".encode()
    ).hexdigest()
    return f"name-alias-backfill:{digest}"


def _command(entity_id: str, name: str, key: str, requested_by: str) -> Command:
    return Command(
        operation=Operation.ALIAS_UPSERT,
        args={
            "entity_id": entity_id,
            "source_system": NAME_ALIAS_SOURCE_SYSTEM,
            "raw_id": key,
            "raw_name": name,
            "if_absent": True,
            "stale_owner_name_key": key,
        },
        target_id=entity_id,
        target_type="alias",
        requested_by=requested_by,
        idempotency_key=_idempotency_key(entity_id, key),
    )


def _build_commands(
    nodes: list[dict[str, object]],
    *,
    requested_by: str,
) -> tuple[list[Command], int, int]:
    by_key: dict[str, list[tuple[str, str]]] = {}
    skipped = 0
    for node in nodes:
        properties = node.get("properties") or {}
        name = properties.get("name") if isinstance(properties, dict) else None
        node_id = node.get("node_id")
        if not isinstance(name, str) or not node_id:
            skipped += 1
            continue
        key = normalize_entity_name(name)
        if not key:
            skipped += 1
            continue
        by_key.setdefault(key, []).append((str(node_id), name))

    contested = sum(1 for rows in by_key.values() if len(rows) > 1)
    commands = [
        _command(rows[0][0], rows[0][1], key, requested_by)
        for key, rows in by_key.items()
        if len(rows) == 1
    ]
    return commands, contested, skipped


def _verify_duplicate(
    graph_store: GraphStore,
    command: Command,
) -> BackfillOutcome | BackfillFailure:
    entity_id = str(command.args["entity_id"])
    try:
        winner = graph_store.resolve_alias(
            NAME_ALIAS_SOURCE_SYSTEM,
            str(command.args["raw_id"]),
        )
    except Exception:
        return BackfillFailure(stage="verify", reason="store", entity_id=entity_id)
    if winner is None:
        return BackfillFailure(stage="verify", reason="protocol", entity_id=entity_id)
    if str(winner.get("entity_id")) == entity_id:
        return "already_bound"
    return "contested"


def _classify_result(
    graph_store: GraphStore,
    command: Command,
    result: CommandResult,
) -> BackfillOutcome | BackfillFailure:
    entity_id = str(command.args["entity_id"])
    if result.status is CommandStatus.SUCCESS:
        outcome_by_message: dict[str, BackfillOutcome] = {
            "Alias bind outcome: bound": "bound",
            "Alias bind outcome: rebound": "rebound",
            "Alias bind outcome: already_bound": "already_bound",
            "Alias bind outcome: conflict": "contested",
        }
        if outcome := outcome_by_message.get(result.message):
            return outcome
        return BackfillFailure(stage="bind", reason="protocol", entity_id=entity_id)
    if result.status is CommandStatus.DUPLICATE:
        return _verify_duplicate(graph_store, command)
    return BackfillFailure(
        stage="bind",
        reason="policy" if result.status is CommandStatus.REJECTED else "store",
        entity_id=entity_id,
    )


def backfill_name_aliases(
    graph_store: GraphStore,
    executor: MutationExecutor,
    *,
    max_nodes: int,
    requested_by: str = "cli:backfill-name-aliases",
) -> BackfillReport:
    """Bind unique names through one bounded :class:`MutationExecutor` batch.

    The snapshot reads one row past ``max_nodes`` and refuses before command
    construction when truncated. Every alias write then traverses validation,
    policy, idempotency, handler execution, and audit emission.
    """
    if max_nodes < 1:
        msg = "max_nodes must be >= 1"
        raise ValueError(msg)

    try:
        nodes = graph_store.query(limit=max_nodes + 1)
    except Exception:
        failure = BackfillFailure(stage="snapshot", reason="store", entity_id=None)
        return BackfillReport(failed=1, failures=[failure])

    if len(nodes) > max_nodes:
        return BackfillReport(truncated=True)

    commands, contested, skipped = _build_commands(
        nodes,
        requested_by=requested_by,
    )
    if not commands:
        return BackfillReport(contested=contested, skipped=skipped)

    results = executor.execute_batch(
        CommandBatch(
            commands=commands,
            strategy=BatchStrategy.CONTINUE_ON_ERROR,
            requested_by=requested_by,
        )
    )

    counts = {
        "bound": 0,
        "rebound": 0,
        "already_bound": 0,
        "contested": contested,
    }
    failures: list[BackfillFailure] = []
    for command, result in zip(commands, results, strict=True):
        outcome = _classify_result(graph_store, command, result)
        if isinstance(outcome, BackfillFailure):
            failures.append(outcome)
        else:
            counts[outcome] += 1

    return BackfillReport(
        bound=counts["bound"],
        rebound=counts["rebound"],
        already_bound=counts["already_bound"],
        contested=counts["contested"],
        skipped=skipped,
        failed=len(failures),
        failures=failures,
        commands_submitted=len(commands),
    )
