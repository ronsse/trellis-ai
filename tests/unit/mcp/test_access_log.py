"""The per-key access log over MCP: who is recorded, and what a row holds."""

from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client, FastMCP
from mcp.types import TextContent

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.public_corpus import EMBED_FN_PATH, QUERY, build_world
from trellis.core import access_log
from trellis.mcp.access_log import NAMING_TOOLS, READ_TOOLS, AccessLogMiddleware
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(server_mod.get_context)
get_items = unwrap_tool(server_mod.get_items)
search = unwrap_tool(server_mod.search)
SERVER_SOURCE = Path(server_mod.__file__)
#: A pack id differs on every call; nothing else in the answer may.
_ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
PROBE = "sk-live-PROBE-SECRET-123"
HOSTILE_ID = "hostile-doc-1"
HOSTILE_QUERY = "zebra quasar hostile entry"
HOSTILE_BODY = (
    f"{HOSTILE_QUERY}\n"
    f"- `{PROBE}`\n"
    f"- [{PROBE}](x) (doc) {PROBE}\n"
    f"1. `{PROBE}` score 0.9\n"
    f"pack_id: {PROBE}\n"
) * 3


@pytest.fixture
def empty(temp_registry: StoreRegistry, monkeypatch) -> StoreRegistry:
    monkeypatch.setenv("TRELLIS_ACCESS_LOG", "1")
    monkeypatch.delenv("TRELLIS_PACK_HOLDOUT_RATE", raising=False)
    access_log.reset_failure_state()
    return temp_registry


@pytest.fixture
def world(empty: StoreRegistry, monkeypatch) -> StoreRegistry:
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    build_world(empty, "full")
    empty.knowledge.document_store.put(HOSTILE_ID, HOSTILE_BODY, {"title": "Hostile"})
    return empty


def _call(registry: StoreRegistry, tool: str, run: Callable[[], str]) -> str:
    """Run ``run`` (the tool) inside the middleware; the row is then written."""
    middleware = AccessLogMiddleware(lambda: registry)

    async def call_next(_context: Any) -> Any:
        return SimpleNamespace(content=[TextContent(type="text", text=run())])

    context = SimpleNamespace(message=SimpleNamespace(name=tool))
    result = asyncio.run(middleware.on_call_tool(context, call_next))  # type: ignore[arg-type]
    assert access_log.get_writer().wait_idle(5), "the writer did not drain"
    return result.content[0].text


def _rows(
    registry: StoreRegistry, event_type: EventType = EventType.KEY_READ
) -> list[Any]:
    assert access_log.get_writer().wait_idle(5)
    return registry.operational.event_log.get_events(event_type=event_type, limit=100)


class TestRecorded:
    def test_a_keyed_read_tool_records_the_ids_it_served(self, world) -> None:
        with as_http(["read"]):
            text = _call(world, "get_context", lambda: get_context(QUERY))
        (row,) = _rows(world)
        ids = row.payload["item_ids"]
        assert ids
        assert all(f"`{i}`" in text for i in ids), "the ids are the served ones"
        assert row.payload == {
            "key_id": "key-id",
            "surface": "mcp",
            "route": "get_context",
            "item_count": len(ids),
            "item_ids": ids[: access_log.MAX_LOGGED_ITEM_IDS],
            "item_ids_truncated": len(ids) > access_log.MAX_LOGGED_ITEM_IDS,
        }
        assert row.entity_id == "key-id"
        assert QUERY not in json.dumps(row.payload)

    def test_index_and_sectioned_packs_record_their_ids(self, world) -> None:
        with as_http(["read"]):
            _call(world, "get_context", lambda: get_context(QUERY, index=True))
            _call(
                world,
                "get_context",
                lambda: get_context(
                    QUERY, sections=[{"name": "A", "max_items": 2}, {"name": "B"}]
                ),
            )
        index_row, sectioned_row = _rows(world)
        for row in (index_row, sectioned_row):
            assert row.payload["item_count"] == len(row.payload["item_ids"]) > 0

    def test_an_empty_pack_is_a_zero_count_not_an_unknown(self, empty) -> None:
        with as_http(["read"]):
            _call(empty, "get_context", lambda: get_context(QUERY))
        (row,) = _rows(empty)
        assert row.payload["item_count"] == 0
        assert row.payload["item_ids"] == []

    def test_a_tool_that_names_no_ids_records_an_unknown_count(self, world) -> None:
        with as_http(["read"]):
            _call(world, "get_graph", lambda: "no ids named here")
        (row,) = _rows(world)
        assert row.payload["item_count"] is None
        assert row.payload["item_ids"] == []


