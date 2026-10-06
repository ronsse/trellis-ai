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

:func:`~trellis_api.middleware.request_validation_error_handler` keeps
FastAPI's 422 and renders a non-finite float anywhere in the error payload
as the token the caller sent. Every finite case stays byte-identical to
FastAPI's default handler, which :class:`TestFiniteErrorsMatchFastApisDefault`
pins.

The client is built by :func:`~trellis_api.app.create_app`, so "the handler
exists" and "the handler is wired" are the same test.

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

#: The bare tokens Python's ``json`` module accepts, sent exactly as written.
NON_FINITE = ["NaN", "Infinity", "-Infinity"]

_JSON = {"content-type": "application/json"}


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
            pytest.param(PACK_FEEDBACK, '{"rating": 1.1}', id="out-of-range-finite"),
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


class TestMeasurementValue:
    """``metric_value`` refuses NaN, and only NaN.

    Whether ``Infinity`` is a legitimate measurement is an open owner
    question; it is accepted, and pinned here.
    """

    def test_nan_answers_422_and_stores_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        logs: list[dict[str, Any]],
    ) -> None:
        resp = _post(client, MEASUREMENTS, _measurement_body("NaN"))
        assert (resp.status_code, _unhandled(logs)) == (422, [])
        assert "metric_value" in resp.json()["detail"]
        assert registry.knowledge.graph_store.query(node_type=MEASUREMENT) == []
        assert registry.operational.event_log.count() == 0

    @pytest.mark.parametrize("token", ["Infinity", "-Infinity"])
    def test_infinity_is_recorded(
        self, client: TestClient, registry: StoreRegistry, token: str
    ) -> None:
        resp = _post(client, MEASUREMENTS, _measurement_body(token))
        assert resp.status_code == 201, resp.text
        [node] = registry.knowledge.graph_store.query(node_type=MEASUREMENT)
        assert node["properties"]["metric_value"] == float(token)
        assert registry.operational.event_log.count() > 0
