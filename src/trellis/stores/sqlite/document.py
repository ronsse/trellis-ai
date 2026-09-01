"""SQLiteDocumentStore — SQLite-backed document store with FTS5."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash as _content_hash
from trellis.core.ids import generate_ulid
from trellis.schemas.classification import LIST_FACETS
from trellis.stores.base.document import (
    DocumentStore,
    chunk_exclusion_clause,
    chunk_id_like_pattern,
    encode_filter_value,
)
from trellis.stores.base.tag_filters import normalize_facet_filter
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


def _build_tag_conditions(
    tag_filters: dict[str, Any],
) -> tuple[list[str], list[Any]]:
    """Build SQL conditions for ``content_tags`` filtering.

    Which facets are lists comes from
    :data:`~trellis.schemas.classification.LIST_FACETS`, the one definition
    every reader shares. This module used to keep its own ``{"domain"}``,
    which silently omitted ``retrieval_affinity`` — a list facet three
    classifiers write. It took the scalar branch, where ``json_extract``
    returns the JSON *text* ``["operational"]`` and ``IN ('operational')``
    is false, so filtering it returned the **inverted set**. A facet added to
    the schema must not need a second edit here to be filterable.

    List facets match if any filter value is in the JSON array; scalar facets
    (``content_type``, ``signal_quality``, ``scope``) match by ``IN (...)``.
    Both wrap in ``IS NULL OR …`` so untagged items pass — mirrors the
    Postgres path's default-pass.

    For a list facet the default-pass also covers an **empty** array:
    ``domain: []`` carries no domain, exactly like a missing key, and
    must pass a domain-scoped query for the same reason. Without this,
    ``EXISTS(json_each(...))`` is false over an empty array and the item
    is *hard-excluded* — and every item the classify-on-write path tags
    stores ``domain: []`` deliberately (see
    :mod:`trellis.classify.ingest`), so tagging a document would have
    silently hidden it from every domain-scoped query.

    Per-facet operators (``in`` / ``not_in`` / ``eq`` / ``ne``) come
    from :func:`normalize_facet_filter` so SQLite and Postgres see
    identical operator semantics. ``not_in`` is the case the new
    shape unlocks: previously callers had to enumerate the inverse
    set (``["high", "standard", "low"]`` to mean "anything but
    noise"), which breaks when a new value is added. Now they can
    spell it directly: ``{"signal_quality": {"not_in": ["noise"]}}``.
    """
    conditions: list[str] = []
    params: list[Any] = []

    for facet, raw_value in tag_filters.items():
        normalized = normalize_facet_filter(raw_value)
        if normalized is None:
            continue
        operator, values = normalized
        # Bound, not interpolated: ``facet`` arrives from wire input, and a
        # JSON path spliced into SQL is an injection surface. SQLite's JSON
        # functions take the path as an ordinary parameter.
        json_path = f"$.content_tags.{facet}"
        if facet in LIST_FACETS:
            sub_parts = " OR ".join("je.value = ?" for _ in values)
            inner = (
                "EXISTS (SELECT 1 FROM json_each(d.metadata_json, ?) je"
                f" WHERE {sub_parts})"
            )
            if operator == "not_in":
                inner = f"NOT {inner}"
            # ``json_array_length`` returns 0 for a *scalar* as well as for
            # an empty array, so the empty-facet default-pass branch has to
            # be guarded on the value actually being an array. Without the
            # guard a scalar-shaped facet (``domain: "engineering"``, a legal
            # shape — ``DocumentMetadata.domain`` is ``list[str] | str |
            # None``) default-passed every query and so could never filter at
            # all. It carries a value; it matches by equality, which is what
            # ``json_each`` over a scalar already yields.
            conditions.append(
                "(json_extract(d.metadata_json, ?) IS NULL"
                " OR (json_type(d.metadata_json, ?) = 'array'"
                " AND json_array_length(d.metadata_json, ?) = 0)"
                f" OR {inner})"
            )
            params.extend([json_path, json_path, json_path, json_path, *values])
        else:
            placeholders = ", ".join("?" for _ in values)
            membership = "NOT IN" if operator == "not_in" else "IN"
            conditions.append(
                "(json_extract(d.metadata_json, ?) IS NULL"
                f" OR json_extract(d.metadata_json, ?) {membership}"
                f" ({placeholders}))"
            )
            params.extend([json_path, json_path, *values])

    return conditions, params


#: Name of the SQL function :func:`_metadata_matches` is registered under.
#: Referenced from the generated SQL rather than typed twice.
_METADATA_MATCH_FN = "trellis_metadata_matches"


def _metadata_matches(metadata_json: str, key: str, expected_json: str) -> bool:
    """SQL callback: does ``metadata[key]`` equal the filter value? (#409)

    Registered on every connection this store opens so that a metadata
    filter of **any** shape is a ``WHERE`` predicate rather than a pass
    over the rows ``LIMIT`` already returned.

    ``search`` used to split filters into SQL-pushable (scalars, bools,
    ``content_tags``) and "complex" (lists, dicts, ``None``), and applied
    the complex half in Python *after* the query. So a filtered search
    returned fewer than ``limit`` rows — possibly zero — while matching
    documents sat just past the ``LIMIT`` window, and the caller could not
    tell "only three matched" from "three of the top twenty matched, and
    there were more further down". That is the defect
    :meth:`DocumentStore.list_documents` names: a predicate has to run
    before ``LIMIT`` for the page to mean anything.

    Comparison is Python's ``==`` over the parsed JSON, which is what the
    post-hoc loop did — so this is a pushdown, not a change to *which*
    documents match. It is also the reason a SQL callback is preferable
    to a hand-written ``json_extract`` comparison per shape: SQLite has no
    structural JSON equality, and ``json_extract`` returns an object as
    *text*, so ``{"a": 1, "b": 2}`` would not match a filter spelling the
    same object as ``{"b": 2, "a": 1}`` — while Postgres' ``jsonb``
    equality (and Python's) says it does. One callback keeps the two
    backends on one semantics instead of a per-shape table that has to be
    kept in sync by hand.

    The caller passes ``json_extract(metadata_json, '$')`` rather than the
    raw column, so *SQLite* rejects a malformed row — with its own
    ``malformed JSON`` message — before this runs. That matters because an
    exception raised inside a SQL callback reaches the caller as the
    opaque ``sqlite3.OperationalError: user-defined function raised
    exception``, with the original message discarded. Parsing in SQL keeps
    the one failure this can realistically hit legible.
    """
    return bool(json.loads(metadata_json).get(key) == json.loads(expected_json))


def _bindable_json_path(key: str) -> str | None:
    r"""``$."key"`` for *key*, or ``None`` if SQLite cannot address it.

    Quoted so a key holding a ``.`` or a ``[`` is read as a literal member
    name rather than as path syntax, and returned to be **bound** rather
    than spliced: the key arrives from wire input, and the
    ``content_tags`` branch already binds its paths for that reason.

    Two character classes have no SQLite JSON-path spelling at all, and both
    parse as a *miss* rather than an error — so a key containing either would
    silently match nothing. Measured against SQLite 3.45:

    * a double quote, because a backslash-escaped quote inside the component
      is not read as a literal quote;
    * a backslash, because inside a quoted component SQLite reads it as the
      start of an escape sequence. For a key spelled ``a\b`` in Python,
      ``json_extract`` under the path ``$."a\b"`` returns ``NULL`` while the
      doubled ``$."a\\b"`` returns the value.

    The backslash case was a live backend divergence, found by the #455
    review gate: Postgres binds the key directly (``metadata -> %s``) and
    matched the same document, inside the change (#409) whose whole point is
    that the two backends cannot disagree about what a filter means. Both
    classes are handed to :func:`_metadata_matches` instead, which compares
    the parsed object in Python and needs no path.
    """
    return None if '"' in key or "\\" in key else f'$."{key}"'


class SQLiteDocumentStore(SQLiteStoreBase, DocumentStore):
    """SQLite-backed document store with FTS5 full-text search.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """The calling thread's connection, with the filter callback bound.

        ``create_function`` is per-connection and the base class opens one
        connection per thread (and reopens after :meth:`close`), so the
        registration is keyed on *this call having opened a new
        connection* rather than on a flag. A flag would survive
        ``close()``, which drops the thread-local connection but cannot
        drop a flag, and the next ``search`` would fail with "no such
        function" on a store that had merely been reopened.
        """
        existing = getattr(self._local, "conn", None)
        conn = super()._get_conn()
        if conn is not existing:
            conn.create_function(
                _METADATA_MATCH_FN, 3, _metadata_matches, deterministic=True
            )
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                doc_id,
                content
            );

            CREATE INDEX IF NOT EXISTS idx_documents_created
                ON documents(created_at);

            CREATE INDEX IF NOT EXISTS idx_documents_hash
                ON documents(content_hash);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def put(
        self,
        doc_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        preserve_updated_at: bool = False,
    ) -> str:
        if doc_id is None:
            doc_id = generate_ulid()

        now = utc_now().isoformat()
        metadata = metadata or {}
        metadata_json = json.dumps(metadata)
        chash = _content_hash(content)

        # `preserve_updated_at` is bound, not spliced: an f-string here would
        # couple the generated SQL to this block's indentation.
        self._conn.execute(
            """
            INSERT INTO documents
                (doc_id, content, content_hash,
                 metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                content = excluded.content,
                content_hash = excluded.content_hash,
                metadata_json = excluded.metadata_json,
                updated_at = CASE WHEN ?
                    THEN documents.updated_at ELSE excluded.updated_at END
            """,
            (doc_id, content, chash, metadata_json, now, now, preserve_updated_at),
        )
        # FTS5 doesn't support ON CONFLICT — delete+insert
        self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        self._conn.execute(
            "INSERT INTO documents_fts (doc_id, content) VALUES (?, ?)",
            (doc_id, content),
        )

        self._conn.commit()
        logger.debug("document_stored", doc_id=doc_id)
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, doc_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("document_deleted", doc_id=doc_id)
        return deleted

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        include_chunks: bool = True,
    ) -> list[dict[str, Any]]:
        sanitized = self._sanitize_fts_query(query)
        if not sanitized:
            return []

        # Every filter shape becomes a WHERE predicate. Nothing is applied
        # to the returned page — see :func:`_metadata_matches` (#409).
        filter_conditions: list[str] = []
        filter_params: list[Any] = []

        if filters:
            for key, value in filters.items():
                if key == "content_tags" and isinstance(value, dict):
                    tag_conds, tag_params = _build_tag_conditions(value)
                    filter_conditions.extend(tag_conds)
                    filter_params.extend(tag_params)
                elif isinstance(value, str | int | float) and (
                    path := _bindable_json_path(key)
                ):
                    # Scalars (``bool`` included — it is an ``int``, and
                    # ``json_extract`` yields 1/0 for a JSON boolean, as
                    # sqlite3 does for a bound one) compare natively, with
                    # SQLite's comparison agreeing with Python's on every
                    # scalar pair the contract pins. Kept out of the
                    # callback because it is the shape the search surfaces
                    # actually pass: on a 5,000-document corpus whose FTS
                    # term matches every row, routing ``?domain=`` through
                    # the callback took a search from 6.6ms to 22.3ms, and
                    # the gap grows with the corpus.
                    #
                    # The ``json_type`` guard is the price of the native
                    # branch. ``json_extract`` returns an object or array as
                    # its *minified text*, so without it a string filter
                    # whose characters happen to equal that text matches the
                    # container — SQLite said yes to
                    # ``{"obj": '{"x":1}'}`` against a stored
                    # ``{"obj": {"x": 1}}`` where Python ``==`` and Postgres
                    # ``jsonb`` both say no. A *false match* is worse than
                    # the silent misses this branch already guards against,
                    # because the caller gets a document back and cannot
                    # tell. ``json_type`` is NULL for an absent key, and
                    # ``NULL NOT IN (...)`` is NULL, so an absent key stays
                    # excluded exactly as the equality already left it.
                    filter_conditions.append(
                        "(json_extract(d.metadata_json, ?) = ?"
                        " AND json_type(d.metadata_json, ?)"
                        " NOT IN ('object', 'array'))"
                    )
                    filter_params.extend([path, value, path])
                else:
                    # Lists, dicts, ``None``, and keys SQLite cannot spell
                    # a path for. SQLite has no structural JSON equality
                    # and ``json_extract`` returns a container as *text*,
                    # so a SQL comparison would call ``{"a": 1, "b": 2}``
                    # and ``{"b": 2, "a": 1}`` unequal — while Python and
                    # Postgres ``jsonb`` call them equal. The callback
                    # computes the equality the contract specifies rather
                    # than an approximation of it, and it takes the whole
                    # object (path ``'$'``), so no key needs escaping.
                    filter_conditions.append(
                        f"{_METADATA_MATCH_FN}"
                        "(json_extract(d.metadata_json, '$'), ?, ?)"
                    )
                    filter_params.extend([key, encode_filter_value(key, value)])

        where_parts = ["documents_fts MATCH ?"]
        sql_params: list[Any] = [sanitized]

        if not include_chunks:
            where_parts.append("d.doc_id NOT LIKE ?")
            sql_params.append(chunk_id_like_pattern())

        if filter_conditions:
            where_parts.extend(filter_conditions)
            sql_params.extend(filter_params)

        sql_params.append(limit)
        where_clause = " AND ".join(where_parts)

        sql = (
            "SELECT d.*, bm25(documents_fts) as rank"
            " FROM documents d"
            " JOIN documents_fts fts ON d.doc_id = fts.doc_id"
            f" WHERE {where_clause}"
            " ORDER BY rank"
            " LIMIT ?"
        )
        cursor = self._conn.execute(sql, sql_params)

        return [self._row_to_dict(row, include_rank=True) for row in cursor.fetchall()]

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize a query string for FTS5 MATCH."""
        if not query or not query.strip():
            return ""

        sanitized = query.replace("\n", " ").replace("\t", " ")
        words = re.findall(r"[a-zA-Z0-9]+", sanitized)
        if not words:
            return ""

        return " OR ".join(f'"{w}"' for w in words[:20])

    # ------------------------------------------------------------------
    # Listing / counting
    # ------------------------------------------------------------------

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        include_chunks: bool = True,
    ) -> list[dict[str, Any]]:
        where, params = chunk_exclusion_clause("?", include_chunks=include_chunks)
        cursor = self._conn.execute(
            f"SELECT * FROM documents{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count(self, *, include_chunks: bool = True) -> int:
        where, params = chunk_exclusion_clause("?", include_chunks=include_chunks)
        cursor = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM documents{where}", params
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row["cnt"])

    # ------------------------------------------------------------------
    # Hash lookup
    # ------------------------------------------------------------------

    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM documents WHERE content_hash = ?", (content_hash,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, *, include_rank: bool = False) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "doc_id": row["doc_id"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_rank:
            doc["rank"] = row["rank"]
        return doc
