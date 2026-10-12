"""Tests for PrecedentDemoteHandler.

Covers ``precedent.demote``: a reversible SCD-2 stamp (``Lifecycle.state
== "demoted"``) on a precedent's graph node, contrasted with ``redact``'s
irreversible purge. Pins the three handler-level checks — node existence,
``node_type == "precedent"``, and provenance — and the provenance split
this PR introduces: a caller submitting under the (not-yet-wired)
automatic requester identity may demote only a precedent whose own
``precedent.promote`` event was itself automatic, while every other
caller (a human) may demote any precedent. The pack-withholding and
``list_precedents`` exclusion effects of the stamp are covered in
``tests/unit/retrieve/test_lifecycle.py`` and
``tests/unit/retrieve/test_precedents.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ValidationError
from trellis.learning.candidate_auto_promote import AUTO_PROMOTE_REQUESTED_BY
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    AUTO_DEMOTE_REQUESTED_BY,
    MAX_DEMOTION_REASON_CHARS,
    PrecedentDemoteHandler,
    _was_auto_promoted,
    create_curate_handlers,
)
from trellis.schemas.classification import LIFECYCLE_KEY
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
