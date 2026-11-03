"""Every route the app serves and every tool the server registers is rostered.

``test_sensitivity_rest_roster_rule.py`` and ``test_sensitivity_surface_rule.py``
read the source for ``@router.<verb>`` and ``@mcp.tool`` decorators. A route
added as ``router.add_api_route(...)``, as ``@app.get`` inside ``create_app``, or
a tool added as ``mcp.tool(fn, ...)`` carries no such decorator, so those rules
never see it, and it is reachable by an ``external`` key with no clearance
check. This rule asks the built objects instead: what the real app routes and
what the server lists, whatever the spelling that registered it.

It shows a served route, registered tool, resource or prompt has a roster line,
and that every roster line is served. A handler is keyed on its full module path
(``trellis_api.routes.explore:list_documents``), not on the file's stem, so a
second module's handler of the same name is not the rostered one. It is rostered
for a number of paths (one unless ``MULTI_PATH`` says more), so the same handler
mounted on a second path fails: a path is where the gate has to hold. Nothing is
excused by a module prefix; the framework's own handlers are named one by one.
It does not show the claim on the line holds; that stays with the source rules,
which read the decorated handlers.
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

ROUTES_PACKAGE = "trellis_api.routes"

#: Served by the app itself and not by a route module: the redirect off ``/``
#: (its target is a constant) and the static UI mount (files, no store read).
APP_OWN_ROUTES: frozenset[str] = frozenset({"trellis_api.app:root_redirect"})
UI_MOUNT = "/ui"

#: FastAPI's own documentation endpoints, named one by one (the closures it
#: registers in ``FastAPI.setup``). A module prefix would excuse any handler
#: that happens to live in a module called ``fastapi.<anything>``.
FRAMEWORK_HANDLERS: frozenset[str] = frozenset(
    {
        "fastapi.applications:openapi",
        "fastapi.applications:swagger_ui_html",
        "fastapi.applications:swagger_ui_redirect",
        "fastapi.applications:redoc_html",
    }
)

#: Served only where an optional extra is installed, named one by one, so these
#: may be absent and "rostered but not served" does not apply. ``/metrics``, from
#: the ``observability`` extra, holds no store content and is gated by
#: ``_require_metrics_access``, pinned in ``tests/unit/api/test_metrics_gate.py``.
OPTIONAL_EXTRA_HANDLERS: frozenset[str] = frozenset(
    {"prometheus_fastapi_instrumentator.instrumentation:metrics"}
)

#: Handlers rostered for more than one route (a method on a path), with how
#: many. Hand read: one, ``PUT`` and ``POST`` on ``/settings/{name}``. Anything
#: else serving a second path, or a second method, is a new surface and fails
#: until a person reads it.
MULTI_PATH: dict[str, int] = {"trellis_api.routes.settings:update_setting": 2}


def full_roster(roster: dict[str, str]) -> dict[str, str]:
    """The source rule's ``stem:function`` keys, keyed on the full module path."""
    return {f"{ROUTES_PACKAGE}.{key}": claim for key, claim in roster.items()}


