"""Behaviour of the immutable core at its two enforcement seams (K5).

``tests/unit/test_immutable_core_rule.py`` proves the two rosters are
complete. This proves they are *load-bearing* — that a forbidden write is
actually refused, at the right stage, with an audit record an operator can
act on, and that no flag lifts either constraint.

The order matters more than it looks. Both halves of this feature could
be written, exported, imported and type-checked while gating nothing at
all: #424's ``policy_gate=None`` shipped for months in exactly that shape,
and #447 found four of five ``_reject`` calls deletable with the full
suite green. So each seam here is exercised through the real entry point
(:meth:`MutationExecutor.execute`,
:func:`~trellis.learning.tuners.promotion.promote_proposal`) rather than
by calling the predicate directly, and each refusal test is paired with
its permitted twin — a rule that refuses everything passes a
refusal-only suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.learning.tuners import (
    preview_promotion,
    promote_proposal,
    reject_proposal,
)
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.immutable_core import (
    GOVERNING_KEYS,
    UNATTENDED_WRITERS,
    governing_key_refusal,
    unattended_writer_refusal,
)
from trellis.schemas.parameters import (
    ParameterProposal,
    ParameterScope,
    ParameterSet,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore

_WORKER = "worker:session-capture"
_PERMITTED = Operation.ENTITY_CREATE
_FORBIDDEN = Operation.REDACTION_APPLY

_ARGS: dict[Operation, dict[str, object]] = {
    Operation.ENTITY_CREATE: {"entity_type": "service", "name": "auth"},
    Operation.REDACTION_APPLY: {"target_id": "ent_1", "reason": "test"},
}


def _handler(created_id: str = "ent_1") -> MagicMock:
    handler = MagicMock()
    handler.handle.return_value = (created_id, "ok")
    return handler


def _executor(event_log: SQLiteEventLog | None = None) -> MutationExecutor:
    """An executor that would happily run either operation.

    Both handlers are registered on purpose. A refusal proved against an
    executor with no handler for the forbidden verb proves nothing — the
    command would have failed anyway, for an unrelated reason.
    """
    return MutationExecutor(
        event_log=event_log,
        handlers={_PERMITTED: _handler(), _FORBIDDEN: _handler("red_1")},
    )


def _command(operation: Operation, requested_by: str) -> Command:
    return Command(
        operation=operation,
        args=dict(_ARGS[operation]),
        requested_by=requested_by,
    )


# ---------------------------------------------------------------------------
# Seam 1 — the unattended-writer allow-list, in the executor
# ---------------------------------------------------------------------------


class TestUnattendedWriterAllowList:
    def test_a_rostered_writer_is_refused_an_operation_outside_its_set(self) -> None:
        result = _executor().execute(_command(_FORBIDDEN, _WORKER))

        assert result.status == CommandStatus.REJECTED
        assert _WORKER in result.message
        assert result.message.startswith("unattended_writer_operation_not_allowed:")

    def test_a_rostered_writer_still_gets_the_operations_it_owns(self) -> None:
        """The paired positive. A gate that refuses everything is not a gate."""
        result = _executor().execute(_command(_PERMITTED, _WORKER))

        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == "ent_1"

    def test_an_unrostered_identity_is_not_touched(self) -> None:
        """Only identities the roster *names* are bound.

        The refusal cannot be inferred from the string: a ``cli:`` verb is
        a human at a terminal as often as it is cron, and refusing one
        would refuse the human. Completeness over ``src/trellis_workers``
        is the AST rule's job, which is why this call is a no-op rather
        than a guess.
        """
        result = _executor().execute(_command(_FORBIDDEN, "cli:redact"))

        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == "red_1"

    def test_the_refusal_is_recorded_as_its_own_stage(self, tmp_path: Path) -> None:
        """``reason`` discriminates the stage, so the banner can too.

        A ``worker:``-labelled ``MUTATION_REJECTED`` is counted by
        ``capture_health`` (``_surface_label`` passes a ``worker:``
        identity through unchanged) and both workers emit
        ``MUTATION_EXECUTED`` on their success paths, so a banner raised
        by this refusal can also clear — the #461 property.
        """
        events = SQLiteEventLog(tmp_path / "events.db")
        try:
            _executor(events).execute(_command(_FORBIDDEN, _WORKER))

            emitted = events.get_events(event_type=EventType.MUTATION_REJECTED)
            assert len(emitted) == 1
            payload = emitted[0].payload
            assert payload["reason"] == "immutable_core"
            assert payload["requested_by"] == _WORKER
            assert payload["operation"] == Operation.REDACTION_APPLY
        finally:
            events.close()

    def test_a_forbidden_operation_reports_the_same_reason_when_args_are_bad(
        self, tmp_path: Path
    ) -> None:
        """The refusal sits above arg validation, and that is deliberate.

        Stage 1 validates ``args`` against the operation's schema. If the
        roster check ran after it, the *same forbidden write* would report
        ``validate`` on a malformed call and ``immutable_core`` on a
        well-formed one — an audit trail whose reason depends on payload
        shape, which is no use for deciding whether a writer is
        misbehaving.
        """
        events = SQLiteEventLog(tmp_path / "events.db")
        try:
            malformed = Command(operation=_FORBIDDEN, args={}, requested_by=_WORKER)
            result = _executor(events).execute(malformed)

            assert result.status == CommandStatus.REJECTED
            emitted = events.get_events(event_type=EventType.MUTATION_REJECTED)
            assert [e.payload["reason"] for e in emitted] == ["immutable_core"]
        finally:
            events.close()

    def test_no_handler_runs_for_a_refused_command(self) -> None:
        """Refused means not executed, not executed-and-logged."""
        handler = _handler("red_1")
        executor = MutationExecutor(handlers={_FORBIDDEN: handler})

        executor.execute(_command(_FORBIDDEN, _WORKER))

        handler.handle.assert_not_called()

    @pytest.mark.parametrize("identity", sorted(UNATTENDED_WRITERS))
    def test_every_rostered_writer_is_refused_something(self, identity: str) -> None:
        """No roster entry is a permit-all in disguise.

        An entry whose set happened to cover every ``Operation`` would
        satisfy the completeness rule and the destructive-intersection
        rule while binding nothing, so each one is checked against a verb
        it does not hold.
        """
        outside = set(Operation) - UNATTENDED_WRITERS[identity]
        assert outside, f"{identity} permits every operation"
        for operation in sorted(outside, key=str):
            command = Command(operation=operation, args={}, requested_by=identity)
            assert unattended_writer_refusal(command) is not None


# ---------------------------------------------------------------------------
# Seam 2 — the governing-key deny-list, in the tuner
# ---------------------------------------------------------------------------


@pytest.fixture
def stores(tmp_path: Path):
    params = SQLiteParameterStore(tmp_path / "parameters.db")
    state = SQLiteTunerStateStore(tmp_path / "tuner_state.db")
    events = SQLiteEventLog(tmp_path / "events.db")
    try:
        yield params, state, events
    finally:
        params.close()
        state.close()
        events.close()


def _proposal(component_id: str, **kw) -> ParameterProposal:
    defaults: dict = {
        "proposal_id": "prop_test",
        "scope": ParameterScope(component_id=component_id, domain="a"),
        "tuner": "rule_tuner",
        "proposed_values": {"min_support": 2.0},
        "sample_size": 30,
    }
    defaults.update(kw)
    return ParameterProposal(**defaults)


def _seeded(stores, component_id: str) -> ParameterProposal:
    """A proposal that would otherwise promote cleanly.

    Sample size is over the floor and a baseline exists, so the *only*
    thing that can reject it is the immutable core. Seeding a proposal
    that would have been rejected anyway is how a gate gets credited with
    a refusal it did not make.
    """
    params, state, _ = stores
    proposal = _proposal(component_id)
    params.put(ParameterSet(scope=proposal.scope, values={"min_support": 1.0}))
    state.put_proposal(proposal)
    return proposal


class TestGoverningKeyDenyList:
    @pytest.mark.parametrize("component_id", sorted(GOVERNING_KEYS))
    def test_a_proposal_targeting_a_learning_loops_own_gate_is_refused(
        self, stores, component_id: str
    ) -> None:
        params, state, events = stores
        proposal = _seeded(stores, component_id)

        result = promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )

        assert result.status == "rejected"
        assert result.reason == f"governing_key_immutable:{component_id}"
        # The parameter set is untouched: refused, not promoted-then-reverted.
        assert params.get_active(proposal.scope).values == {"min_support": 1.0}

    def test_an_ordinary_component_still_promotes(self, stores) -> None:
        """The paired positive, again — most scopes are tunable and must stay so."""
        params, state, events = stores
        proposal = _seeded(stores, "retrieve.packer")

        result = promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )

        assert result.status == "promoted"
        assert params.get_active(proposal.scope).values == {"min_support": 2.0}

    def test_force_does_not_unlock_a_governing_key(self, stores) -> None:
        """``force`` skips *the policy gate*, and that is all it may skip.

        A flag that also lifted this would make the one constraint
        separating a self-tuning loop from a loop that tunes its own stop
        advisory — and ``force`` is reachable from the CLI, so "advisory"
        would mean "one flag away" for anything that shells out.
        """
        params, state, events = stores
        proposal = _seeded(stores, "learning.schema_evolution")

        result = promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
            force=True,
        )

        assert result.status == "rejected"
        assert result.reason == "governing_key_immutable:learning.schema_evolution"
        assert params.get_active(proposal.scope).values == {"min_support": 1.0}

    def test_the_refusal_rides_the_existing_rejection_event(self, stores) -> None:
        """A new reason, not a new event type and not a new channel.

        The payload keys are asserted explicitly because
        ``TUNER_PROPOSAL_REJECTED`` already ships: the only non-test
        reader (``retrieve/metrics_timeseries.py``) counts by type and
        reads no field, but a key added or dropped here changes a
        published audit record.
        """
        params, state, events = stores
        proposal = _seeded(stores, "learning.tag_evolution")

        promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )

        emitted = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)
        assert len(emitted) == 1
        payload = emitted[0].payload
        assert payload["reason"] == "governing_key_immutable:learning.tag_evolution"
        assert payload["proposal_id"] == proposal.proposal_id
        assert payload["scope"] == list(proposal.scope.key())
        assert payload["proposed_values"] == {"min_support": 2.0}
        assert payload["sample_size"] == 30
        # No baseline is resolved before refusing, so there is no effect to
        # report. Reading the store to decorate a refusal is how a refusal
        # comes to depend on store state.
        assert payload["effect_size"] is None
        assert emitted[0].entity_id == proposal.proposal_id

    def test_the_proposal_is_left_in_a_terminal_state(self, stores) -> None:
        """Refused once, not re-offered on the next nightly pass."""
        params, state, events = stores
        proposal = _seeded(stores, "learning.scoring")

        promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )
        stored = state.get_proposal(proposal.proposal_id)
        assert stored.status == "rejected"

        again = promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )
        assert again.status == "skipped"
        assert events.count(event_type=EventType.TUNER_PROPOSAL_REJECTED) == 1

    @pytest.mark.parametrize("component_id", sorted(GOVERNING_KEYS))
    def test_the_preview_predicts_exactly_what_the_commit_delivers(
        self, stores, component_id: str
    ) -> None:
        """A dry run that disagrees with its commit is #437 in other clothes.

        ``preview_promotion`` is what the Review-queue UI and
        ``trellis tuner preview`` render. If it reported ``promoted`` for
        a scope the commit then refuses, the surface a human reads would
        be the one telling them the wrong thing.
        """
        params, state, events = stores
        proposal = _seeded(stores, component_id)

        preview = preview_promotion(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
        )
        # Pure read — the preview must not have consumed the proposal.
        assert events.count() == 0
        assert state.get_proposal(proposal.proposal_id).status == "pending"

        result = promote_proposal(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            event_log=events,
        )

        assert preview.status == result.status == "rejected"
        assert preview.reason == result.reason

    def test_the_preview_refuses_under_force_too(self, stores) -> None:
        params, state, _ = stores
        proposal = _seeded(stores, "learning.domain_normalization")

        preview = preview_promotion(
            proposal.proposal_id,
            tuner_state=state,
            parameter_store=params,
            force=True,
        )

        assert preview.status == "rejected"
        assert preview.reason.startswith("governing_key_immutable:")

    def test_manual_rejection_keeps_its_own_payload(self, stores) -> None:
        """``reject_proposal`` deliberately does not route through ``_reject``.

        Its event carries ``manual: True`` and no ``effect_size`` at all.
        Folding it into the shared refusal constructor would have been
        tidier and would have silently added a key to an audit event that
        already ships — the shape #456 warns about, in reverse.
        """
        _, state, events = stores
        proposal = _proposal("retrieve.packer")
        state.put_proposal(proposal)

        reject_proposal(proposal.proposal_id, tuner_state=state, event_log=events)

        payload = events.get_events(event_type=EventType.TUNER_PROPOSAL_REJECTED)[
            0
        ].payload
        assert payload["manual"] is True
        assert "effect_size" not in payload


# ---------------------------------------------------------------------------
# The predicates themselves
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_governing_key_refusal_names_the_key_it_refused(self) -> None:
        for key in GOVERNING_KEYS:
            assert governing_key_refusal(key) == f"governing_key_immutable:{key}"

    def test_governing_key_refusal_passes_everything_else(self) -> None:
        for key in ("retrieve.packer", "learning", "learning.", ""):
            assert governing_key_refusal(key) is None

    def test_a_governing_prefix_is_not_a_governing_key(self) -> None:
        """Exact membership, not ``startswith``.

        A prefix test would refuse ``learning.schema_evolution.pilot``,
        which no module declares, and would keep refusing it after the
        real key was renamed — the rule's equality check cannot see a
        refusal that fires on ids nothing declares.
        """
        assert governing_key_refusal("learning.schema_evolution.pilot") is None

    def test_unattended_writer_refusal_names_writer_and_operation(self) -> None:
        refusal = unattended_writer_refusal(_command(_FORBIDDEN, _WORKER))
        assert refusal is not None
        assert _WORKER in refusal
        assert Operation.REDACTION_APPLY.value in refusal
