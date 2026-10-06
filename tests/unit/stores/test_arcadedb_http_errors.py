"""ArcadeDB's HTTP calls fail as ``StoreError``, without the server's reply.

``execute_sql`` and ``ensure_database`` reach ArcadeDB over HTTP. A request
that gets no answer, a reply that is not HTTP and an error answer are each
raised as :class:`StoreError` naming the operation and the HTTP status or the
error's type. The URL, the server's reply and the socket error's text stay
out of the message: a redaction that fails on one writes it to the audit log.
Both calls send through one request helper, so each case runs through one.
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.stub_http_server import StubHttpServer, http_response
from trellis.errors import StoreError
from trellis.stores.arcadedb.base import ensure_database
from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

MARKER = "preescape-server-reply-0001"
DATABASE = "db_preescape"
PASSWORD = "unused-secret"  # noqa: S105 — test placeholder, not a real credential


def _answer(status: int, body: str) -> Callable[[str, dict[str, Any]], bytes]:
    return lambda _path, _body: http_response(status, body)


@pytest.fixture
def refused_url() -> Iterator[str]:
    # A bound socket that never listens refuses every connection, and holding
    # it keeps the port from being reused while the test runs.
    with socket.socket() as unheard:
        unheard.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{unheard.getsockname()[1]}"


def test_an_error_answer_is_a_store_error_without_its_body() -> None:
    body = json.dumps({"error": "Cannot execute command", "detail": MARKER})
    with (
        StubHttpServer(_answer(400, body)) as server,
        pytest.raises(StoreError) as caught,
    ):
        ensure_database(server.url, "root", PASSWORD, DATABASE)

    assert caught.value.message == "ArcadeDB create-database failed: HTTP 400"
    assert caught.value.store == "graph"
    assert len(server.requests) == 1


def test_a_reply_that_is_not_an_http_answer_is_a_store_error() -> None:
    reply = f"NOT-HTTP {MARKER}\r\n\r\n".encode()
    with (
        StubHttpServer(lambda _path, _body: reply) as server,
        pytest.raises(StoreError) as caught,
    ):
        ensure_database(server.url, "root", PASSWORD, DATABASE)

    assert caught.value.message == "ArcadeDB create-database failed: BadStatusLine"
    assert caught.value.store == "graph"
    assert type(caught.value.__cause__) is http.client.BadStatusLine


def test_an_existing_database_is_not_an_error() -> None:
    body = f'{{"error": "Database \'{DATABASE}\' already exists"}}'
    with StubHttpServer(_answer(400, body)) as server:
        assert ensure_database(server.url, "root", PASSWORD, DATABASE) is False

    assert server.requests == [
        ("/api/v1/server", {"command": f"create database {DATABASE}"})
    ]


@pytest.mark.parametrize(
    ("open_store", "store"),
    [
        pytest.param(
            lambda url: ArcadeDBVectorStore(
                http_url=url, password=PASSWORD, dimensions=3
            ),
            "vector",
            id="vector-schema",
        ),
        pytest.param(
            lambda url: ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema(
                http_url=url, user="root", password=PASSWORD, database=DATABASE
            ),
            "graph",
            id="graph-migration",
        ),
    ],
)
def test_a_store_names_itself_on_a_refused_connection(
    open_store: Callable[[str], object], store: str, refused_url: str
) -> None:
    with pytest.raises(StoreError) as caught:
        open_store(refused_url)

    assert caught.value.message == (
        "ArcadeDB SQL command failed: URLError(ConnectionRefusedError)"
    )
    assert caught.value.store == store


def _schema_ok_rest_fails(_path: str, body: dict[str, Any]) -> bytes:
    if str(body["command"]).startswith("CREATE "):
        return http_response(200, json.dumps({"result": []}))
    return http_response(500, json.dumps({"error": "Internal", "detail": MARKER}))


def test_a_vector_call_the_server_refuses_names_the_vector_store() -> None:
    with StubHttpServer(_schema_ok_rest_fails) as server:
        store = ArcadeDBVectorStore(
            http_url=server.url, password=PASSWORD, dimensions=3
        )
        with pytest.raises(StoreError) as caught:
            store.delete("ent-preescape-0001")

    assert caught.value.message == "ArcadeDB SQL command failed: HTTP 500"
    assert caught.value.store == "vector"
    assert str(server.requests[-1][1]["command"]).startswith("UPDATE Node SET")
