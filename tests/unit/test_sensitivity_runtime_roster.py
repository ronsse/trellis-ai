"""Every route the app serves and every tool the server registers is rostered.

``test_sensitivity_rest_roster_rule.py`` and ``test_sensitivity_surface_rule.py``
read the source for ``@router.<verb>`` and ``@mcp.tool`` decorators. A route
added as ``router.add_api_route(...)``, as ``@app.get`` inside ``create_app``, or
a tool added as ``mcp.tool(fn, ...)`` carries no such decorator, so those rules
never see it, and it is reachable by an ``external`` key with no clearance
check. This rule asks the built objects instead: what the real app routes and
what the server lists, whatever the spelling that registered it.

It shows a served route or registered tool has a roster line, and that every
roster line is served. It does not show the claim on the line holds; that stays
with the source rules, which read the decorated handlers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP

import trellis_api.app as app_module
from tests.ast_rules import assert_hand_read_floor
from tests.unit.test_sensitivity_rest_roster_rule import ROSTER as REST_ROSTER
from tests.unit.test_sensitivity_surface_rule import ROSTER as MCP_ROSTER
from trellis.mcp.auth import set_auth_enforced
from trellis.mcp.server import mcp
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Served by the app itself and not by a route module: the redirect off ``/``
#: (its target is a constant) and the static UI mount (files, no store read).
#: ``/openapi.json``, ``/docs`` and ``/redoc`` are FastAPI's own endpoints.
APP_OWN_ROUTES: frozenset[str] = frozenset({"app:root_redirect"})
UI_MOUNT = "/ui"
FRAMEWORK_MODULES = ("fastapi.", "starlette.")


def _leaf_routes(routes: Any) -> Iterator[Any]:
    """Every route that runs a handler, through lazily included routers."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _leaf_routes(included.routes)
        else:
            yield route


def served_handlers(app: FastAPI) -> tuple[dict[str, int], list[str]]:
    """``module:function`` -> how many paths serve it, and what is not a handler."""
    handlers: dict[str, int] = {}
    unexplained: list[str] = []
    for route in _leaf_routes(app.routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            mounted = getattr(route, "app", None)
            if route.path == UI_MOUNT and isinstance(mounted, StaticFiles):
                continue
            unexplained.append(f"{type(route).__name__} {route.path}")
            continue
        module = endpoint.__module__
        if module.startswith(FRAMEWORK_MODULES):
            continue
        key = f"{module.rsplit('.', 1)[-1]}:{endpoint.__name__}"
        handlers[key] = handlers.get(key, 0) + 1
    return handlers, unexplained


def rest_violations(
    handlers: dict[str, int], unexplained: list[str], roster: dict[str, str]
) -> list[str]:
    allowed = set(roster) | APP_OWN_ROUTES
    found = [
        f"{route}: served, not a handler a person rostered" for route in unexplained
    ]
    found.extend(
        f"{key}: served at runtime, not in the roster"
        for key in sorted(set(handlers) - allowed)
    )
    found.extend(
        f"{key}: rostered but the app serves no such handler"
        for key in sorted(set(roster) - set(handlers))
    )
    return found


def registered_tools(server: FastMCP[Any]) -> set[str]:
    """Names the server lists, with enforcement dropped so the listing is whole."""
    # ``list_tools`` is scope-filtered, so an anonymous listing is empty.
    set_auth_enforced(enforced=False)
    try:
        return {tool.name for tool in asyncio.run(server.list_tools())}
    finally:
        set_auth_enforced(enforced=True)


def mcp_violations(registered: set[str], roster: dict[str, str]) -> list[str]:
    found = [
        f"{name}: registered at runtime, not in the roster"
        for name in sorted(registered - set(roster))
    ]
    found.extend(
        f"{name}: rostered but the server registers no such tool"
        for name in sorted(set(roster) - registered)
    )
    return found


@pytest.fixture
def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    try:
        yield create_app()
    finally:
        registry.close()
        app_module._registry = None


def test_every_route_the_app_serves_is_rostered(real_app: FastAPI) -> None:
    handlers, unexplained = served_handlers(real_app)
    # Hand read: 76 rostered handler functions, one of which serves two paths.
    assert_hand_read_floor(
        len(handlers),
        76,
        subject="handlers the running app serves",
        hint="a router was not walked, or the route object's shape changed",
    )
    assert_hand_read_floor(
        sum(handlers.values()),
        77,
        subject="paths the running app serves through those handlers",
        hint="a router was not walked, or the route object's shape changed",
    )
    found = rest_violations(handlers, unexplained, REST_ROSTER)
    assert found == [], "\n".join(found)


def test_every_tool_the_server_registers_is_rostered() -> None:
    registered = registered_tools(mcp)
    assert_hand_read_floor(
        len(registered),
        16,
        subject="tools the running server lists",
        hint="the listing was filtered, or a provider was not read",
    )
    found = mcp_violations(registered, MCP_ROSTER)
    assert found == [], "\n".join(found)


_HERE = __name__.rsplit(".", 1)[-1]


class TestTheWalkCatchesADefectiveSubject:
    """The three spellings the source rules cannot see, built and then walked."""

    ROSTER: ClassVar[dict[str, str]] = {f"{_HERE}:listed": "gated"}

    def _app(self) -> tuple[FastAPI, APIRouter]:
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        return app, router

    def _check(self, app: FastAPI) -> list[str]:
        return rest_violations(*served_handlers(app), self.ROSTER)

    def test_the_clean_subject_passes(self) -> None:
        app, _ = self._app()
        assert self._check(app) == []

    def test_a_route_added_with_add_api_route_fails(self) -> None:
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        def leak_docs() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        router.add_api_route("/leak-add", leak_docs, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        assert self._check(app) == [
            f"{_HERE}:leak_docs: served at runtime, not in the roster"
        ]

    def test_a_route_declared_on_the_app_itself_fails(self) -> None:
        app, _ = self._app()

        @app.get("/api/v1/leak-app")
        def leak_app() -> dict[str, str]:
            return {}

        assert self._check(app) == [
            f"{_HERE}:leak_app: served at runtime, not in the roster"
        ]

    def test_a_mount_that_is_not_the_ui_fails(self) -> None:
        app, _ = self._app()
        app.mount("/files", FastAPI())
        assert any("/files" in v for v in self._check(app))

    def test_a_roster_line_for_a_handler_no_longer_served_fails(self) -> None:
        assert self._check(FastAPI()) == [
            f"{_HERE}:listed: rostered but the app serves no such handler"
        ]

    def test_a_tool_registered_by_the_call_form_fails(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.tool
        def reads() -> str:
            return ""

        def leak_tool() -> str:
            return ""

        server.tool(leak_tool)
        found = mcp_violations(registered_tools(server), {"reads": "gated"})
        assert found == ["leak_tool: registered at runtime, not in the roster"]

    def test_a_tool_that_is_only_decorated_passes(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.tool
        def reads() -> str:
            return ""

        assert mcp_violations(registered_tools(server), {"reads": "gated"}) == []
