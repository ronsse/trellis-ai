"""A by-id read must not let a hidden row confirm a guessed id.

An ``external`` key's answer must not depend on anything it may not read. A
hidden row whose stored metadata cannot be parsed used to answer its own id with
a 500 where a missing id answers 404, so a key could confirm a guess. On a
public-only clearance such a row now answers exactly as a missing id; a store
fault is not a property of the row and stays a 500, and every clearance above
public is unchanged.

The shapes are the ones reproduced against a raw SQLite row (``metadata_json``
that is not JSON, empty, a control character, nested past the recursion limit,
and text that is not UTF-8 in ``metadata_json`` or ``content``). The Postgres
shape is a JSONB *string* that is not JSON, which the Postgres row mapper hands to
``json.loads``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import generate_api_key
from trellis.retrieve.sensitivity import read_document_row
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CORRUPT_ID = "hidden-corrupt-id"
MISSING_ID = "no-such-id"
STAMP = "2026-01-01T00:00:00+00:00"

#: ``name -> (metadata_json SQL expression, content SQL expression)``.
RAW_SHAPES: dict[str, tuple[str, str]] = {
    "not-json": ("'{not json'", "'hidden'"),
    "empty": ("''", "'hidden'"),
    "control-char": ("char(123, 34, 97, 34, 58, 34, 10, 34, 125)", "'hidden'"),
    "nested-too-deep": (f"'{'[' * 5000}{']' * 5000}'", "'hidden'"),
    "metadata-not-utf8": ("CAST(x'7b22ff7d' AS TEXT)", "'hidden'"),
    "content-not-utf8": ("'{}'", "CAST(x'ff' AS TEXT)"),
}


def insert_raw(store: Any, doc_id: str, metadata_sql: str, content_sql: str) -> None:
    store._conn.execute(
        "INSERT INTO documents "  # noqa: S608 - the SQL fragments are test literals
        "(doc_id, content, content_hash, metadata_json, created_at, updated_at) "
        f"VALUES (?, {content_sql}, 'h', {metadata_sql}, ?, ?)",
        (doc_id, STAMP, STAMP),
    )
    store._conn.commit()


class World:
    def __init__(self, registry: StoreRegistry, client: TestClient) -> None:
        self.registry = registry
        self.client = client

    def key(self, scopes: list[str]) -> dict[str, str]:
        token, record = generate_api_key("k-" + "-".join(scopes), scopes)
        self.registry.operational.api_key_store.create(record)
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    reg.knowledge.document_store.put("pub-1", "visible", {**label("public")})
    try:
        yield World(reg, TestClient(create_app(), raise_server_exceptions=False))
    finally:
        reg.close()
        app_module._registry = None


def _masked(response: Any, doc_id: str) -> tuple[int, Any]:
    return response.status_code, json.loads(response.text.replace(doc_id, "<id>"))


class TestAnUnparseableRowAnswersAsAMissingId:
    @pytest.mark.parametrize("shape", list(RAW_SHAPES))
    def test_an_external_key_gets_the_missing_id_answer(
        self, world: World, shape: str
    ) -> None:
        insert_raw(
            world.registry.knowledge.document_store, CORRUPT_ID, *RAW_SHAPES[shape]
        )
        headers = world.key(["read", "external"])
        corrupt = world.client.get(f"/api/v1/documents/{CORRUPT_ID}", headers=headers)
        missing = world.client.get(f"/api/v1/documents/{MISSING_ID}", headers=headers)
        assert missing.status_code == 404
        assert _masked(corrupt, CORRUPT_ID) == _masked(missing, MISSING_ID)
        # The control: a readable public document is still served.
        served = world.client.get("/api/v1/documents/pub-1", headers=headers)
        assert served.status_code == 200

    @pytest.mark.parametrize("shape", list(RAW_SHAPES))
    def test_a_key_above_public_still_gets_the_fault(
        self, world: World, shape: str
    ) -> None:
        insert_raw(
            world.registry.knowledge.document_store, CORRUPT_ID, *RAW_SHAPES[shape]
        )
        response = world.client.get(
            f"/api/v1/documents/{CORRUPT_ID}", headers=world.key(["read"])
        )
        assert response.status_code == 500


class TestAStoreFaultIsNotHidden:
    @pytest.mark.parametrize(
        "fault",
        [
            sqlite3.OperationalError("disk I/O error"),
            sqlite3.OperationalError("database is locked"),
            RuntimeError("store down"),
            ConnectionError("refused"),
        ],
        ids=["io", "locked", "runtime", "connection"],
    )
    def test_an_outage_stays_a_500_for_an_external_key(
        self, world: World, monkeypatch: pytest.MonkeyPatch, fault: Exception
    ) -> None:
        def down(doc_id: str) -> dict[str, Any]:
            raise fault

        monkeypatch.setattr(world.registry.knowledge.document_store, "get", down)
        for doc_id in (CORRUPT_ID, MISSING_ID, "pub-1"):
            response = world.client.get(
                f"/api/v1/documents/{doc_id}", headers=world.key(["read", "external"])
            )
            assert response.status_code == 500, doc_id


class _Store:
    """A document store whose ``get`` raises ``fault``."""

    def __init__(self, fault: BaseException) -> None:
        self.fault = fault

    def get(self, doc_id: str) -> dict[str, Any]:
        raise self.fault


def _postgres_jsonb_string_fault() -> BaseException:
    """What the Postgres row mapper raises for a JSONB value ``"str"``."""
    pytest.importorskip("psycopg_pool")  # the Postgres store imports its pool
    from trellis.stores.postgres.document import (
        PostgresDocumentStore,
    )

    row = (CORRUPT_ID, "text", "h", "str", None, None)
    try:
        PostgresDocumentStore._row_to_dict(row)  # type: ignore[arg-type]
    except json.JSONDecodeError as exc:
        return exc
    pytest.fail("a JSONB string that is not JSON must reach json.loads")


def _decode_error(text: str) -> BaseException:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return exc
    pytest.fail("the text must not parse")


PARSE_FAULTS = {
    "postgres-jsonb-string": _postgres_jsonb_string_fault,
    "jsonb-string-decoded": lambda: _decode_error("str"),
    "json-decode": lambda: json.JSONDecodeError("Expecting value", "", 0),
    "recursion": lambda: RecursionError("maximum recursion depth exceeded"),
    "sqlite-not-utf8": lambda: sqlite3.OperationalError(
        "Could not decode to UTF-8 column 'metadata_json' with text '{\"�}'"
    ),
}


class TestTheReadHelper:
    @pytest.mark.parametrize("name", list(PARSE_FAULTS))
    def test_a_public_only_clearance_reads_a_parse_fault_as_missing(
        self, name: str
    ) -> None:
        store = _Store(PARSE_FAULTS[name]())
        assert read_document_row(store, CORRUPT_ID, "public") is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("name", list(PARSE_FAULTS))
    @pytest.mark.parametrize("clearance", ["internal", "confidential", "restricted"])
    def test_every_other_clearance_propagates_it(
        self, name: str, clearance: str
    ) -> None:
        fault = PARSE_FAULTS[name]()
        with pytest.raises(type(fault)):
            read_document_row(_Store(fault), CORRUPT_ID, clearance)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "fault",
        [
            sqlite3.OperationalError("disk I/O error"),
            sqlite3.OperationalError("unable to open database file"),
            ValueError("anything else"),
            KeyError("k"),
        ],
    )
    def test_a_store_fault_propagates_even_for_public(self, fault: Exception) -> None:
        with pytest.raises(type(fault)):
            read_document_row(_Store(fault), CORRUPT_ID, "public")  # type: ignore[arg-type]
