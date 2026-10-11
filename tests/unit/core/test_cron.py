"""Tests for the 5-field cron matcher and job due-ness (trellis.core.cron)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trellis.core.cron import (
    CronSchedule,
    CronSyntaxError,
    is_due_since,
    is_job_due,
)


def dt(*args: int) -> datetime:
    """A tz-aware datetime literal — ``cron.py``'s real callers never see a
    naive one (``trellis.core.base.utc_now()`` always carries ``UTC``).
    """
    return datetime(*args, tzinfo=UTC)


class TestCronScheduleParse:
    def test_every_minute(self) -> None:
        schedule = CronSchedule.parse("* * * * *")
        assert schedule.minute == frozenset(range(60))
        assert schedule.dom_restricted is False
        assert schedule.dow_restricted is False

    def test_fixed_time_daily(self) -> None:
        schedule = CronSchedule.parse("30 3 * * *")
        assert schedule.minute == frozenset({30})
        assert schedule.hour == frozenset({3})
        assert schedule.dom_restricted is False
        assert schedule.dow_restricted is False

    def test_range_and_step(self) -> None:
        schedule = CronSchedule.parse("0 0-5/2 * * *")
        assert schedule.hour == frozenset({0, 2, 4})

    def test_comma_list(self) -> None:
        schedule = CronSchedule.parse("0,15,30,45 * * * *")
        assert schedule.minute == frozenset({0, 15, 30, 45})

    def test_dow_7_normalizes_to_sunday_0(self) -> None:
        schedule = CronSchedule.parse("0 0 * * 7")
        assert schedule.day_of_week == frozenset({0})

    def test_dom_restricted_flag(self) -> None:
        schedule = CronSchedule.parse("0 0 15 * *")
        assert schedule.dom_restricted is True
        assert schedule.dow_restricted is False

    def test_dow_restricted_flag(self) -> None:
        schedule = CronSchedule.parse("0 0 * * 1")
        assert schedule.dom_restricted is False
        assert schedule.dow_restricted is True

    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "* * * *",  # only 4 fields
            "* * * * * *",  # 6 fields
            "@daily",
            "60 * * * *",  # minute out of range
            "* 24 * * *",  # hour out of range
            "* * 32 * *",  # day-of-month out of range
            "* * * 13 *",  # month out of range
            "* * * * 8",  # day-of-week out of range
            "* * * * ,",  # empty field component
            "bad * * * *",
        ],
    )
    def test_invalid_expressions_raise(self, expression: str) -> None:
        with pytest.raises(CronSyntaxError):
            CronSchedule.parse(expression)


class TestCronScheduleMatches:
    def test_simple_match(self) -> None:
        schedule = CronSchedule.parse("30 3 * * *")
        assert schedule.matches(dt(2026, 10, 10, 3, 30)) is True
        assert schedule.matches(dt(2026, 10, 10, 3, 31)) is False
        assert schedule.matches(dt(2026, 10, 10, 4, 30)) is False

    def test_unrestricted_dom_and_dow_match_any_day(self) -> None:
        schedule = CronSchedule.parse("0 5 * * *")
        # 2026-10-10 is a Saturday; 2026-10-11 is a Sunday. Both should hit.
        assert schedule.matches(dt(2026, 10, 10, 5, 0)) is True
        assert schedule.matches(dt(2026, 10, 11, 5, 0)) is True

    def test_dom_restricted_only_constrains_on_dom(self) -> None:
        schedule = CronSchedule.parse("0 0 15 * *")
        assert schedule.matches(dt(2026, 10, 15, 0, 0)) is True
        assert schedule.matches(dt(2026, 10, 16, 0, 0)) is False

    def test_dow_restricted_only_constrains_on_dow(self) -> None:
        # Monday only.
        schedule = CronSchedule.parse("0 0 * * 1")
        monday = dt(2026, 10, 12, 0, 0)
        assert monday.isoweekday() == 1
        assert schedule.matches(monday) is True
        tuesday = dt(2026, 10, 13, 0, 0)
        assert schedule.matches(tuesday) is False

    def test_dom_and_dow_both_restricted_combine_with_or(self) -> None:
        # 15th OR Monday.
        schedule = CronSchedule.parse("0 0 15 * 1")
        # 2026-10-15 is a Thursday: hits on day-of-month alone.
        thursday_the_15th = dt(2026, 10, 15, 0, 0)
        assert thursday_the_15th.isoweekday() == 4
        assert schedule.matches(thursday_the_15th) is True
        # 2026-10-12 is a Monday, not the 15th: hits on day-of-week alone.
        a_monday = dt(2026, 10, 12, 0, 0)
        assert schedule.matches(a_monday) is True
        # Neither the 15th nor a Monday: no match.
        neither = dt(2026, 10, 13, 0, 0)
        assert neither.isoweekday() != 1
        assert schedule.matches(neither) is False

    def test_dow_sunday_7_matches_same_as_0(self) -> None:
        schedule = CronSchedule.parse("0 0 * * 7")
        sunday = dt(2026, 10, 11, 0, 0)
        assert sunday.isoweekday() == 7
        assert schedule.matches(sunday) is True


class TestIsDueSince:
    def test_never_run_is_immediately_due(self) -> None:
        assert is_due_since("0 3 * * *", None, dt(2026, 10, 10, 12, 0)) is True

    def test_no_tick_since_last_run_is_not_due(self) -> None:
        last = dt(2026, 10, 10, 3, 0)
        now = dt(2026, 10, 10, 3, 30)
        assert is_due_since("0 3 * * *", last, now) is False

    def test_a_tick_since_last_run_is_due(self) -> None:
        last = dt(2026, 10, 10, 3, 1)
        now = dt(2026, 10, 11, 3, 0)
        assert is_due_since("0 3 * * *", last, now) is True

    def test_exactly_on_the_boundary_is_due(self) -> None:
        last = dt(2026, 10, 10, 2, 59)
        now = dt(2026, 10, 10, 3, 0)
        assert is_due_since("0 3 * * *", last, now) is True

    def test_now_before_cursor_is_not_due(self) -> None:
        # last_completed_at is already in the future relative to now.
        last = dt(2026, 10, 10, 5, 0)
        now = dt(2026, 10, 10, 4, 0)
        assert is_due_since("0 3 * * *", last, now) is False

    def test_huge_gap_is_bounded_and_reports_due(self) -> None:
        last = dt(2020, 1, 1, 0, 0)
        now = dt(2026, 10, 10, 0, 0)
        assert is_due_since("0 3 * * *", last, now) is True


class TestIsJobDue:
    def test_disabled_is_never_due(self) -> None:
        assert (
            is_job_due(
                enabled=False,
                cadence="* * * * *",
                run_requested_at=dt(2026, 10, 10, 0, 0),
                last_completed_at=None,
                now=dt(2026, 10, 10, 12, 0),
            )
            is False
        )

    def test_manual_only_with_no_request_is_never_due(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence=None,
                run_requested_at=None,
                last_completed_at=None,
                now=dt(2026, 10, 10, 12, 0),
            )
            is False
        )

    def test_manual_only_with_a_fresh_request_is_due(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence=None,
                run_requested_at=dt(2026, 10, 10, 11, 0),
                last_completed_at=None,
                now=dt(2026, 10, 10, 12, 0),
            )
            is True
        )

    def test_manual_only_request_older_than_last_run_is_not_due(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence=None,
                run_requested_at=dt(2026, 10, 10, 9, 0),
                last_completed_at=dt(2026, 10, 10, 10, 0),
                now=dt(2026, 10, 10, 12, 0),
            )
            is False
        )

    def test_manual_only_request_equal_to_last_run_is_not_due(self) -> None:
        # A request timestamp exactly equal to the last completed run is not
        # "newer" — it is the request that run already serviced (or a
        # not-yet-cleared field), not a fresh one. Only strictly-after counts.
        same = dt(2026, 10, 10, 10, 0)
        assert (
            is_job_due(
                enabled=True,
                cadence=None,
                run_requested_at=same,
                last_completed_at=same,
                now=dt(2026, 10, 10, 12, 0),
            )
            is False
        )

    def test_run_requested_newer_than_last_completed_overrides_cadence(self) -> None:
        # Cadence alone would say "not due" (no tick since last run), but a
        # fresh run_requested_at should win regardless.
        assert (
            is_job_due(
                enabled=True,
                cadence="0 3 * * *",
                run_requested_at=dt(2026, 10, 10, 11, 0),
                last_completed_at=dt(2026, 10, 10, 3, 0),
                now=dt(2026, 10, 10, 12, 0),
            )
            is True
        )

    def test_cadence_due_with_an_older_run_request_ignored(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence="0 3 * * *",
                run_requested_at=dt(2026, 10, 9, 1, 0),
                last_completed_at=dt(2026, 10, 9, 2, 0),
                now=dt(2026, 10, 10, 4, 0),
            )
            is True
        )

    def test_cadence_not_due_delegates_to_is_due_since(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence="0 3 * * *",
                run_requested_at=None,
                last_completed_at=dt(2026, 10, 10, 3, 0),
                now=dt(2026, 10, 10, 3, 30),
            )
            is False
        )

    def test_never_completed_with_a_cadence_is_due(self) -> None:
        assert (
            is_job_due(
                enabled=True,
                cadence="0 3 * * *",
                run_requested_at=None,
                last_completed_at=None,
                now=dt(2026, 10, 10, 3, 30),
            )
            is True
        )
