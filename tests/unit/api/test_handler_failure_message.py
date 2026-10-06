"""A failed handler's REST answer carries no driver text.

A trigger on the SQLite graph store's ``nodes`` table refuses the write with
synthetic driver text, so the ``entity.create`` handler raises a raw
``sqlite3.IntegrityError`` out of the shipped store and the executor's
untyped catch fails the command. The route answers 400 with the FAILED
result's message, which names that type and carries none of its text.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Stands in for a driver's own error text, which can carry query text and
#: values.
_MARKER = "synthetic-secret-c55h"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """The real app over a graph store whose node inserts are refused."""
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    registry = StoreRegistry(stores_dir=stores_dir)
    app_module._registry = registry
    try:
        _ = registry.knowledge.graph_store  # creates graph.db
        conn = sqlite3.connect(stores_dir / "graph.db")
        try:
            conn.execute(
                "CREATE TRIGGER refuse_the_node BEFORE INSERT ON nodes "
                f"BEGIN SELECT RAISE(ABORT, 'server says: {_MARKER}'); END"
            )
            conn.commit()
        finally:
            conn.close()
        yield TestClient(create_app(), raise_server_exceptions=False)
    finally:
        registry.close()
        app_module._registry = None


def test_a_driver_error_is_named_by_its_type(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/entities",
        json={"entity_type": "service", "name": "synthetic-rest-entity"},
    )

    assert _MARKER not in resp.text
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "Execution failed: IntegrityError"}
