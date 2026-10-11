"""CLI contract for the schedule registry: record-job-run, due-jobs, init-schedule,
set, run.

``set``/``run`` are the CLI-parity half of plan p1 PR 6's admin REST surface
(``GET``/``PATCH /admin/jobs``, ``POST /admin/jobs/{name}/run`` in
``trellis_api.routes.admin``) and a "Schedules" tab in the admin UI now cover
the same ground from a browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.schedule_store import ScheduleStore
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.schedule_seed import default_scheduled_jobs

if TYPE_CHECKING:
    import pytest

runner = CliRunner()


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    return stores_dir


def _job(**overrides: object) -> ScheduledJob:
    defaults: dict[str, object] = {
        "name": "tune",
        "cadence": "0 4 * * *",
    }
    defaults.update(overrides)
    return ScheduledJob.model_validate(defaults)


def _seed_jobs(stores_dir: Path, jobs: list[ScheduledJob]) -> None:
    ScheduleStore(stores_dir / "schedule.json").put_many(jobs)


def _write_raw_rows(stores_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Write schedule.json rows that skip ``ScheduledJob`` validation entirely.

    Used to prove a row that could never be *constructed* through the
    schema still can't make it through ``due-jobs`` if it somehow lands
    in the file directly (a tampered write, or a legacy row from before
    this redesign).
    """
    (stores_dir / "schedule.json").write_text(
        json.dumps({"jobs": rows}), encoding="utf-8"
    )


def _events_for(stores_dir: Path, job_name: str) -> list[dict[str, Any]]:
    registry = StoreRegistry(stores_dir=stores_dir)
    try:
        events = registry.operational.event_log.get_events(
            entity_id=f"job:{job_name}", order="asc", limit=10
        )
        return [e.model_dump(mode="json") for e in events]
    finally:
        registry.close()


