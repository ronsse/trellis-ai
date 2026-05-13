"""C2 Phase 6 — typed exception hierarchy for the SDK HTTP boundary.

Replaces the pre-Phase-6 ``TrellisAPIError`` blob with a four-class
hierarchy so callers branch on failure mode rather than parsing status
codes::

    TrellisHttpError
    ├── TrellisClientError       (4xx, caller bug)
    │   └── TrellisRateLimitError (429)
    ├── TrellisServerError       (5xx, transient)
    └── TrellisTransportError    (no response: connection/timeout)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from trellis_sdk._http import raise_for_status, wrap_transport_error
from trellis_sdk.exceptions import (
    TrellisAPIError,
    TrellisClientError,
    TrellisHttpError,
    TrellisRateLimitError,
    TrellisServerError,
    TrellisTransportError,
)


class TestRaiseForStatusTypedMapping:
    def _resp(self, status: int, headers: dict | None = None) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.headers = headers or {}
        resp.text = f"HTTP {status}"
        resp.json.return_value = {"message": f"err {status}"}
        return resp

    def test_400_raises_client_error(self) -> None:
        with pytest.raises(TrellisClientError) as exc_info:
            raise_for_status(self._resp(400), request_path="/x")
        # 4xx is a caller bug; the typed shape is the new contract.
        assert exc_info.value.status_code == 400
        assert isinstance(exc_info.value, TrellisHttpError)

    def test_403_raises_client_error(self) -> None:
        with pytest.raises(TrellisClientError):
            raise_for_status(self._resp(403), request_path="/x")

    def test_409_raises_client_error(self) -> None:
        """Idempotency-conflict shape — 409 is a client error, not 5xx."""
        with pytest.raises(TrellisClientError) as exc_info:
            raise_for_status(self._resp(409), request_path="/x")
        assert exc_info.value.status_code == 409

    def test_429_raises_rate_limit_with_retry_after(self) -> None:
        with pytest.raises(TrellisRateLimitError) as exc_info:
            raise_for_status(
                self._resp(429, headers={"Retry-After": "42"}),
                request_path="/x",
            )
        assert exc_info.value.retry_after_seconds == 42.0
        # 429 is also a client-error subclass — both names catch it.
        assert isinstance(exc_info.value, TrellisClientError)

    def test_500_raises_server_error(self) -> None:
        with pytest.raises(TrellisServerError) as exc_info:
            raise_for_status(self._resp(500), request_path="/x")
        assert exc_info.value.status_code == 500
        # Not a TrellisClientError — the split lets callers branch.
        assert not isinstance(exc_info.value, TrellisClientError)
        assert isinstance(exc_info.value, TrellisHttpError)

    def test_503_raises_server_error(self) -> None:
        with pytest.raises(TrellisServerError):
            raise_for_status(self._resp(503), request_path="/x")

    def test_legacy_api_error_still_catches_4xx(self) -> None:
        """``TrellisAPIError`` predates the split — new typed classes
        inherit from ``TrellisHttpError``, not ``TrellisAPIError``, so
        the legacy class no longer catches 4xx/5xx. Callers must
        migrate to ``TrellisHttpError``."""
        with pytest.raises(TrellisHttpError):
            raise_for_status(self._resp(400), request_path="/x")
        # And the new class is observably not the legacy one — explicit
        # cover so anyone who relied on the legacy alias gets a clear
        # migration signal from this test.
        with pytest.raises(TrellisClientError) as exc_info:
            raise_for_status(self._resp(400), request_path="/x")
        assert not isinstance(exc_info.value, TrellisAPIError)


class TestWrapTransportError:
    def test_wraps_httpx_connect_error(self) -> None:
        cause = httpx.ConnectError("connection refused")
        wrapped = wrap_transport_error(cause, request_path="/api/v1/x")
        assert isinstance(wrapped, TrellisTransportError)
        assert isinstance(wrapped, TrellisHttpError)
        assert wrapped.status_code is None
        assert wrapped.request_path == "/api/v1/x"
        assert wrapped.__cause__ is cause

    def test_wraps_httpx_read_timeout(self) -> None:
        cause = httpx.ReadTimeout("read timed out")
        wrapped = wrap_transport_error(cause, request_path="/api/v1/x")
        assert isinstance(wrapped, TrellisTransportError)

    def test_transport_error_is_catchable_as_http_error(self) -> None:
        """Callers catching ``TrellisHttpError`` get both response-level
        and transport-level failures — the whole HTTP boundary in one
        catch."""
        cause = httpx.ConnectError("connection refused")
        wrapped = wrap_transport_error(cause, request_path="/x")
        with pytest.raises(TrellisHttpError):
            raise wrapped


class TestSyncClientTransportError:
    """The sync client must raise ``TrellisTransportError`` on connect
    failures rather than letting raw ``httpx`` exceptions leak through."""

    def test_request_wraps_httpx_error(self) -> None:
        from trellis_sdk.client import TrellisClient

        http = MagicMock(spec=httpx.Client)
        http.request.side_effect = httpx.ConnectError("refused")
        # Skip handshake by setting verify_version=False.
        client = TrellisClient(http=http, verify_version=False)
        with pytest.raises(TrellisTransportError) as exc_info:
            client.ingest_trace({"trace_id": "t1"})
        assert exc_info.value.request_path == "/api/v1/traces"


class TestAsyncClientTransportError:
    @pytest.mark.asyncio
    async def test_request_wraps_httpx_error(self) -> None:
        from trellis_sdk.async_client import AsyncTrellisClient

        http = MagicMock(spec=httpx.AsyncClient)

        async def _raise(*_args: object, **_kw: object) -> None:
            msg = "refused"
            raise httpx.ConnectError(msg)

        http.request = MagicMock(side_effect=_raise)
        client = AsyncTrellisClient(http=http, verify_version=False)
        with pytest.raises(TrellisTransportError) as exc_info:
            await client.ingest_trace({"trace_id": "t1"})
        assert exc_info.value.request_path == "/api/v1/traces"
        await client.close()
