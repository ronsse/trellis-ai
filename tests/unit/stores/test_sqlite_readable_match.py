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


#: How many nesting levels earlier than the plain read the predicate may withhold a
#: row. On CPython 3.11 Python frames count against the parser's depth, and the one
#: frame ``_parse_as_the_read_does`` adds puts the callback's parse exactly where the
#: read's parse sits, so the two limits are the same level (measured: 0). From 3.12
#: the limit is a C recursion count that Python frames do not touch, and entering
#: the callback from C costs two levels (measured on 3.12.3: 2) that no Python code
#: can give back; 8 leaves room for a build that counts a little more.
_EARLIER_BY_AT_MOST = 0 if sys.version_info < (3, 12) else 8

_ROW_HEAD = _LABEL[:-1] + ', "x": '


def _nest(store: SQLiteDocumentStore, depth: int) -> None:
    conn = store._get_conn()
    conn.execute(
        "UPDATE documents SET metadata_json = ? WHERE doc_id = 'z-corrupt'",
        (_ROW_HEAD + "[" * depth + "]" * depth + "}",),
    )
    conn.commit()


def _plain_read(store: SQLiteDocumentStore) -> str:
    try:
        store.list_documents(include_chunks=False)
    except RecursionError:
        return "raises"
    return "served"


def _public_read(store: SQLiteDocumentStore) -> str:
    try:
        rows = store.list_documents(include_chunks=False, filters=public_label_filter())
    except RecursionError:
        return "raises"
    return "served" if "z-corrupt" in _ids(rows) else "withheld"


def _depth_json_cannot_parse() -> int:
    depth = 1_000
    while True:
        try:
            json.loads("[" * depth + "]" * depth)
        except RecursionError:
            return depth
        depth *= 2


def _first_depth_not_served(
    store: SQLiteDocumentStore, read: Any, hi: int
) -> tuple[int, str]:
    """Smallest nesting depth at which ``read`` stops serving the row, and how.

    Found by bisecting. Both reads are called from here, one frame down, so they
    sit at the same stack depth and the two boundaries are comparable; the state
    at the boundary is taken here too, because a caller one frame shallower sees
    a different limit.
    """
    lo = 1
    _nest(store, lo)
    assert read(store) == "served"
    _nest(store, hi)
    state = read(store)
    assert state != "served"
    while hi - lo > 1:
        mid = (lo + hi) // 2
        _nest(store, mid)
        got = read(store)
        if got == "served":
            lo = mid
        else:
            hi, state = mid, got
    return hi, state


class TestTheParserLimit:
    """The predicate and the read after it hit the parser's limit together.

    Neither bound is assumed: both are found on the running interpreter by
    bisecting, so 3.11 (recursion depth) and 3.12 and later (C recursion) are
    each exercised.
    """

    @staticmethod
    def _boundaries(store: SQLiteDocumentStore) -> tuple[int, int, str]:
        _seed(store, "{}")
        hi = _depth_json_cannot_parse()
        plain, _ = _first_depth_not_served(store, _plain_read, hi)
        public, how = _first_depth_not_served(store, _public_read, hi)
        return plain, public, how

    def test_the_predicate_withholds_a_row_only_where_the_plain_read_raises(
        self, store: SQLiteDocumentStore
    ) -> None:
        plain, public, how = self._boundaries(store)
        assert how == "withheld", "the predicate passed a row the read then failed on"
        assert public <= plain
        assert plain - public <= _EARLIER_BY_AT_MOST, (plain, public)

    def test_no_depth_within_reach_of_the_limit_fails_a_public_read(
        self, store: SQLiteDocumentStore
    ) -> None:
        plain, _, _ = self._boundaries(store)
        for depth in range(plain - 12, plain + 12):
            _nest(store, depth)
            assert _public_read(store) != "raises", depth

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


