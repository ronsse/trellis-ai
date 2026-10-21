"""Fixtures for the line between a mined precedent candidate and a promotion.

``PrecedentMiner`` once wrote its candidates as ``PRECEDENT_PROMOTED``
events. Event rows are immutable, so a log that ran the old miner still
holds them, and every reader that means "a promoted precedent" has to skip
them. :func:`emit_historical_miner_row` reproduces that row byte for byte so
a reader test seeds the real shape rather than the shape the current miner
writes; :func:`promote_through_executor` seeds the governed counterpart.
"""

from __future__ import annotations

from typing import Literal

from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.registry import StoreRegistry

MinerShape = Literal["generated", "extracted"]


def emit_historical_miner_row(
    event_log: EventLog,
    *,
    entity_id: str,
    title: str = "Mined pattern",
    shape: MinerShape = "generated",
) -> None:
    """Write a ``PRECEDENT_PROMOTED`` row exactly as the old miner did.

    ``generated`` is the LLM path (``precedent_candidate_generated``,
    title and confidence, no body); ``extracted`` is the deterministic path
    (``precedent_extracted``, trace id and outcome, no title either).
    """
    if shape == "generated":
        payload = {
            "action": "precedent_candidate_generated",
            "title": title,
            "confidence": 0.6,
        }
    else:
        payload = {
            "trace_id": "trace-mined",
            "outcome_status": "failure",
            "action": "precedent_extracted",
        }
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="precedent_miner",
        entity_id=entity_id,
        entity_type="precedent",
        payload=payload,
    )


def promote_through_executor(
    registry: StoreRegistry,
    *,
    trace_id: str,
    title: str = "Governed lesson",
    domain: str | None = None,
) -> None:
    """Promote a trace-sourced precedent through the governed executor."""
    args: dict[str, object] = {
        "trace_id": trace_id,
        "title": title,
        "description": f"{title} body",
    }
    if domain is not None:
        args["domain"] = domain
    result = build_curate_executor(registry).execute(
        Command(
            operation=Operation.PRECEDENT_PROMOTE,
            args=args,
            target_type="trace",
            requested_by="cli:promote",
        )
    )
    assert result.status == CommandStatus.SUCCESS