def _leaf_routes(routes: Any) -> Iterator[Any]:
    """Every route that runs a handler, through lazily included routers."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _leaf_routes(included.routes)
        else:
            yield route


def served_handlers(app: FastAPI) -> tuple[dict[str, list[str]], list[str]]:
    """``module:function`` -> the ``METHODS path`` routes serving it, and the rest."""
    handlers: dict[str, list[str]] = {}
    unexplained: list[str] = []
    for route in _leaf_routes(app.routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            mounted = getattr(route, "app", None)
            if route.path == UI_MOUNT and isinstance(mounted, StaticFiles):
                continue
            unexplained.append(f"{type(route).__name__} {route.path}")
            continue
        key = f"{endpoint.__module__}:{endpoint.__name__}"
        methods = ",".join(sorted(getattr(route, "methods", None) or ()))
        handlers.setdefault(key, []).append(f"{methods} {route.path}")
    return handlers, unexplained


def rest_violations(
    handlers: dict[str, list[str]],
    unexplained: list[str],
    roster: dict[str, str],
    multi_path: dict[str, int] | None = None,
) -> list[str]:
    """``roster`` is keyed on full module paths (see :func:`full_roster`)."""
    paths_allowed = MULTI_PATH if multi_path is None else multi_path
    allowed = (
        set(roster) | APP_OWN_ROUTES | FRAMEWORK_HANDLERS | OPTIONAL_EXTRA_HANDLERS
    )
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
    found.extend(
        f"{key}: served at {len(paths)} routes {sorted(paths)}, rostered for "
        f"{paths_allowed.get(key, 1)}"
        for key, paths in sorted(handlers.items())
        if key in allowed
        and key not in FRAMEWORK_HANDLERS
        and len(paths) != paths_allowed.get(key, 1)
    )
    return found


async def _list_surfaces(server: FastMCP[Any]) -> set[str]:
    found = {f"tool:{tool.name}" for tool in await server.list_tools()}
    found |= {f"resource:{res.uri}" for res in await server.list_resources()}
    found |= {
        f"resource:{template.uri_template}"
        for template in await server.list_resource_templates()
    }
    found |= {f"prompt:{prompt.name}" for prompt in await server.list_prompts()}
    return found


def registered_surfaces(server: FastMCP[Any]) -> set[str]:
    """Tools, resources (and templates) and prompts the server lists.

    Enforcement is dropped so the listing is whole. A resource or a prompt is
    reachable by a client the way a tool is, and the source rule reads
    ``@mcp.tool`` alone, so each is named ``tool:<name>``, ``resource:<uri>`` or
    ``prompt:<name>`` and must be rostered under that name.
    """
    # ``list_tools`` is scope-filtered, so an anonymous listing is empty.
    set_auth_enforced(enforced=False)
    try:
        return asyncio.run(_list_surfaces(server))
    finally:
        set_auth_enforced(enforced=True)


def tool_roster(roster: dict[str, str]) -> dict[str, str]:
    """The source rule's tool names, keyed as :func:`registered_surfaces` names them."""
    return {f"tool:{name}": claim for name, claim in roster.items()}


