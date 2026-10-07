"""Tests for the ``api_key=`` credential on TrellisClient / AsyncTrellisClient.

All content here (header values, observation payloads) is synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from trellis.testing import in_memory_client
from trellis_sdk._http import API_KEY_HEADER
from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import TrellisClientError, TrellisTransportError

_SYNTHETIC_KEY = "synthetic-test-key-do-not-use"

#: The server's legacy shared-secret variable. Tests set it to prove the
#: client never sends what a server process holds in its environment.
_SERVER_KEY_VAR = "TRELLIS_API_KEY"


def _recording_handler(calls: list[dict[str, str]]):
    """A handler that records each request's headers and answers 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.url.path == "/api/version":
            return httpx.Response(
                200, json={"api_major": 1, "api_minor": 2, "sdk_min": "0.0.0"}
            )
        return httpx.Response(200, json={"pack_id": "p1", "items": []})

    return handler


def _install_mock_transport(
    client: TrellisClient | AsyncTrellisClient, handler
) -> None:
    """Swap the transport of the httpx client the constructor built.

    ``api_key=`` only applies on the ``base_url=`` path (it is refused
    beside an injected ``http=``), so these tests build a real client and
    then replace its transport.
    """
    client._http._transport = httpx.MockTransport(handler)


class TestApiKeyHeaderSync:
    def test_header_sent_on_handshake_and_call(self) -> None:
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver", api_key=_SYNTHETIC_KEY)
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert len(calls) == 2, "expected one handshake call plus one real call"
        for headers in calls:
            assert headers.get(API_KEY_HEADER.lower()) == _SYNTHETIC_KEY

    def test_no_header_without_api_key_even_with_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SERVER_KEY_VAR, _SYNTHETIC_KEY)
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver")
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert len(calls) == 2
        for headers in calls:
            assert API_KEY_HEADER.lower() not in headers


class TestApiKeyHeaderAsync:
    async def test_header_sent_on_handshake_and_call(self) -> None:
        calls: list[dict[str, str]] = []
        client = AsyncTrellisClient(
            base_url="http://testserver", api_key=_SYNTHETIC_KEY
        )
        _install_mock_transport(client, _recording_handler(calls))

        await client.assemble_pack("intent")

        assert len(calls) == 2
        for headers in calls:
            assert headers.get(API_KEY_HEADER.lower()) == _SYNTHETIC_KEY

    async def test_no_header_without_api_key_even_with_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SERVER_KEY_VAR, _SYNTHETIC_KEY)
        calls: list[dict[str, str]] = []
        client = AsyncTrellisClient(base_url="http://testserver")
        _install_mock_transport(client, _recording_handler(calls))

        await client.assemble_pack("intent")

        assert len(calls) == 2
        for headers in calls:
            assert API_KEY_HEADER.lower() not in headers


class TestApiKeyRefused:
    @pytest.mark.parametrize(
        ("client_cls", "http_cls"),
        [(TrellisClient, httpx.Client), (AsyncTrellisClient, httpx.AsyncClient)],
    )
    def test_refused_beside_injected_http(self, client_cls: Any, http_cls: Any) -> None:
        http = http_cls(base_url="http://testserver")
        with pytest.raises(ValueError, match="api_key"):
            client_cls(http=http, api_key=_SYNTHETIC_KEY)

    @pytest.mark.parametrize("client_cls", [TrellisClient, AsyncTrellisClient])
    def test_empty_key_refused(self, client_cls: Any) -> None:
        with pytest.raises(ValueError, match="empty"):
            client_cls(base_url="http://testserver", api_key="")


class TestApiKeyNeverLeaks:
    def test_key_absent_from_401_exception_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/version":
                return httpx.Response(
                    200, json={"api_major": 1, "api_minor": 2, "sdk_min": "0.0.0"}
                )
            return httpx.Response(
                401, json={"detail": "missing or invalid API credentials"}
            )

        client = TrellisClient(base_url="http://testserver", api_key=_SYNTHETIC_KEY)
        _install_mock_transport(client, handler)

        with pytest.raises(TrellisClientError) as excinfo:
            client.assemble_pack("intent")

        assert _SYNTHETIC_KEY not in str(excinfo.value)
        assert _SYNTHETIC_KEY not in repr(excinfo.value)

    def test_key_absent_from_transport_error_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            connect_error_message = "boom"
            raise httpx.ConnectError(connect_error_message, request=request)

        client = TrellisClient(
            base_url="http://testserver", api_key=_SYNTHETIC_KEY, verify_version=False
        )
        _install_mock_transport(client, handler)

        with pytest.raises(TrellisTransportError) as excinfo:
            client.assemble_pack("intent")

        assert _SYNTHETIC_KEY not in str(excinfo.value)
        assert _SYNTHETIC_KEY not in repr(excinfo.value)


class TestEndToEndAuthRequired:
    """``api_key=`` against the real auth layer, through the in-memory app.

    ``trellis.testing.inmemory._build_app`` wires no router-level auth, so
    ``record_observation``, whose route declares ``require_scope`` inline,
    is the SDK call this fixture can use to prove the server accepts the
    header the client sends.
    """

    _PAYLOAD: ClassVar[dict[str, Any]] = {
        "subject_entity_id": "synthetic-entity-1",
        "subject_entity_type": "synthetic_type",
        "observer_agent_id": "sdk-test-agent",
        "content": "synthetic observation for the api-key auth test",
    }

    def test_api_key_authenticates_and_env_secret_is_not_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv(_SERVER_KEY_VAR, _SYNTHETIC_KEY)
        with in_memory_client(tmp_path / "stores") as app_client:
            app_transport = app_client._http._transport

            anonymous = TrellisClient(
                base_url="http://testserver", verify_version=False
            )
            anonymous._http._transport = app_transport
            with pytest.raises(TrellisClientError) as excinfo:
                anonymous.record_observation(dict(self._PAYLOAD))
            assert excinfo.value.status_code == 401

            keyed = TrellisClient(
                base_url="http://testserver",
                api_key=_SYNTHETIC_KEY,
                verify_version=False,
            )
            keyed._http._transport = app_transport
            assert keyed.record_observation(dict(self._PAYLOAD))