class TestRecordJobRun:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "100",
                "--exit-code",
                "0",
                "--format",
                "bogus",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert "bogus" in result.output

    def test_success_emits_started_and_completed_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)

        result = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "1500",
                "--exit-code",
                "0",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["job"] == "tune"
        assert payload["exit_code"] == 0
        assert payload["job_status"] == "ok"
        assert payload["recorded"] is True
        assert payload["run_id"]

        events = _events_for(stores_dir, "tune")
        types = [e["event_type"] for e in events]
        assert types == [
            EventType.JOB_RUN_STARTED.value,
            EventType.JOB_RUN_COMPLETED.value,
        ]
        completed_payload = events[1]["payload"]
        assert completed_payload["duration_ms"] == 1500
        assert completed_payload["exit_code"] == 0
        assert completed_payload["status"] == "ok"
        assert completed_payload["run_id"] == events[0]["payload"]["run_id"]
        # write_provenance is stamped by EventLog.emit on every event.
        assert "write_provenance" in events[0]["metadata"]

    def test_a_failed_job_run_is_still_a_successful_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recording that a job failed is this command succeeding."""
        _env(tmp_path, monkeypatch)

        result = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "500",
                "--exit-code",
                "1",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["job_status"] == "failed"
        assert payload["exit_code"] == 1

    def test_explicit_status_overrides_the_derived_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)

        result = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "0",
                "--exit-code",
                "0",
                "--status",
                "skipped_overlap",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["job_status"] == "skipped_overlap"

    def test_text_arm_names_the_job_and_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)

        result = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "10",
                "--exit-code",
                "0",
            ],
        )

        assert result.exit_code == 0, result.output
        out = plain(result.output)
        assert "tune" in out
        assert "exit_code=0" in out
        assert "status=ok" in out


class TestDueJobs:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(app, ["admin", "due-jobs", "--format", "bogus"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert "bogus" in result.output

    def test_no_schedule_file_is_zero_due_jobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["count"] == 0
        assert payload["due_jobs"] == []
        assert payload["schedule_file_present"] is False

    def test_a_never_run_periodic_job_is_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["count"] == 1
        assert payload["due_jobs"][0]["name"] == "tune"

    def test_a_manual_only_job_with_no_request_is_not_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="worker-enrich", cadence=None)])

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["count"] == 0

    def test_due_job_command_is_the_catalog_argv_not_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The schedule file never supplies a command — ``due-jobs`` joins
        it from :data:`trellis.schedule.catalog.JOB_CATALOG` by name.
        """
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        due = json.loads(result.stdout)["due_jobs"][0]
        assert due["command"] == [
            "trellis",
            "worker",
            "tune",
            "--dry-run",
            "--format",
            "json",
        ]
        assert due["host_only"] is False

    def test_a_host_only_due_job_reports_null_command_and_host_only_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="capture-nightly", cadence="0 3 * * *")])

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        due = json.loads(result.stdout)["due_jobs"][0]
        assert due["name"] == "capture-nightly"
        assert due["command"] is None
        assert due["host_only"] is True

    def test_a_tampered_command_row_is_never_emitted_as_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """brief item 2/6: a row carrying ``command`` can't make ``due-jobs``
        emit it — the row degrades instead of running.
        """
        stores_dir = _env(tmp_path, monkeypatch)
        _write_raw_rows(
            stores_dir,
            [
                {
                    "name": "tune",
                    "cadence": "0 4 * * *",
                    "command": ["bash", "-c", "touch /tmp/pwned"],
                }
            ],
        )

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        # The whole file degrades (one invalid row), so due-jobs must say
        # so rather than report 0 due jobs as though everything parsed.
        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "degraded"
        assert payload["due_jobs"] == []

    def test_an_unknown_job_name_is_never_emitted_as_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """brief item 2/6: an unknown catalog name never appears as due."""
        stores_dir = _env(tmp_path, monkeypatch)
        _write_raw_rows(
            stores_dir,
            [{"name": "not-a-real-job", "cadence": "0 4 * * *"}],
        )

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "degraded"
        assert payload["due_jobs"] == []

    def test_a_recently_completed_periodic_job_drops_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-trips through the real record-job-run command, not a hand
        seeded event — ``utc_now()`` is tz-aware, and a hand-built naive
        timestamp would pass only because it never exercises that path.
        """
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])

        record = runner.invoke(
            app,
            [
                "admin",
                "record-job-run",
                "--job",
                "tune",
                "--duration-ms",
                "10",
                "--exit-code",
                "0",
                "--format",
                "json",
            ],
        )
        assert record.exit_code == 0, record.output

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["count"] == 0

    def test_a_disabled_job_is_never_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *", enabled=False)])

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["count"] == 0

    def test_degraded_file_reports_degraded_and_exits_store_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "degraded"
        assert payload["store_degradation"] is not None

    def test_degraded_file_reports_degraded_and_exits_store_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(app, ["admin", "due-jobs"])

        assert result.exit_code == EXIT_STORE, result.output
        out = plain(result.output)
        assert "DEGRADED" in out


class TestInitSchedule:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(app, ["admin", "init-schedule", "--format", "bogus"])
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert "bogus" in result.output

    def test_seeds_every_default_job_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        expected_names = {j.name for j in default_scheduled_jobs()}

        result = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert set(payload["added"]) == expected_names
        assert payload["skipped"] == []

        store = ScheduleStore(stores_dir / "schedule.json")
        assert {j.name for j in store.list()} == expected_names

    def test_a_second_run_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _ = stores_dir
        first = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert second.exit_code == 0, second.output
        payload = json.loads(second.stdout)
        assert payload["added"] == []
        assert set(payload["skipped"]) == {j.name for j in default_scheduled_jobs()}

    def test_never_overwrites_an_operator_edited_job_of_the_same_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", enabled=False)])

        result = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert "tune" in json.loads(result.stdout)["skipped"]
        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").enabled is False

    def test_degraded_file_exits_store_and_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert result.exit_code == EXIT_STORE, result.output
        assert json.loads(result.stdout)["status"] == "degraded"


class TestScheduleSet:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["admin", "schedule", "set", "tune", "--enabled", "--format", "bogus"],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert "bogus" in result.output

    def test_unknown_job_name_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "admin",
                "schedule",
                "set",
                "not-a-real-job",
                "--enabled",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert json.loads(result.stdout)["code"] == "unknown_job"

    def test_no_options_is_rejected_as_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["admin", "schedule", "set", "tune", "--format", "json"]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert json.loads(result.stdout)["code"] == "no_fields"

    def test_bad_cadence_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            [
                "admin",
                "schedule",
                "set",
                "tune",
                "--cadence",
                "nonsense",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert json.loads(result.stdout)["code"] == "validation_error"

    def test_out_of_range_timeout_is_rejected_by_the_option_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app,
            ["admin", "schedule", "set", "tune", "--timeout", "0", "--format", "json"],
        )
        assert result.exit_code != 0, result.output

    def test_enabling_a_job_writes_only_enabled_and_emits_schedule_updated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *", enabled=True)])

        result = runner.invoke(
            app,
            ["admin", "schedule", "set", "tune", "--disabled", "--format", "json"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["job"]["enabled"] is False
        assert payload["job"]["cadence"] == "0 4 * * *"  # untouched

        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").cadence == "0 4 * * *"

        events = _events_for(stores_dir, "tune")
        updated = [
            e for e in events if e["event_type"] == EventType.SCHEDULE_UPDATED.value
        ]
        assert len(updated) == 1
        payload = updated[0]["payload"]
        assert payload["old"]["enabled"] is True
        assert payload["new"]["enabled"] is False
        assert "cadence" not in payload["old"]
        assert payload["key_name"].startswith("cli:")

    def test_cadence_none_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])

        result = runner.invoke(
            app,
            [
                "admin",
                "schedule",
                "set",
                "tune",
                "--cadence",
                "none",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["job"]["cadence"] is None
        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").cadence is None

    def test_timeout_set_to_a_valid_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune")])

        result = runner.invoke(
            app,
            [
                "admin",
                "schedule",
                "set",
                "tune",
                "--timeout",
                "600",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["job"]["timeout_seconds"] == 600

    def test_degraded_store_exits_store_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(
            app,
            ["admin", "schedule", "set", "tune", "--enabled", "--format", "json"],
        )

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["message"]

    def test_text_output_names_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(app, ["admin", "schedule", "set", "tune", "--enabled"])
        assert result.exit_code == 0, result.output
        out = plain(result.output)
        assert "tune" in out


class TestScheduleRun:
    def test_rejects_an_unknown_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "bogus"]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output

    def test_unknown_job_name_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["admin", "schedule", "run", "not-a-real-job", "--format", "json"]
        )
        assert result.exit_code == EXIT_VALIDATION, result.output
        assert json.loads(result.stdout)["code"] == "unknown_job"

    def test_never_claims_synchronous_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _env(tmp_path, monkeypatch)
        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        hint = json.loads(result.stdout)["dispatcher_hint"].lower()
        assert "dispatcher" in hint
        for forbidden in ("completed", "succeeded", "finished", "executed job"):
            assert forbidden not in hint

    def test_writes_only_run_requested_at_and_emits_job_run_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(
            stores_dir,
            [_job(name="tune", cadence="0 4 * * *", enabled=False, timeout_seconds=55)],
        )

        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        assert payload["run_requested_at"]

        store = ScheduleStore(stores_dir / "schedule.json")
        row = store.get("tune")
        assert row.run_requested_at is not None
        assert row.enabled is False
        assert row.cadence == "0 4 * * *"
        assert row.timeout_seconds == 55

        events = _events_for(stores_dir, "tune")
        requested = [
            e for e in events if e["event_type"] == EventType.JOB_RUN_REQUESTED.value
        ]
        assert len(requested) == 1
        assert requested[0]["payload"]["key_name"].startswith("cli:")

    def test_degraded_store_exits_store_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "json"]
        )

        assert result.exit_code == EXIT_STORE, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"


def _deny_schedule(stores_dir: Path, operation: str) -> None:
    """Declare one enforced ``deny`` rule for *operation*, scoped to schedules.

    Mirrors ``tests/unit/api/test_admin_jobs_routes.py``'s helper of the
    same name: both ``set``/``run`` stamp ``target_type="schedule"`` on
    the ``Command`` they build and execute via
    ``trellis.mutate.build_schedule_executor``, whose ``policy_gate`` is
    wired identically to ``build_curate_executor``'s — this is Stage 2
    running on the CLI half of the admin-only surface.
    """
    policy = Policy(
        policy_type="mutation",
        scope=PolicyScope(level="entity_type", value="schedule"),
        rules=[
            PolicyRule(operation=operation, condition="synthetic-freeze", action="deny")
        ],
        enforcement="enforce",
    )
    (stores_dir / "policies.json").write_text(
        json.dumps({"policies": [policy.model_dump(mode="json")]}),
        encoding="utf-8",
    )


class TestPolicyDeny:
    def test_a_policy_scoped_to_schedules_refuses_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])
        _deny_schedule(stores_dir, "schedule.update")

        result = runner.invoke(
            app,
            ["admin", "schedule", "set", "tune", "--disabled", "--format", "json"],
        )

        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["code"] == "validation_error"
        assert "denied by policy" in payload["message"].lower()
        # Nothing changed: the seeded row, not a partial write.
        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").enabled is True

    def test_a_policy_scoped_to_schedules_refuses_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])
        _deny_schedule(stores_dir, "job.run_request")

        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "json"]
        )

        assert result.exit_code == EXIT_VALIDATION, result.output
        payload = json.loads(result.stdout)
        assert payload["code"] == "validation_error"
        assert "denied by policy" in payload["message"].lower()
        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").run_requested_at is None

    def test_a_policy_scoped_to_schedules_leaves_other_commands_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The policy is scoped to ``schedule.update``; ``run`` on the
        same job (a different operation) is untouched."""
        stores_dir = _env(tmp_path, monkeypatch)
        _seed_jobs(stores_dir, [_job(name="tune", cadence="0 4 * * *")])
        _deny_schedule(stores_dir, "schedule.update")

        result = runner.invoke(
            app, ["admin", "schedule", "run", "tune", "--format", "json"]
        )

        assert result.exit_code == 0, result.output
