"""Tests for PrecedentDemoteHandler.

Covers ``precedent.demote``: an SCD-2 stamp (``Lifecycle.state ==
"demoted"``) on a precedent's graph node, contrasted with ``redact``'s
irreversible purge -- there is no dedicated reversal verb for this one
yet, only ``entity.update`` setting the state back. Pins the
handler-level checks — node existence, graph-node-backed scope
(:func:`~trellis.mutate.handlers._is_promoted_precedent_event`),
``node_type == "precedent"``, provenance, current-state re-demotion
idempotency, and the non-``current``-state refusal — and the provenance
split this PR introduces: a caller submitting under the (not-yet-wired)
automatic requester identity may demote only a precedent whose own
``precedent.promote`` event was itself automatic, while every other
caller (a human) may demote any graph-backed precedent that is currently
``current``. The pack-withholding effect of the stamp is covered in
``tests/unit/retrieve/test_withholding.py``; the ``list_precedents`` /
``/precedents/domains`` exclusion effects in
``tests/unit/retrieve/test_precedents.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ValidationError
from trellis.learning.candidate_auto_promote import AUTO_PROMOTE_REQUESTED_BY
from trellis.mutate import build_curate_executor, handlers
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    AUTO_DEMOTE_REQUESTED_BY,
    MAX_DEMOTION_REASON_CHARS,
    PrecedentDemoteHandler,
    PrecedentReinstateHandler,
    _was_auto_promoted,
    create_curate_handlers,
    precedent_already_demoted_message,
)
from trellis.retrieve.builder_factory import build_strategies
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create_node(registry: StoreRegistry, **kwargs: Any) -> str:
    return registry.knowledge.graph_store.upsert_node(
        node_id=kwargs.get("node_id"),
        node_type=kwargs.get("node_type", "precedent"),
        properties=kwargs.get("properties", {"name": "Pooling pattern"}),
        document_ids=kwargs.get("document_ids"),
    )


def _promote(registry: StoreRegistry, target_id: str, *, requested_by: str) -> None:
    """Submit a real ``precedent.promote`` through the executor.

    Only the executor's Stage 5 emits ``MUTATION_EXECUTED`` (the
    handler itself only emits ``PRECEDENT_PROMOTED``), so
    :func:`_was_auto_promoted`'s query needs a real promote command to
    have gone through ``build_curate_executor``, not a direct
    ``.handle()`` call.
    """
    cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={"title": "Pooling pattern", "description": "desc"},
        target_id=target_id,
        target_type="entity",
        requested_by=requested_by,
    )
    result = build_curate_executor(registry).execute(cmd)
    assert result.status == CommandStatus.SUCCESS


def _demote(
    target_id: str,
    reason: str = "superseded by a broader pattern",
    *,
    requested_by: str = "cli:demote-precedent",
) -> Command:
    return Command(
        operation=Operation.PRECEDENT_DEMOTE,
        args={"target_id": target_id, "reason": reason},
        target_id=target_id,
        target_type="precedent",
        requested_by=requested_by,
    )


def _reinstate(
    target_id: str,
    reason: str = "re-reviewed and still useful",
    *,
    requested_by: str = "cli:reinstate-precedent",
) -> Command:
    return Command(
        operation=Operation.PRECEDENT_REINSTATE,
        args={"target_id": target_id, "reason": reason},
        target_id=target_id,
        target_type="precedent",
        requested_by=requested_by,
    )


def _promote_trace(registry: StoreRegistry, trace_id: str) -> None:
    """Submit a real trace-sourced ``precedent.promote`` (no graph entity).

    Mirrors ``PrecedentPromoteHandler``'s trace-mined path: ``target_id``
    is left unset so the handler falls back to ``trace_id`` as the
    emitted event's ``entity_id``, and no graph node is ever minted for
    it. This is the fixture the graph-node-scope tests below need: a
    real ``PRECEDENT_PROMOTED`` event whose id is not graph-backed.
    """
    cmd = Command(
        operation=Operation.PRECEDENT_PROMOTE,
        args={"trace_id": trace_id, "title": "A mined pattern", "description": "desc"},
        target_type="trace",
        requested_by="cli:promote",
    )
    result = build_curate_executor(registry).execute(cmd)
    assert result.status == CommandStatus.SUCCESS


class TestPrecedentDemoteHandler:
    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.PRECEDENT_DEMOTE in handlers
        assert isinstance(handlers[Operation.PRECEDENT_DEMOTE], PrecedentDemoteHandler)

    def test_demotes_precedent_node(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        node_id = _create_node(registry)

        returned_id, message = PrecedentDemoteHandler(registry).handle(
            _demote(node_id, reason="no longer useful")
        )

        assert returned_id == node_id
        assert node_id in message
        node = graph.get_node(node_id)
        assert node is not None
        lifecycle = node["properties"][LIFECYCLE_KEY]
        assert lifecycle["state"] == "demoted"
        assert lifecycle["deprecation_reason"] == "no longer useful"

        # SCD-2: reversible, so the pre-demotion version survives in
        # history. ``get_node_history`` orders newest-first, so the
        # demoted version is ``history[0]`` and everything after it
        # predates the demotion.
        history = graph.get_node_history(node_id)
        assert len(history) >= 2
        assert history[0]["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        assert not any(
            (v.get("properties", {}).get(LIFECYCLE_KEY) or {}).get("state") == "demoted"
            for v in history[1:]
        )

        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_DEMOTED
        )
        assert len(events) == 1
        assert events[0].payload["target_id"] == node_id
        assert events[0].payload["reason"] == "no longer useful"

    # -- Acceptance test 1: non-precedent targets are refused --------------

    def test_refuses_non_precedent_target(self, registry: StoreRegistry) -> None:
        """A handler that drops the ``node_type`` check must demote (fail this test).

        Defective subject: comment out the ``node["node_type"] !=
        "precedent"`` branch in ``PrecedentDemoteHandler.handle`` — the
        owner node below then gets demoted and this assertion fails.
        """
        owner_id = _create_node(
            registry, node_type="owner", properties={"name": "Nate"}
        )

        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote(owner_id))

        assert exc_info.value.code == "target_not_precedent"
        node = registry.knowledge.graph_store.get_node(owner_id)
        assert node is not None
        assert LIFECYCLE_KEY not in node["properties"]

    def test_missing_target_is_refused(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote("nonexistent"))
        assert exc_info.value.code == "target_not_found"

    # -- Acceptance test 2: provenance depends on who is demoting ----------

    def test_auto_caller_refused_for_human_approved_precedent(
        self, registry: StoreRegistry
    ) -> None:
        """An automatic caller may demote only what it itself auto-promoted.

        Defective subject (a): a handler that trusts a caller-supplied
        ``is_auto_created`` boolean instead of re-deriving provenance
        from the event log would accept whatever a caller claims and
        this test would pass a forged claim through — make
        ``PrecedentDemoteHandler.handle`` read
        ``command.args.get("is_auto_created")`` instead of calling
        :func:`_was_auto_promoted`, pass a command with that flag set to
        ``True``, and watch it demote a human-approved precedent anyway.
        """
        node_id = _create_node(registry)
        _promote(registry, node_id, requested_by="cli:promote-learning")

        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(
                _demote(node_id, requested_by=AUTO_DEMOTE_REQUESTED_BY)
            )

        assert exc_info.value.code == "target_not_auto_promoted"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert LIFECYCLE_KEY not in node["properties"]

    def test_auto_caller_may_demote_auto_promoted_precedent(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry)
        _promote(registry, node_id, requested_by=AUTO_PROMOTE_REQUESTED_BY)

        returned_id, _ = PrecedentDemoteHandler(registry).handle(
            _demote(node_id, requested_by=AUTO_DEMOTE_REQUESTED_BY)
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_human_caller_may_demote_any_precedent(
        self, registry: StoreRegistry
    ) -> None:
        """A human (or any non-automatic caller) may demote any precedent,
        including one the automatic policy promoted.

        Defective subject (b): a handler that checks provenance
        unconditionally (never branching on ``command.requested_by ==
        AUTO_DEMOTE_REQUESTED_BY``) refuses this human caller too —
        remove the ``if command.requested_by ==
        AUTO_DEMOTE_REQUESTED_BY:`` guard in
        ``PrecedentDemoteHandler.handle`` so the check always runs, and
        this human-initiated demote of an auto-promoted precedent raises
        instead of succeeding.
        """
        node_id = _create_node(registry)
        _promote(registry, node_id, requested_by=AUTO_PROMOTE_REQUESTED_BY)

        returned_id, _ = PrecedentDemoteHandler(registry).handle(
            _demote(node_id, requested_by="cli:demote-precedent")
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_was_auto_promoted_false_with_no_promote_event(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry)
        assert _was_auto_promoted(registry.operational.event_log, node_id) is False

    # -- Reason validation (mirrors RedactionApplyHandler) ------------------

    def test_blank_reason_rejected(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote(node_id, reason=""))
        assert exc_info.value.code == "demotion_reason_required"

    def test_overlong_reason_rejected(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(
                _demote(node_id, reason="x" * (MAX_DEMOTION_REASON_CHARS + 1))
            )
        assert exc_info.value.code == "demotion_reason_too_long"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert LIFECYCLE_KEY not in node["properties"]

    # -- Through the executor ------------------------------------------------

    def test_through_executor_success(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        result = build_curate_executor(registry).execute(_demote(node_id))
        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == node_id

    def test_missing_target_through_executor_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry).execute(_demote("nope"))
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "target_not_found"

    # -- Re-demoting an already-demoted precedent is an idempotent no-op --

    def test_redemoting_is_an_idempotent_noop(self, registry: StoreRegistry) -> None:
        """A second demote on an already-demoted precedent writes nothing.

        No new SCD-2 version, no new PRECEDENT_DEMOTED event, the stored
        ``deprecation_reason`` from the first demotion survives
        untouched, and the handler's message says so explicitly.

        Defective subject: a handler that skips the
        ``current_state == "demoted"`` check and always falls through to
        the write would open a second SCD-2 version and emit a second
        event here — change the condition to ``if False:`` (or delete
        the branch) in ``PrecedentDemoteHandler.handle`` and both the
        history-length and event-count assertions below fail.
        """
        graph = registry.knowledge.graph_store
        event_log = registry.operational.event_log
        node_id = _create_node(registry)
        handler = PrecedentDemoteHandler(registry)

        handler.handle(_demote(node_id, reason="first reason"))
        history_after_first = graph.get_node_history(node_id)
        events_after_first = event_log.get_events(
            event_type=EventType.PRECEDENT_DEMOTED
        )
        assert len(events_after_first) == 1

        returned_id, message = handler.handle(
            _demote(node_id, reason="a different reason, ignored")
        )

        assert returned_id == node_id
        assert message == precedent_already_demoted_message(node_id)
        history_after_second = graph.get_node_history(node_id)
        assert len(history_after_second) == len(history_after_first)
        events_after_second = event_log.get_events(
            event_type=EventType.PRECEDENT_DEMOTED
        )
        assert len(events_after_second) == 1
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["deprecation_reason"] == "first reason"

    def test_redemoting_through_executor_is_success_not_rejected(
        self, registry: StoreRegistry
    ) -> None:
        """The CLI/REST/MCP surfaces must see SUCCESS (exit 0 / 200), not a
        rejection, for the idempotent no-op.
        """
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id))

        result = executor.execute(_demote(node_id))

        assert result.status == CommandStatus.SUCCESS
        assert result.message == precedent_already_demoted_message(node_id)

    # -- Only a `current` precedent may be demoted --------------------------

    @pytest.mark.parametrize("state", ["superseded", "archived", "draft", "deprecated"])
    def test_non_current_state_is_refused(
        self, registry: StoreRegistry, state: str
    ) -> None:
        """Demoting a precedent whose lifecycle state is not ``current`` is
        refused, naming the actual state, and never overwrites the
        existing lifecycle record.

        Defective subject: a handler that omits the
        ``current_state != CURRENT_STATE`` check would demote the
        ``superseded``/``archived``/``draft``/``deprecated`` node below
        instead of refusing it — delete that branch in
        ``PrecedentDemoteHandler.handle`` and this test's
        ``pytest.raises`` finds nothing raised.
        """
        node_id = _create_node(registry)
        graph = registry.knowledge.graph_store
        lifecycle = Lifecycle(state=state).model_dump(mode="json")  # type: ignore[arg-type]
        node = graph.get_node(node_id)
        assert node is not None
        graph.upsert_node(
            node_id=node_id,
            node_type="precedent",
            properties={**node["properties"], LIFECYCLE_KEY: lifecycle},
        )

        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote(node_id))

        assert exc_info.value.code == "target_not_current"
        assert state in str(exc_info.value)
        node_after = graph.get_node(node_id)
        assert node_after is not None
        assert node_after["properties"][LIFECYCLE_KEY]["state"] == state

    def test_current_state_may_still_be_demoted(self, registry: StoreRegistry) -> None:
        """Sanity check alongside the parametrized refusal above: a precedent
        with no lifecycle stamp at all (fail-open to "current") and one
        explicitly stamped ``"current"`` both still demote successfully.
        """
        node_id = _create_node(registry)
        graph = registry.knowledge.graph_store
        lifecycle = Lifecycle(state="current").model_dump(mode="json")
        node = graph.get_node(node_id)
        assert node is not None
        graph.upsert_node(
            node_id=node_id,
            node_type="precedent",
            properties={**node["properties"], LIFECYCLE_KEY: lifecycle},
        )

        returned_id, _ = PrecedentDemoteHandler(registry).handle(_demote(node_id))

        assert returned_id == node_id
        node_after = graph.get_node(node_id)
        assert node_after is not None
        assert node_after["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    # -- Only a graph-backed (learning-promoted) precedent can be demoted;
    # -- a trace-/miner-sourced precedent id gets a legible refusal, not
    # -- "Node not found" ----------------------------------------------------

    def test_trace_sourced_precedent_refused_with_legible_message(
        self, registry: StoreRegistry
    ) -> None:
        """A trace- or miner-sourced precedent id has a real
        ``PRECEDENT_PROMOTED`` event but never minted a graph entity.
        Demoting it must say so specifically, not report the generic
        "Node not found" an unrelated/garbage id gets.

        Defective subject: a handler that checks only ``graph.get_node(...)
        is None`` and always raises the generic ``target_not_found``
        message would pass a garbage-id test identically to this one —
        remove the ``_is_promoted_precedent_event`` branch in
        ``PrecedentDemoteHandler.handle`` and this test's ``code``
        assertion fails (it falls back to ``target_not_found``).
        """
        _promote_trace(registry, "trace-abc-123")

        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote("trace-abc-123"))

        assert exc_info.value.code == "target_not_graph_backed"
        message = str(exc_info.value)
        assert "only promoted learning precedents can be demoted" in message
        assert "has no graph node" in message

    def test_trace_sourced_precedent_message_differs_from_garbage_id(
        self, registry: StoreRegistry
    ) -> None:
        """A genuinely unrelated id (no promote event, no graph node at
        all) still gets the generic "Node not found" refusal -- the two
        failure modes must not collapse into the same message.
        """
        with pytest.raises(ValidationError) as exc_info:
            PrecedentDemoteHandler(registry).handle(_demote("totally-unrelated-id"))

        assert exc_info.value.code == "target_not_found"
        assert "Node not found" in str(exc_info.value)


class TestPrecedentReinstateHandler:
    """Covers ``precedent.reinstate`` (item 4 of the demotion-holes
    follow-up): the reversal verb ``precedent.demote`` shipped without.
    Mirrors ``TestPrecedentDemoteHandler``'s structure -- same reason
    validation, same not-found/not-graph-backed/not-a-precedent checks --
    plus the two behaviours unique to reinstatement: it is human-only (no
    automatic requester may reinstate anything), and only a currently
    ``"demoted"`` target may be reinstated (no idempotent no-op branch).
    """

    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.PRECEDENT_REINSTATE in handlers
        assert isinstance(
            handlers[Operation.PRECEDENT_REINSTATE], PrecedentReinstateHandler
        )

    def test_reinstates_a_demoted_precedent(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        node_id = _create_node(registry)
        PrecedentDemoteHandler(registry).handle(_demote(node_id, reason="stale"))

        returned_id, message = PrecedentReinstateHandler(registry).handle(
            _reinstate(node_id, reason="re-reviewed, still a good pattern")
        )

        assert returned_id == node_id
        assert node_id in message
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "current"

        # SCD-2: the demoted version survives in history, newest-first.
        history = graph.get_node_history(node_id)
        assert len(history) >= 3
        assert history[0]["properties"][LIFECYCLE_KEY]["state"] == "current"
        assert any(
            (v.get("properties", {}).get(LIFECYCLE_KEY) or {}).get("state") == "demoted"
            for v in history[1:]
        )

        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert len(events) == 1
        assert events[0].payload["target_id"] == node_id
        assert events[0].payload["reason"] == "re-reviewed, still a good pattern"
        assert events[0].entity_id == node_id

    # -- Human-only: the automatic requester identity is refused outright --

    def test_automatic_requester_is_refused(self, registry: StoreRegistry) -> None:
        """Defective subject: a handler that omits the
        ``command.requested_by == AUTO_DEMOTE_REQUESTED_BY`` guard (or
        narrows it the way demotion narrows its own provenance check,
        rather than refusing outright) lets the automatic identity
        reinstate -- delete the ``if command.requested_by ==
        AUTO_DEMOTE_REQUESTED_BY:`` branch in
        ``PrecedentReinstateHandler.handle`` and this test's
        ``pytest.raises`` finds nothing raised.
        """
        node_id = _create_node(registry)
        PrecedentDemoteHandler(registry).handle(_demote(node_id))

        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(
                _reinstate(node_id, requested_by=AUTO_DEMOTE_REQUESTED_BY)
            )

        assert exc_info.value.code == "reinstate_requires_human"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    # -- Only a currently-demoted target may be reinstated; no idempotent
    # -- no-op the way re-demoting an already-demoted precedent has one --

    @pytest.mark.parametrize(
        "state", ["current", "superseded", "archived", "draft", "deprecated"]
    )
    def test_non_demoted_state_is_refused(
        self, registry: StoreRegistry, state: str
    ) -> None:
        """Defective subject: a handler that omits the ``current_state !=
        "demoted"`` check would reinstate every state below instead of
        refusing it -- delete that branch in
        ``PrecedentReinstateHandler.handle`` and this test's
        ``pytest.raises`` finds nothing raised.
        """
        node_id = _create_node(registry)
        graph = registry.knowledge.graph_store
        lifecycle = Lifecycle(state=state).model_dump(mode="json")  # type: ignore[arg-type]
        node = graph.get_node(node_id)
        assert node is not None
        graph.upsert_node(
            node_id=node_id,
            node_type="precedent",
            properties={**node["properties"], LIFECYCLE_KEY: lifecycle},
        )

        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate(node_id))

        assert exc_info.value.code == "target_not_demoted"
        assert state in str(exc_info.value)
        node_after = graph.get_node(node_id)
        assert node_after is not None
        assert node_after["properties"][LIFECYCLE_KEY]["state"] == state

    def test_reinstating_twice_is_refused_not_a_silent_noop(
        self, registry: StoreRegistry
    ) -> None:
        """The first reinstate already moved the state to "current", so a
        second reinstate call is just the ``state == "current"`` branch
        of the parametrized test above -- pinned explicitly because this
        is the specific shape item 4 calls out ("reinstating twice").
        """
        node_id = _create_node(registry)
        PrecedentDemoteHandler(registry).handle(_demote(node_id))
        PrecedentReinstateHandler(registry).handle(_reinstate(node_id))

        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate(node_id))

        assert exc_info.value.code == "target_not_demoted"

    # -- Not found / not graph-backed / not a precedent ---------------------

    def test_missing_target_is_refused(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate("nonexistent"))
        assert exc_info.value.code == "target_not_found"

    def test_refuses_non_precedent_target(self, registry: StoreRegistry) -> None:
        owner_id = _create_node(
            registry, node_type="owner", properties={"name": "Nate"}
        )
        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate(owner_id))
        assert exc_info.value.code == "target_not_precedent"

    def test_trace_sourced_precedent_refused_with_legible_message(
        self, registry: StoreRegistry
    ) -> None:
        _promote_trace(registry, "trace-abc-123")

        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate("trace-abc-123"))

        assert exc_info.value.code == "target_not_graph_backed"
        message = str(exc_info.value)
        assert "only promoted learning precedents can be reinstated" in message
        assert "has no graph node" in message

    # -- Reason validation ----------------------------------------------------

    def test_blank_reason_rejected(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        PrecedentDemoteHandler(registry).handle(_demote(node_id))
        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(_reinstate(node_id, reason=""))
        assert exc_info.value.code == "reinstate_reason_required"

    def test_overlong_reason_rejected(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        PrecedentDemoteHandler(registry).handle(_demote(node_id))
        with pytest.raises(ValidationError) as exc_info:
            PrecedentReinstateHandler(registry).handle(
                _reinstate(node_id, reason="x" * (MAX_DEMOTION_REASON_CHARS + 1))
            )
        assert exc_info.value.code == "reinstate_reason_too_long"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    # -- Through the executor ------------------------------------------------

    def test_through_executor_success(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id))

        result = executor.execute(_reinstate(node_id))

        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == node_id

    def test_human_identity_through_executor_is_not_narrowed(
        self, registry: StoreRegistry
    ) -> None:
        """Unlike demotion's auto-caller narrowing, reinstatement has no
        "but it promoted this one" exception -- a human may reinstate a
        precedent the automatic policy itself promoted and demoted.
        """
        node_id = _create_node(registry)
        _promote(registry, node_id, requested_by=AUTO_PROMOTE_REQUESTED_BY)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id, requested_by=AUTO_DEMOTE_REQUESTED_BY))

        result = executor.execute(_reinstate(node_id, requested_by="cli:reinstate"))

        assert result.status == CommandStatus.SUCCESS


class TestVectorRowSyncOnDemoteAndReinstate:
    """``PrecedentDemoteHandler`` and ``PrecedentReinstateHandler`` must
    each reach the precedent's *vector* row, not just its graph node -- a
    vector row's metadata is a snapshot taken at embed time
    (:func:`~trellis.mutate.handlers._sync_vector_lifecycle`), and the
    semantic search strategy serves from that snapshot. Mirrors
    ``TestVectorRowSync`` in ``test_retention_handler.py``, which pins the
    same gap for archival/restore.

    End to end: a vector row is embedded, demoted, and run through the
    real collect seam -- the same ``SemanticSearch`` strategy and
    ``PackBuilder`` every production surface uses
    (:func:`~trellis.retrieve.builder_factory.build_strategies`), not a
    hand-authored ``PackItem`` standing in for what the strategy would
    have produced -- which must withhold it. Then reinstated, and the
    seam must stop withholding it.
    """

    @staticmethod
    def _embed(registry: StoreRegistry, node_id: str) -> None:
        registry.knowledge.vector_store.upsert(
            node_id,
            [0.1, 0.2, 0.3],
            {"excerpt": f"excerpt of {node_id}", "content_tags": {}},
        )

    @staticmethod
    def _semantic_strategy(registry: StoreRegistry) -> SearchStrategy:
        """The real ``SemanticSearch`` strategy, built the same way
        :func:`~trellis.retrieve.builder_factory.build_pack_builder` builds
        it for every production surface. Isolated from ``KeywordSearch``/
        ``GraphSearch`` (also in ``build_strategies``'s returned list) the
        same way the existing document-restore seam tests isolate
        ``KeywordSearch`` -- a live graph read would otherwise mask the
        very staleness this test exists to catch.
        """
        result = build_strategies(
            registry,  # type: ignore[arg-type]
            embedding_fn=lambda _text: [0.1, 0.2, 0.3],
        )
        return next(s for s in result.strategies if s.name == "semantic")

    def test_demote_stamps_the_vector_row_and_the_seam_withholds_it(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: remove the ``_sync_vector_lifecycle(...)``
        call from ``PrecedentDemoteHandler.handle`` and the vector row's
        ``metadata`` keeps no ``lifecycle`` key at all -- the row-level
        assertion fails first, and the real collect seam would still
        serve the stale hit rather than withhold it.
        """
        node_id = _create_node(registry)
        self._embed(registry, node_id)

        build_curate_executor(registry).execute(_demote(node_id, reason="stale"))

        row = registry.knowledge.vector_store.get(node_id)
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == "demoted"
        # Metadata-only: nothing was re-embedded.
        assert row["vector"] == pytest.approx([0.1, 0.2, 0.3])

        builder = PackBuilder(strategies=[self._semantic_strategy(registry)])
        pack = builder.build(intent="pooling pattern")
        assert node_id not in [item.item_id for item in pack.items]

    def test_reinstate_re_syncs_the_vector_row_and_the_seam_serves_it_again(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: remove the ``_sync_vector_lifecycle(...)``
        call from ``PrecedentReinstateHandler.handle`` and the vector row
        below keeps reading ``demoted`` after reinstatement -- the real
        collect seam would then keep withholding a precedent that
        ``get_node`` already reports as current again.
        """
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id, reason="stale"))

        executor.execute(_reinstate(node_id, reason="re-reviewed"))

        row = registry.knowledge.vector_store.get(node_id)
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == "current"

        builder = PackBuilder(strategies=[self._semantic_strategy(registry)])
        pack = builder.build(intent="pooling pattern")
        assert node_id in [item.item_id for item in pack.items]

    def test_missing_vector_row_is_not_an_error_for_either_verb(
        self, registry: StoreRegistry
    ) -> None:
        """A precedent never embedded (no vector row) must still be
        demotable and reinstatable -- the sync is a best-effort repair,
        not a precondition.
        """
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)

        demote_result = executor.execute(_demote(node_id, reason="stale"))
        assert demote_result.status == CommandStatus.SUCCESS
        assert registry.knowledge.vector_store.get(node_id) is None

        reinstate_result = executor.execute(_reinstate(node_id, reason="back"))
        assert reinstate_result.status == CommandStatus.SUCCESS
        assert registry.knowledge.vector_store.get(node_id) is None


class TestVectorSyncFailureSurfacesAsAWarning:
    """F3: a vector-sync failure on demote/reinstate must not be only a
    WARNING log line. The graph write already landed by the time the
    sync runs (fail soft, not a rollback), so the command still reports
    SUCCESS -- but a JSON caller has no other way to learn the semantic
    path went stale, so the same condition must also land in the
    result's ``warnings``. The log line itself must name the actual
    verb (``precedent_demote_...`` / ``precedent_reinstate_...``) rather
    than the generic ``retention_...`` name shared with prune/restore,
    since this is neither.
    """

    @staticmethod
    def _embed(registry: StoreRegistry, node_id: str) -> None:
        registry.knowledge.vector_store.upsert(
            node_id,
            [0.1, 0.2, 0.3],
            {"excerpt": f"excerpt of {node_id}", "content_tags": {}},
        )

    @staticmethod
    def _break_vector_upsert(
        registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise_upsert(*_args: Any, **_kwargs: Any) -> None:
            msg = "vector backend unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.vector_store, "upsert", _raise_upsert)

    def test_demote_reports_a_vector_sync_failure_in_warnings(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        self._break_vector_upsert(registry, monkeypatch)

        result = build_curate_executor(registry).execute(
            _demote(node_id, reason="stale")
        )

        # The graph write still landed: a backend outage on the mirror
        # must not roll back the demotion itself.
        assert result.status == CommandStatus.SUCCESS
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        assert any(node_id in w for w in result.warnings)
        assert any(
            "precedent_demote_vector_lifecycle_sync_failed" in w
            for w in result.warnings
        )

    def test_reinstate_reports_a_vector_sync_failure_in_warnings(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id, reason="stale"))
        self._break_vector_upsert(registry, monkeypatch)

        result = executor.execute(_reinstate(node_id, reason="back"))

        assert result.status == CommandStatus.SUCCESS
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "current"
        assert any(node_id in w for w in result.warnings)
        assert any(
            "precedent_reinstate_vector_lifecycle_sync_failed" in w
            for w in result.warnings
        )

    def test_demote_reports_a_vector_sync_failure_in_the_event_payload(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CommandResult.warnings`` is a synchronous read, but a
        consumer of the event log (rather than the command response)
        needs its own way to see the semantic path went stale -- the
        ``PRECEDENT_DEMOTED`` event itself must carry the failure, not
        only a log line.

        Defective subject: drop the ``vector_sync_failed`` key from
        ``PrecedentDemoteHandler``'s event payload and this assertion
        finds nothing.
        """
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        self._break_vector_upsert(registry, monkeypatch)

        result = build_curate_executor(registry).execute(
            _demote(node_id, reason="stale")
        )

        assert result.status == CommandStatus.SUCCESS
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_DEMOTED
        )
        assert len(events) == 1
        assert events[0].payload["vector_sync_failed"] is True

    def test_reinstate_reports_a_vector_sync_failure_in_the_event_payload(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id, reason="stale"))
        self._break_vector_upsert(registry, monkeypatch)

        result = executor.execute(_reinstate(node_id, reason="back"))

        assert result.status == CommandStatus.SUCCESS
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert len(events) == 1
        assert events[0].payload["vector_sync_failed"] is True

    def test_demote_event_payload_omits_the_key_on_success(
        self, registry: StoreRegistry
    ) -> None:
        """Control case: a healthy sync must not grow the key at all, so a
        payload with no vector backend trouble stays byte-identical to
        before this fix existed."""
        node_id = _create_node(registry)
        self._embed(registry, node_id)

        build_curate_executor(registry).execute(_demote(node_id, reason="stale"))

        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_DEMOTED
        )
        assert len(events) == 1
        assert "vector_sync_failed" not in events[0].payload


