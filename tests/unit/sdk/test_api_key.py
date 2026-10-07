"""Tests for TrellisClient / AsyncTrellisClient api_key= credential injection.

Follow-up from the #804 gate finding: the SDK sent no credential, so a
reader pointed at a server running ``TRELLIS_AUTH_MODE=required`` got an
undocumented 401 on the first call unless they knew to inject
``http=httpx.Client(headers=...)`` manually. All content here (header
values, observation payloads) is synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from trellis.testing import in_memory_client
from trellis_sdk._http import API_KEY_ENV_VAR, API_KEY_HEADER
from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import TrellisClientError, TrellisTransportError

_SYNTHETIC_KEY = "synthetic-test-key-do-not-use"


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
    """Swap the client's own httpx transport for a MockTransport.

    ``api_key=`` only takes effect on the ``httpx.Client``/``AsyncClient``
    the constructor builds itself (the ``base_url=`` path), so these tests
    build a real client and then replace its transport — unlike the
    existing SDK tests, which inject a pre-built ``http=`` and therefore
    can't exercise ``api_key=`` at all (that combination is rejected, see
    ``TestApiKeyRejectsInjectedHttp`` below).
    """
    client._http._transport = httpx.MockTransport(handler)


class TestApiKeyHeaderSync:
    def test_header_sent_on_handshake_and_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver", api_key=_SYNTHETIC_KEY)
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert len(calls) == 2, "expected one handshake call plus one real call"
        for headers in calls:
            assert headers.get(API_KEY_HEADER.lower()) == _SYNTHETIC_KEY

    def test_no_header_when_api_key_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver")
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert len(calls) == 2
        for headers in calls:
            assert API_KEY_HEADER.lower() not in headers

    def test_env_fallback_used_when_api_key_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(API_KEY_ENV_VAR, "env-fallback-key")
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver")
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert calls[0].get(API_KEY_HEADER.lower()) == "env-fallback-key"

    def test_explicit_api_key_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(API_KEY_ENV_VAR, "env-fallback-key")
        calls: list[dict[str, str]] = []
        client = TrellisClient(base_url="http://testserver", api_key="explicit-key")
        _install_mock_transport(client, _recording_handler(calls))

        client.assemble_pack("intent")

        assert calls[0].get(API_KEY_HEADER.lower()) == "explicit-key"


class TestApiKeyRejectsInjectedHttp:
    """Mutual exclusion with ``http=``, mirroring ``base_url``/``http``.

    An injected ``http=`` client's headers belong to the caller: silently
    setting a header on it (or silently ignoring ``api_key=``) would be a
    footgun either way, so construction refuses the combination outright
    instead of guessing which of the two was meant.
    """

    def test_sync_rejects_api_key_with_http(self) -> None:
        http = httpx.Client(
            transport=httpx.MockTransport(_recording_handler([])),
            base_url="http://testserver",
        )
        with pytest.raises(ValueError, match="api_key"):
            TrellisClient(http=http, api_key=_SYNTHETIC_KEY)

    def test_async_rejects_api_key_with_http(self) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(_recording_handler([])),
            base_url="http://testserver",
        )
        with pytest.raises(ValueError, match="api_key"):
            AsyncTrellisClient(http=http, api_key=_SYNTHETIC_KEY)


class TestApiKeyHeaderAsync:
    async def test_header_sent_on_handshake_and_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        calls: list[dict[str, str]] = []
        client = AsyncTrellisClient(
            base_url="http://testserver", api_key=_SYNTHETIC_KEY
        )
        _install_mock_transport(client, _recording_handler(calls))

        await client.assemble_pack("intent")

        assert len(calls) == 2
        for headers in calls:
            assert headers.get(API_KEY_HEADER.lower()) == _SYNTHETIC_KEY

    async def test_no_header_when_api_key_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        calls: list[dict[str, str]] = []
        client = AsyncTrellisClient(base_url="http://testserver")
        _install_mock_transport(client, _recording_handler(calls))

        await client.assemble_pack("intent")

        for headers in calls:
            assert API_KEY_HEADER.lower() not in headers


class TestApiKeyNeverLeaks:
    def test_key_absent_from_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
        client = TrellisClient(base_url="http://testserver", api_key=_SYNTHETIC_KEY)
        assert _SYNTHETIC_KEY not in repr(client)

    def test_key_absent_from_401_exception_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

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

    def test_key_absent_from_transport_error_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

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
    """Exercises real `TRELLIS_AUTH_MODE` enforcement through the in-memory
    API client, not a mocked transport.

    ``trellis.testing.inmemory._build_app`` does not replicate production
    ``create_app()``'s router-level ``dependencies=[Depends(require_scope(...))]``
    wiring, so most routes (``assemble_pack`` among them) skip auth
    entirely in this fixture regardless of ``TRELLIS_AUTH_MODE`` — verified
    by probe before writing this test. ``POST /api/v1/observations``
    (``record_observation``) is the one route whose auth dependency is
    declared inline on the route itself, so it is the one case this
    fixture can use to prove the header the SDK now sends is what the
    server's auth layer actually requires.
    """

    _PAYLOAD: ClassVar[dict[str, Any]] = {
        "subject_entity_id": "synthetic-entity-1",
        "subject_entity_type": "synthetic_type",
        "observer_agent_id": "sdk-test-agent",
        "content": "synthetic observation for the api-key auth test",
    }

    def test_credential_required_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv(API_KEY_ENV_VAR, _SYNTHETIC_KEY)
        with in_memory_client(tmp_path / "stores") as client:
            with pytest.raises(TrellisClientError) as excinfo:
                client.record_observation(dict(self._PAYLOAD))
            assert excinfo.value.status_code == 401

            client._http.headers[API_KEY_HEADER] = _SYNTHETIC_KEY
            observation_id = client.record_observation(dict(self._PAYLOAD))
            assert observation_id
