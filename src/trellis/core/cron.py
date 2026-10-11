"""A minimal, dependency-free 5-field cron matcher.

Why hand-written rather than a dependency: no cron-parsing library (e.g.
``croniter``) is declared anywhere in this repo (``pyproject.toml``,
``uv.lock``), and adding one for a single call site — "has this cadence
fired since this timestamp?" — is more than the job registry needs.
This module implements exactly the standard 5-field grammar
(``minute hour day-of-month month day-of-week``), nothing more: no
``@yearly``/``@reboot`` aliases, no seconds field, no timezone handling
beyond whatever :class:`~datetime.datetime` the caller passes in (the
registry's schedules run on local host time, matching the host crontab
they replace).

Day-of-month and day-of-week combine with OR, matching standard cron
semantics, when both are restricted (neither is ``*``). When exactly one
is restricted, it alone constrains — the unrestricted field reads as "any
day" in every cron implementation this one is modelling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: Bound on the minute-by-minute walk in :func:`is_due_since`. ~400 days at
#: one-minute resolution. A gap this large means the job has not completed
#: in well over a year — certainly due — so the walk gives up and says so
#: rather than iterating for an unbounded cadence (e.g. a day-of-month/month
#: combination that lands once a decade).
_MAX_WALK_MINUTES = 60 * 24 * 400

_FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day_of_month": (1, 31),
    "month": (1, 12),
    "day_of_week": (0, 7),  # 0 and 7 both mean Sunday.
}

_FIELD_ORDER = ("minute", "hour", "day_of_month", "month", "day_of_week")

#: A cron expression has exactly five space-separated fields.
_CRON_FIELD_COUNT = 5

#: ``isoweekday()`` returns 1 (Monday) through 7 (Sunday); cron's own 0 and 7
#: both mean Sunday, so 7 from either calendar normalizes to this schedule's 0.
_ISOWEEKDAY_SUNDAY = 7


class CronSyntaxError(ValueError):
    """A cron expression is not a valid 5-field expression."""


def _parse_field(raw: str, lo: int, hi: int) -> frozenset[int]:
    """Parse one comma-separated cron field into the set of values it matches."""
    values: set[int] = set()
    for component in raw.split(","):
        part = component.strip()
        if not part:
            msg = f"empty field component in {raw!r}"
            raise CronSyntaxError(msg)
        step = 1
        if "/" in part:
            base, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                msg = f"bad step in {part!r}"
                raise CronSyntaxError(msg) from exc
            if step <= 0:
                msg = f"step must be positive in {part!r}"
                raise CronSyntaxError(msg)
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                msg = f"bad range in {part!r}"
                raise CronSyntaxError(msg) from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                msg = f"bad value in {part!r}"
                raise CronSyntaxError(msg) from exc
        if start > end or start < lo or end > hi:
            msg = f"value out of range {lo}-{hi} in {part!r} (field {raw!r})"
            raise CronSyntaxError(msg)
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A parsed 5-field cron expression."""

    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    #: The original text, kept for error messages and round-tripping.
    expression: str
    #: Whether day-of-month was restricted (not ``*``) — needed for the
    #: OR-combination rule with day-of-week.
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        """Parse a standard 5-field cron expression.

        Raises :class:`CronSyntaxError` on anything else — a 6-field
        expression with seconds, a named alias like ``@daily``, an
        out-of-range value, or a blank string.
        """
        text = expression.strip()
        fields = text.split()
        if len(fields) != _CRON_FIELD_COUNT:
            msg = (
                f"expected {_CRON_FIELD_COUNT} space-separated fields (minute "
                f"hour day-of-month month day-of-week), got {len(fields)} "
                f"in {expression!r}"
            )
            raise CronSyntaxError(msg)
        parsed = {
            name: _parse_field(raw, *_FIELD_BOUNDS[name])
            for name, raw in zip(_FIELD_ORDER, fields, strict=True)
        }
        # Normalize day-of-week 7 -> 0 (both mean Sunday).
        dow = frozenset(
            0 if v == _ISOWEEKDAY_SUNDAY else v for v in parsed["day_of_week"]
        )
        return cls(
            minute=parsed["minute"],
            hour=parsed["hour"],
            day_of_month=parsed["day_of_month"],
            month=parsed["month"],
            day_of_week=dow,
            expression=text,
            dom_restricted=fields[2].strip() != "*",
            dow_restricted=fields[4].strip() != "*",
        )

    def matches(self, when: datetime) -> bool:
        """Whether ``when`` (minute resolution) is a tick of this schedule."""
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        dom_hit = when.day in self.day_of_month
        # isoweekday(): Monday=1..Sunday=7; this schedule's 0 is Sunday too.
        iso_dow = when.isoweekday()
        dow_value = 0 if iso_dow == _ISOWEEKDAY_SUNDAY else iso_dow
        dow_hit = dow_value in self.day_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_hit or dow_hit
        if self.dom_restricted:
            return dom_hit
        if self.dow_restricted:
            return dow_hit
        return True


def is_due_since(
    cadence: str, last_completed_at: datetime | None, now: datetime
) -> bool:
    """Whether ``cadence`` has had at least one tick in ``(last_completed_at, now]``.

    ``last_completed_at is None`` (never run) is immediately due — there is
    no prior tick to measure from, and nothing about "never run" implies
    "not due". Otherwise walks forward minute by minute from the minute
    after ``last_completed_at`` looking for a match, capped at
    :data:`_MAX_WALK_MINUTES`: a cadence that has not fired in ~400 days is
    treated as due without finishing the walk.

    No backlog replay: this answers a yes/no "is it due", not "how many
    times should it have fired" — catching up after downtime still means
    firing once, which is what a boolean due-ness check gives for free.
    """
    if last_completed_at is None:
        return True
    schedule = CronSchedule.parse(cadence)
    cursor = last_completed_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if cursor > now:
        return False
    steps = 0
    while cursor <= now:
        if schedule.matches(cursor):
            return True
        cursor += timedelta(minutes=1)
        steps += 1
        if steps > _MAX_WALK_MINUTES:
            return True
    return False


def is_job_due(
    *,
    enabled: bool,
    cadence: str | None,
    run_requested_at: datetime | None,
    last_completed_at: datetime | None,
    now: datetime,
) -> bool:
    """Whether a job should run now, combining cadence and a manual request.

    Order of checks, each one a deliberate priority:

    1. ``enabled`` is false → never due, regardless of cadence or a pending
       "Run now" request. Disabling a job is meant to be absolute.
    2. ``run_requested_at`` newer than ``last_completed_at`` → due. This is
       the whole of "Run now": a manual request is a timestamp newer than
       the last completed run, true for both a periodic job (fires ahead of
       its cadence) and a manual-only one (``cadence is None``, which is
       otherwise never due on its own).
    3. ``cadence is None`` → not due (nothing left to make it so).
    4. Otherwise, delegate to :func:`is_due_since`.
    """
    if not enabled:
        return False
    if run_requested_at is not None and (
        last_completed_at is None or run_requested_at > last_completed_at
    ):
        return True
    if cadence is None:
        return False
    return is_due_since(cadence, last_completed_at, now)
