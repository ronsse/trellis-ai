"""Opening a Bolt store on a server it cannot use is a ``StoreError``.

The schema statements a Bolt store runs while it opens are its first round
trip, so an unreachable server or refused credentials fail there. The
driver's error is raised as :class:`StoreError` naming the store and the
error's type, with the error chained as its cause. The driver's text, which
names hosts and carries the server's messages, stays out of the message: a
redaction that opens the store writes the message to the audit log. The
graph and vector stores open through one helper, so each case runs on one.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

pytest.importorskip("neo4j")

from neo4j.exceptions import AuthError, ServiceUnavailable

from trellis.errors import StoreError
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis.stores.neo4j.graph import Neo4jGraphStore
from trellis.stores.neo4j.vector import Neo4jVectorStore

SERVER_TEXT = "preescape-server-text-0001"
PASSWORD = "unused-secret"  # noqa: S105 — test placeholder, not a real credential


class _RefusingDriver:
    """A driver whose every statement raises ``error``."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    @contextmanager
    def session(self, **_config: object) -> Iterator[_RefusingDriver]:
        yield self

    def run(self, *_args: object, **_params: object) -> None:
        raise self.error


@pytest.fixture
def refused_uri() -> Iterator[str]:
    # A bound socket that never listens refuses every connection, and holding
    # it keeps the port from being reused while the test runs.
    with socket.socket() as unheard:
        unheard.bind(("127.0.0.1", 0))
        yield f"bolt://127.0.0.1:{unheard.getsockname()[1]}"


def test_an_unreachable_server_is_a_store_error(refused_uri: str) -> None:
    with pytest.raises(StoreError) as caught:
        Neo4jGraphStore(refused_uri, password=PASSWORD)

    message = caught.value.message
    assert message == "Opening the graph store failed: ServiceUnavailable"
    assert caught.value.store == "graph"
    cause = caught.value.__cause__
    assert isinstance(cause, ServiceUnavailable)
    # The text the message leaves out is on the cause.
    assert refused_uri.removeprefix("bolt://") in str(cause)


def test_refused_credentials_are_a_store_error() -> None:
    error = AuthError(SERVER_TEXT)

    with pytest.raises(StoreError) as caught:
        Neo4jVectorStore("bolt://unused", driver=_RefusingDriver(error), dimensions=3)

    assert caught.value.message == "Opening the vector store failed: AuthError"
    assert caught.value.store == "vector"
    assert caught.value.__cause__ is error


def test_an_error_from_outside_the_driver_is_raised_unchanged() -> None:
    error = RuntimeError(SERVER_TEXT)

    with pytest.raises(RuntimeError) as caught:
        BoltOpenCypherGraphStore(
            driver=_RefusingDriver(error), database="neo4j", owns_driver=False
        )

    assert caught.value is error
