"""The immutable core — what a self-tuning system may never retune.

Trellis has two loops that write without a human in the call: the
parameter tuner (:mod:`trellis.learning.tuners`), which proposes and can
auto-promote a :class:`~trellis.schemas.parameters.ParameterSet`, and the
unattended writers in ``trellis_workers``, which submit
:class:`~trellis.mutate.commands.Command` objects from cron. Both are
deliberate. What neither may do is change the things that *bound* them —
a loop that can move its own stop is not gated, it is decorated.

Two rosters, one per loop, and they fail in opposite directions on
purpose.

**:data:`GOVERNING_KEYS` is a deny-list** over
``ParameterScope.component_id``. A proposal's scope is copied verbatim
from the outcome event it aggregates
(``rule_tuner.py``'s ``ParameterScope(component_id=event.component_id,
...)``), and :func:`~trellis.learning.tuners.promotion.promote_proposal`
validated the proposal's *status* and the *policy gate* and inspected
nothing about the scope. A proposal naming ``learning.schema_evolution``
would therefore have written the snapshot that
``schema_evolution._resolve_thresholds`` reads — one learning loop
re-tuning the stop of another, and in that particular case the stop on
the pass that auto-mutates ``trellis.schemas.well_known``. Denying is
right here because the population is small, named, and enumerable from
``src/``: a *new* learning loop that forgets to register would otherwise
be silently tunable, and the AST rule in
``tests/unit/test_immutable_core_rule.py`` is what stops that roster
rotting.

**:data:`UNATTENDED_WRITERS` is an allow-list** over
``Command.requested_by``. Here the population that must not grow is the
*operations*, not the writers: a new ``Operation`` member added next year
must be refused to an unattended writer until somebody decides otherwise,
so the roster names what each writer may issue and everything else is
refused. Deny-listing the destructive verbs instead would have handed
every future verb to cron by default.

**Measured before it was written, and it is inert on arrival.** As of
2026-09-20 exactly two identities exist under ``src/trellis_workers``
(both behind module constants, which is why a literal-only sweep found
neither): ``worker:embed-traces`` issues ``evidence.ingest`` and
``worker:session-capture`` issues ``entity.create`` / ``link.create`` via
``memory_ingest_hook``. **No unattended writer issues a delete, redact or
purge operation today**, so this rule changes no behaviour the day it
lands. That is the point rather than a weakness — the same argument #424
makes about Stage 2: a constraint written once the actuator exists is a
constraint written after the incident.

Scope, stated rather than assumed. The allow-list binds ``worker:``
identities because those are the only ones whose *unattendedness is a
property of the identity*. A nightly ``trellis curate`` run submits under
a ``cli:`` identity indistinguishable from the same command typed at a
terminal, so binding it would refuse the human, and refusing the human is
not what this module is for. Making cron legible to the audit trail needs
a distinct surface, which is a separate change.
"""

from __future__ import annotations

from collections.abc import Mapping

from trellis.mutate.commands import Command, Operation

# ---------------------------------------------------------------------------
# Half 1 — the parameter keys that govern another loop
# ---------------------------------------------------------------------------

#: ``ParameterScope.component_id`` values whose parameters are another
#: learning loop's gate, and which therefore cannot be the *target* of a
#: tuner proposal.
#:
#: Spelled as literals rather than imported from the four owning modules
#: because ``trellis.mutate`` sits below ``trellis.learning`` in the
#: import order (``learning/scoring.py`` imports ``mutate``; nothing in
#: ``mutate`` imports ``learning``) and this module is not worth
#: inverting that for. The duplication is bounded two ways, both in
#: ``tests/unit/test_immutable_core_rule.py``: an equality test that
#: imports the four live constants, and an AST scan of ``src/`` that
#: fails on a fifth governing constant nobody added here.
GOVERNING_KEYS: frozenset[str] = frozenset(
    {
        # schema_evolution's thresholds gate the pass that auto-mutates
        # trellis.schemas.well_known — the sharpest of the four.
        "learning.schema_evolution",
        # tag_evolution's support/lift floors gate keyword promotion into
        # the domain facet, which hard-excludes on mismatch (#282).
        "learning.tag_evolution",
        # domain_normalization's alias floors gate tag merges, which
        # redirect every document carrying the alias.
        "learning.domain_normalization",
        # scoring's promote/noise thresholds gate which precedents reach
        # get_lessons at all.
        "learning.scoring",
    }
)


