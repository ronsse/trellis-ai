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
from trellis.mutate.evidence import ensure_evidence_document
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

    Knowledge-plane-only deployments: configure the ``event_log`` store with
    ``{"backend": "null"}`` so ``registry.operational.event_log`` resolves to
    :class:`~trellis.stores.null.event_log.NullEventLog`. Both the executor
    *and* the curate handlers (which emit through
    ``registry.operational.event_log``) then treat mutation-event emission as
    an intentional no-op — governed graph / vector writes run with no
    Operational-Plane persistence, no ``event_log=None`` special-casing, and
    no downstream monkey patch. See issue #196.
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
    "ensure_evidence_document",
]
