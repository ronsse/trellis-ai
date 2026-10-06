"""A local HTTP server that answers every POST with the bytes a test scripts.

``StubHttpServer(reply)`` listens on a free 127.0.0.1 port, and :attr:`url`
is its base URL. ``reply(path, body)`` gets each request's path and decoded
JSON body and returns the raw bytes written back before the connection
closes: :func:`http_response` builds a well-formed answer with any status,
other bytes are a reply that is not HTTP, and ``b""`` closes the connection
without an answer. Every request is kept on :attr:`requests`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = Callable[[str, dict[str, Any]], bytes]


def http_response(status: int, body: str) -> bytes:
    """A well-formed HTTP/1.1 answer with ``status`` and ``body``."""
    data = body.encode()
    head = (
        f"HTTP/1.1 {status} Stub\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode() + data


class StubHttpServer:
    """Serve ``reply`` on a free local port inside a ``with`` block."""

    def __init__(self, reply: Reply) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        requests = self.requests

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                requests.append((self.path, body))
                self.wfile.write(reply(self.path, body))
                self.close_connection = True

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the test output free of access-log lines."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever)

    def __enter__(self) -> StubHttpServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