def governing_key_refusal(component_id: str) -> str | None:
    """Reason to refuse a proposal targeting *component_id*, or ``None``.

    Returns a string in the ``PromotionPolicy`` rejection vocabulary so
    it rides the existing ``TUNER_PROPOSAL_REJECTED`` payload's
    ``reason`` field unchanged — this is a new reason, not a new event
    and not a new channel.
    """
    if component_id in GOVERNING_KEYS:
        return f"governing_key_immutable:{component_id}"
    return None


# ---------------------------------------------------------------------------
# Half 2 — what the writers that already run unattended may issue
# ---------------------------------------------------------------------------

#: Operations that remove or overwrite information the store already
#: holds, such that a later read cannot recover it.
#:
#: This set does **not** enforce anything — :data:`UNATTENDED_WRITERS` is
#: the enforcement primitive, and it fails closed without consulting this
#: set at all. What this is for is making the claim *"no unattended
#: writer may delete, redact or purge"* checkable against the allow-list
#: rather than merely asserted in prose, which a test does by
#: intersecting the two. Keeping it out of the enforcement path is
#: deliberate: a deny-list that gates real traffic is one forgotten enum
#: member away from being wrong, and this one would rot exactly the way
#: #443's three-against-six control-key roster did.
#:
#: ``entity.update`` is *not* here: the graph is SCD-2, so an update adds
#: a version and ``get_node_history`` still returns the prior one.
#: ``alias.upsert`` *is*, because a rebind detaches the alias from its
#: previous owner and nothing retains that binding.
DESTRUCTIVE_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.ENTITY_MERGE,
        Operation.ALIAS_UPSERT,
        Operation.LINK_REMOVE,
        Operation.LABEL_REMOVE,
        Operation.REDACTION_APPLY,
        Operation.RETENTION_PRUNE,
    }
)

#: Every other operation, named explicitly so that adding a member to
#: :class:`Operation` without classifying it fails a test rather than
#: quietly landing on the safe side of a subtraction.
NON_DESTRUCTIVE_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.TRACE_INGEST,
        Operation.TRACE_APPEND_STEP,
        Operation.TRACE_RECORD_OUTCOME,
        Operation.EVIDENCE_INGEST,
        Operation.EVIDENCE_ATTACH,
        Operation.PRECEDENT_PROMOTE,
        Operation.PRECEDENT_UPDATE,
        Operation.ENTITY_CREATE,
        Operation.ENTITY_UPDATE,
        Operation.LINK_CREATE,
        Operation.LABEL_ADD,
        Operation.FEEDBACK_RECORD,
        Operation.OBSERVATION_RECORD,
        Operation.MEASUREMENT_RECORD,
        Operation.RETENTION_RESTORE,
    }
)

#: ``Command.requested_by`` → the operations that writer may issue.
#:
#: Hand-read off ``src/trellis_workers`` on 2026-09-20 and pinned by an
#: AST scan, because both identities live behind module constants
#: (``REQUESTED_BY`` / ``_REQUESTED_BY``) and a sweep for a quoted value
#: after ``requested_by=`` finds neither — which is how the 22 ``cli:`` /
#: ``api:`` / ``mcp:`` identities got enumerated while the two that
#: actually matter were missed on the first pass.
#:
#: An entry is the *complete* set for that writer. Widening one is a
#: deliberate act with a review attached; forgetting to widen one shows
#: up as a rejected write with ``reason="immutable_core"`` in the audit
#: log and on the ``trellis analyze health`` capture banner, not as a
#: silent no-op.
UNATTENDED_WRITERS: Mapping[str, frozenset[Operation]] = {
    # trellis_workers/trace_embed/handler.py — build_trace_summary_command
    "worker:embed-traces": frozenset({Operation.EVIDENCE_INGEST}),
    # trellis_workers/session_capture/capture.py — sync_records, whose
    # drafts reach result_to_batch as creates only.
    "worker:session-capture": frozenset(
        {Operation.ENTITY_CREATE, Operation.LINK_CREATE}
    ),
}


def unattended_writer_refusal(command: Command) -> str | None:
    """Reason to refuse *command*, or ``None`` if it is permitted.

    Only ever refuses a ``requested_by`` that is *in* the roster: an
    unrecognised identity is not an unattended writer and is none of this
    function's business. Completeness of the roster over
    ``src/trellis_workers`` is the AST rule's job, not this call's —
    trying to infer unattendedness from the identity string at runtime
    would make every future surface's naming choice load-bearing.
    """
    permitted = UNATTENDED_WRITERS.get(command.requested_by)
    if permitted is None:
        return None
    if command.operation in permitted:
        return None
    return (
        f"unattended_writer_operation_not_allowed:"
        f"{command.requested_by}:{command.operation}"
    )