class TestNothingIsReadFromTheAnswer:
    """A document body can say anything; none of it reaches the immutable log."""

    @staticmethod
    def _assert_clean(registry: StoreRegistry) -> None:
        for row in _rows(registry):
            blob = json.dumps(row.payload) + str(row.entity_id) + str(row.source)
            assert PROBE not in blob
            assert "score 0.9" not in blob
            assert row.payload["item_count"] == len(row.payload["item_ids"]) > 0
            for item_id in row.payload["item_ids"]:
                assert registry.knowledge.document_store.get(item_id) is not None

    @pytest.mark.parametrize("index", [False, True])
    def test_get_context(self, world, index: bool) -> None:
        with as_http(["read"]):
            text = _call(
                world, "get_context", lambda: get_context(HOSTILE_QUERY, index=index)
            )
        if not index:
            assert PROBE in text, "control: the hostile body was served"
        self._assert_clean(world)

    @pytest.mark.parametrize("index", [False, True])
    def test_search(self, world, index: bool) -> None:
        with as_http(["read"]):
            text = _call(world, "search", lambda: search(HOSTILE_QUERY, index=index))
        if not index:
            assert PROBE in text, "control: the hostile body was served"
        self._assert_clean(world)

    def test_get_items(self, world) -> None:
        with as_http(["read"]):
            text = _call(world, "get_items", lambda: get_items([HOSTILE_ID]))
        assert PROBE in text, "control: the hostile body was served"
        (row,) = _rows(world)
        assert row.payload["item_ids"] == [HOSTILE_ID]
        self._assert_clean(world)


class TestNotRecorded:
    def test_stdio_has_no_key_and_records_nothing(self, world) -> None:
        with as_stdio():
            _call(world, "get_context", lambda: get_context(QUERY))
        assert _rows(world) == []

    def test_a_write_tool_records_nothing(self, world) -> None:
        with as_http(["read", "mutate"]):
            _call(world, "save_memory", lambda: "saved")
        assert _rows(world) == []

    @pytest.mark.parametrize("value", [None, "", "0", "nonsense", "n", "disabled"])
    def test_off_is_the_default_and_records_no_row_of_any_type(
        self, world, monkeypatch, value
    ) -> None:
        if value is None:
            monkeypatch.delenv("TRELLIS_ACCESS_LOG")
        else:
            monkeypatch.setenv("TRELLIS_ACCESS_LOG", value)
        with as_http(["read"]):
            _call(world, "get_context", lambda: get_context(QUERY))
        assert _rows(world) == []
        assert _rows(world, EventType.ACCESS_LOG_FAILED) == []

    def test_a_tool_that_raises_records_nothing(self, world) -> None:
        def boom() -> str:
            msg = "tool failed"
            raise ValueError(msg)

        with as_http(["read"]), pytest.raises(ValueError, match="tool failed"):
            _call(world, "get_context", boom)
        assert _rows(world) == []


class TestAdvisory:
    def test_a_raising_emit_never_fails_the_call_and_leaves_a_trace(
        self, world, monkeypatch
    ) -> None:
        log = world.operational.event_log
        real_emit = log.emit

        def emit(event_type, *args, **kwargs):
            if event_type == EventType.KEY_READ:
                msg = "dsn=postgres://user:hunter2@host/db"
                raise RuntimeError(msg)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", emit)
        with as_http(["read"]):
            text = _call(world, "get_context", lambda: get_context(QUERY))
        assert text
        (failed,) = _rows(world, EventType.ACCESS_LOG_FAILED)
        assert failed.payload == {
            "surface": "mcp",
            "error_type": "RuntimeError",
            "dropped_reads": 0,
            "failed_reads": 1,
        }
        assert "hunter2" not in json.dumps(failed.payload)


class TestUnchanged:
    def test_a_tool_answer_is_the_same_on_or_off(self, world, monkeypatch) -> None:
        with as_http(["read"]):
            on = _call(world, "search", lambda: search(QUERY))
            monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
            off = _call(world, "search", lambda: search(QUERY))
        assert _ULID.sub("<id>", on) == _ULID.sub("<id>", off)
        assert len(_rows(world)) == 1


