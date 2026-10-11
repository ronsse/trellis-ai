"""Tests for the job catalog — the only source of a scheduled job's command.

Pins brief e169's remaining boundary tests that aren't about
``schedule.json`` or ``due-jobs`` directly: the catalog's own shape, the
host-job name guard, and that no personal absolute path ever leaks into
either the catalog or the seed module built from it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from trellis.schedule import catalog as catalog_module
from trellis.schedule.catalog import (
    HOST_JOB_NAME_PATTERN,
    JOB_CATALOG,
    JobSpec,
    validate_host_job_name,
)
from trellis_cli import schedule_seed as schedule_seed_module
from trellis_cli.schedule_seed import default_scheduled_jobs


class TestJobCatalogShape:
    def test_every_entry_either_carries_argv_or_is_host_only(self) -> None:
        for name, spec in JOB_CATALOG.items():
            if spec.host_only:
                assert spec.argv is None, f"{name}: host-only job must not carry argv"
            else:
                assert spec.argv, f"{name}: non-host-only job must carry argv"

    def test_the_documented_host_only_jobs_are_exactly_these_four(self) -> None:
        host_only = {name for name, spec in JOB_CATALOG.items() if spec.host_only}
        assert host_only == {
            "capture-nightly",
            "curate-nightly",
            "backup-nightly",
            "roadmap-nightly",
        }

    def test_migrate_graph_is_deliberately_excluded(self) -> None:
        assert "migrate-graph" not in JOB_CATALOG

    def test_every_name_matches_the_host_job_name_pattern(self) -> None:
        """Every catalog key must be safe as $TRELLIS_HOST_JOBS_DIR/<name>,
        even for a Trellis-native job — the pattern is the one
        boundary, not two.
        """
        for name in JOB_CATALOG:
            assert HOST_JOB_NAME_PATTERN.fullmatch(name), name

    def test_timeout_seconds_within_bounds(self) -> None:
        for name, spec in JOB_CATALOG.items():
            assert 1 <= spec.default_timeout_seconds <= 86400, name


class TestValidateHostJobName:
    @pytest.mark.parametrize(
        "name", ["tune", "capture-nightly", "a", "job-123", "abc-def-9"]
    )
    def test_accepts_well_formed_names(self, name: str) -> None:
        assert validate_host_job_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "../etc/passwd",
            "foo/bar",
            "foo/../bar",
            "..",
            "/etc/passwd",
            "UPPER",
            "has space",
            "semi;colon",
            "",
            "dollar$sign",
        ],
    )
    def test_rejects_path_traversal_and_unsafe_characters(self, name: str) -> None:
        with pytest.raises(ValueError, match="does not match"):
            validate_host_job_name(name)


class TestJobSpecSelfValidation:
    def test_host_only_job_with_argv_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not carry argv"):
            JobSpec(
                name="bad-host-job",
                description="x",
                host_only=True,
                argv=("echo", "no"),
            )

    def test_non_host_only_job_without_argv_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must carry a non-empty argv"):
            JobSpec(name="bad-native-job", description="x", host_only=False)

    def test_bad_name_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="does not match"):
            JobSpec(
                name="../escape",
                description="x",
                host_only=True,
            )

    def test_out_of_bounds_timeout_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            JobSpec(
                name="bad-timeout",
                description="x",
                argv=("trellis",),
                default_timeout_seconds=0,
            )

    def test_invalid_cadence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="default_cadence is invalid"):
            JobSpec(
                name="bad-cadence",
                description="x",
                argv=("trellis",),
                default_cadence="not a cron",
            )


class TestNoAbsolutePersonalPath:
    """The second defect in the brief: a public repo must not hardcode a
    deployment-owner's home directory. Checked three ways so a future
    edit that reintroduces it in any one place is caught.
    """

    def test_catalog_source_has_no_home_path(self) -> None:
        source = inspect.getsource(catalog_module)
        assert "/home/" not in source

    def test_schedule_seed_source_has_no_home_path(self) -> None:
        source = inspect.getsource(schedule_seed_module)
        assert "/home/" not in source

    def test_no_catalog_argv_contains_the_old_hardcoded_skynet_hub_path(self) -> None:
        """Pins the exact literal that was the second defect: the old
        ``schedule_seed.py`` hardcoded ``/home/nronsse/projects/skynet-hub/
        stacks/trellis``. ``_FEEDBACK_LOG_DIR`` legitimately resolves under
        ``Path.home()`` on a real machine (same fallback pattern as
        ``trellis.stores.registry``), so a bare ``"/home/"`` substring
        check is a false positive there — this checks for the specific
        hardcoded string instead.
        """
        old_path = "/home/nronsse/projects/skynet-hub/stacks/trellis"
        for name, spec in JOB_CATALOG.items():
            if spec.argv is None:
                continue
            for token in spec.argv:
                assert old_path not in token, f"{name}: {token!r}"

    def test_no_default_scheduled_job_round_trips_a_home_path(self) -> None:
        for job in default_scheduled_jobs():
            dumped = job.model_dump_json()
            assert "/home/" not in dumped, job.name

    def test_config_dir_respects_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", "/srv/trellis-config")
        assert catalog_module._config_dir() == catalog_module.Path(
            "/srv/trellis-config"
        )

    def test_data_dir_respects_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_DATA_DIR", "/srv/trellis-data")
        assert catalog_module._data_dir() == catalog_module.Path("/srv/trellis-data")

    def test_config_dir_fallback_is_env_driven_not_a_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``TRELLIS_CONFIG_DIR`` set: falls back to ``Path.home()``,
        not any fixed literal — proven by changing ``HOME`` and watching
        the result move with it.
        """
        monkeypatch.delenv("TRELLIS_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert catalog_module._config_dir() == tmp_path / ".trellis"