def mcp_violations(registered: set[str], roster: dict[str, str]) -> list[str]:
    found = [
        f"{name}: registered at runtime, not in the roster"
        for name in sorted(registered - set(roster))
    ]
    found.extend(
        f"{name}: rostered but the server registers no such surface"
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
    # Hand read: 79 rostered handler functions, one of which serves two routes.
    assert_hand_read_floor(
        len(handlers),
        79,
        subject="handlers the running app serves",
        hint="a router was not walked, or the route object's shape changed",
    )
    assert_hand_read_floor(
        sum(len(paths) for paths in handlers.values()),
        80,
        subject="routes the running app serves through those handlers",
        hint="a router was not walked, or the route object's shape changed",
    )
    found = rest_violations(handlers, unexplained, full_roster(REST_ROSTER))
    assert found == [], "\n".join(found)


def test_every_surface_the_server_registers_is_rostered() -> None:
    registered = registered_surfaces(mcp)
    assert_hand_read_floor(
        len([name for name in registered if name.startswith("tool:")]),
        18,
        subject="tools the running server lists",
        hint="the listing was filtered, or a provider was not read",
    )
    found = mcp_violations(registered, tool_roster(MCP_ROSTER))
    assert found == [], "\n".join(found)


_HERE = __name__


class TestTheWalkCatchesADefectiveSubject:
    """The spellings the source rules cannot see, built and then walked."""

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

    def _listed_router(self) -> tuple[FastAPI, APIRouter]:
        """An app and a router holding ``listed``, not yet included."""
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        return app, router

    def test_the_same_handler_on_a_second_path_fails(self) -> None:
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        router.add_api_route("/listed-again", listed, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        found = self._check(app)
        assert len(found) == 1
        assert found[0].startswith(f"{_HERE}:listed: served at 2 routes")
        assert "GET /listed-again" in found[0]

    def test_the_same_handler_under_a_second_method_fails(self) -> None:
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        router.add_api_route("/listed", listed, methods=["DELETE"])
        app.include_router(router, prefix="/api/v1")
        found = self._check(app)
        assert len(found) == 1
        assert "DELETE /listed" in found[0]

    def test_a_second_route_passes_only_when_a_person_rostered_it(self) -> None:
        app = FastAPI()
        router = APIRouter()

        def listed() -> dict[str, str]:
            return {}

        router.add_api_route("/listed", listed, methods=["GET"])
        router.add_api_route("/listed-again", listed, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        handlers, unexplained = served_handlers(app)
        rostered = {f"{_HERE}:listed": 2}
        assert rest_violations(handlers, unexplained, self.ROSTER, rostered) == []

    def test_a_framework_prefixed_module_is_not_excused_by_its_prefix(self) -> None:
        app, router = self._listed_router()
        modules = ("fastapi.leak", "starlette.leak", "fastapi.applications")
        for module in modules:

            def leak_docs() -> dict[str, str]:
                return {}

            leak_docs.__module__ = module
            router.add_api_route(f"/leak-{module}", leak_docs, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        assert self._check(app) == [
            f"{module}:leak_docs: served at runtime, not in the roster"
            for module in sorted(modules)
        ]

    def test_the_frameworks_documentation_endpoints_are_named_one_by_one(self) -> None:
        handlers, unexplained = served_handlers(FastAPI())
        assert unexplained == []
        assert set(handlers) == FRAMEWORK_HANDLERS
        assert rest_violations(handlers, unexplained, {}) == []

    def test_a_handler_of_a_rostered_name_in_another_module_fails(self) -> None:
        app, router = self._listed_router()

        def listed() -> dict[str, str]:
            return {}

        # The same function name in a module whose last component is the
        # rostered module's: a roster keyed on the leaf would take it for the
        # rostered one.
        imposter = f"elsewhere.{_HERE.rsplit('.', 1)[-1]}"
        listed.__module__ = imposter
        router.add_api_route("/imposter", listed, methods=["GET"])
        app.include_router(router, prefix="/api/v1")
        assert self._check(app) == [
            f"{imposter}:listed: served at runtime, not in the roster"
        ]

    def test_a_tool_registered_by_the_call_form_fails(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.tool
        def reads() -> str:
            return ""

        def leak_tool() -> str:
            return ""

        server.tool(leak_tool)
        found = mcp_violations(registered_surfaces(server), {"tool:reads": "gated"})
        assert found == ["tool:leak_tool: registered at runtime, not in the roster"]

    def test_a_tool_that_is_only_decorated_passes(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.tool
        def reads() -> str:
            return ""

        assert (
            mcp_violations(registered_surfaces(server), {"tool:reads": "gated"}) == []
        )

    def test_a_resource_fails(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.resource("res://leak")
        def leak_resource() -> str:
            return ""

        assert mcp_violations(registered_surfaces(server), {}) == [
            "resource:res://leak: registered at runtime, not in the roster"
        ]

    def test_a_resource_template_fails(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.resource("res://leak/{name}")
        def leak_template(name: str) -> str:
            return name

        assert mcp_violations(registered_surfaces(server), {}) == [
            "resource:res://leak/{name}: registered at runtime, not in the roster"
        ]

    def test_a_prompt_fails(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.prompt
        def leak_prompt() -> str:
            return ""

        assert mcp_violations(registered_surfaces(server), {}) == [
            "prompt:leak_prompt: registered at runtime, not in the roster"
        ]

    def test_a_tool_and_a_prompt_of_one_name_are_two_surfaces(self) -> None:
        server: FastMCP[Any] = FastMCP("subject")

        @server.tool(name="same")
        def a_tool() -> str:
            return ""

        @server.prompt(name="same")
        def a_prompt() -> str:
            return ""

        assert mcp_violations(registered_surfaces(server), {"tool:same": "gated"}) == [
            "prompt:same: registered at runtime, not in the roster"
        ]
