"""The access-log middleware as an ASGI component, and the writer behind it.

The recording never rides the request: a hung store write must not delay a read,
starve the server's thread pool or hold up shutdown. These tests drive a real
uvicorn server, not only the in-process client.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.testclient import TestClient

import trellis_api.access_log as rest_log
from trellis.core import access_log
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.access_log import ITEM_ROUTES, AccessLogMiddleware

ANSWER = {"results": [{"doc_id": "d1"}, {"doc_id": "d2"}]}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("TRELLIS_ACCESS_LOG", "1")
    access_log.reset_failure_state()
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    monkeypatch.setattr(rest_log, "_registry", lambda: reg)
    try:
        yield reg
    finally:
        access_log.get_writer().wait_idle(5)
        reg.close()


def rows(reg: StoreRegistry, event_type: EventType = EventType.KEY_READ) -> list[Any]:
    assert access_log.get_writer().wait_idle(5), "the writer did not drain"
    return reg.operational.event_log.get_events(event_type=event_type, limit=1000)


class Stamp:
    """Stands in for ``require_scope``: the key id comes from ``x-key``."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            key = dict(scope["headers"]).get(b"x-key")
            scope.setdefault("state", {})["access_key_id"] = (
                key.decode() if key else None
            )
        await self.app(scope, receive, send)


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.answer = ANSWER
    router = APIRouter()

    @router.get("/search")
    def search() -> JSONResponse:
        return JSONResponse(app.state.answer)

    @router.get("/events")
    def events() -> JSONResponse:
        return JSONResponse(
            {"events": [{"entity_id": "ent-1"}], "results": [{"doc_id": "x"}]}
        )

    @router.get("/documents/{doc_id:path}")
    def document(doc_id: str) -> JSONResponse:
        return JSONResponse({"document": {"doc_id": doc_id}})

    @router.get("/plain")
    def plain() -> PlainTextResponse:
        return PlainTextResponse("not json")

    @router.get("/empty")
    def empty() -> Response:
        return Response(status_code=200)

    @router.get("/forbidden")
    def forbidden() -> None:
        raise HTTPException(status_code=403)

    @router.get("/missing")
    def missing() -> None:
        raise HTTPException(status_code=404)

    @router.get("/typed")
    def typed(n: int) -> JSONResponse:
        return JSONResponse(ANSWER)

    app.include_router(router, prefix="/api/v1")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(Stamp)
    return app


KEY = {"x-key": "key-1"}


class TestWhatIsRecorded:
    def test_a_populated_list(self, registry) -> None:
        resp = TestClient(build_app()).get("/api/v1/search", headers=KEY)
        assert resp.status_code == 200
        (row,) = rows(registry)
        assert row.payload == {
            "key_id": "key-1",
            "surface": "rest",
            "route": "GET /api/v1/search",
            "item_count": 2,
            "item_ids": ["d1", "d2"],
            "item_ids_truncated": False,
        }

    def test_an_empty_list_is_a_real_zero(self, registry) -> None:
        app = build_app()
        app.state.answer = {"results": []}
        TestClient(app).get("/api/v1/search", headers=KEY)
        (row,) = rows(registry)
        assert row.payload["item_count"] == 0
        assert row.payload["item_ids"] == []

    def test_a_route_off_the_allowlist_records_null_and_no_ids(self, registry) -> None:
        TestClient(build_app()).get("/api/v1/events", headers=KEY)
        (row,) = rows(registry)
        assert row.payload["route"] == "GET /api/v1/events"
        assert row.payload["item_count"] is None
        assert row.payload["item_ids"] == []
        assert "ent-1" not in str(row.payload)

    def test_the_route_is_the_template_and_the_id_is_the_answers(
        self, registry
    ) -> None:
        TestClient(build_app()).get("/api/v1/documents/a/b/c", headers=KEY)
        (row,) = rows(registry)
        assert row.payload["route"] == "GET /api/v1/documents/{doc_id:path}"
        assert row.payload["item_ids"] == ["a/b/c"]

    @pytest.mark.parametrize("path", ["plain", "empty"])
    def test_a_non_json_or_empty_200_records_an_unknown_count(
        self, registry, monkeypatch, path: str
    ) -> None:
        monkeypatch.setitem(
            ITEM_ROUTES, f"GET /api/v1/{path}", (("results", "*"), "doc_id")
        )
        resp = TestClient(build_app()).get(f"/api/v1/{path}", headers=KEY)
        assert resp.status_code == 200
        (row,) = rows(registry)
        assert row.payload["item_count"] is None
        assert row.payload["item_ids"] == []

    @pytest.mark.parametrize(
        ("path", "status"),
        [("forbidden", 403), ("missing", 404), ("typed?n=x", 422)],
    )
    def test_a_refused_or_failed_read_records_nothing(
        self, registry, path: str, status: int
    ) -> None:
        resp = TestClient(build_app()).get(f"/api/v1/{path}", headers=KEY)
        assert resp.status_code == status
        assert rows(registry) == []

    def test_no_key_records_nothing(self, registry) -> None:
        TestClient(build_app()).get("/api/v1/search")
        assert rows(registry) == []