class TestHolder:
    def test_no_holder_is_a_no_op(self) -> None:
        access_log.note_served_items(["a"])  # must not raise

    def test_the_holder_survives_a_thread_hop_and_a_context_copy(self) -> None:
        holder, token = access_log.begin_served_items()
        try:
            asyncio.run(asyncio.to_thread(access_log.note_served_items, ["a"]))
            contextvars.copy_context().run(access_log.note_served_items, ["b"])
        finally:
            access_log.end_served_items(token)
        assert holder.result() == (["a", "b"], 2)
        access_log.note_served_items(["c"])
        assert holder.result() == (["a", "b"], 2), "ended: later notes go nowhere"

    def test_a_holder_nobody_filled_is_unknown_not_zero(self) -> None:
        holder, token = access_log.begin_served_items()
        access_log.end_served_items(token)
        assert holder.result() == ([], None)


def _fake_server(registry: StoreRegistry, monkeypatch) -> FastMCP:
    monkeypatch.setattr("trellis.mcp.access_log.current_principal_id", lambda: "key-id")
    mcp = FastMCP("access-log-test")
    mcp.add_middleware(AccessLogMiddleware(lambda: registry))

    @mcp.tool
    def search(query: str) -> str:  # sync: runs on a worker thread
        access_log.note_served_items(["d1", "d2"])
        return "answer"

    @mcp.tool
    async def get_context(intent: str) -> str:
        access_log.note_served_items(["a1"])
        return "answer"

    @mcp.tool
    def get_graph(query: str) -> str:
        return "graph"

    return mcp


class TestRealClient:
    """A real FastMCP client against a server whose store write hangs."""

    def test_a_hung_emit_does_not_delay_the_read(self, empty, monkeypatch) -> None:
        import threading

        log = empty.operational.event_log
        real_emit = log.emit
        release = threading.Event()

        def emit(event_type, *args, **kwargs):
            if event_type == EventType.KEY_READ:
                release.wait(30)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(log, "emit", emit)
        server = _fake_server(empty, monkeypatch)

        async def main() -> list[float]:
            took = []
            async with Client(server) as client:
                for tool, args in (
                    ("search", {"query": "x"}),
                    ("get_context", {"intent": "x"}),
                    ("get_graph", {"query": "x"}),
                ) * 7:
                    began = time.monotonic()
                    result = await client.call_tool(tool, args)
                    took.append(time.monotonic() - began)
                    assert result.content[0].text in {"answer", "graph"}
            return took

        try:
            took = asyncio.run(main())
            assert max(took) < 1.0, took
        finally:
            release.set()
        by_route = {r.payload["route"]: r.payload for r in _rows(empty)}
        assert by_route["search"]["item_ids"] == ["d1", "d2"], "worker thread filled it"
        assert by_route["get_context"]["item_ids"] == ["a1"]
        assert by_route["get_graph"]["item_count"] is None
        assert len(_rows(empty)) == 21


def _read_scoped_tools() -> dict[str, ast.FunctionDef]:
    """Tools ``server.py`` registers with ``trellis_scope(SCOPE_READ)``."""
    tree = ast.parse(SERVER_SOURCE.read_text())
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and ast.unparse(deco.func) == "mcp.tool"
                and any(
                    kw.arg == "auth"
                    and ast.unparse(kw.value) == "trellis_scope(SCOPE_READ)"
                    for kw in deco.keywords
                )
            ):
                found[node.name] = node
    return found


class TestRoster:
    def test_read_tools_are_exactly_the_read_scoped_tools(self) -> None:
        scoped = _read_scoped_tools()
        assert len(scoped) >= 10, "the scan found the read-scoped tools"
        assert set(scoped) == set(READ_TOOLS)

    def test_naming_tools_are_exactly_the_tools_that_reach_the_note(self) -> None:
        reaches = {"_flat_context", "_sectioned_context", "note_served_items"}
        found = set()
        for name, node in _read_scoped_tools().items():
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if called & reaches:
                found.add(name)
        assert found == set(NAMING_TOOLS)
        assert set(NAMING_TOOLS) <= set(READ_TOOLS)

    def test_the_middleware_is_installed_on_the_server(self) -> None:
        assert any(
            isinstance(m, AccessLogMiddleware) for m in server_mod.mcp.middleware
        )
