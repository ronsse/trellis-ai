"""Admin-only handlers for the schedule-registry write surface.

``ScheduleUpdateHandler`` and ``JobRunRequestHandler`` are the only code
that calls :meth:`~trellis.stores.schedule_store.ScheduleStore.put`. They
back the admin REST route (``PATCH /admin/jobs/{name}``,
``POST /admin/jobs/{name}/run``) and the CLI (``trellis admin schedule
set`` / ``run``) through :func:`trellis.mutate.build_schedule_executor` —
and *only* that executor. Deliberately **not** part of
``create_curate_handlers``: that dict backs ``POST /api/v1/commands/batch``
(``mutate`` scope) and MCP ``execute_mutation``, and a mutate-scoped
credential must not be able to edit a job's schedule or request a run —
those are admin-scoped actions by a different authorization axis
entirely. Submitting either operation to the curate-wired executor hits
"no handler registered for: <operation>" (``CommandStatus.FAILED``), the
same refusal path an unknown operation gets anywhere else
(``tests/unit/mutate/test_schedule_handlers.py::TestNotRegisteredInCurateHandlers``,
``tests/unit/api/test_routes.py::test_batch_refuses_schedule_admin_operations``,
``tests/unit/mcp/test_execute_mutation.py::
TestScheduleAdminOperationsAreNotMcpReachable`` pin this).

Both handlers do their own validation *before* calling
:meth:`ScheduleStore.put`: target-type/id stamping, a field allowlist (so
an extra key is refused, never silently written), the catalog-membership
and cron/bounds checks :class:`~trellis.schemas.schedule.ScheduledJob`
already enforces, and the store's own degraded/stale refusal. Every one
of those raises :class:`~trellis.errors.ValidationError`, which the
executor turns into a ``REJECTED`` result naming ``code`` in
``metadata["rejection_reason"]`` — never a bare ``StoreWriteRefusedError``
escaping to ``CommandStatus.FAILED``, which would have discarded the
store's own error text (see ``executor.py``'s ``except (StoreError,
TrellisError)`` branch, which sets no metadata at all).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ValidationError as PydanticValidationError

from trellis.core.base import utc_now
from trellis.core.error_sanitize import describe_validation_error
from trellis.errors import StoreWriteRefusedError, ValidationError
from trellis.mutate.commands import Command
from trellis.schedule.catalog import JOB_CATALOG, JobSpec
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.base.event_log import EventType
from trellis.stores.schedule_store import ScheduleStore

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

#: The only fields ``schedule.update`` may touch — the same boundary the
#: route's and CLI's wire layers already enforce (``extra="forbid"`` on
#: ``AdminJobPatchRequest``; the Typer options on ``admin schedule set``).
#: Enforced again here because this handler is reachable from any future
#: caller of ``build_schedule_executor``, not only today's two surfaces.
_ALLOWED_UPDATE_FIELDS = frozenset({"enabled", "cadence", "timeout_seconds"})


def _schedule_store_for(registry: StoreRegistry) -> ScheduleStore:
    """Build a fresh, uncached :class:`ScheduleStore`.

    Mirrors ``trellis_api.routes.admin._get_schedule_store``'s rationale:
    a host CLI and a containerised API both write the same bind-mounted
    ``schedule.json``, so caching here could roll back a concurrent
    write. Not imported from ``trellis_api`` — ``trellis.mutate`` sits
    below ``trellis_api`` in the import order and must not depend on it.
    """
    stores_dir = registry.stores_dir
    if stores_dir is None:
        msg = "stores_dir must be set on registry to use ScheduleStore"
        raise ValueError(msg)
    return ScheduleStore(stores_dir / "schedule.json")


def _require_schedule_target(command: Command) -> str:
    """The job name, or a raised ``ValidationError`` if mis-stamped.

    Both admin operations are meaningless without a catalog job name to
    act on, and this is a different failure than "unknown job" — it says
    the *caller* built the command wrong (e.g. a batch/MCP surface that
    forgot ``target_type``), not that the name it gave isn't catalogued.
    """
    if command.target_type != "schedule" or not command.target_id:
        msg = (
            "schedule admin operations require target_type='schedule' and "
            f"a target_id; got target_type={command.target_type!r} "
            f"target_id={command.target_id!r}"
        )
        raise ValidationError(msg, code="target_mismatch")
    return command.target_id


def _require_catalog_job(name: str) -> JobSpec:
    spec = JOB_CATALOG.get(name)
    if spec is None:
        msg = f"{name!r} is not a trellis.schedule.catalog.JOB_CATALOG job"
        raise ValidationError(msg, code="unknown_job")
    return spec


def _current_or_default(store: ScheduleStore, spec: JobSpec) -> ScheduledJob:
    row = store.get(spec.name)
    if row is not None:
        return row
    return ScheduledJob(name=spec.name, cadence=spec.default_cadence)


def _raise_for_store_refusal(exc: StoreWriteRefusedError) -> NoReturn:
    """Re-raise a store refusal as the handler's own ``ValidationError``.

    ``StoreWriteRefusedError`` is a ``StoreError``, which the executor's
    ``except (StoreError, TrellisError)`` branch turns into
    ``CommandStatus.FAILED`` with *no* metadata at all — the caller would
    see a generic "Execution failed: ..." and lose the ability to tell a
    degraded/stale refusal apart from any other failure. Converting to
    ``ValidationError`` routes it through the ``REJECTED`` branch instead,
    which keeps ``exc.code`` in ``metadata["rejection_reason"]`` and the
    store's own message (recovery command included) in ``result.message``
    — the only two channels a ``CommandResult`` has.
    """
    code = (exc.code or "store_write_refused").lower()
    raise ValidationError(str(exc), code=code) from exc


class ScheduleUpdateHandler:
    """Write ``enabled`` / ``cadence`` / ``timeout_seconds`` for one job.

    ``command.args`` carries only the fields the caller actually set
    (mirrors ``model_fields_set`` / ``_was_passed``) — a field absent from
    ``args`` is left unchanged, never defaulted. All validation —
    target-type stamping, the field allowlist, catalog membership, and
    ``ScheduledJob``'s own cron/bounds checks — runs before
    :meth:`ScheduleStore.put` is ever called, so a rejected update never
    reaches the store.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        name = _require_schedule_target(command)
        spec = _require_catalog_job(name)

        extra = set(command.args) - _ALLOWED_UPDATE_FIELDS
        if extra:
            msg = f"Unsupported schedule field(s): {', '.join(sorted(extra))}"
            raise ValidationError(msg, code="invalid_field")

        store = _schedule_store_for(self._registry)
        current = _current_or_default(store, spec)

        merged = current.model_dump(mode="json")
        old_values: dict[str, Any] = {}
        new_values: dict[str, Any] = {}
        for key in ("enabled", "cadence", "timeout_seconds"):
            if key in command.args:
                old_values[key] = getattr(current, key)
                new_values[key] = command.args[key]
                merged[key] = command.args[key]
        merged["name"] = spec.name
        merged["updated_at"] = utc_now().isoformat()

        try:
            updated = ScheduledJob.model_validate(merged)
        except (PydanticValidationError, TypeError) as exc:
            msg = f"Schedule update validation failed: {describe_validation_error(exc)}"
            raise ValidationError(msg, code="schedule_validation") from exc

        try:
            store.put(updated)
        except StoreWriteRefusedError as exc:
            _raise_for_store_refusal(exc)

        self._registry.operational.event_log.emit(
            EventType.SCHEDULE_UPDATED,
            source="mutation_executor",
            entity_id=f"job:{name}",
            entity_type="scheduled_job",
            payload={
                "job": name,
                "old": old_values,
                "new": new_values,
                "key_id": command.metadata.get("key_id"),
                "key_name": command.metadata.get("key_name"),
            },
        )
        return name, f"Schedule updated: {name}"