def _scope() -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/search",
        "state": {"access_key_id": "k1"},
        "route": SimpleNamespace(path="/search"),
    }


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request"}


def _drive(body: bytes | list[bytes], *, status: int = 200) -> list[str]:
    """Run the middleware; the order of what was sent and what was queued."""
    order: list[str] = []
    chunks = [body] if isinstance(body, bytes) else body

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        for n, chunk in enumerate(chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": n < len(chunks) - 1,
                }
            )

    async def send(message: Any) -> None:
        order.append(message["type"])

    asyncio.run(AccessLogMiddleware(app)(_scope(), _noop_receive, send))
    return order


class TestOrderingAndOff:
    def test_the_row_is_queued_only_after_the_last_chunk_was_sent(
        self, registry, monkeypatch
    ) -> None:
        queued: list[dict[str, Any]] = []
        order_probe: list[str] = []

        def enqueue(*args: Any, **kwargs: Any) -> bool:
            order_probe.append("enqueue")
            queued.append(kwargs)
            return True

        monkeypatch.setattr(rest_log, "enqueue_key_read", enqueue)
        sent = _drive([b'{"results": [{"doc_id"', b': "d1"}]}'])
        assert sent == [
            "http.response.start",
            "http.response.body",
            "http.response.body",
        ]
        assert order_probe == ["enqueue"], "queued once, after the app returned"
        (kwargs,) = queued
        assert kwargs["items"]() == (["d1"], 1), "the whole body, joined"

    def test_enqueue_happens_after_every_send(self, registry, monkeypatch) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            rest_log, "enqueue_key_read", lambda *a, **k: order.append("enqueue")
        )

        async def app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def send(message: Any) -> None:
            order.append(message["type"])

        asyncio.run(AccessLogMiddleware(app)(_scope(), _noop_receive, send))
        assert order == [
            "http.response.start",
            "http.response.body",
            "enqueue",
        ]

    @pytest.mark.parametrize("status", [403, 404, 422, 500])
    def test_a_non_200_is_never_queued(self, registry, monkeypatch, status) -> None:
        queued: list[Any] = []
        monkeypatch.setattr(
            rest_log, "enqueue_key_read", lambda *a, **k: queued.append(1)
        )
        _drive(b"{}", status=status)
        assert queued == []

    @pytest.mark.parametrize(
        "value", [None, "", "0", "nonsense", "n", "f", "disabled", "none"]
    )
    def test_off_is_a_pass_through_that_never_touches_the_body(
        self, registry, monkeypatch, value
    ) -> None:
        class Spy(bytes):
            touched = 0

            def __len__(self) -> int:
                Spy.touched += 1
                return super().__len__()

        if value is None:
            monkeypatch.delenv("TRELLIS_ACCESS_LOG")
        else:
            monkeypatch.setenv("TRELLIS_ACCESS_LOG", value)
        queued: list[Any] = []
        monkeypatch.setattr(
            rest_log, "enqueue_key_read", lambda *a, **k: queued.append(1)
        )
        _drive(Spy(b'{"results": []}'))
        assert queued == []
        assert Spy.touched == 0

    def test_on_does_read_the_body(self, registry, monkeypatch) -> None:
        class Spy(bytes):
            touched = 0

            def __len__(self) -> int:
                Spy.touched += 1
                return super().__len__()

        monkeypatch.setattr(rest_log, "enqueue_key_read", lambda *a, **k: True)
        _drive(Spy(b'{"results": []}'))
        assert Spy.touched > 0, "control: the spy sees a buffered body"


