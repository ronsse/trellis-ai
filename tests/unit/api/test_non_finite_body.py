"""A validation error echoing a non-finite number answers 422.

Python's ``json`` module parses every request body, and it accepts the bare
tokens ``NaN``, ``Infinity`` and ``-Infinity``, so any caller can put one in
any body. FastAPI's default ``RequestValidationError`` handler echoes the
rejected value back as ``detail[].input``, Starlette renders JSON with
``allow_nan=False``, and building that 422 raises ``ValueError``, so the
catch-all answers ``500 internal_error`` and logs a traceback for a
caller's malformed body. A missing-field error echoes the whole body as
its ``input``, so a non-finite number anywhere in a body missing a field
reaches that renderer.

A body that is not valid UTF-8 has the same shape of defect for a
different reason. FastAPI's own routing already turns a JSON body's
decode failure into a 422 or 400 before this handler ever sees it
(:class:`TestUndecodableJsonBodyIsAlreadyOutOfScope`), but a body sent
with no JSON content type — ``text/plain``, say — reaches a model with no
JSON parse to fail on first, so the validation error's ``input`` is the
raw, undecodable ``bytes`` themselves. ``jsonable_encoder``'s default
``bytes`` encoder is a strict ``.decode()``, which raises
``UnicodeDecodeError``, and the catch-all answers 500 again.

:func:`~trellis_api.middleware.request_validation_error_handler` keeps
FastAPI's 422 and renders a non-finite float, or undecodable bytes,
anywhere in the error payload as the token or text the caller sent. Every
finite, decodable case stays byte-identical to FastAPI's default handler,
which :class:`TestFiniteErrorsMatchFastApisDefault` pins.

The client is built by :func:`~trellis_api.app.create_app`, so "the handler
exists" and "the handler is wired" are the same test.

The same handler has a second echo-shaped defect (issue #869 F5): a lone
UTF-16 surrogate instead of a non-finite float, answering 400 rather than
422 because there is no finite-token substitute for an unparseable code
point. ``TestLoneSurrogateAnswers400`` covers it.

Every id here is synthetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import trellis_api.app as app_module
from tests.structlog_isolation import reset_structlog_global_state
from trellis.feedback.recording import feedback_log_dir
from trellis.schemas.well_known import MEASUREMENT
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import Response

FEEDBACK = "/api/v1/feedback"
PACK_FEEDBACK = "/api/v1/packs/pk-synthetic-1/feedback"
MEASUREMENTS = "/api/v1/measurements"
PRECEDENTS = "/api/v1/precedents"
INGEST_BULK = "/api/v1/ingest/bulk"

#: The bare tokens Python's ``json`` module accepts, sent exactly as written.
NON_FINITE = ["NaN", "Infinity", "-Infinity"]

#: Not valid UTF-8: a lone UTF-16 BOM with no bytes behind it.
NON_UTF8_BODY = b"\xff\xfe"

_JSON = {"content-type": "application/json"}
_TEXT_PLAIN = {"content-type": "text/plain"}


def _measurement_body(value: str) -> str:
    """A complete measurement body with *value* spliced in as a bare token."""
    return (
        '{"subject_entity_id": "ent-synthetic-1", "subject_entity_type": "Dataset",'
        ' "metric_name": "metric_synthetic", "observer_agent_id": "agent-synthetic",'
        f' "metric_value": {value}}}'
    )


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """The real app. ``raise_server_exceptions=False`` shows what a caller sees."""
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def logs() -> Iterator[list[dict[str, Any]]]:
    """Every structlog event the request writes.

    The reset comes first because a logger memoised by an earlier test, or
    a level left behind by one, would make ``capture_logs`` blind, and a
    blind capture passes every "no such line" assertion.
    """
    reset_structlog_global_state()
    with capture_logs() as captured:
        yield captured
    reset_structlog_global_state()


def _post(client: TestClient, path: str, body: str) -> Response:
    return client.post(path, content=body, headers=_JSON)


def _unhandled(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == "api_unhandled_exception"]


def _assert_nothing_recorded(registry: StoreRegistry) -> None:
    assert registry.operational.event_log.count() == 0
    assert registry.stores_dir is not None
    jsonl = feedback_log_dir(registry.stores_dir) / "pack_feedback.jsonl"
    assert not jsonl.exists()


class TestNonFiniteRatingAnswers422:
    """Both feedback routes refuse a non-finite rating with a 422."""

    @pytest.mark.parametrize("token", NON_FINITE)
    @pytest.mark.parametrize("path", [FEEDBACK, PACK_FEEDBACK])
    def test_answers_422_echoing_the_token_and_records_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
        path: str,
        token: str,
    ) -> None:
        body = f'{{"target_id": "tg-synthetic-1", "rating": {token}}}'
        resp = _post(client, path, body)
        # One assertion, so a failure shows the status and the log together.
        assert (resp.status_code, _unhandled(logs)) == (422, [])
        detail = resp.json()["detail"]
        [error] = [e for e in detail if e["loc"] == ["body", "rating"]]
        assert error["input"] == token
        _assert_nothing_recorded(registry)

    def test_a_non_finite_value_nested_in_the_body_renders_as_its_token(
        self, client: TestClient, logs: list[dict[str, Any]]
    ) -> None:
        """Rendering reaches every depth of the error payload, not only the top."""
        body = '{"rating": 0.5, "extra": [NaN, {"deep": -Infinity}]}'
        resp = _post(client, FEEDBACK, body)
        assert (resp.status_code, _unhandled(logs)) == (422, [])
        by_loc = {tuple(e["loc"]): e for e in resp.json()["detail"]}
        assert by_loc["body", "extra"]["input"] == ["NaN", {"deep": "-Infinity"}]
        assert by_loc["body", "target_id"]["input"] == {
            "rating": 0.5,
            "extra": ["NaN", {"deep": "-Infinity"}],
        }


class TestRatingRange:
    """``/api/v1/feedback`` holds ``rating`` to the MCP tool's [0.0, 1.0]."""

    @pytest.mark.parametrize("rating", ["-0.1", "1.1"])
    def test_out_of_range_answers_422_and_records_nothing(
        self, client: TestClient, registry: StoreRegistry, rating: str
    ) -> None:
        body = f'{{"target_id": "tg-synthetic-1", "rating": {rating}}}'
        resp = _post(client, FEEDBACK, body)
        assert resp.status_code == 422, resp.text
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "rating"]
        _assert_nothing_recorded(registry)

    @pytest.mark.parametrize("rating", ["0.0", "0.5", "1.0"])
    def test_in_range_is_recorded(
        self, client: TestClient, registry: StoreRegistry, rating: str
    ) -> None:
        body = f'{{"target_id": "tg-synthetic-1", "rating": {rating}}}'
        resp = _post(client, FEEDBACK, body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"
        assert registry.operational.event_log.count() > 0


class TestRatingUnitMismatchHint:
    """A ``/10`` or ``/100`` rating's 422 names the likely fix.

    Both feedback routes validate ``rating`` with the same
    ``Field(ge=0.0, le=1.0)``, so both go through
    :func:`trellis_api.middleware._with_rating_hint`.
    """

    @pytest.mark.parametrize("path", [FEEDBACK, PACK_FEEDBACK])
    @pytest.mark.parametrize(("rating", "guess"), [("7", "0.7"), ("85", "0.85")])
    def test_hint_is_appended_to_the_range_error(
        self,
        client: TestClient,
        registry: StoreRegistry,
        path: str,
        rating: str,
        guess: str,
    ) -> None:
        body = f'{{"target_id": "tg-synthetic-1", "rating": {rating}}}'
        resp = _post(client, path, body)
        assert resp.status_code == 422, resp.text
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "rating"]
        assert f"did you mean {guess}?" in error["msg"]
        _assert_nothing_recorded(registry)

    def test_a_rating_with_no_plausible_unit_gets_no_guess(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        body = '{"target_id": "tg-synthetic-1", "rating": -1.0}'
        resp = _post(client, FEEDBACK, body)
        assert resp.status_code == 422, resp.text
        [error] = resp.json()["detail"]
        assert "did you mean" not in error["msg"]
        _assert_nothing_recorded(registry)


class TestFiniteErrorsMatchFastApisDefault:
    """For a finite body, the response is FastAPI's own, byte for byte.

    The comparison app is the real one with FastAPI's default handler
    registered over ours, so the two differ in exactly one handler.
    """

    @pytest.fixture
    def default_client(self, registry: StoreRegistry) -> TestClient:
        app = create_app()
        app.add_exception_handler(
            RequestValidationError,
            request_validation_exception_handler,  # type: ignore[arg-type]
        )
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            pytest.param(FEEDBACK, '{"rating": 0.5}', id="missing-field"),
            # 150.0 is outside both unit-mismatch buckets our handler
            # checks (see TestRatingUnitMismatchHint above), so it stays a
            # plain out-of-range miss with no added guess -- byte-identical.
            pytest.param(PACK_FEEDBACK, '{"rating": 150.0}', id="out-of-range-finite"),
        ],
    )
    def test_byte_identical(
        self,
        client: TestClient,
        default_client: TestClient,
        path: str,
        body: str,
    ) -> None:
        ours = _post(client, path, body)
        default = _post(default_client, path, body)
        assert ours.status_code == default.status_code == 422
        assert ours.headers["content-type"] == default.headers["content-type"]
        assert ours.content == default.content


