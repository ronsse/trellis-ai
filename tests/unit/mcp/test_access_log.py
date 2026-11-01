"""The per-key access log over MCP: who is recorded, and what a row reads back."""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from mcp.types import TextContent

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.public_corpus import EMBED_FN_PATH, QUERY, build_world
from trellis.core import access_log
from trellis.mcp.access_log import (
    COUNTED_TOOLS,
    READ_TOOLS,
    AccessLogMiddleware,
    returned_items,
)
from trellis.retrieve.formatters import format_advisories_as_markdown
from trellis.schemas.advisory import Advisory, AdvisoryCategory
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(server_mod.get_context)
search = unwrap_tool(server_mod.search)
SERVER_SOURCE = Path(server_mod.__file__)
#: A pack id differs on every call; nothing else in the answer may.
_ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


@pytest.fixture
def world(temp_registry: StoreRegistry, monkeypatch) -> StoreRegistry:
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.delenv("TRELLIS_PACK_HOLDOUT_RATE", raising=False)
    monkeypatch.setattr(access_log, "_last_failure_signal", [])
    build_world(temp_registry, "full")
    return temp_registry


def _call(registry: StoreRegistry, tool: str, text: str) -> Any:
    """Run the middleware around a tool that answered ``text``."""
    middleware = AccessLogMiddleware(lambda: registry)
    result = SimpleNamespace(content=[TextContent(type="text", text=text)])

    async def call_next(_context: Any) -> Any:
        return result

    context = SimpleNamespace(message=SimpleNamespace(name=tool))
    returned = asyncio.run(middleware.on_call_tool(context, call_next))  # type: ignore[arg-type]
    assert returned is result
    return returned


def _rows(
    registry: StoreRegistry, event_type: EventType = EventType.KEY_READ
) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=event_type, limit=100)


class TestRecorded:
    def test_a_keyed_read_tool_records_the_ids_it_served(self, world) -> None:
        with as_http(["read"]):
            text = get_context(QUERY)
            _call(world, "get_context", text)
        (row,) = _rows(world)
        served = returned_items("get_context", text)[0]
        assert served
        assert row.payload == {
            "key_id": "key-id",
            "surface": "mcp",
            "route": "get_context",
            "item_count": len(served),
            "item_ids": served[: access_log.MAX_LOGGED_ITEM_IDS],
            "item_ids_truncated": len(served) > access_log.MAX_LOGGED_ITEM_IDS,
        }
        assert row.entity_id == "key-id"
        assert QUERY not in json.dumps(row.payload)

    def test_an_empty_pack_is_a_zero_count_not_an_unknown(self, world) -> None:
        with as_http(["read"]):
            _call(world, "get_context", "No relevant context found.")
        (row,) = _rows(world)
        assert row.payload["item_count"] == 0
        assert row.payload["item_ids"] == []

    def test_a_tool_without_item_lines_records_an_unknown_count(self, world) -> None:
        with as_http(["read"]):
            _call(world, "get_graph", "no items here")
        (row,) = _rows(world)
        assert row.payload["item_count"] is None


class TestNotRecorded:
    def test_stdio_has_no_key_and_records_nothing(self, world) -> None:
        with as_stdio():
            _call(world, "get_context", get_context(QUERY))
        assert _rows(world) == []

    def test_a_write_tool_records_nothing(self, world) -> None:
        with as_http(["read", "mutate"]):
            _call(world, "save_memory", "saved")
        assert _rows(world) == []

    def test_off_means_no_row_of_any_type(self, world, monkeypatch) -> None:
        monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
        with as_http(["read"]):
            _call(world, "get_context", get_context(QUERY))
        assert _rows(world) == []
        assert _rows(world, EventType.ACCESS_LOG_FAILED) == []


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
            result = _call(world, "get_context", get_context(QUERY))
        assert result.content[0].text
        (failed,) = _rows(world, EventType.ACCESS_LOG_FAILED)
        assert failed.payload == {"surface": "mcp", "error_type": "RuntimeError"}
        assert "hunter2" not in json.dumps(failed.payload)


class TestUnchanged:
    def test_a_tool_answer_is_the_same_on_or_off(self, world, monkeypatch) -> None:
        with as_http(["read"]):
            on = search(QUERY)
            _call(world, "search", on)
            monkeypatch.setenv("TRELLIS_ACCESS_LOG", "0")
            off = search(QUERY)
            _call(world, "search", off)
        assert _ULID.sub("<id>", on) == _ULID.sub("<id>", off)
        assert len(_rows(world)) == 1


class TestReadingMarkdownBack:
    def test_a_flat_pack_reads_back_every_item(self, world) -> None:
        with as_http(["read"]):
            text = get_context(QUERY)
        ids, count = returned_items("get_context", text)
        assert count == len(ids) > 0
        assert all(f"`{i}`" in text for i in ids)

    def test_an_index_pack_reads_back_every_line(self, world) -> None:
        with as_http(["read"]):
            text = get_context(QUERY, index=True)
        ids, count = returned_items("get_context", text)
        assert count == len(ids) > 0

    def test_a_sectioned_pack_reads_back_every_line(self, world) -> None:
        with as_http(["read"]):
            text = get_context(
                QUERY, sections=[{"name": "A", "max_items": 2}, {"name": "B"}]
            )
        ids, count = returned_items("get_context", text)
        assert count == len(ids) > 0

    def test_an_advisory_is_not_an_item(self, world) -> None:
        advisory = Advisory.model_construct(
            advisory_id="ADVISORYID0000000000000000",
            category=AdvisoryCategory.QUERY,
            message="Include a word.",
        )
        block = format_advisories_as_markdown([advisory])
        assert "ADVISORYID" in block
        assert returned_items("get_context", block) == ([], 0)
        with as_http(["read"]):
            text = get_context(QUERY)
        assert returned_items("get_context", text + "\n" + block) == returned_items(
            "get_context", text
        )


def _read_scoped_tools() -> set[str]:
    """Tools ``server.py`` registers with ``trellis_scope(SCOPE_READ)``."""
    tree = ast.parse(SERVER_SOURCE.read_text())
    found: set[str] = set()
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
                found.add(node.name)
    return found


class TestRoster:
    def test_read_tools_are_exactly_the_read_scoped_tools(self) -> None:
        scoped = _read_scoped_tools()
        assert len(scoped) >= 10, "the scan found the read-scoped tools"
        assert scoped == set(READ_TOOLS)

    def test_counted_tools_are_read_tools(self) -> None:
        assert set(COUNTED_TOOLS) <= set(READ_TOOLS)

    def test_the_middleware_is_installed_on_the_server(self) -> None:
        assert any(
            isinstance(m, AccessLogMiddleware) for m in server_mod.mcp.middleware
        )