class Blocking:
    """An event log whose ``KEY_READ`` write blocks until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.emitted: list[Any] = []

    def emit(self, event_type: Any, *args: Any, **kwargs: Any) -> None:
        self.started.set()
        self.release.wait(30)
        self.emitted.append(event_type)


def _stub_registry(log: Any) -> Any:
    return SimpleNamespace(operational=SimpleNamespace(event_log=log))


def _entry(registry: Any, size: int = 100, ids: tuple[str, ...] = ("d",)) -> Any:
    return access_log.KeyReadEntry(
        registry=registry,
        key_id="k",
        surface="rest",
        route="GET /x",
        items=lambda: (list(ids), len(ids)),
        size=size,
    )


@pytest.fixture
def blocked():
    access_log.reset_failure_state()
    log = Blocking()
    yield log
    log.release.set()


class TestWriter:
    def test_a_blocked_emit_does_not_block_the_caller(self, blocked) -> None:
        writer = access_log.AccessLogWriter()
        reg = _stub_registry(blocked)
        began = time.monotonic()
        assert all(writer.submit(_entry(reg)) for _ in range(20))
        assert time.monotonic() - began < 0.5
        assert blocked.started.wait(2)
        blocked.release.set()
        assert writer.wait_idle(5)
        assert len(blocked.emitted) == 20

    def test_a_full_queue_drops_and_counts(self, blocked) -> None:
        writer = access_log.AccessLogWriter(max_entries=2)
        reg = _stub_registry(blocked)
        assert writer.submit(_entry(reg))
        assert blocked.started.wait(2), "the writer holds the first entry"
        assert writer.submit(_entry(reg))
        assert writer.submit(_entry(reg))
        assert not writer.submit(_entry(reg)), "no room: dropped, not blocked"
        assert not writer.submit(_entry(reg))
        assert access_log.pending_unrecorded() == (2, 0)
        blocked.release.set()
        assert writer.wait_idle(5)

    def test_the_byte_bound_drops_and_counts(self, blocked) -> None:
        writer = access_log.AccessLogWriter(max_bytes=1000)
        reg = _stub_registry(blocked)
        assert writer.submit(_entry(reg, size=600))
        assert blocked.started.wait(2)
        assert writer.submit(_entry(reg, size=600))
        assert not writer.submit(_entry(reg, size=600)), "1200 bytes is over 1000"
        assert access_log.pending_unrecorded() == (1, 0)
        blocked.release.set()
        assert writer.wait_idle(5)

    def test_dropped_reads_surface_when_the_store_can_next_write(
        self, registry
    ) -> None:
        access_log.count_unrecorded(dropped=3)
        access_log.enqueue_key_read(
            registry,
            key_id="k1",
            surface="rest",
            route="GET /x",
            items=lambda: (["d"], 1),
        )
        (signal,) = rows(registry, EventType.ACCESS_LOG_FAILED)
        assert signal.payload["dropped_reads"] == 3
        assert signal.payload["failed_reads"] == 0
        assert access_log.pending_unrecorded() == (0, 0)

    def test_a_stuck_writer_does_not_delay_shutdown_past_its_bound(
        self, blocked
    ) -> None:
        writer = access_log.AccessLogWriter()
        reg = _stub_registry(blocked)
        assert writer.submit(_entry(reg))
        assert blocked.started.wait(2), "the writer is stuck inside the emit"
        began = time.monotonic()
        writer.shutdown(timeout=0.3)
        assert time.monotonic() - began < 1.5
        assert not writer.submit(_entry(reg)), "stopping: new reads are dropped"

    def test_no_registry_or_no_key_records_nothing(self, registry) -> None:
        assert not access_log.enqueue_key_read(
            None, key_id="k", surface="rest", route="r", items=lambda: ([], None)
        )
        assert not access_log.enqueue_key_read(
            registry, key_id=None, surface="rest", route="r", items=lambda: ([], None)
        )
        assert rows(registry) == []


class TestFailureSignal:
    def test_a_signal_with_no_registry_consumes_no_rate_limit(self, registry) -> None:
        access_log.signal_failure(None, "rest", RuntimeError("x"))
        access_log.signal_failure(None, "rest", RuntimeError("x"))
        assert access_log.pending_unrecorded() == (0, 2)
        assert access_log._state.last_signal is None
        access_log.signal_failure(registry, "rest", RuntimeError("x"))
        (signal,) = rows(registry, EventType.ACCESS_LOG_FAILED)
        assert signal.payload["failed_reads"] == 3


class TestRealServer:
    """A real uvicorn server with a hung store write behind it."""

    def test_reads_and_healthz_stay_prompt_while_the_emit_hangs(
        self, registry, monkeypatch
    ) -> None:
        log = registry.operational.event_log
        real_emit = log.emit
        release = threading.Event()
        started = threading.Event()

        def emit(event_type: Any, *args: Any, **kwargs: Any) -> Any:
            if event_type == EventType.KEY_READ:
                started.set()
                release.wait(60)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", emit)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="off",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.05)
            assert server.started, "uvicorn did not start"
            base = f"http://127.0.0.1:{port}"

            def hit(n: int) -> tuple[str, int, float]:
                path = "/healthz" if n % 10 == 0 else "/api/v1/search"
                began = time.monotonic()
                resp = httpx.get(base + path, headers=KEY, timeout=10)
                return path, resp.status_code, time.monotonic() - began

            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(hit, range(80)))
            assert started.wait(5), "control: the emit really is hung"
            assert all(status == 200 for _, status, _ in results)
            assert sum(p == "/api/v1/search" for p, _, _ in results) >= 50
            assert max(took for *_, took in results) < 3.0
            assert any(p == "/healthz" for p, _, _ in results)
        finally:
            release.set()
            server.should_exit = True
            thread.join(10)
        assert len(rows(registry)) == 72