class TestUndecodableBodyAnswers422:
    """A body that is not valid UTF-8 answers 422, not 500.

    Only a route reached with no JSON content type exposes this: FastAPI's
    own body parsing (``fastapi.routing``) already converts a JSON body's
    decode failure into a 422 or a 400 before this handler runs, so that
    path never carries undecodable bytes as far as ``jsonable_encoder``.
    """

    @pytest.mark.parametrize("path", [FEEDBACK, PACK_FEEDBACK, MEASUREMENTS])
    def test_answers_422_echoing_the_text_and_records_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
        path: str,
    ) -> None:
        resp = client.post(path, content=NON_UTF8_BODY, headers=_TEXT_PLAIN)
        # One assertion, so a failure shows the status and the log together.
        assert (resp.status_code, _unhandled(logs)) == (422, [])
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body"]
        assert error["input"] == NON_UTF8_BODY.decode(errors="backslashreplace")
        _assert_nothing_recorded(registry)

    def test_the_echoed_text_carries_only_what_the_caller_sent(
        self, client: TestClient
    ) -> None:
        """Leak check: the rendered text names exactly the bytes sent, in order.

        ``unicode_escape`` is ``backslashreplace``'s own inverse, so decoding
        the echoed text with it and re-encoding to raw bytes must reproduce
        the request body exactly — nothing invented, nothing dropped.
        """
        resp = client.post(FEEDBACK, content=NON_UTF8_BODY, headers=_TEXT_PLAIN)
        [error] = resp.json()["detail"]
        roundtrip = error["input"].encode("ascii").decode("unicode_escape")
        assert roundtrip.encode("latin-1") == NON_UTF8_BODY

    def test_decodable_bytes_stay_byte_identical_to_fastapis_default(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Valid-UTF-8 bytes routed the same way are unaffected by the new encoder."""
        app = create_app()
        app.add_exception_handler(
            RequestValidationError,
            request_validation_exception_handler,  # type: ignore[arg-type]
        )
        default_client = TestClient(app, raise_server_exceptions=False)
        body = b"not a json object, but plain decodable text"
        ours = client.post(FEEDBACK, content=body, headers=_TEXT_PLAIN)
        default = default_client.post(FEEDBACK, content=body, headers=_TEXT_PLAIN)
        assert ours.status_code == default.status_code == 422
        assert ours.content == default.content


class TestUndecodableJsonBodyIsAlreadyOutOfScope:
    """A JSON-content-type body never reaches this handler's bytes path.

    FastAPI's routing parses the body before validation runs: a
    ``json.JSONDecodeError`` becomes a 422 with ``input: {}`` (not the raw
    bytes), and any other decode failure (what a non-UTF-8 byte raises)
    becomes a 400 — both outside :func:`request_validation_error_handler`,
    and both pre-existing. Pinned so a future change to that routing is
    caught here rather than reopening the 500 this PR closes elsewhere.
    """

    @pytest.mark.parametrize("path", [FEEDBACK, MEASUREMENTS])
    def test_invalid_json_syntax_in_a_valid_utf8_body_answers_422(
        self, client: TestClient, path: str
    ) -> None:
        resp = client.post(path, content=b"{not valid json", headers=_JSON)
        assert resp.status_code == 422, resp.text
        [error] = resp.json()["detail"]
        assert error["type"] == "json_invalid"

    @pytest.mark.parametrize("path", [FEEDBACK, MEASUREMENTS])
    def test_non_utf8_bytes_under_json_content_type_never_answers_500(
        self, client: TestClient, logs: list[dict[str, Any]], path: str
    ) -> None:
        resp = client.post(path, content=NON_UTF8_BODY, headers=_JSON)
        assert resp.status_code in (400, 422), resp.text
        assert _unhandled(logs) == []


class TestMeasurementValue:
    """``metric_value`` refuses every non-finite float, not only NaN.

    ``Infinity`` used to be accepted; it is refused now because no
    consumer reads it as "infinite" — Postgres' JSONB refuses it
    outright, SQLite stores it as invalid JSON, REST shows it as
    ``null``, and the CLI prints a bare ``Infinity`` token that is not
    valid JSON (see the ``Measurement.metric_value`` field docstring).
    """

    @pytest.mark.parametrize(
        "token", ["NaN", "Infinity", "-Infinity", "1e400", '"inf"']
    )
    def test_non_finite_answers_422_and_stores_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
        token: str,
    ) -> None:
        resp = _post(client, MEASUREMENTS, _measurement_body(token))
        assert (resp.status_code, _unhandled(logs)) == (422, [])
        assert "metric_value" in resp.json()["detail"]
        assert registry.knowledge.graph_store.query(node_type=MEASUREMENT) == []
        assert registry.operational.event_log.count() == 0

    def test_largest_finite_float_is_recorded(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """``1e308`` is finite (``1e400`` overflows; this stays well short)."""
        resp = _post(client, MEASUREMENTS, _measurement_body("1e308"))
        assert resp.status_code == 201, resp.text
        [node] = registry.knowledge.graph_store.query(node_type=MEASUREMENT)
        assert node["properties"]["metric_value"] == 1e308
        assert registry.operational.event_log.count() > 0


class TestLoneSurrogateAnswers400:
    """A lone UTF-16 surrogate in any string field answers 400, not 500.

    Issue #869 F5. pydantic-core rejects an escaped lone surrogate
    (``"\\ud800"``) while decoding the JSON body into *any* ``str``
    field, with a ``string_unicode`` error whose own ``input`` carries
    the raw surrogate forward. FastAPI's default handler (and this
    module's own ``request_validation_error_handler`` before this fix)
    echoes that ``input`` back in the 422 body; ``JSONResponse.render``
    calls ``str.encode("utf-8")`` on the result, which refuses a lone
    surrogate outright ("surrogates not allowed") -- raising *while
    building the error response*, past every handler, so the catch-all
    answers 500.

    The fix lives once in ``request_validation_error_handler`` (wired
    for every route in :func:`trellis_api.app.create_app`), so any two
    routes with a plain ``str`` body field prove it; one ingest route
    (``/ingest/bulk``) and one curate route (``/precedents``) per the
    brief. Neither route-specific try/except matters here -- the body
    never reaches the route function at all.
    """

    def test_ingest_bulk_entity_name_answers_400_and_ingests_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
    ) -> None:
        body = '{"entities": [{"entity_type": "service", "name": "\\ud800"}]}'
        resp = _post(client, INGEST_BULK, body)
        assert (resp.status_code, _unhandled(logs)) == (400, [])
        # Naming the problem, never echoing the raw value.
        detail = resp.json()["detail"]
        assert "surrogate" in detail
        assert "\ud800" not in resp.text
        assert registry.operational.event_log.count() == 0

    def test_precedents_title_answers_400_and_promotes_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
    ) -> None:
        body = '{"trace_id": "tr-synthetic-1", "title": "\\ud800", "description": "d"}'
        resp = _post(client, PRECEDENTS, body)
        assert (resp.status_code, _unhandled(logs)) == (400, [])
        detail = resp.json()["detail"]
        assert "surrogate" in detail
        assert "\ud800" not in resp.text
        assert registry.operational.event_log.count() == 0
