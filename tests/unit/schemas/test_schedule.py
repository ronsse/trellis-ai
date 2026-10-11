"""Tests for the ScheduledJob schema (trellis.schemas.schedule)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trellis.schemas.schedule import ScheduledJob


def _job(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "tune",
        "command": ["trellis", "worker", "tune", "--dry-run"],
        "cadence": "0 4 * * *",
        "description": "Parameter tuner, dry-run.",
    }
    base.update(overrides)
    return base


class TestScheduledJobValid:
    def test_minimal_valid_job(self) -> None:
        job = ScheduledJob.model_validate(_job())
        assert job.name == "tune"
        assert job.command == ["trellis", "worker", "tune", "--dry-run"]
        assert job.enabled is True
        assert job.host_only is False
        assert job.timeout_seconds == 3600
        assert job.run_requested_at is None

    def test_manual_only_cadence_none(self) -> None:
        job = ScheduledJob.model_validate(_job(cadence=None))
        assert job.cadence is None

    def test_host_only_flag(self) -> None:
        job = ScheduledJob.model_validate(_job(host_only=True))
        assert job.host_only is True

    def test_run_requested_at_roundtrip(self) -> None:
        when = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
        job = ScheduledJob.model_validate(_job(run_requested_at=when))
        assert job.run_requested_at == when

    def test_disabled_job(self) -> None:
        job = ScheduledJob.model_validate(_job(enabled=False))
        assert job.enabled is False


class TestScheduledJobRejectsMalformedInput:
    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(name=""))

    def test_whitespace_only_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(name="   "))

    def test_shell_string_command_rejected(self) -> None:
        """``command`` must be an argv list; pydantic v2 never coerces a str."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduledJob.model_validate(_job(command="trellis worker tune --dry-run"))
        errors = exc_info.value.errors()
        assert any(err["type"] == "list_type" for err in errors)

    def test_empty_command_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(command=[]))

    def test_blank_argv_entry_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(command=["trellis", "  ", "tune"]))

    def test_invalid_cron_expression_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(cadence="not a cron"))

    def test_out_of_range_cron_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(cadence="60 * * * *"))

    def test_blank_description_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(description=""))

    def test_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(timeout_seconds=0))

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledJob.model_validate(_job(unknown_field=True))
