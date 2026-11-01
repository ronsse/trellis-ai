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
import sys
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from trellis.retrieve.public_rank import public_label_filter
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, public_label_record
from trellis.stores.base.document import ReadableMatch
from trellis.stores.sqlite.document import (
    SQLiteDocumentStore,
    _metadata_matches_closed,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_LABEL = json.dumps({DATA_CLASSIFICATION_KEY: public_label_record()})

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
    # Far past every interpreter's parser limit (3.11 stops near 1000 frames, later
    # versions at a C-stack bound of their own), so the row is unreadable on all of
    # them. A depth near any one limit would be readable on another.
    "nested-too-deep": "[" * 200_000 + "]" * 200_000,
    # The public record, then bytes after a NUL: SQLite's ``json_extract`` stops at
    # the NUL and reads a complete object, ``json.loads`` of the column does not.
    "nul-tail": _LABEL + "\x00junk",
    # JSON5 that SQLite's JSON functions can be made to accept and ``json.loads``
    # refuses.
    "json5-trailing-comma": _LABEL[:-1] + ",}",
    "json5-unquoted-key": _LABEL.replace(
        f'"{DATA_CLASSIFICATION_KEY}"', DATA_CLASSIFICATION_KEY, 1
    ),
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


#: A read that goes through SQLite's own JSON functions (a plain dict filter, a
#: scalar filter) accepts or refuses a NUL tail, JSON5 and ``NaN`` according to the
#: SQLite release (JSON5 input arrived in 3.42), so for the rows below only the read
#: that parses in Python alone is pinned. ``tests/unit/stores`` cannot know which
#: release CI links; the SQL text of every non-marker filter is unchanged by
#: ``ReadableMatch``.
NOT_PINNED: Any = ...

#: What every other read does with the same row, as ``(a plain dict filter, a
#: scalar filter, no filter)``: the ids served, or ``None`` for a read that raises
#: (``sqlite3.OperationalError``, ``ValueError`` or ``RecursionError``). Measured
#: on the commit before ``ReadableMatch`` existed and left as it was written, so
#: a change that moves any of it is a decision. ``string-holding-an-object`` is
#: the one row the plain filter *serves*: its text reads as the public record.
OTHER_READS: dict[str, tuple[Any, ...]] = {
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
    "nul-tail": (
        NOT_PINNED,
        NOT_PINNED,
        None,
    ),
    "json5-trailing-comma": (
        NOT_PINNED,
        NOT_PINNED,
        None,
    ),
    "json5-unquoted-key": (
        NOT_PINNED,
        NOT_PINNED,
        None,
    ),
}


def _assert_reads(got: tuple[Any, ...], expected: tuple[Any, ...]) -> None:
    """Compare each read with its pin, leaving a ``NOT_PINNED`` slot unchecked."""
    assert len(got) == len(expected)
    for slot, (read, pin) in enumerate(zip(got, expected, strict=True)):
        if pin is not NOT_PINNED:
            assert read == pin, f"read {slot}: {read!r} != {pin!r}"


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
        _assert_reads(got, OTHER_READS[shape])


#: Rows ``json.loads`` reads and the strict grammar of SQLite's ``json_valid``
#: refuses. The row is labelled public, so a public read serves it, as the by-id
#: read does.
READABLE_BUT_NOT_STRICT_JSON: dict[str, str] = {
    "nan": _LABEL[:-1] + ', "x": NaN}',
    "infinity": _LABEL[:-1] + ', "x": Infinity}',
    "negative-infinity": _LABEL[:-1] + ', "x": -Infinity}',
}

#: ``(a plain dict filter, a scalar filter, no filter)`` for each of those rows,
#: measured on the commit before ``ReadableMatch`` existed.
READABLE_OTHER_READS: tuple[Any, ...] = (
    NOT_PINNED,
    NOT_PINNED,
    ["a-public", "b-internal", "c-public", "z-corrupt"],
)


@pytest.mark.parametrize("shape", sorted(READABLE_BUT_NOT_STRICT_JSON))
class TestAReadableRowIsServed:
    def test_the_label_filter_serves_it_and_records_nothing(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        _seed(store, READABLE_BUT_NOT_STRICT_JSON[shape])
        with structlog.testing.capture_logs() as logs:
            rows = store.list_documents(
                include_chunks=False, filters=public_label_filter()
            )
        assert _ids(rows) == ["a-public", "c-public", "z-corrupt"]
        assert not [e for e in logs if e["event"] == "public_read_unparseable_row"]

    def test_every_other_read_is_unchanged(
        self, store: SQLiteDocumentStore, shape: str
    ) -> None:
        _seed(store, READABLE_BUT_NOT_STRICT_JSON[shape])
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
        _assert_reads(got, READABLE_OTHER_READS)


class TestADocIdThatIsNotUtf8:
    """CPython cannot hand a callback a text value that is not UTF-8."""

    def _insert(self, store: SQLiteDocumentStore, metadata_json: str) -> None:
        store.put("a-public", "alpha", PUBLIC)
        store.put("b-internal", "beta", INTERNAL)
        store.put("c-public", "gamma", {**PUBLIC, "content_type": "note"})
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO documents VALUES (CAST(x'ff' AS TEXT), 'h', 'h', ?,"
            " '2020', '2020')",
            (metadata_json,),
        )
        conn.commit()

    def test_a_hidden_row_with_one_is_not_served_and_does_not_fail_the_read(
        self, store: SQLiteDocumentStore
    ) -> None:
        self._insert(store, json.dumps(INTERNAL))
        rows = store.list_documents(include_chunks=False, filters=public_label_filter())
        assert _ids(rows) == ["a-public", "c-public"]
        # What every other read does is unchanged: a plain dict filter and a scalar
        # filter never fetch the row, and an unfiltered read cannot decode its id.
        plain = {DATA_CLASSIFICATION_KEY: dict(public_label_record())}
        assert (
            _outcome(lambda: store.list_documents(include_chunks=False, filters=plain)),
            _outcome(
                lambda: store.list_documents(
                    include_chunks=False, filters={"content_type": "note"}
                )
            ),
            _outcome(lambda: store.list_documents(include_chunks=False)),
        ) == (["a-public", "c-public"], ["c-public"], None)

    def test_a_corrupt_row_with_one_is_recorded_without_failing_the_read(
        self, store: SQLiteDocumentStore
    ) -> None:
        self._insert(store, "[1]")
        with structlog.testing.capture_logs() as logs:
            rows = store.list_documents(
                include_chunks=False, filters=public_label_filter()
            )
        assert _ids(rows) == ["a-public", "c-public"]
        warned = [e for e in logs if e["event"] == "public_read_unparseable_row"]
        assert [e["doc_id"] for e in warned] == ["\ufffd"]


class TestAParserLimitNeverFailsARowThePredicatePassed:
    def test_a_read_never_raises_near_the_interpreters_depth_limit(
        self, store: SQLiteDocumentStore
    ) -> None:
        """The predicate and the read that follows parse the same bytes.

        A row nested to just under the limit is readable or not depending on how
        many frames are on the stack, and the callback runs on a different stack
        depth than the read after it. Whichever side of the limit a depth falls
        on, the row is served whole or withheld; it never passes the predicate and
        then raises while the page is built. The sweep covers every depth within
        reach of the limit, 990 among them.
        """
        _seed(store, "{}")
        conn = store._get_conn()
        head = _LABEL[:-1] + ', "x": '
        limit = sys.getrecursionlimit()
        for depth in range(limit - 300, limit + 6):
            conn.execute(
                "UPDATE documents SET metadata_json = ? WHERE doc_id = 'z-corrupt'",
                (head + "[" * depth + "]" * depth + "}",),
            )
            conn.commit()
            rows = store.list_documents(
                include_chunks=False, filters=public_label_filter()
            )
            assert {"a-public", "c-public"} <= set(_ids(rows)), depth

    def test_a_shallow_nested_row_is_served(self, store: SQLiteDocumentStore) -> None:
        _seed(store, _LABEL[:-1] + ', "x": ' + "[" * 100 + "]" * 100 + "}")
        rows = store.list_documents(include_chunks=False, filters=public_label_filter())
        assert _ids(rows) == ["a-public", "c-public", "z-corrupt"]


class TestTheFilterValueIsNotTheRow:
    def test_a_filter_value_that_is_not_json_raises_instead_of_withholding(
        self,
    ) -> None:
        """A bad filter value is a bug in the caller, not an unreadable row."""
        with structlog.testing.capture_logs() as logs, pytest.raises(ValueError):
            _metadata_matches_closed(b"{}", b"id", "key", "{not json")
        assert not logs