class TestVectorSyncRoutesThroughTheSharedMirror:
    """The demote/reinstate vector stamp must go through the same mirror
    the classify layer uses for ``content_tags``/``auto_importance``
    (:func:`~trellis.core.vector_metadata.sync_vector_metadata_outcome`),
    not a second, private get/merge/upsert sequence kept in sync by hand.

    A revert to that private sequence would still pass every test in
    ``TestVectorRowSyncOnDemoteAndReinstate`` above -- the row ends up
    stamped either way, since both implementations produce the same
    effect -- so this spies on the shared function directly instead of
    inferring routing from an effect two implementations share.
    """

    @staticmethod
    def _embed(registry: StoreRegistry, node_id: str) -> None:
        registry.knowledge.vector_store.upsert(
            node_id,
            [0.1, 0.2, 0.3],
            {"excerpt": f"excerpt of {node_id}", "content_tags": {}},
        )

    @staticmethod
    def _spy_on_the_mirror(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[tuple[str, tuple[str, ...]]]:
        calls: list[tuple[str, tuple[str, ...]]] = []
        real = handlers.sync_vector_metadata_outcome

        def _wrapped(
            store: Any, item_id: str, document_metadata: Any, *, keys: Any
        ) -> Any:
            calls.append((item_id, tuple(keys)))
            return real(store, item_id, document_metadata, keys=keys)

        monkeypatch.setattr(handlers, "sync_vector_metadata_outcome", _wrapped)
        return calls

    def test_demote_calls_the_shared_vector_metadata_mirror(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        calls = self._spy_on_the_mirror(monkeypatch)

        build_curate_executor(registry).execute(_demote(node_id, reason="stale"))

        assert calls == [(node_id, (LIFECYCLE_KEY,))], calls

    def test_reinstate_calls_the_shared_vector_metadata_mirror(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        self._embed(registry, node_id)
        executor = build_curate_executor(registry)
        executor.execute(_demote(node_id, reason="stale"))
        calls = self._spy_on_the_mirror(monkeypatch)

        executor.execute(_reinstate(node_id, reason="back"))

        assert calls == [(node_id, (LIFECYCLE_KEY,))], calls

    def test_a_missing_vector_row_still_calls_the_shared_mirror(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No embedding at all: the mirror is still the one asked, and it
        is the mirror's own "absent" outcome -- not a pre-check in the
        handler -- that turns a missing row into a no-op.
        """
        node_id = _create_node(registry)
        calls = self._spy_on_the_mirror(monkeypatch)

        result = build_curate_executor(registry).execute(
            _demote(node_id, reason="stale")
        )

        assert result.status == CommandStatus.SUCCESS
        assert calls == [(node_id, (LIFECYCLE_KEY,))], calls
