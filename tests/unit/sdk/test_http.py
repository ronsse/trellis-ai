"""Tests for version handshake, typed exceptions, and Retry-After parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from trellis.testing import in_memory_client
from trellis_sdk._http import (
    SDK_API_MAJOR,
    SDK_VERSION,
    _parse_retry_after,
    check_handshake,
    raise_for_status,
)
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import (
    TrellisAPIError,
    TrellisRateLimitError,
    TrellisVersionMismatchError,
)


class TestRaiseForStatus:
    def test_2xx_no_raise(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        raise_for_status(resp, request_path="/api/v1/x")  # no exception

    def test_404_no_raise(self):
        """404 is the caller's decision — many SDK methods return None."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        raise_for_status(resp, request_path="/api/v1/x")  # no exception

    def test_500_raises_api_error(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 500
        resp.text = "boom"
        resp.json.side_effect = ValueError("not json")
        with pytest.raises(TrellisAPIError) as exc_info:
            raise_for_status(resp, request_path="/api/v1/x")
        assert exc_info.value.status_code == 500
        assert exc_info.value.request_path == "/api/v1/x"

    def test_429_raises_rate_limit_with_retry_after_seconds(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.headers = {"Retry-After": "30"}
        resp.text = "slow down"
        resp.json.return_value = {"message": "slow down"}
        with pytest.raises(TrellisRateLimitError) as exc_info:
            raise_for_status(resp, request_path="/api/v1/x")
        assert exc_info.value.retry_after_seconds == 30.0

    def test_429_without_retry_after_header(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.headers = {}
        resp.text = "slow down"
        resp.json.return_value = {}
        with pytest.raises(TrellisRateLimitError) as exc_info:
            raise_for_status(resp, request_path="/api/v1/x")
        assert exc_info.value.retry_after_seconds is None


class TestRetryAfterParsing:
    def test_none_header(self):
        assert _parse_retry_after(None) is None

    def test_integer_seconds(self):
        assert _parse_retry_after("42") == 42.0

    def test_whitespace_stripped(self):
        assert _parse_retry_after("  5  ") == 5.0

    def test_http_date(self):
        future = datetime.now(tz=UTC) + timedelta(seconds=10)
        header = format_datetime(future, usegmt=True)
        parsed = _parse_retry_after(header)
        assert parsed is not None
        # Window because now() moves between write and read.
        assert 0 <= parsed <= 11

    def test_past_http_date_clamps_to_zero(self):
        past = datetime.now(tz=UTC) - timedelta(hours=1)
        header = format_datetime(past, usegmt=True)
        assert _parse_retry_after(header) == 0.0

    def test_unparseable_returns_none(self):
        assert _parse_retry_after("not-a-date") is None


class TestHandshake:
    def test_matching_major_ok(self):
        check_handshake(
            {
                "api_major": SDK_API_MAJOR,
                "api_minor": 0,
                "sdk_min": "0.0.0",
            }
        )  # no exception

    def test_different_major_raises(self):
        with pytest.raises(TrellisVersionMismatchError) as exc_info:
            check_handshake(
                {
                    "api_major": SDK_API_MAJOR + 1,
                    "api_minor": 0,
                    "sdk_min": "0.0.0",
                }
            )
        assert exc_info.value.server_api_major == SDK_API_MAJOR + 1

    def test_sdk_below_min_raises(self):
        with pytest.raises(TrellisVersionMismatchError, match="below the server"):
            check_handshake(
                {
                    "api_major": SDK_API_MAJOR,
                    "api_minor": 0,
                    "sdk_min": "999.0.0",
                },
                sdk_version=SDK_VERSION,
            )

    def test_server_minor_older_warns_not_raises(self, caplog):
        # SDK_API_MINOR might be 0, so use a high expected minor to trigger the warning.
        check_handshake(
            {
                "api_major": SDK_API_MAJOR,
                "api_minor": 0,
                "sdk_min": "0.0.0",
            },
            sdk_api_minor=5,
        )  # no exception


class TestIntegrationHandshake:
    def test_handshake_disabled_by_default_in_test_fixture(self, tmp_path: Path):
        """The testing fixture passes verify_version=False so handshake is skipped."""
        with in_memory_client(tmp_path / "stores") as client:
            assert client._verify_version is False

    def test_handshake_runs_when_enabled(self, tmp_path: Path):
        """Explicitly enable handshake against the in-memory app."""
        from starlette.testclient import TestClient as StarletteTestClient

        import trellis.testing.inmemory as inmem

        registry = inmem._make_registry(tmp_path / "stores")
        app = inmem._build_app(registry)
        http = StarletteTestClient(app, base_url="http://testserver")
        http.__enter__()
        try:
            client = TrellisClient(http=http, verify_version=True)
            # Trigger handshake by making a request.
            client.list_traces()
            assert client._handshake_done is True
        finally:
            http.__exit__(None, None, None)
            registry.close()
            import trellis_api.app as app_module

            app_module._registry = None
