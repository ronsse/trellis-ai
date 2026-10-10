"""Follow-up to trellis-ai#829/#836/#837's "describe, don't quote" sweep
(issue #206): ``record_feedback``'s malformed-2xx-body path.

``TrellisClient.record_feedback``/``AsyncTrellisClient.record_feedback``
validate the response body against
:class:`~trellis_wire.PackFeedbackResponse`. ``TrellisProtocolError``'s own
docstring names the trigger — "a proxy/interstitial returning HTML, or
SDK<->server wire-schema skew" — so the body reaching ``model_validate``
is not guaranteed to be anything the Trellis server itself sanitized.
Before the fix, the handler built its message as
``f"unexpected response body for {path}: {exc}"``: a pydantic
``ValidationError``'s own ``str()`` composes ``input_value=<the body's own
field value>`` into every line by design, so whatever that body held was
quoted back whole into the raised ``TrellisProtocolError``. Fixed by
routing through :func:`trellis_sdk._http.describe_body_parse_error`
(``errors(include_input=False)``, keeping only ``loc``/``type``) -- a
local, dependency-free port of
:func:`trellis.core.error_sanitize.describe_validation_error`.
``trellis_sdk`` must not import ``trellis.*``
(``tests/unit/sdk/test_isolation.py`` enforces this by AST-walking the
package), which an earlier draft of this fix violated by importing the
core helper directly; that draft never reached ``main``.
"""

from __future__ import annotations

import httpx
import pytest

from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient
from trellis_sdk.exceptions import TrellisProtocolError

#: A credential-shaped planted value (the task brief's own example).
#: ``describe_body_parse_error`` never serializes it at all -- for a
#: pydantic ``ValidationError`` it drops ``msg``/``input_value``
#: unconditionally, keeping only ``loc`` and ``type``.
HOSTILE = "sk-ant-TESTTOKEN-9f8e7d6c5b4a"


def _malformed_body() -> dict[str, object]:
    """A ``PackFeedbackResponse``-shaped body with one field of the wrong
    type, carrying the planted value -- as a proxy/misbehaving server
    might send back verbatim."""
    return {
        "status": "ok",
        "pack_id": "p1",
        "feedback_id": "f1",
        "feedback": "positive",
        "event_log_in_sync": {"leak": HOSTILE},
        "event_log_emitted": True,
        "event_log_skipped_as_duplicate": False,
    }


class TestSyncRecordFeedbackMalformedBody:
    def test_drops_the_value(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_malformed_body())

        http = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://testserver"
        )
        with (
            TrellisClient(http=http, verify_version=False) as client,
            pytest.raises(TrellisProtocolError) as excinfo,
        ):
            client.record_feedback(pack_id="p1", success=True)

        message = str(excinfo.value)
        assert HOSTILE not in message
        assert "event_log_in_sync" in message
        assert "bool_type" in message


class TestAsyncRecordFeedbackMalformedBody:
    async def test_drops_the_value(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_malformed_body())

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://testserver"
        )
        async with AsyncTrellisClient(http=http, verify_version=False) as client:
            with pytest.raises(TrellisProtocolError) as excinfo:
                await client.record_feedback(pack_id="p1", success=True)

        message = str(excinfo.value)
        assert HOSTILE not in message
        assert "event_log_in_sync" in message
        assert "bool_type" in message
