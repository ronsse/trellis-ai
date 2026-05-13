"""Typed SDK exceptions.

Callers catch these to branch on HTTP failure mode rather than parsing
stringified status codes. Every non-2xx response from the API becomes
one of the :class:`TrellisHttpError` subclasses; network / transport
errors map to :class:`TrellisTransportError` so callers never need to
import ``httpx`` to handle them.

Hierarchy:

.. code-block:: text

    TrellisError                      (base)
    ├── TrellisHttpError              (any HTTP boundary failure)
    │   ├── TrellisClientError        (4xx, caller bug)
    │   │   └── TrellisRateLimitError (429, carries Retry-After)
    │   ├── TrellisServerError        (5xx, transient)
    │   └── TrellisTransportError     (no response: connection/timeout)
    ├── TrellisAPIError               (legacy alias, see below)
    └── TrellisVersionMismatchError   (handshake)

``TrellisAPIError`` predates the split hierarchy; new code should
catch :class:`TrellisHttpError` (or a more specific subclass).
``TrellisAPIError`` remains as a thin backwards-compatible alias.
"""

from __future__ import annotations


class TrellisError(Exception):
    """Base for all SDK-raised errors."""


class TrellisHttpError(TrellisError):
    """Any failure at the HTTP boundary — bad response or no response.

    Subclasses split the failure surface so callers can write::

        try:
            client.ingest_trace(t)
        except TrellisRateLimitError as exc:
            time.sleep(exc.retry_after_seconds or 1.0)
        except TrellisClientError:
            raise  # caller bug — don't retry
        except TrellisServerError:
            ...  # transient — back off + retry
        except TrellisTransportError:
            ...  # network blip — retry with jitter

    ``status_code`` is the HTTP status when one was received, else
    ``None`` (transport-level failures never see a response).
    ``body`` is the parsed JSON body when available, else ``None``.
    ``request_path`` helps diagnose which call failed when multiple
    clients share a base URL.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: object = None,
        request_path: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.request_path = request_path
        if status_code is not None:
            prefix = (
                f"[{status_code} on {request_path}]"
                if request_path
                else f"[{status_code}]"
            )
        else:
            prefix = f"[transport on {request_path}]" if request_path else "[transport]"
        super().__init__(f"{prefix} {message}")


class TrellisClientError(TrellisHttpError):
    """4xx response — the caller's request was rejected.

    Don't retry; fix the call and try again. ``429`` is the one
    documented exception (see :class:`TrellisRateLimitError`) — the
    server is telling the caller to retry later, not that the
    request was malformed.
    """


class TrellisRateLimitError(TrellisClientError):
    """HTTP 429 from the API.

    ``retry_after_seconds`` is parsed from the ``Retry-After`` response
    header — either as an integer number of seconds or as an HTTP-date
    converted to a delta. ``None`` when the header is missing or
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
            message,
            status_code=429,
            body=body,
            request_path=request_path,
        )


class TrellisServerError(TrellisHttpError):
    """5xx response — the server hit an unexpected error.

    Treat as transient: retry with backoff. Repeated 5xx on the same
    request is a server-side bug worth paging on.
    """


class TrellisTransportError(TrellisHttpError):
    """No response received — connection refused, timeout, DNS failure.

    Wraps the underlying transport exception via ``__cause__``. Always
    safe to retry on idempotent calls; for non-idempotent calls callers
    must pass a stable ``Idempotency-Key`` header so a server-side
    commit (lost on the response wire) isn't duplicated by the retry.
    """

    def __init__(
        self,
        message: str,
        *,
        request_path: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=None,
            body=None,
            request_path=request_path,
        )


class TrellisAPIError(TrellisHttpError):
    """Legacy class for non-2xx HTTP responses — **not caught by new code paths**.

    Predates the split into :class:`TrellisClientError` /
    :class:`TrellisServerError`. The new typed subclasses inherit from
    :class:`TrellisHttpError`, not from ``TrellisAPIError``, so existing
    ``except TrellisAPIError`` callers will **stop catching** 4xx/5xx
    responses raised by :func:`raise_for_status`. Migrate to
    :class:`TrellisHttpError` (catches everything) or one of the
    specific subclasses (4xx vs 5xx vs 429 vs transport).

    ``raise_for_status`` only emits ``TrellisAPIError`` itself for the
    1xx/3xx fallthrough — every realistic non-2xx path lands on a
    typed subclass. This class is effectively reserved for hand-rolled
    callers and the version-handshake leftovers.

    ``status_code`` is required here (unlike :class:`TrellisHttpError`
    where ``None`` is allowed for transport errors).
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        body: object = None,
        request_path: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
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
    "TrellisClientError",
    "TrellisError",
    "TrellisHttpError",
    "TrellisRateLimitError",
    "TrellisServerError",
    "TrellisTransportError",
    "TrellisVersionMismatchError",
]
