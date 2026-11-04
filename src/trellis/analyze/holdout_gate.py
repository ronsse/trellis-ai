"""The readout seal on the pack holdout's outcome figures.

The holdout experiment is only blind if nobody reads an outcome before the
pre-registered horizon. A convention does not keep that: ``trellis analyze
holdout`` prints the per-arm means, the estimate, the interval, the p-value and
(``--by-commit``) each build's per-arm means to anyone who runs it, and the REST
route and the Reports tab run the same function. This module is the one gate in
front of all of them.

``TRELLIS_HOLDOUT_READOUT_AFTER`` is an ISO-8601 date or timestamp, read as UTC
when it carries no offset. Unset or blank, nothing is sealed and an adopter who
never set it sees no change. Set, :func:`require_readout_open` raises
:class:`~trellis.errors.HoldoutReadoutSealedError` until that instant and
returns quietly from it on. A value that is not a date raises
:class:`~trellis.errors.ConfigError`: the gate fails closed, so a typo in the
horizon withholds the outcome instead of opening it.

**What is sealed.** Everything that computes an outcome-derived figure from the
event log, which today is :func:`trellis.analyze.holdout.analyze_holdout` and
nothing else, ``--by-commit`` included (it prints each arm's mean). Surfaces
call :func:`analyze_holdout_gated`, never ``analyze_holdout``;
``tests/unit/analyze/test_holdout_gate_rule.py`` enumerates the entry points
and fails when a new surface, or a new outcome-mode function, skips it. The
operational read (:func:`~trellis.analyze.holdout.analyze_holdout_operational`)
reads no outcome value and is never sealed.

**What a refusal carries.** The horizon and the fact that the outcome is sealed.
The gate runs before the event log is read for any figure, so nothing about the
arms or their sizes can ride the refusal, and it writes no event: a refusal
leaves the store as it found it.
"""

from __future__ import annotations

import functools
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from trellis.analyze.holdout import analyze_holdout
from trellis.errors import ConfigError, HoldoutReadoutSealedError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: ISO-8601 date or timestamp before which the outcome readout is refused.
#: Naive values are UTC. Read on every call, so a long-lived server needs a
#: restart to change it, like every other env knob.
HOLDOUT_READOUT_AFTER_ENV = "TRELLIS_HOLDOUT_READOUT_AFTER"

#: What the refusal points a reader at: the read that shows the experiment is
#: running without reading an outcome.
OPERATIONAL_HINT = "trellis analyze holdout --operational"

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _as_utc(moment: datetime) -> datetime:
    return (
        moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    )


def _now() -> datetime:
    """The clock a gated surface reads; a test freezes it here."""
    return datetime.now(UTC)


def readout_horizon(env: Mapping[str, str] | None = None) -> datetime | None:
    """The instant the outcome readout opens, as aware UTC, or ``None`` if unset.

    Raises:
        ConfigError: the variable is set to something that is not an ISO-8601
            date or timestamp. The caller must treat that as sealed.
    """
    source = os.environ if env is None else env
    raw = source.get(HOLDOUT_READOUT_AFTER_ENV, "").strip()
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except (ValueError, OverflowError) as exc:
        message = (
            f"{HOLDOUT_READOUT_AFTER_ENV} is not an ISO-8601 date or timestamp "
            f"(got {raw[:64]!r}); the holdout outcome stays sealed until it is "
            f"fixed."
        )
        raise ConfigError(message, setting=HOLDOUT_READOUT_AFTER_ENV) from exc


def require_readout_open(
    now: datetime | None = None, *, env: Mapping[str, str] | None = None
) -> None:
    """Raise unless the holdout outcome may be read at ``now`` (default: the clock).

    The seal lifts at the horizon itself: a read at exactly that instant is
    open. A naive ``now`` is read as UTC.

    Raises:
        HoldoutReadoutSealedError: ``now`` precedes the horizon. The message
            names the horizon and :data:`OPERATIONAL_HINT`, and nothing else.
        ConfigError: the horizon is set and unreadable (fails closed).
    """
    horizon = readout_horizon(env)
    if horizon is None:
        return
    moment = _now() if now is None else _as_utc(now)
    if moment >= horizon:
        return
    message = (
        f"The holdout outcome readout is sealed until {horizon.isoformat()} "
        f"({HOLDOUT_READOUT_AFTER_ENV}). `{OPERATIONAL_HINT}` shows whether "
        f"the experiment is running without reading an outcome."
    )
    raise HoldoutReadoutSealedError(message, horizon=horizon)


def _sealed(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """``function`` behind :func:`require_readout_open`, signature unchanged."""

    @functools.wraps(function)
    def gated(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        require_readout_open()
        return function(*args, **kwargs)

    return gated


#: ``analyze_holdout`` behind the seal: the one entry every surface that reports
#: the outcome calls. The gate runs before ``analyze_holdout`` reads a row.
analyze_holdout_gated = _sealed(analyze_holdout)

__all__ = [
    "HOLDOUT_READOUT_AFTER_ENV",
    "OPERATIONAL_HINT",
    "analyze_holdout_gated",
    "readout_horizon",
    "require_readout_open",
]
