"""REST side of the per-key access log (:mod:`trellis.core.access_log`).

A pure ASGI middleware, outside every route and outside ``trellis.retrieve``:
it watches the response a keyed read already produced, then records one
``access.key_read`` row after the last byte has gone out, off the event loop.

* **Who.** ``require_scope`` stamps ``request.state.access_key_id`` once a real
  key has passed its scope check. The legacy shared secret, auth mode ``off``
  and an anonymous call stamp nothing, so they buffer nothing and record nothing.
* **Which reads.** A ``GET`` under ``/api/v1``, plus the two pack routes
  (:data:`READ_POST_ROUTES`), answered ``200``. A refused or failed call
  returned nothing, so it records nothing.
* **What.** The route template, and the ids the JSON answer carries
  (:func:`extract_returned_items`): the id of every row in a top-level list of
  rows, one nested level down for sectioned packs. Only the ids are kept. A body
  over :data:`MAX_BODY_BYTES`, or one that is not a JSON object, records
  ``item_count: null``.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import anyio.to_thread
import structlog

from trellis.core.access_log import SURFACE_REST, record_key_read, signal_failure

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

API_PREFIX = "/api/v1/"
#: ``POST`` routes that read: they assemble and return a pack.
READ_POST_ROUTES = frozenset({"/api/v1/packs", "/api/v1/packs/sectioned"})
#: A larger answer is not buffered; its row records ``item_count: null``.
MAX_BODY_BYTES = 1_048_576
_ID_KEYS = ("item_id", "doc_id", "node_id", "trace_id", "entity_id", "id")
#: Top-level lists that are not rows the key was served.
_NOT_ROWS = frozenset({"advisories"})


def _row_id(row: Any) -> str | None:
    if isinstance(row, dict):
        for key in _ID_KEYS:
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_returned_items(payload: Any) -> tuple[list[str], int | None]:
    """``(ids, true count)`` of the rows a JSON answer carries.

    Rows are the dicts in a top-level list (``items``, ``results``, ...); a
    row with no id of its own is searched one level down, for ``sections``.
    A single record with an id of its own (a document read) is one item.
    ``None`` for a count only when the payload is not a JSON object.
    """
    if not isinstance(payload, dict):
        return [], None
    ids: list[str] = []
    for key, value in payload.items():
        if key in _NOT_ROWS or not isinstance(value, list):
            continue
        for row in value:
            row_id = _row_id(row)
            if row_id is not None:
                ids.append(row_id)
            elif isinstance(row, dict):
                ids.extend(
                    nested_id
                    for inner in row.values()
                    if isinstance(inner, list)
                    for nested_id in (_row_id(r) for r in inner)
                    if nested_id is not None
                )
    if not ids:
        # A single record: its own id, or the id of the one record it wraps
        # (``{"status": "ok", "document": {...}}``).
        single = _row_id(payload) or next(
            (
                row_id
                for value in payload.values()
                if (row_id := _row_id(value)) is not None
            ),
            None,
        )
        if single is not None:
            ids.append(single)
    return ids, len(ids)


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


def _record(scope: Scope, key_id: str, body: bytes | None) -> None:
    """Runs in a worker thread. Never raises."""
    registry = None
    try:
        from trellis_api.app import get_registry  # noqa: PLC0415

        registry = get_registry()
        route = route_template(scope)

        def items() -> tuple[list[str], int | None]:
            if body is None:
                return [], None
            return extract_returned_items(json.loads(body))

        record_key_read(
            registry,
            key_id=key_id,
            surface=SURFACE_REST,
            route=f"{scope['method']} {route}",
            items=items,
        )
    except Exception as exc:  # advisory step; never fail the read
        signal_failure(registry, SURFACE_REST, exc)


class AccessLogMiddleware:
    """Record one ``access.key_read`` row per authenticated read by a key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_read(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        state: dict[str, Any] = {"ok": False, "size": 0, "over": False, "key_id": None}

        async def send_wrapper(message: Message) -> None:
            try:
                if message["type"] == "http.response.start":
                    key_id = (scope.get("state") or {}).get("access_key_id")
                    state["key_id"] = key_id
                    state["ok"] = bool(key_id) and message["status"] == HTTPStatus.OK
                elif message["type"] == "http.response.body" and state["ok"]:
                    body = message.get("body", b"")
                    state["size"] += len(body)
                    if state["size"] > MAX_BODY_BYTES:
                        state["over"] = True
                        chunks.clear()
                    elif not state["over"]:
                        chunks.append(body)
            except Exception:  # advisory; the answer must still go out
                state["ok"] = False
            await send(message)

        await self.app(scope, receive, send_wrapper)
        if state["ok"]:
            try:
                body = None if state["over"] else b"".join(chunks)
                await anyio.to_thread.run_sync(_record, scope, state["key_id"], body)
            except Exception as exc:  # advisory step; never fail the read
                signal_failure(None, SURFACE_REST, exc)
