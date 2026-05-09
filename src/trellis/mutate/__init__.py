"""Governed mutation pipeline for Trellis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    Operation,
    OperationRegistry,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.policy_gate import DefaultPolicyGate

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry


def build_curate_executor(registry: StoreRegistry) -> MutationExecutor:
    """Build a :class:`MutationExecutor` wired with the default curate handlers.

    Centralises the boilerplate that every surface (CLI, REST API, MCP)
    used to repeat: import handlers, build the dict, attach the operational
    event log. New handlers added to ``create_curate_handlers`` flow through
    every caller without each surface having to update its wiring.
    """
    from trellis.mutate.handlers import create_curate_handlers  # noqa: PLC0415

    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(registry),
    )


__all__ = [
    "BatchStrategy",
    "Command",
    "CommandBatch",
    "CommandResult",
    "CommandStatus",
    "DefaultPolicyGate",
    "MutationExecutor",
    "Operation",
    "OperationRegistry",
    "build_curate_executor",
]
