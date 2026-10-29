"""Tests for ``ScheduleUpdateHandler``/``JobRunRequestHandler``: the only
code that calls :meth:`ScheduleStore.put`, reachable only
through :func:`trellis.mutate.build_schedule_executor` — never through
:func:`trellis.mutate.build_curate_executor`'s handler dict, which backs
``POST /api/v1/commands/batch`` and MCP ``execute_mutation``. A mutate-scoped
credential on either of those surfaces must not be able to edit a job's
schedule or request a run; that boundary is pinned here directly (both
operations absent from ``create_curate_handlers``) and end-to-end in
``tests/unit/api/test_routes.py::test_batch_refuses_schedule_admin_operations``
and ``tests/unit/mcp/test_execute_mutation.py::
TestScheduleAdminOperationsAreNotMcpReachable``.

Covers the boundary ``trellis.mutate.schedule_handlers`` documents: a catalog
name only; only ``enabled``/``cadence``/``timeout_seconds`` (``schedule.
update``) or no fields at all (``job.run_request``); every validation step
runs before :meth:`ScheduleStore.put` is ever called; a degraded or stale
store refuses with the store's own error text, never a generic failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.errors import StaleStoreWriteError, ValidationError
from trellis.mutate import build_curate_executor, build_schedule_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.schedule_handlers import (
    JobRunRequestHandler,
    ScheduleUpdateHandler,
)
from trellis.schedule.catalog import JOB_CATALOG
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.schedule_store import ScheduleStore

#: Any real catalog job works; the handlers are job-agnostic.
_JOB = next(iter(JOB_CATALOG))


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _schedule_path(registry: StoreRegistry) -> Path:
    assert registry.stores_dir is not None
    return registry.stores_dir / "schedule.json"


# ---------------------------------------------------------------------------
# The admin/mutate boundary itself
# ---------------------------------------------------------------------------


class TestNotRegisteredInCurateHandlers:
    """Deliberately absent from the dict that backs batch/MCP (see module
    docstring). If a future edit ever merges these in, this is the one
    test whose failure says so directly, not just the end-to-end batch/MCP
    refusal tests."""

    def test_schedule_update_absent(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers_for(registry)
        assert Operation.SCHEDULE_UPDATE not in handlers

    def test_job_run_request_absent(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers_for(registry)
        assert Operation.JOB_RUN_REQUEST not in handlers

    def test_curate_executor_fails_closed_on_schedule_update(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")
        result = executor.execute(Command(operation=Operation.SCHEDULE_UPDATE, args={}))
        assert result.status == CommandStatus.FAILED
        assert "no handler registered" in result.message.lower()

    def test_curate_executor_fails_closed_on_job_run_request(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")
        result = executor.execute(Command(operation=Operation.JOB_RUN_REQUEST, args={}))
        assert result.status == CommandStatus.FAILED
        assert "no handler registered" in result.message.lower()


def create_curate_handlers_for(registry: StoreRegistry) -> dict[Operation, object]:
    """Local import wrapper — mirrors how ``build_curate_executor`` itself
    imports ``create_curate_handlers`` lazily (``trellis/mutate/__init__.py``)."""
    from trellis.mutate.handlers import create_curate_handlers

    return create_curate_handlers(registry)


# ---------------------------------------------------------------------------
# ScheduleUpdateHandler
# ---------------------------------------------------------------------------


class TestScheduleUpdateHandler:
    def test_registered_only_in_schedule_executor(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_schedule_executor(registry)
        assert isinstance(
            executor._handlers[Operation.SCHEDULE_UPDATE], ScheduleUpdateHandler
        )

    def test_requires_target_type_and_id(self, registry: StoreRegistry) -> None:
        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(Command(operation=Operation.SCHEDULE_UPDATE, args={}))
        assert excinfo.value.code == "target_mismatch"

    def test_wrong_target_type_is_refused(self, registry: StoreRegistry) -> None:
        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="trace",
                    target_id=_JOB,
                    args={},
                )
            )
        assert excinfo.value.code == "target_mismatch"

    def test_unknown_job_is_refused(self, registry: StoreRegistry) -> None:
        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="schedule",
                    target_id="not-a-real-job",
                    args={"enabled": False},
                )
            )
        assert excinfo.value.code == "unknown_job"

    def test_unsupported_field_is_refused(self, registry: StoreRegistry) -> None:
        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"command": ["rm", "-rf", "/"]},
                )
            )
        assert excinfo.value.code == "invalid_field"

    def test_validation_runs_before_any_store_write(
        self, registry: StoreRegistry
    ) -> None:
        """A refused update never touches the store file at all — proves
        the field allowlist is checked before ``ScheduleStore.put``, not
        after (the ordering a write-before-validation mutant breaks)."""
        handler = ScheduleUpdateHandler(registry)
        path = _schedule_path(registry)
        assert not path.exists()

        with pytest.raises(ValidationError):
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"argv": ["sh", "-c", "x"]},
                )
            )

        assert not path.exists()

    def test_success_updates_only_named_fields_and_emits_event(
        self, registry: StoreRegistry
    ) -> None:
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name=_JOB, cadence="0 4 * * *", timeout_seconds=100))

        handler = ScheduleUpdateHandler(registry)
        returned_id, message = handler.handle(
            Command(
                operation=Operation.SCHEDULE_UPDATE,
                target_type="schedule",
                target_id=_JOB,
                args={"enabled": False},
                metadata={"key_id": "k1", "key_name": "api:test-key"},
            )
        )

        assert returned_id == _JOB
        assert _JOB in message

        reloaded = ScheduleStore(_schedule_path(registry)).get(_JOB)
        assert reloaded is not None
        assert reloaded.enabled is False
        assert reloaded.cadence == "0 4 * * *"  # untouched
        assert reloaded.timeout_seconds == 100  # untouched

        events = registry.operational.event_log.get_events(
            event_type=EventType.SCHEDULE_UPDATED, entity_id=f"job:{_JOB}"
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["old"]["enabled"] is True
        assert payload["new"]["enabled"] is False
        assert "cadence" not in payload["old"]
        assert payload["key_name"] == "api:test-key"

    def test_degraded_store_refuses_with_the_stores_own_text(
        self, registry: StoreRegistry
    ) -> None:
        path = _schedule_path(registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")

        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"enabled": False},
                )
            )
        assert excinfo.value.code == "degraded_store_write"
        assert str(excinfo.value)  # the store's own text, not a generic one

    def test_stale_store_refuses_with_the_stores_own_text(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self: ScheduleStore, job: ScheduledJob) -> None:
            msg = "the file changed after this process read it"
            raise StaleStoreWriteError(
                msg,
                store="schedule",
                recovery="trellis admin due-jobs --format json",
            )

        monkeypatch.setattr(ScheduleStore, "put", _boom)
        handler = ScheduleUpdateHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.SCHEDULE_UPDATE,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"enabled": False},
                )
            )
        assert excinfo.value.code == "stale_store_write"
        assert "changed after" in str(excinfo.value)

    def test_through_the_schedule_executor_rejection_names_the_reason(
        self, registry: StoreRegistry
    ) -> None:
        """End to end through ``build_schedule_executor``: the
        ``ValidationError`` the handler raises becomes a ``REJECTED``
        ``CommandResult`` naming ``rejection_reason`` in ``metadata`` —
        never a bare ``FAILED`` with no metadata at all."""
        executor = build_schedule_executor(registry)
        result = executor.execute(
            Command(
                operation=Operation.SCHEDULE_UPDATE,
                target_type="schedule",
                target_id="not-a-real-job",
                args={"enabled": False},
            )
        )
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "unknown_job"


# ---------------------------------------------------------------------------
# JobRunRequestHandler
# ---------------------------------------------------------------------------


class TestJobRunRequestHandler:
    def test_registered_only_in_schedule_executor(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_schedule_executor(registry)
        assert isinstance(
            executor._handlers[Operation.JOB_RUN_REQUEST], JobRunRequestHandler
        )

    def test_requires_target_type_and_id(self, registry: StoreRegistry) -> None:
        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(Command(operation=Operation.JOB_RUN_REQUEST, args={}))
        assert excinfo.value.code == "target_mismatch"

    def test_unknown_job_is_refused(self, registry: StoreRegistry) -> None:
        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id="not-a-real-job",
                )
            )
        assert excinfo.value.code == "unknown_job"

    def test_takes_no_fields(self, registry: StoreRegistry) -> None:
        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"run_requested_at": "2026-01-01T00:00:00Z"},
                )
            )
        assert excinfo.value.code == "invalid_field"

    def test_validation_runs_before_any_store_write(
        self, registry: StoreRegistry
    ) -> None:
        handler = JobRunRequestHandler(registry)
        path = _schedule_path(registry)
        assert not path.exists()

        with pytest.raises(ValidationError):
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                    args={"extra": 1},
                )
            )

        assert not path.exists()

    def test_success_writes_only_run_requested_at_and_emits_event(
        self, registry: StoreRegistry
    ) -> None:
        store = ScheduleStore(_schedule_path(registry))
        store.put(
            ScheduledJob(
                name=_JOB, cadence="0 4 * * *", enabled=True, timeout_seconds=77
            )
        )

        handler = JobRunRequestHandler(registry)
        returned_id, message = handler.handle(
            Command(
                operation=Operation.JOB_RUN_REQUEST,
                target_type="schedule",
                target_id=_JOB,
                metadata={"key_id": "k1", "key_name": "api:test-key"},
            )
        )

        assert returned_id == _JOB
        assert _JOB in message

        reloaded = ScheduleStore(_schedule_path(registry)).get(_JOB)
        assert reloaded is not None
        assert reloaded.run_requested_at is not None
        assert reloaded.enabled is True  # untouched
        assert reloaded.cadence == "0 4 * * *"  # untouched
        assert reloaded.timeout_seconds == 77  # untouched

        events = registry.operational.event_log.get_events(
            event_type=EventType.JOB_RUN_REQUESTED, entity_id=f"job:{_JOB}"
        )
        assert len(events) == 1
        assert events[0].payload["key_name"] == "api:test-key"

    def test_never_stored_job_is_refused_not_enrolled(
        self, registry: StoreRegistry
    ) -> None:
        """Gate #860, blocker 2 / F1: run-now on a job with no stored row
        must refuse, never silently write the catalog's default cadence."""
        path = _schedule_path(registry)
        assert not path.exists()

        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                )
            )
        assert excinfo.value.code == "not_scheduled"
        assert not path.exists()  # no row was ever created

    def test_disabled_job_is_refused(self, registry: StoreRegistry) -> None:
        """Gate #860, blocker 2: a deferred run of a disabled job would be
        a surprise spend once a dispatcher eventually picks it up."""
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name=_JOB, cadence="0 4 * * *", enabled=False))

        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                )
            )
        assert excinfo.value.code == "job_disabled"

        reloaded = ScheduleStore(_schedule_path(registry)).get(_JOB)
        assert reloaded is not None
        assert reloaded.run_requested_at is None  # nothing written

    def test_degraded_store_refuses_with_the_stores_own_text(
        self, registry: StoreRegistry
    ) -> None:
        path = _schedule_path(registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")

        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                )
            )
        assert excinfo.value.code == "degraded_store_write"

    def test_degraded_store_refuses_even_a_disabled_stored_row(
        self, registry: StoreRegistry
    ) -> None:
        """Gate #860 probe P5: an operator disables a job, then its row's
        JSON goes bad. A degraded load can't distinguish "disabled" from
        "unreadable", so this must still 409 (degraded_store_write), never
        422 (not_scheduled) — the latter would wrongly imply the row was
        cleanly read and simply absent."""
        path = _schedule_path(registry)
        store = ScheduleStore(path)
        store.put(ScheduledJob(name=_JOB, cadence="0 4 * * *", enabled=False))
        path.write_text("not json", encoding="utf-8")

        handler = JobRunRequestHandler(registry)
        with pytest.raises(ValidationError) as excinfo:
            handler.handle(
                Command(
                    operation=Operation.JOB_RUN_REQUEST,
                    target_type="schedule",
                    target_id=_JOB,
                )
            )
        assert excinfo.value.code == "degraded_store_write"
