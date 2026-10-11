"""CLI contract for the schedule registry: record-job-run, due-jobs, init-schedule.

No REST route and no UI exist for any of this yet (plan p1, PR 6) — these
three admin commands and the ``ScheduleStore``/``ScheduledJob`` they sit on
are the whole surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from tests.cli_output import plain
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
        "command": ["trellis", "worker", "tune", "--dry-run"],
        "cadence": "0 4 * * *",
        "description": "Parameter tuner, dry-run.",
    }
    defaults.update(overrides)
    return ScheduledJob.model_validate(defaults)


def _seed_jobs(stores_dir: Path, jobs: list[ScheduledJob]) -> None:
    ScheduleStore(stores_dir / "schedule.json").put_many(jobs)


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
        _seed_jobs(
            stores_dir,
            [
                _job(
                    name="worker-enrich",
                    cadence=None,
                    command=["trellis", "worker", "enrich"],
                )
            ],
        )

        result = runner.invoke(app, ["admin", "due-jobs", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["count"] == 0

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
        _seed_jobs(stores_dir, [_job(name="tune", description="operator-edited")])

        result = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert "tune" in json.loads(result.stdout)["skipped"]
        store = ScheduleStore(stores_dir / "schedule.json")
        assert store.get("tune").description == "operator-edited"

    def test_degraded_file_exits_store_and_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores_dir = _env(tmp_path, monkeypatch)
        (stores_dir / "schedule.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(app, ["admin", "init-schedule", "--format", "json"])

        assert result.exit_code == EXIT_STORE, result.output
        assert json.loads(result.stdout)["status"] == "degraded"
