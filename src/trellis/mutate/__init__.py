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
from trellis.mutate.policy_source import (
    POLICY_FILENAME,
    build_policy_gate,
    load_policies,
    resolve_policy_path,
)

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

    **Stage 2 is wired here.** The policy gate is built from the
    deployment's declared policies (see
    :mod:`trellis.mutate.policy_source`). A deployment that has declared
    none gets an *empty* gate, which is behaviourally identical to the
    no-gate configuration that preceded this — pinned by
    ``tests/unit/mutate/test_policy_wiring.py``. A gate is passed
    unconditionally rather than only when policies exist, so the pipeline
    is genuinely the documented five stages and "is the gate wired?" has
    an observable answer instead of depending on file state.

    The gate is rebuilt per call, matching the per-call construction of
    the handlers. When no policy file exists — the default — that costs a
    single ``stat()``, well below the 13 handler objects this function
    already constructs. A deployment that *has* declared policies pays one
    small JSON read per mutation surface call; cache here if that ever
    shows up in a profile, but do not cache away the ability to pick up an
    edited policy file without a restart.
    """
    from trellis.mutate.handlers import create_curate_handlers  # noqa: PLC0415
    from trellis.mutate.policy_source import build_policy_gate  # noqa: PLC0415

    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(registry),
        policy_gate=build_policy_gate(registry),
    )


__all__ = [
    "POLICY_FILENAME",
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
    "build_policy_gate",
    "ensure_evidence_document",
    "load_policies",
    "resolve_policy_path",
]
