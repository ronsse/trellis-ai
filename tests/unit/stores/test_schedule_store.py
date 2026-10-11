"""Tests for ScheduleStore — JSON file-based job registry persistence.

Right-sized against :mod:`tests.unit.stores.test_policy_store`: the shared
read-leniently/refuse-to-write machinery (atomic writes, symlink handling,
the staleness fingerprint, non-finite-float guards) is exercised once per
*behaviour*, not duplicated test-by-test from that file — it is the same
``DegradableJsonStore`` base, unmodified here. What is specific to
``ScheduleStore`` — its row key (``name``, not an id the store mints),
the duplicate-name reject rule, and that the degraded/stale write paths
are wired on *this* subclass too — gets its own coverage below.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.schedule_shapes import DEGENERATE_SCHEDULE_FILES, DEGENERATE_SCHEDULE_IDS
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
from trellis.schemas.schedule import ScheduledJob
from trellis.stores.schedule_store import ScheduleStore


def _job(**overrides: object) -> ScheduledJob:
    defaults: dict[str, object] = {
        "name": "tune",
        "command": ["trellis", "worker", "tune", "--dry-run"],
        "cadence": "0 4 * * *",
        "description": "Parameter tuner, dry-run.",
    }
    defaults.update(overrides)
    return ScheduledJob.model_validate(defaults)


def _damaged(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "schedule.json"
    path.write_text(text, encoding="utf-8")
    return path


class TestScheduleStore:
    def test_put_and_list(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        job = _job()
        store.put(job)
        jobs = store.list()
        assert len(jobs) == 1
        assert jobs[0].name == "tune"

    def test_get_by_name(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        store.put(_job())
        found = store.get("tune")
        assert found is not None
        assert found.name == "tune"

    def test_get_nonexistent(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        assert store.get("nonexistent") is None

    def test_remove(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        store.put(_job())
        assert store.remove("tune") is True
        assert store.list() == []

    def test_remove_nonexistent(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        assert store.remove("nonexistent") is False

    def test_put_replaces_same_name(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        store.put(_job())
        store.put(_job(enabled=False))
        jobs = store.list()
        assert len(jobs) == 1
        assert jobs[0].enabled is False

    def test_put_many_returns_count(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        count = store.put_many([_job(name="a"), _job(name="b")])
        assert count == 2
        assert {j.name for j in store.list()} == {"a", "b"}

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "schedule.json"
        ScheduleStore(path).put(_job())
        store2 = ScheduleStore(path)
        jobs = store2.list()
        assert len(jobs) == 1
        assert jobs[0].name == "tune"

    def test_empty_store_on_new_path(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "new" / "schedule.json")
        assert store.list() == []


class TestDuplicateNameDegradesRatherThanCollapsing:
    """Two rows with the same name must not silently collapse to one."""

    def test_a_duplicate_name_in_the_file_degrades(self, tmp_path: Path) -> None:
        job = _job()
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps(
                {"jobs": [job.model_dump(mode="json"), job.model_dump(mode="json")]}
            ),
            encoding="utf-8",
        )

        store = ScheduleStore(path)

        assert store.is_degraded is True
        degradation = store.degradation
        assert degradation is not None
        assert degradation.reason == "invalid_rows"
        # The first occurrence is kept; the duplicate is the one skipped.
        assert degradation.rows_loaded == 1
        assert degradation.rows_skipped == 1


class TestDegenerateShapesDegradeAndRefuse:
    """Each shape from the shared table must degrade the load *and* refuse
    the write — the same property ``PolicyStore``/``AdvisoryStore`` hold.
    """

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        DEGENERATE_SCHEDULE_FILES,
        ids=DEGENERATE_SCHEDULE_IDS,
    )
    def test_shape_degrades(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        store = ScheduleStore(_damaged(tmp_path, text))

        assert store.is_degraded is True, f"{name} loaded as a clean store"
        degradation = store.degradation
        assert degradation is not None
        assert degradation.reason == reason
        assert degradation.recovery.startswith("mv ")

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        DEGENERATE_SCHEDULE_FILES,
        ids=DEGENERATE_SCHEDULE_IDS,
    )
    def test_shape_refuses_every_write_and_leaves_the_bytes_alone(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        path = _damaged(tmp_path, text)
        store = ScheduleStore(path)
        before = path.read_bytes()

        with pytest.raises(DegradedStoreWriteError) as exc_info:
            store.put(_job())
        assert exc_info.value.store == "schedule"
        with pytest.raises(DegradedStoreWriteError):
            store.put_many([_job()])
        with pytest.raises(DegradedStoreWriteError):
            store.remove("anything")

        assert path.read_bytes() == before, f"{name}: the damaged file was written"

    @pytest.mark.parametrize(
        ("name", "text", "reason"),
        DEGENERATE_SCHEDULE_FILES,
        ids=DEGENERATE_SCHEDULE_IDS,
    )
    def test_a_refused_write_does_not_mutate_memory(
        self, tmp_path: Path, name: str, text: str, reason: str
    ) -> None:
        store = ScheduleStore(_damaged(tmp_path, text))
        before = store.list()

        with pytest.raises(DegradedStoreWriteError):
            store.put(_job())

        assert store.list() == before


class TestDegradationIsPerRow:
    """One unparseable job costs one job, not the registry.

    The dispatcher's whole view of what to run comes from ``list()``; a
    single bad row blanking every job would silently stop running all of
    them, not just the one that broke.
    """

    def test_a_good_row_survives_a_bad_neighbour(self, tmp_path: Path) -> None:
        good = _job()
        bad_row = {"name": "bad", "command": "not-a-list"}
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps({"jobs": [good.model_dump(mode="json"), bad_row]}),
            encoding="utf-8",
        )

        store = ScheduleStore(path)

        assert [j.name for j in store.list()] == [good.name]
        degradation = store.degradation
        assert degradation is not None
        assert degradation.reason == "invalid_rows"
        assert degradation.rows_loaded == 1
        assert degradation.rows_skipped == 1

    def test_a_partial_load_still_refuses_to_write(self, tmp_path: Path) -> None:
        good = _job()
        bad_row = {"name": "bad", "command": "not-a-list"}
        path = tmp_path / "schedule.json"
        path.write_text(
            json.dumps({"jobs": [good.model_dump(mode="json"), bad_row]}),
            encoding="utf-8",
        )
        before = path.read_bytes()
        store = ScheduleStore(path)

        with pytest.raises(DegradedStoreWriteError):
            store.put(_job(name="other"))

        assert path.read_bytes() == before


class TestStaleWritesAreRefused:
    def test_a_second_writer_is_not_silently_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "schedule.json"
        ScheduleStore(path).put(_job())
        store = ScheduleStore(path)
        theirs = json.dumps(
            {
                "jobs": [
                    _job().model_dump(mode="json"),
                    _job(name="other").model_dump(mode="json"),
                ]
            }
        )
        path.write_text(theirs, encoding="utf-8")

        with pytest.raises(StaleStoreWriteError):
            store.put(_job(name="third"))
        assert path.read_text(encoding="utf-8") == theirs

    def test_consecutive_writes_from_one_store_do_not_trip_the_guard(
        self, tmp_path: Path
    ) -> None:
        store = ScheduleStore(tmp_path / "schedule.json")
        store.put(_job())
        store.put(_job(name="other"))  # must not raise
