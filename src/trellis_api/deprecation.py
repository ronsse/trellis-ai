"""Route deprecation registry + header emitter.

A single source of truth for which routes are deprecated, when they'll
be removed, and what the replacement is.  Read by two consumers:

* :func:`apply_deprecation_headers` — sets ``Deprecation`` /
  ``Sunset`` / ``Link`` response headers on requests to a deprecated
  route (RFC 8594 + RFC 9745).
* The ``/api/version`` handshake route — exposes the full list so
  clients can log warnings *before* they hit a deprecated path.

Adding a deprecation is one line in :data:`ROUTE_DEPRECATIONS` — no
per-route code changes needed beyond calling
``apply_deprecation_headers(response, "/api/v1/old/path")`` in the
handler (or wiring it once via a dependency if the list grows).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from trellis.core.base import TrellisModel

if TYPE_CHECKING:
    from fastapi import Response


class DeprecationEntry(TrellisModel):
    """One deprecated route.

    ``deprecated_since`` and ``sunset_on`` are ISO dates.  The route
    path is stored on the key side of :data:`ROUTE_DEPRECATIONS` —
    keep it absolute (``/api/v1/...``) so it matches ``request.url.path``.
    """

    deprecated_since: date
    sunset_on: date
    replacement: str | None = None
    reason: str | None = None


# path -> DeprecationEntry.  Empty on initial landing — the mechanism
# ships before any actual deprecations so later PRs can add a single
# line here without plumbing changes.
ROUTE_DEPRECATIONS: dict[str, DeprecationEntry] = {}


def apply_deprecation_headers(response: Response, path: str) -> None:
    """Set RFC-standard deprecation headers on ``response`` if ``path``
    is in :data:`ROUTE_DEPRECATIONS`.

    No-op when the path is not deprecated — safe to call unconditionally
    from any handler.  Headers set:

    * ``Deprecation: @<unix_ts>`` (RFC 9745)
    * ``Sunset: <HTTP-date>`` (RFC 8594)
    * ``Link: <replacement>; rel="successor-version"`` when a
      replacement is recorded.
    """
    entry = ROUTE_DEPRECATIONS.get(path)
    if entry is None:
        return

    # RFC 9745: Deprecation as an IMF-fixdate or @unix-seconds; we use
    # the @unix-seconds shorthand since it's easier to generate.
    deprecated_ts = int(
        _date_to_epoch_seconds(entry.deprecated_since),
    )
    response.headers["Deprecation"] = f"@{deprecated_ts}"

    # RFC 8594: Sunset as HTTP-date.
    response.headers["Sunset"] = _http_date(entry.sunset_on)

    if entry.replacement:
        response.headers["Link"] = f'<{entry.replacement}>; rel="successor-version"'


def _date_to_epoch_seconds(d: date) -> int:
    """Convert a date to Unix epoch seconds (UTC midnight)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _http_date(d: date) -> str:
    """Format a date as an HTTP-date (RFC 7231 §7.1.1.1)."""
    from datetime import UTC, datetime  # noqa: PLC0415
    from email.utils import format_datetime  # noqa: PLC0415

    return format_datetime(
        datetime(d.year, d.month, d.day, tzinfo=UTC),
        usegmt=True,
    )


__all__ = [
    "ROUTE_DEPRECATIONS",
    "DeprecationEntry",
    "apply_deprecation_headers",
]
