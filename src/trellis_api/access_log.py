"""REST side of the per-key access log (:mod:`trellis.core.access_log`).

A pure ASGI middleware, outside every route and outside ``trellis.retrieve``:
it watches the response a keyed read already produced, then hands one entry to
the access-log writer thread after the last byte has gone out. Nothing here
runs a store call, parses a body or waits on a lock held by one.

* **Who.** ``require_scope`` stamps ``request.state.access_key_id`` once a real
  key has passed its scope check. The legacy shared secret, auth mode ``off``
  and an anonymous call stamp nothing, so they buffer nothing and record nothing.
* **Which reads.** A ``GET`` under ``/api/v1``, plus the two pack routes
  (:data:`READ_POST_ROUTES`), answered ``200``. A refused or failed call
  (403, 404, 422, ...) returned nothing, so it records nothing.
* **Off is a pass-through.** The knob is read when the response starts, before
  a single body chunk is looked at, so an off process buffers nothing.
* **What.** The route template (never the raw path) and the ids the answer
  carries. The ids come from an allowlist of item-serving routes
  (:data:`ITEM_ROUTES`), each with an exact path to its id field, keyed on the
  matched route template so an id in the URL cannot select an entry. Every
  other route records ``item_count: null`` and no ids: a route that does not
  name its items is not guessed at, and nothing else in a body is read. A
  body over :data:`MAX_BODY_BYTES`, one that is not JSON, or one that does not
  have the allowlisted shape also records ``item_count: null``.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.access_log import (
    ENTRY_OVERHEAD_BYTES,
    SURFACE_REST,
    access_log_enabled,
    enqueue_key_read,
    signal_failure,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

API_PREFIX = "/api/v1/"
#: ``POST`` routes that read: they assemble and return a pack.
READ_POST_ROUTES = frozenset({"/api/v1/packs", "/api/v1/packs/sectioned"})
#: A larger answer is not buffered; its row records ``item_count: null``.
MAX_BODY_BYTES = 1_048_576

#: The routes whose answer is a set of items the key was served, as
#: ``"METHOD route-template" -> (path into the JSON, id field)``. ``"*"`` in a
#: path walks every element of a list; a path that ends on one object counts
#: that object as one item. Hand-read from each route's response; a route
#: absent here records ``item_count: null``.
ITEM_ROUTES: dict[str, tuple[tuple[str, ...], str]] = {
    "GET /api/v1/search": (("results", "*"), "doc_id"),
    "POST /api/v1/packs": (("items", "*"), "item_id"),
    "POST /api/v1/packs/sectioned": (("sections", "*", "items", "*"), "item_id"),
    "GET /api/v1/documents/{doc_id:path}": (("document",), "doc_id"),
    "GET /api/v1/graph/search": (("results", "*"), "entity_id"),
    "GET /api/v1/entities/{entity_id:path}": (("entity",), "node_id"),
    "GET /api/v1/traces": (("traces", "*"), "trace_id"),
    "GET /api/v1/traces/{trace_id}": (("trace",), "trace_id"),
}


def extract_ids(
    payload: Any, path: tuple[str, ...], field: str
) -> tuple[list[str], int | None]:
    """``(ids, true count)`` at ``path`` in ``payload``; ``None`` if the shape differs.

    The count is the number of objects the path reaches, so an empty list is a
    real zero. A shape that is not the one the route is allowlisted for (a key
    missing, a non-list where a list belongs, an object without the id field)
    is unknown, not zero.
    """
    nodes: list[Any] = [payload]
    for step in path:
        walked: list[Any] = []
        for node in nodes:
            if step == "*":
                if not isinstance(node, list):
                    return [], None
                walked.extend(node)
            elif isinstance(node, dict) and step in node:
                walked.append(node[step])
            else:
                return [], None
        nodes = walked
    ids: list[str] = []
    for node in nodes:
        value = node.get(field) if isinstance(node, dict) else None
        if not isinstance(value, str) or not value:
            return [], None
        ids.append(value)
    return ids, len(ids)


def items_from_body(
    body: bytes | None, spec: tuple[tuple[str, ...], str] | None
) -> tuple[list[str], int | None]:
    """The ids an allowlisted route's body carries. Runs on the writer thread."""
    if spec is None or not body:
        return [], None
    try:
        payload = json.loads(body)
    except ValueError:
        return [], None
    return extract_ids(payload, *spec)


def is_read(method: str, path: str) -> bool:
    return path.startswith(API_PREFIX) and (
        method == "GET" or (method == "POST" and path in READ_POST_ROUTES)
    )


def route_template(scope: Scope) -> str:
    """The matched route's template, e.g. ``/api/v1/documents/{doc_id:path}``.

    Newer FastAPI keeps an included router's path relative to its prefix, so the
    prefix is restored here; older releases already carry it. A request that
    matched no route (it cannot be a keyed 200 read) falls back to its path.
    """
    tail = getattr(scope.get("route"), "path", None)
    if not isinstance(tail, str):
        return str(scope["path"])
    return tail if tail.startswith(API_PREFIX) else API_PREFIX.rstrip("/") + tail


def _registry() -> StoreRegistry:
    from trellis_api.app import get_registry  # noqa: PLC0415

    return get_registry()


class AccessLogMiddleware:
    """Record one ``access.key_read`` row per authenticated read by a key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_read(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        state: dict[str, Any] = {
            "ok": False,
            "size": 0,
            "over": False,
            "key_id": None,
            "registry": None,
            "route": "",
            "spec": None,
        }

        def begin(message: Message) -> None:
            key_id = (scope.get("state") or {}).get("access_key_id")
            if not key_id or message["status"] != HTTPStatus.OK:
                return
            registry = _registry()
            if not access_log_enabled(registry):
                return
            template = route_template(scope)
            route = f"{scope['method']} {template}"
            state.update(
                ok=True,
                key_id=key_id,
                registry=registry,
                route=route,
                spec=ITEM_ROUTES.get(route),
            )

        async def send_wrapper(message: Message) -> None:
            try:
                if message["type"] == "http.response.start":
                    begin(message)
                elif (
                    message["type"] == "http.response.body"
                    and state["ok"]
                    and state["spec"] is not None
                ):
                    body = message.get("body", b"")
                    state["size"] += len(body)
                    if state["size"] > MAX_BODY_BYTES:
                        state["over"] = True
                        chunks.clear()
                    elif not state["over"]:
                        chunks.append(body)
            except Exception as exc:  # advisory; the answer must still go out
                state["ok"] = False
                signal_failure(None, SURFACE_REST, exc)
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if not state["ok"]:
            return
        try:
            spec = state["spec"]
            body = None if state["over"] or spec is None else b"".join(chunks)
            enqueue_key_read(
                state["registry"],
                key_id=state["key_id"],
                surface=SURFACE_REST,
                route=state["route"],
                items=lambda: items_from_body(body, spec),
                size=ENTRY_OVERHEAD_BYTES + (len(body) if body else 0),
            )
        except Exception as exc:  # advisory step; never fail the read
            signal_failure(state["registry"], SURFACE_REST, exc)
