"""Tests for ``GET /api/v1/loops``.

The claim under test: a loop with no event yet must read "never run",
never a bare ``0`` that is indistinguishable from "ran and did
nothing" — and the route requires admin scope like the rest of the
Review-queue surfaces it sits beside.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


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


class TestLoopHealthRoute:
    def test_no_events_reports_never_run_not_zero(self, client) -> None:
        resp = client.get("/api/v1/loops")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["loops"]) == 9
        for row in data["loops"]:
            if row["name"] == "tuner":
                # Documented exception: pending_count/auto_promoted_total are
                # live reads and stay populated at a real 0.
                assert row["last_run_at"] is None
                assert row["counters"] == {"pending_count": 0, "auto_promoted_total": 0}
                continue
            assert row["last_run_at"] is None, row["name"]
            assert row["last_status"] is None, row["name"]
            assert row["counters"] is None, row["name"]

    def test_a_real_cycle_with_zero_counters_differs_from_never_run(
        self, registry, client
    ) -> None:
        registry.operational.event_log.emit(
            EventType.CURATE_CYCLE_COMPLETED,
            source="trellis.worker.curate",
            payload={
                "status": "ok",
                "noise_tagged": 0,
                "noise_refused_non_document": 0,
                "advisories_generated": 0,
                "advisories_refused": 0,
                "advisories_suppressed": 0,
                "advisories_boosted": 0,
                "advisory_store_degraded": False,
                "advisory_store_stale": False,
                "learning_observations": 0,
                "learning_candidates": 0,
                "learning_promotion_ready": {"count": 0},
                "skipped_stages": [],
                "dry_run": False,
            },
        )

        data = client.get("/api/v1/loops").json()

        noise_row = next(r for r in data["loops"] if r["name"] == "noise_demotion")
        assert noise_row["last_run_at"] is not None
        assert noise_row["last_status"] == "ok"
        assert noise_row["counters"] == {
            "noise_tagged": 0,
            "noise_refused_non_document": 0,
        }

    def test_every_row_carries_actuates_and_what_it_changes(self, client) -> None:
        data = client.get("/api/v1/loops").json()
        for row in data["loops"]:
            assert isinstance(row["actuates"], bool)
            assert row["what_it_changes"]
            assert row["description"]

    def test_job_name_matches_the_one_loop_job_mapping(self, client) -> None:
        """Each loop names the catalog job that drives it, from
        trellis.ops.loop_health.LOOP_JOB -- the single source of truth
        AdminJobSummary.loops is also derived from."""
        from trellis.ops.loop_health import LOOP_JOB

        data = client.get("/api/v1/loops").json()
        rows = {row["name"]: row for row in data["loops"]}
        for loop_name, job_name in LOOP_JOB.items():
            assert rows[loop_name]["job_name"] == job_name
        for loop_name, row in rows.items():
            if loop_name not in LOOP_JOB:
                assert row["job_name"] is None, loop_name


class TestLoopHealthScope:
    @pytest.fixture
    def auth_client(self, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        return TestClient(create_app())

    def test_requires_credential(self, auth_client) -> None:
        assert auth_client.get("/api/v1/loops").status_code == 401

    def test_read_scope_is_forbidden(self, registry, auth_client) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = auth_client.get("/api/v1/loops", headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_admin_scope_passes(self, registry, auth_client) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = auth_client.get("/api/v1/loops", headers={"X-API-Key": token})
        assert resp.status_code == 200


def _set_setting(registry: StoreRegistry, name: str, value: object) -> None:
    """Write one operator override through the governed settings handler —
    the same path ``trellis admin settings set`` and the settings route
    use — rather than poking ``settings.json`` directly."""
    from trellis.mutate.commands import Command, Operation
    from trellis.mutate.handlers import SettingsUpdateHandler

    handler = SettingsUpdateHandler(registry)
    handler.handle(
        Command(
            operation=Operation.SETTINGS_UPDATE,
            args={"name": name, "value": value},
            target_id=name,
            target_type="setting",
        )
    )


_TUNE_CYCLE_PAYLOAD = {
    "tuner_name": "rule_tuner",
    "enabled": True,
    "dry_run": False,
    "proposals_considered": 1,
    "auto_promoted": 0,
    "rolled_back": 0,
    "pending_manual": 1,
    "outcomes": [],
}


class TestTunerActuatesFollowsResolvedSettings:
    """``actuates`` on the tuner row must require BOTH the resolved
    ``learning.auto_promote.enabled`` setting AND a non-dry-run
    ``TUNE_CYCLE_COMPLETED`` event (the scheduled ``tune`` job always
    passes ``--dry-run``, so its presence is exactly that second signal).
    Neither alone may flip it.
    """

    def test_neither_setting_nor_event_is_false(self, client) -> None:
        row = next(
            r
            for r in client.get("/api/v1/loops").json()["loops"]
            if r["name"] == "tuner"
        )
        assert row["actuates"] is False

    def test_setting_enabled_alone_with_no_event_is_still_false(
        self, registry, client
    ) -> None:
        _set_setting(registry, "learning.auto_promote.enabled", True)
        row = next(
            r
            for r in client.get("/api/v1/loops").json()["loops"]
            if r["name"] == "tuner"
        )
        assert row["actuates"] is False

    def test_event_alone_with_setting_disabled_is_still_false(
        self, registry, client
    ) -> None:
        registry.operational.event_log.emit(
            EventType.TUNE_CYCLE_COMPLETED,
            source="trellis.worker.tune",
            payload=_TUNE_CYCLE_PAYLOAD,
        )
        row = next(
            r
            for r in client.get("/api/v1/loops").json()["loops"]
            if r["name"] == "tuner"
        )
        assert row["actuates"] is False

    def test_setting_enabled_and_event_present_is_true(self, registry, client) -> None:
        _set_setting(registry, "learning.auto_promote.enabled", True)
        registry.operational.event_log.emit(
            EventType.TUNE_CYCLE_COMPLETED,
            source="trellis.worker.tune",
            payload=_TUNE_CYCLE_PAYLOAD,
        )
        row = next(
            r
            for r in client.get("/api/v1/loops").json()["loops"]
            if r["name"] == "tuner"
        )
        assert row["actuates"] is True


class TestCandidateAutoPromoteNeverActuates:
    """``learning_candidate_auto_promote``'s row reads ``actuates`` from
    the latest ``LEARNING_AUTO_PROMOTE_EVALUATED`` event's own effective
    mode (``loop_health.py``'s ``_build_candidate_auto_promote_row``),
    never from settings directly -- this route has no settings access of
    its own. So setting ``enabled=True`` here, with no run having
    happened, must still read ``actuates is False``: unlike the tuner
    row (which combines a caller-supplied resolved boolean with an
    event), there is no event-independent path to ``True`` for this row.
    """

    def test_stays_false_with_every_relevant_setting_enabled_and_no_run(
        self, registry, client
    ) -> None:
        _set_setting(registry, "learning.auto_promote.enabled", True)
        _set_setting(registry, "learning.candidate_auto_promote.enabled", True)
        row = next(
            r
            for r in client.get("/api/v1/loops").json()["loops"]
            if r["name"] == "learning_candidate_auto_promote"
        )
        assert row["actuates"] is False


class TestLoopScheduleStatusAndStaleness:
    """Unit coverage of ``admin._loop_schedule_status_and_staleness`` with a
    fully controlled ``now``/``last_run_at`` — the route's own
    ``utc_now()`` call cannot be pinned, and ``EventLog.emit`` always
    stamps the real clock, so the stale-vs-not boundary is tested directly
    against the helper rather than through a live event.
    """

    @staticmethod
    def _store(tmp_path):
        from trellis.stores.schedule_store import ScheduleStore

        return ScheduleStore(tmp_path / "schedule.json")

    def test_no_job_name_is_never_scheduled_or_stale(self, tmp_path) -> None:
        from trellis.core.base import utc_now
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        status, stale = _loop_schedule_status_and_staleness(
            None, None, self._store(tmp_path), utc_now()
        )
        assert status is None
        assert stale is False

    def test_catalog_job_with_no_stored_row_is_not_scheduled(self, tmp_path) -> None:
        from trellis.core.base import utc_now
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        status, stale = _loop_schedule_status_and_staleness(
            "tune", utc_now(), self._store(tmp_path), utc_now()
        )
        assert status == "not_scheduled"
        assert stale is False

    def test_disabled_stored_row_is_not_scheduled(self, tmp_path) -> None:
        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=False))
        status, stale = _loop_schedule_status_and_staleness(
            "tune", utc_now(), store, utc_now()
        )
        assert status == "not_scheduled"
        assert stale is False

    def test_manual_only_stored_row_is_not_scheduled(self, tmp_path) -> None:
        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence=None, enabled=True))
        status, stale = _loop_schedule_status_and_staleness(
            "tune", utc_now(), store, utc_now()
        )
        assert status == "not_scheduled"
        assert stale is False

    def test_scheduled_with_no_last_run_is_never_stale(self, tmp_path) -> None:
        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))
        status, stale = _loop_schedule_status_and_staleness(
            "tune", None, store, utc_now()
        )
        assert status == "scheduled"
        assert stale is False

    def test_scheduled_recent_run_is_not_stale(self, tmp_path) -> None:
        from datetime import timedelta

        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))
        now = utc_now()
        status, stale = _loop_schedule_status_and_staleness(
            "tune", now - timedelta(hours=1), store, now
        )
        assert status == "scheduled"
        assert stale is False

    def test_scheduled_run_older_than_twice_cadence_plus_slack_is_stale(
        self, tmp_path
    ) -> None:
        """``tune``'s catalog cadence is daily (``0 4 * * *``), so its tick
        interval is 24h and the threshold is ``2*24h + 2h slack = 50h``. A
        run 10h old must read fresh; one 60h old must read stale — wide
        margins on both sides of that boundary, not a knife-edge value.
        """
        from datetime import timedelta

        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))
        now = utc_now()

        fresh_status, fresh_stale = _loop_schedule_status_and_staleness(
            "tune", now - timedelta(hours=10), store, now
        )
        assert fresh_status == "scheduled"
        assert fresh_stale is False

        stale_status, stale_stale = _loop_schedule_status_and_staleness(
            "tune", now - timedelta(hours=60), store, now
        )
        assert stale_status == "scheduled"
        assert stale_stale is True

    def test_threshold_is_specifically_twice_the_interval_not_once(
        self, tmp_path
    ) -> None:
        """Pin the multiplier itself, not just "some cadence-derived
        threshold exists": 30h is stale against a single missed daily
        tick plus slack (``1*24h + 2h = 26h``) but fresh against two
        (``2*24h + 2h = 50h``) -- a regression from 2 to 1 flips this one
        case while the wide-margin test above would not notice.
        """
        from datetime import timedelta

        from trellis.core.base import utc_now
        from trellis.schemas.schedule import ScheduledJob
        from trellis_api.routes.admin import _loop_schedule_status_and_staleness

        store = self._store(tmp_path)
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))
        now = utc_now()

        status, stale = _loop_schedule_status_and_staleness(
            "tune", now - timedelta(hours=30), store, now
        )
        assert status == "scheduled"
        assert stale is False


class TestLoopScheduleStatusViaRoute:
    """End-to-end wiring check: the route must actually thread
    ``ScheduleStore`` state into each row's ``schedule_status``/``stale``,
    not just the unit-level helper above.
    """

    def test_no_stored_schedule_rows_read_not_scheduled_or_none(self, client) -> None:
        data = client.get("/api/v1/loops").json()
        for row in data["loops"]:
            if row["job_name"] is None:
                assert row["schedule_status"] is None, row["name"]
            else:
                assert row["schedule_status"] == "not_scheduled", row["name"]
            assert row["stale"] is False, row["name"]

    def test_stored_enabled_cadenced_job_with_a_fresh_run_reads_scheduled(
        self, registry, client
    ) -> None:
        from trellis.schemas.schedule import ScheduledJob
        from trellis.stores.schedule_store import ScheduleStore

        store = ScheduleStore(registry.stores_dir / "schedule.json")
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))
        registry.operational.event_log.emit(
            EventType.TUNE_CYCLE_COMPLETED,
            source="trellis.worker.tune",
            payload=_TUNE_CYCLE_PAYLOAD,
        )

        data = client.get("/api/v1/loops").json()
        row = next(r for r in data["loops"] if r["name"] == "tuner")
        assert row["schedule_status"] == "scheduled"
        assert row["stale"] is False

    def test_a_loop_with_no_job_name_ignores_schedule_store_state(
        self, registry, client
    ) -> None:
        """``precedent_promotion`` has no ``job_name`` (nothing in
        ``LOOP_JOB`` drives it) -- storing an unrelated schedule row must
        not give it a schedule status it never had a job to have."""
        from trellis.schemas.schedule import ScheduledJob
        from trellis.stores.schedule_store import ScheduleStore

        store = ScheduleStore(registry.stores_dir / "schedule.json")
        store.put(ScheduledJob(name="tune", cadence="0 4 * * *", enabled=True))

        data = client.get("/api/v1/loops").json()
        row = next(r for r in data["loops"] if r["name"] == "precedent_promotion")
        assert row["job_name"] is None
        assert row["schedule_status"] is None
        assert row["stale"] is False
