"""A ``ReadableMatch`` filter withholds a row the SQLite store cannot read.

``tests/unit/api/test_external_key_hidden_corpus.py`` drives the same rows through
every public route. This file pins the store contract directly: the public
label filter returns every readable row that matches, never raises on one it
cannot read, and leaves every ordinary filter (a plain dict, a scalar, no filter)
exactly as it was.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from trellis.retrieve.public_rank import public_label_filter
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, public_label_record
from trellis.stores.base.document import ReadableMatch
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Raw ``metadata_json`` a store fault could leave on a row.
UNREADABLE: dict[str, bytes | str] = {
    "not-json": "{not json",
    "empty": "",
    "list": "[1]",
    "null": "null",
    "string": '"str"',
    "number": "5",
    # A JSON *string* whose text is an object: SQLite's ``json_extract`` hands the
    # text back unquoted, so a parse of it would read as the object.
    "string-holding-an-object": json.dumps(
        json.dumps({DATA_CLASSIFICATION_KEY: public_label_record()})
    ),
    "not-utf8": b'{"\xff": 1}',
    "nested-too-deep": "[" * 3000 + "]" * 3000,
}

PUBLIC = {DATA_CLASSIFICATION_KEY: public_label_record(), "title": "open"}
INTERNAL = {"title": "closed"}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    s = SQLiteDocumentStore(tmp_path / "docs.db")
    yield s
    s.close()


def _seed(store: SQLiteDocumentStore, raw: bytes | str) -> None:
    store.put("a-public", "alpha", PUBLIC)
    store.put("b-internal", "beta", INTERNAL)
    store.put("c-public", "gamma", {**PUBLIC, "content_type": "note"})
    store.put("z-corrupt", "delta", {})
    conn = store._get_conn()
    conn.execute(
        "UPDATE documents SET metadata_json = CAST(? AS TEXT) WHERE doc_id = ?"
        if isinstance(raw, bytes)
        else "UPDATE documents SET metadata_json = ? WHERE doc_id = ?",
        (raw, "z-corrupt"),
    )
    conn.commit()


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(r["doc_id"] for r in rows)


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
class TestTheLabelFilterWithholdsAnUnreadableRow:
    def test_every_readable_public_row_is_served_and_the_corrupt_one_is_not(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        _seed(store, UNREADABLE[shape])
        rows = store.list_documents(include_chunks=False, filters=public_label_filter())
        assert _ids(rows) == ["a-public", "c-public"]

    def test_a_scalar_filter_beside_it_does_not_reach_the_row_first(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        """A ``json_extract`` term elsewhere in the ``WHERE`` raises on bad text."""
        _seed(store, UNREADABLE[shape])
        rows = store.list_documents(
            include_chunks=False,
            filters={"content_type": "note", **public_label_filter()},
        )
        assert _ids(rows) == ["c-public"]

    def test_it_is_recorded_once_per_scan_with_the_by_id_event(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        _seed(store, UNREADABLE[shape])
        with structlog.testing.capture_logs() as logs:
            store.list_documents(include_chunks=False, filters=public_label_filter())
        warned = [e for e in logs if e["event"] == "public_read_unparseable_row"]
        assert [(e["doc_id"], e["log_level"]) for e in warned] == [
            ("z-corrupt", "warning")
        ]

    def test_the_marker_is_the_same_dict_as_the_public_record(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        assert public_label_filter() == {DATA_CLASSIFICATION_KEY: public_label_record()}
        assert isinstance(public_label_filter()[DATA_CLASSIFICATION_KEY], ReadableMatch)


#: What every other read does with the same row, as ``(a plain dict filter, a
#: scalar filter, no filter)``: the ids served, or ``None`` for a read that raises
#: (``sqlite3.OperationalError``, ``ValueError`` or ``RecursionError``). Measured
#: on the commit before ``ReadableMatch`` existed and left as it was written, so
#: a change that moves any of it is a decision. ``string-holding-an-object`` is
#: the one row the plain filter *serves*: its text reads as the public record.
OTHER_READS: dict[str, tuple[list[str] | None, ...]] = {
    "empty": (
        None,
        None,
        None,
    ),
    "list": (
        None,
        ["c-public"],
        ["a-public", "b-internal", "c-public", "z-corrupt"],
    ),
    "nested-too-deep": (
        None,
        None,
        None,
    ),
    "not-json": (
        None,
        None,
        None,
    ),
    "not-utf8": (
        None,
        ["c-public"],
        None,
    ),
    "null": (
        None,
        ["c-public"],
        ["a-public", "b-internal", "c-public", "z-corrupt"],
    ),
    "number": (
        None,
        ["c-public"],
        ["a-public", "b-internal", "c-public", "z-corrupt"],
    ),
    "string": (
        None,
        ["c-public"],
        ["a-public", "b-internal", "c-public", "z-corrupt"],
    ),
    "string-holding-an-object": (
        ["a-public", "c-public", "z-corrupt"],
        ["c-public"],
        ["a-public", "b-internal", "c-public", "z-corrupt"],
    ),
}


def _outcome(call: Any) -> list[str] | None:
    try:
        return _ids(call())
    except (sqlite3.OperationalError, ValueError, RecursionError):
        return None


@pytest.mark.parametrize("shape", sorted(UNREADABLE))
class TestEveryOtherReadIsUnchanged:
    def test_a_plain_dict_filter_a_scalar_filter_and_no_filter(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        _seed(store, UNREADABLE[shape])
        plain = {DATA_CLASSIFICATION_KEY: dict(public_label_record())}
        got = (
            _outcome(lambda: store.list_documents(include_chunks=False, filters=plain)),
            _outcome(
                lambda: store.list_documents(
                    include_chunks=False, filters={"content_type": "note"}
                )
            ),
            _outcome(lambda: store.list_documents(include_chunks=False)),
        )
        assert got == OTHER_READS[shape]
