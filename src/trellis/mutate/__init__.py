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
from trellis.mutate.evidence_ingest import (
    EvidenceEmbedMode,
    build_evidence_ingest_command,
    build_evidence_ingest_command_from_args,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.immutable_core import (
    DESTRUCTIVE_OPERATIONS,
    GOVERNING_KEYS,
    NON_DESTRUCTIVE_OPERATIONS,
    UNATTENDED_WRITERS,
    governing_key_refusal,
    unattended_writer_refusal,
)
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.mutate.policy_source import (
    POLICY_FILENAME,
    POLICY_GATE_SURFACE,
    build_policy_gate,
    load_policies,
    resolve_policy_path,
)

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry


def build_curate_executor(
    registry: StoreRegistry,
    *,
    evidence_embed: EvidenceEmbedMode = "soft",
) -> MutationExecutor:
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
        handlers=create_curate_handlers(registry, evidence_embed=evidence_embed),
        policy_gate=build_policy_gate(registry),
    )


def build_schedule_executor(registry: StoreRegistry) -> MutationExecutor:
    """Build the admin-only executor for the schedule-registry write surface.

    Wires :data:`~trellis.mutate.commands.Operation.SCHEDULE_UPDATE` and
    :data:`~trellis.mutate.commands.Operation.JOB_RUN_REQUEST` to
    :class:`~trellis.mutate.schedule_handlers.ScheduleUpdateHandler` /
    :class:`~trellis.mutate.schedule_handlers.JobRunRequestHandler` —
    deliberately a *disjoint* two-handler dict, never merged into
    :func:`create_curate_handlers`. That dict backs
    ``POST /api/v1/commands/batch`` (``mutate`` scope) and MCP
    ``execute_mutation``; those two ops are admin-scoped, so only the
    admin REST route and ``trellis admin schedule`` build an executor
    from *this* function. Submitting either operation to the
    curate-wired executor hits "no handler registered" (``FAILED``),
    which is the refusal, not an oversight.

    Stage 2 runs exactly as :func:`build_curate_executor` wires it — same
    ``policy_gate=build_policy_gate(registry)`` construction — so an
    ``entity_type="schedule"`` deny policy refuses this surface too.
    """
    from trellis.mutate.policy_source import build_policy_gate  # noqa: PLC0415
    from trellis.mutate.schedule_handlers import (  # noqa: PLC0415
        JobRunRequestHandler,
        ScheduleUpdateHandler,
    )

    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers={
            Operation.SCHEDULE_UPDATE: ScheduleUpdateHandler(registry),
            Operation.JOB_RUN_REQUEST: JobRunRequestHandler(registry),
        },
        policy_gate=build_policy_gate(registry),
    )


def build_settings_executor(registry: StoreRegistry) -> MutationExecutor:
    """Build a :class:`MutationExecutor` wired with ONLY the settings handler.

    ``Operation.SETTINGS_UPDATE`` is deliberately absent from
    :func:`build_curate_executor`'s handler dict (see
    ``create_settings_handlers``'s docstring) because that executor backs
    the generic mutation surfaces -- REST ``POST /commands/batch`` and MCP
    ``execute_mutation`` -- which have no admin-scope gate of their own.
    Only the admin-scoped REST route
    (``trellis_api/routes/settings.py``) and ``trellis admin settings``
    call this builder, so settings.update stays reachable from exactly
    those two surfaces.

    Stage 2 still always runs here, same as ``build_curate_executor``:
    the policy gate is built from the deployment's declared policies
    unconditionally, never only when policies exist
    (``tests/unit/test_policy_gate_rule.py``). A deployment can therefore
    add an ``entity_type: setting`` deny policy and have it enforced on
    this path exactly as on any other governed write.
    """
    from trellis.mutate.handlers import create_settings_handlers  # noqa: PLC0415
    from trellis.mutate.policy_source import build_policy_gate  # noqa: PLC0415

    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_settings_handlers(registry),
        policy_gate=build_policy_gate(registry),
    )


def redirect_unreachable_settings_update(result: CommandResult) -> CommandResult:
    """Point a caller at the real settings write path; ``status`` unchanged.

    ``build_curate_executor`` -- which backs REST ``POST /commands/batch``
    and MCP ``execute_mutation`` -- never registers
    ``Operation.SETTINGS_UPDATE`` (see ``build_settings_executor``'s
    docstring above): those two surfaces have no admin-scope gate of
    their own. ``MutationExecutor.execute``'s Stage 4 "no handler"
    fallback is honest about why nothing happened -- FAILED, no handler,
    nothing evaluated, no ``MUTATION_REJECTED`` event -- so this leaves
    ``status`` as FAILED rather than relabelling it REJECTED, which this
    codebase's own convention
    (``tests/unit/mutate/test_rejection_reason_carrier.py``: "a REJECTED
    result names why, as its MUTATION_REJECTED event does") reserves for
    a result whose reason an audit event also carries; Stage 4's
    no-handler branch returns before Stage 5 and emits no event, so
    there is no reason here for a fabricated ``rejection_reason`` to
    name truthfully.

    It only rewrites the message, so a caller who tried
    ``settings.update`` on one of these two surfaces is told where it
    *is* reachable -- ``PUT``/``POST /api/v1/settings/{name}`` or
    ``trellis admin settings set``/``unset`` -- instead of a bare "no
    handler registered". A no-op for every other operation, and for any
    other ``settings.update`` outcome, so both call sites apply it to
    every result they get back rather than threading a settings-only
    branch through the generic executor itself.
    """
    if result.operation is not Operation.SETTINGS_UPDATE:
        return result
    if result.status is not CommandStatus.FAILED:
        return result
    if not result.message.startswith("No handler registered for:"):
        return result
    return result.model_copy(
        update={
            "message": (
                f"{result.message} -- settings.update is reachable only "
                "through PUT (or POST) /api/v1/settings/{name}, or "
                "`trellis admin settings set`/`unset`; this generic "
                "mutation surface does not carry it."
            )
        }
    )


__all__ = [
    "DESTRUCTIVE_OPERATIONS",
    "GOVERNING_KEYS",
    "NON_DESTRUCTIVE_OPERATIONS",
    "POLICY_FILENAME",
    "POLICY_GATE_SURFACE",
    "UNATTENDED_WRITERS",
    "BatchStrategy",
    "Command",
    "CommandBatch",
    "CommandResult",
    "CommandStatus",
    "DefaultPolicyGate",
    "EvidenceEmbedMode",
    "MutationExecutor",
    "Operation",
    "OperationRegistry",
    "build_curate_executor",
    "build_evidence_ingest_command",
    "build_evidence_ingest_command_from_args",
    "build_policy_gate",
    "build_schedule_executor",
    "build_settings_executor",
    "ensure_evidence_document",
    "governing_key_refusal",
    "load_policies",
    "redirect_unreachable_settings_update",
    "resolve_policy_path",
    "unattended_writer_refusal",
]
