"""Typed SDK exceptions.

Callers catch these to branch on HTTP failure mode rather than parsing
stringified status codes.  Every non-2xx response from the API becomes
one of these; network / transport errors pass through as
:class:`httpx.HTTPError` subclasses untouched.
"""

from __future__ import annotations


class TrellisError(Exception):
    """Base for all SDK-raised errors."""


class TrellisAPIError(TrellisError):
    """Non-2xx response from the Trellis API.

    ``status_code`` is the HTTP status; ``body`` is the parsed JSON
    body when available (else the raw text).  ``request_path`` helps
    with diagnostics when multiple clients share the same base URL.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        body: object = None,
        request_path: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.request_path = request_path
        prefix = (
            f"[{status_code} on {request_path}]"
            if request_path
            else f"[{status_code}]"
        )
        super().__init__(f"{prefix} {message}")


class TrellisRateLimitError(TrellisAPIError):
    """HTTP 429 from the API.

    ``retry_after_seconds`` is parsed from the ``Retry-After`` response
    header — either as an integer number of seconds or as an HTTP-date
    converted to a delta.  ``None`` when the header is missing or
    unparseable; callers should fall back to exponential backoff.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None,
        body: object = None,
        request_path: str | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            429,
            message,
            body=body,
            request_path=request_path,
        )


class TrellisVersionMismatchError(TrellisError):
    """The server's API version is incompatible with this SDK.

    Raised at handshake time (first call) when:

    * The server's ``api_major`` differs from :data:`SDK_API_MAJOR`, or
    * The SDK's own version is below the server's ``sdk_min``.

    Minor drift (server older than SDK's expected minor) logs a warning
    instead of raising.
    """

    def __init__(
        self,
        message: str,
        *,
        server_api_major: int,
        server_api_minor: int,
        sdk_min: str,
    ) -> None:
        self.server_api_major = server_api_major
        self.server_api_minor = server_api_minor
        self.sdk_min = sdk_min
        super().__init__(message)


__all__ = [
    "TrellisAPIError",
    "TrellisError",
    "TrellisRateLimitError",
    "TrellisVersionMismatchError",
]