class JobRunRequestHandler:
    """Stamp ``run_requested_at`` on one job, leaving every other field as-is.

    Never executes the job inline; a host dispatcher polling
    ``due-jobs`` is what picks the request up. Takes no args at all —
    any key in ``command.args`` is refused.

    Refuses rather than writes a default row for a job with no stored
    schedule (``code="not_scheduled"``) and refuses a disabled job
    (``code="job_disabled"``) instead of silently enrolling either into
    the catalog's default cadence — a deferred run of an LLM-spending
    job would otherwise be a surprise spend (gate #860, blocker 2, F1).
    Both REST (``POST /admin/jobs/{name}/run``) and the CLI
    (``trellis admin schedule run``) go through this one handler via
    :func:`trellis.mutate.build_schedule_executor`, so the rule applies
    identically to both.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        name = _require_schedule_target(command)
        spec = _require_catalog_job(name)

        if command.args:
            extra = ", ".join(sorted(command.args))
            msg = f"job.run_request takes no fields; got: {extra}"
            raise ValidationError(msg, code="invalid_field")

        store = _schedule_store_for(self._registry)
        # The not-scheduled/disabled checks below trust ``store.get()``
        # returning ``None`` to mean "no row" — but on a degraded store
        # ``get()`` also returns ``None`` for a row that exists and just
        # didn't parse (gate #860's probe P5: an operator disables "tune",
        # then one row's JSON goes bad; the degraded read can no longer
        # see that disabled row at all). Only a clean load can tell "no
        # row" apart from "row we can't read", so a degraded store skips
        # straight to the existing refuse-the-write path below, which is
        # correct either way: ``store.put`` always refuses while
        # degraded, regardless of what the (possibly unreadable) current
        # row would have been.
        if store.is_degraded:
            current = _current_or_default(store, spec)
        else:
            maybe_current = store.get(spec.name)
            if maybe_current is None:
                msg = (
                    f"{name!r} has no stored schedule row; run "
                    "'trellis admin init-schedule' first."
                )
                raise ValidationError(msg, code="not_scheduled")
            if not maybe_current.enabled:
                msg = f"{name!r} is disabled; enable it first."
                raise ValidationError(msg, code="job_disabled")
            current = maybe_current

        now = utc_now()
        updated = current.model_copy(
            update={"name": spec.name, "run_requested_at": now, "updated_at": now}
        )

        try:
            store.put(updated)
        except StoreWriteRefusedError as exc:
            _raise_for_store_refusal(exc)

        previous = (
            current.run_requested_at.isoformat()
            if current.run_requested_at is not None
            else None
        )
        self._registry.operational.event_log.emit(
            EventType.JOB_RUN_REQUESTED,
            source="mutation_executor",
            entity_id=f"job:{name}",
            entity_type="scheduled_job",
            payload={
                "job": name,
                "previous_run_requested_at": previous,
                "run_requested_at": now.isoformat(),
                "key_id": command.metadata.get("key_id"),
                "key_name": command.metadata.get("key_name"),
            },
        )
        return name, f"Run requested for {name} at {now.isoformat()}"