#: ``(column, filter name) -> (WHERE terms, bound parameters)`` for every filter
#: shape that is not a ``ReadableMatch``, copied from the commit before that
#: marker existed. A change that moves any of it changes what a plain, scalar,
#: list, null or tag filter reads on every surface, so it is a decision.
PLAIN_FILTER_SQL: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {
    ("metadata_json", "plain-dict"): (
        ("trellis_metadata_matches(json_extract(metadata_json, '$'), ?, ?)",),
        (
            "data_classification",
            '{"sensitivity": "public"}',
        ),
    ),
    ("metadata_json", "scalar"): (
        (
            (
                "(json_extract(metadata_json, ?) = ? AND "
                "json_type(metadata_json, ?) NOT IN ('object', 'array'))"
            ),
        ),
        (
            '$."content_type"',
            "note",
            '$."content_type"',
        ),
    ),
    ("metadata_json", "bool"): (
        (
            (
                "(json_extract(metadata_json, ?) = ? AND "
                "json_type(metadata_json, ?) NOT IN ('object', 'array'))"
            ),
        ),
        (
            '$."flag"',
            True,
            '$."flag"',
        ),
    ),
    ("metadata_json", "list"): (
        ("trellis_metadata_matches(json_extract(metadata_json, '$'), ?, ?)",),
        (
            "tags",
            '["a", "b"]',
        ),
    ),
    ("metadata_json", "none"): (
        ("trellis_metadata_matches(json_extract(metadata_json, '$'), ?, ?)",),
        (
            "owner",
            "null",
        ),
    ),
    ("metadata_json", "content-tags"): (
        (
            (
                "(json_extract(d.metadata_json, ?) IS NULL OR "
                "(json_type(d.metadata_json, ?) = 'array' AND "
                "json_array_length(d.metadata_json, ?) = 0) OR EXISTS (SELECT "
                "1 FROM json_each(d.metadata_json, ?) je WHERE je.value = ?))"
            ),
        ),
        (
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            "x",
        ),
    ),
    ("metadata_json", "quoted-key"): (
        ("trellis_metadata_matches(json_extract(metadata_json, '$'), ?, ?)",),
        (
            'a"b',
            "1",
        ),
    ),
    ("d.metadata_json", "plain-dict"): (
        ("trellis_metadata_matches(json_extract(d.metadata_json, '$'), ?, ?)",),
        (
            "data_classification",
            '{"sensitivity": "public"}',
        ),
    ),
    ("d.metadata_json", "scalar"): (
        (
            (
                "(json_extract(d.metadata_json, ?) = ? AND "
                "json_type(d.metadata_json, ?) NOT IN ('object', 'array'))"
            ),
        ),
        (
            '$."content_type"',
            "note",
            '$."content_type"',
        ),
    ),
    ("d.metadata_json", "bool"): (
        (
            (
                "(json_extract(d.metadata_json, ?) = ? AND "
                "json_type(d.metadata_json, ?) NOT IN ('object', 'array'))"
            ),
        ),
        (
            '$."flag"',
            True,
            '$."flag"',
        ),
    ),
    ("d.metadata_json", "list"): (
        ("trellis_metadata_matches(json_extract(d.metadata_json, '$'), ?, ?)",),
        (
            "tags",
            '["a", "b"]',
        ),
    ),
    ("d.metadata_json", "none"): (
        ("trellis_metadata_matches(json_extract(d.metadata_json, '$'), ?, ?)",),
        (
            "owner",
            "null",
        ),
    ),
    ("d.metadata_json", "content-tags"): (
        (
            (
                "(json_extract(d.metadata_json, ?) IS NULL OR "
                "(json_type(d.metadata_json, ?) = 'array' AND "
                "json_array_length(d.metadata_json, ?) = 0) OR EXISTS (SELECT "
                "1 FROM json_each(d.metadata_json, ?) je WHERE je.value = ?))"
            ),
        ),
        (
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            '$.content_tags."domain"',
            "x",
        ),
    ),
    ("d.metadata_json", "quoted-key"): (
        ("trellis_metadata_matches(json_extract(d.metadata_json, '$'), ?, ?)",),
        (
            'a"b',
            "1",
        ),
    ),
}

PLAIN_FILTERS: dict[str, dict[str, Any]] = {
    "plain-dict": {"data_classification": {"sensitivity": "public"}},
    "scalar": {"content_type": "note"},
    "bool": {"flag": True},
    "list": {"tags": ["a", "b"]},
    "none": {"owner": None},
    "content-tags": {"content_tags": {"domain": {"eq": "x"}}},
    "quoted-key": {'a"b': 1},
}


@pytest.mark.parametrize(("column", "name"), sorted(PLAIN_FILTER_SQL))
class TestThePlainFilterSqlIsUnchanged:
    def test_the_sql_and_parameters_are_the_ones_before_readablematch(
        self, column: str, name: str
    ) -> None:
        conds, params = SQLiteDocumentStore._filter_conditions(
            PLAIN_FILTERS[name], column
        )
        assert (tuple(conds), tuple(params)) == PLAIN_FILTER_SQL[(column, name)]
