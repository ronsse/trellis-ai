"""Tests for the ScheduledJob schema (trellis.schemas.schedule).

``ScheduledJob`` carries no command — ``name`` must be a key in
``trellis.schedule.catalog.JOB_CATALOG``, validated here on every
construction (which is also every read and every write, since both go
through ``model_validate``/the constructor). A tampered or legacy row
that still carries ``command`` is rejected by ``extra="forbid"``, not
executed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trellis.schedule.catalog import JOB_CATALOG
from trellis.schemas.schedule import ScheduledJob


def _job(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "tune",
        "cadence": "0 4 * * *",
    }
    base.update(overrides)
    return base


class TestScheduledJobValid:
    def test_minimal_valid_job(self) -> None:
        job = ScheduledJob.model_validate(_job())
        assert job.name == "tune"
        assert job.enabled is True
        assert job.timeout_seconds is None
        assert job.run_requested_at is None

    def test_manual_only_cadence_none(self) -> None:
        job = ScheduledJob.model_validate(_job(cadence=None))
        assert job.cadence is None

    def test_every_catalog_job_name_is_a_valid_row(self) -> None:
        """No assertion a single name could satisfy: every catalog entry."""
        for name, spec in JOB_CATALOG.items():
            job = ScheduledJob.model_validate(
                {"name": name, "cadence": spec.default_cadence}
            )
            assert job.name == name

    def test_run_requested_at_roundtrip(self) -> None:
        when = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
        job = ScheduledJob.model_validate(_job(run_requested_at=when))
        assert job.run_requested_at == when

    def test_disabled_job(self) -> None:
        job = ScheduledJob.model_validate(_job(enabled=False))
        assert job.enabled is False

    def test_timeout_seconds_override(self) -> None:
        job = ScheduledJob.model_validate(_job(timeout_seconds=120))
        assert job.timeout_seconds == 120

    def test_timeout_seconds_bounds_are_inclusive(self) -> None:
        low = ScheduledJob.model_validate(_job(timeout_seconds=1))
        high = ScheduledJob.model_validate(_job(timeout_seconds=86400))
        assert low.timeout_seconds == 1
        assert high.timeout_seconds == 86400


class TestScheduledJobRejectsMalformedInput:
    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(name=""))

    def test_whitespace_only_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(name="   "))

    def test_unknown_catalog_name_rejected(self) -> None:
        """An unknown name is a degraded row, never a run (brief item 2)."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduledJob.model_validate(_job(name="not-a-real-job"))
        assert "JOB_CATALOG" in str(exc_info.value)

    def test_legacy_command_field_rejected(self) -> None:
        """A tampered row carrying ``command`` must not execute it.

        ``extra="forbid"`` rejects the field outright — the row degrades
        in the store rather than ever reaching ``due-jobs`` with an
        attacker- or legacy-supplied command (brief item 2 and 6).
        """
        with pytest.raises(ValidationError) as exc_info:
            ScheduledJob.model_validate(
                _job(command=["bash", "-c", "touch /tmp/pwned"])
            )
        errors = exc_info.value.errors()
        assert any(err["type"] == "extra_forbidden" for err in errors)

    def test_legacy_description_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(description="stale field"))

    def test_legacy_host_only_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(host_only=True))

    def test_invalid_cron_expression_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(cadence="not a cron"))

    def test_out_of_range_cron_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(cadence="60 * * * *"))

    def test_zero_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(timeout_seconds=0))

    def test_timeout_over_one_day_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(timeout_seconds=86401))

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(unknown_field=True))
