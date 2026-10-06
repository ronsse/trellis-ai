"""Opening a Bolt store on a server it cannot use is a ``StoreError``.

The schema statements a Bolt store runs while it opens are its first round
trip, so an unreachable server or refused credentials fail there. The
driver's error is raised as :class:`StoreError` naming the store and the
error's type, with the error chained as its cause. The driver's text, which
names hosts and carries the server's messages, stays out of the message: a
redaction that opens the store writes the message to the audit log.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

pytest.importorskip("neo4j")

from neo4j.exceptions import AuthError, ServiceUnavailable

from trellis.errors import StoreError
from trellis.stores.arcadedb.graph import ArcadeDBGraphStore
from trellis.stores.base.graph import GraphStore
from trellis.stores.base.vector import VectorStore
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore
from trellis.stores.neo4j.graph import Neo4jGraphStore
from trellis.stores.neo4j.vector import Neo4jVectorStore

SERVER_TEXT = "preescape-server-text-0001"
PASSWORD = "unused-secret"  # noqa: S105 — test placeholder, not a real credential

Opener = Callable[..., GraphStore | VectorStore]


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


@pytest.mark.parametrize(
    ("open_store", "store"),
    [
        pytest.param(
            lambda uri: Neo4jGraphStore(uri, password=PASSWORD),
            "graph",
            id="neo4j-graph",
        ),
        pytest.param(
            lambda uri: Neo4jVectorStore(uri, password=PASSWORD, dimensions=3),
            "vector",
            id="neo4j-vector",
        ),
        pytest.param(
            lambda uri: ArcadeDBGraphStore(
                uri, password=PASSWORD, ensure_database_exists=False
            ),
            "graph",
            id="arcadedb-graph",
        ),
    ],
)
def test_an_unreachable_server_is_a_store_error(
    open_store: Opener, store: str, refused_uri: str
) -> None:
    with pytest.raises(StoreError) as caught:
        open_store(refused_uri)

    message = caught.value.message
    assert message == f"Opening the {store} store failed: ServiceUnavailable"
    assert caught.value.store == store
    cause = caught.value.__cause__
    assert isinstance(cause, ServiceUnavailable)
    # The text the message leaves out is on the cause.
    assert refused_uri.removeprefix("bolt://") in str(cause)


@pytest.mark.parametrize(
    ("open_store", "store"),
    [
        pytest.param(
            lambda driver: BoltOpenCypherGraphStore(
                driver=driver, database="neo4j", owns_driver=False
            ),
            "graph",
            id="graph",
        ),
        pytest.param(
            lambda driver: Neo4jVectorStore(
                "bolt://unused", driver=driver, dimensions=3
            ),
            "vector",
            id="vector",
        ),
    ],
)
def test_refused_credentials_are_a_store_error(open_store: Opener, store: str) -> None:
    error = AuthError(SERVER_TEXT)

    with pytest.raises(StoreError) as caught:
        open_store(_RefusingDriver(error))

    assert caught.value.message == f"Opening the {store} store failed: AuthError"
    assert caught.value.store == store
    assert caught.value.__cause__ is error


def test_an_error_from_outside_the_driver_is_raised_unchanged() -> None:
    error = RuntimeError(SERVER_TEXT)

    with pytest.raises(RuntimeError) as caught:
        BoltOpenCypherGraphStore(
            driver=_RefusingDriver(error), database="neo4j", owns_driver=False
        )

    assert caught.value is error
