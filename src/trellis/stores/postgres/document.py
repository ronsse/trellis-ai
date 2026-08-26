"""PostgresDocumentStore — Postgres-backed document store with tsvector FTS."""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash as _content_hash
from trellis.core.ids import generate_ulid
from trellis.schemas.classification import LIST_FACETS
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.tag_filters import normalize_facet_filter
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(doc_id, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'title', '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'domain', '')), 'B') ||
        setweight(to_tsvector('english', content), 'C')
    ) STORED
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_documents_tsv ON documents USING GIN(tsv)",
]


#: An OR-semantics ``tsquery`` over exactly the lexemes ``plainto_tsquery``
#: produces — same stemming, same stopword removal, ``|`` instead of ``&``.
#:
#: ``plainto_tsquery`` ANDs every term, so a natural-language intent had to
#: appear *in full* in one document. Measured against the production corpus,
#: ``"implement the classify layer tagging pipeline"`` matched 0 documents
#: under AND and 267 under OR: recall fell toward zero as the intent got more
#: specific, which is backwards, and the keyword axis is the only axis that
#: reads ``content_tags`` — so tag filtering went dark exactly when the query
#: was most specific. SQLite has always OR-ed (``_sanitize_fts_query`` joins
#: with ``OR``), so this was also a silent divergence between the two
#: backends, with every test written against the SQLite one.
#:
#: Results stay ordered by ``ts_rank`` and capped by ``LIMIT``, so a loose
#: match costs rank rather than precision. The string transform is safe
#: because ``plainto_tsquery`` emits only ``&`` — phrase operators come from
#: ``phraseto_tsquery``, which is not used here.
_OR_TSQUERY = "replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery"


class PostgresDocumentStore(PostgresStoreBase, DocumentStore):
    """Postgres-backed document store with tsvector full-text search."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
            # Migrate existing tables: upgrade tsv column to include metadata
            self._migrate_tsv_weights(cur)

    def _migrate_tsv_weights(self, cur: Any) -> None:
        """Upgrade tsv column to weighted search if it only indexes content.

        Safe to call repeatedly — checks the column definition before altering.
        """
        cur.execute(
            """
            SELECT pg_get_expr(adbin, adrelid)
            FROM pg_attrdef
            JOIN pg_attribute ON attrelid = adrelid AND attnum = adnum
            WHERE attrelid = 'documents'::regclass AND attname = 'tsv'
            """
        )
        row = cur.fetchone()
        if row and "setweight" not in str(row[0]):
            logger.info(
                "Upgrading documents.tsv to weighted search"
                " (doc_id + title + domain + content)"
            )
            cur.execute("ALTER TABLE documents DROP COLUMN tsv")
            cur.execute("""
                ALTER TABLE documents ADD COLUMN tsv tsvector
                GENERATED ALWAYS AS (
                    setweight(to_tsvector('english',
                        coalesce(doc_id, '')), 'A') ||
                    setweight(to_tsvector('english',
                        coalesce(metadata->>'title', '')), 'A') ||
                    setweight(to_tsvector('english',
                        coalesce(metadata->>'domain', '')), 'B') ||
                    setweight(to_tsvector('english', content), 'C')
                ) STORED
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_tsv"
                " ON documents USING GIN(tsv)"
            )

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

        now = utc_now()
        metadata = metadata or {}
        metadata_json = json.dumps(metadata)
        chash = _content_hash(content)

        # `preserve_updated_at` is bound, not spliced: an f-string here would
        # couple the generated SQL to this block's indentation. Mirrors the
        # SQLite backend so both honour the same contract test.
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (doc_id, content, content_hash, metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    metadata = EXCLUDED.metadata,
                    updated_at = CASE WHEN %s
                        THEN documents.updated_at ELSE EXCLUDED.updated_at END
                """,
                (
                    doc_id,
                    content,
                    chash,
                    metadata_json,
                    now,
                    now,
                    preserve_updated_at,
                ),
            )
        logger.debug("document_stored", doc_id=doc_id)
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents WHERE doc_id = %s
                """,
                (doc_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, doc_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,))
            deleted = bool(cur.rowcount > 0)
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
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []

        conditions = [f"tsv @@ {_OR_TSQUERY}"]
        params: list[Any] = [query]

        if filters:
            for key, value in filters.items():
                if key == "content_tags" and isinstance(value, dict):
                    # Mirror the SQLite path's ``content_tags`` semantics
                    # so PackBuilder's ``signal_quality`` allowlist (the
                    # default ``[high, standard, low]`` filter that
                    # excludes noise) actually filters here. Without
                    # this, ``apply_noise_tags`` updates a doc's
                    # metadata but the next pack still includes it.
                    #
                    # Operator parsing comes from the shared
                    # :func:`normalize_facet_filter` so SQLite and
                    # Postgres see identical semantics. Adds first-class
                    # ``not_in`` so callers can spell "anything but
                    # noise" directly.
                    for facet, raw in value.items():
                        normalized = normalize_facet_filter(raw)
                        if normalized is None:
                            continue
                        operator, values_list = normalized
                        placeholders = ", ".join(["%s"] * len(values_list))
                        membership = "NOT IN" if operator == "not_in" else "IN"
                        # ``metadata -> 'content_tags' ->> facet`` reads
                        # the JSON path as text. NULL (facet missing)
                        # is falsy under ``IN (...)`` and would exclude
                        # un-tagged items, so an explicit IS NULL OR
                        # branch keeps default-pass semantics — items
                        # without the facet tag are kept regardless of
                        # whether the operator is in or not_in.
                        #
                        # An *empty* list facet (``domain: []``) carries
                        # no value either, but reads as the text '[]',
                        # so it needs its own default-pass branch or the
                        # item is hard-excluded. classify-on-write stores
                        # exactly that shape (see trellis.classify.ingest),
                        # which would otherwise make every tagged document
                        # invisible to domain-scoped queries. Mirrors the
                        # SQLite path's json_array_length branch.
                        if facet in LIST_FACETS:
                            # A list facet stored as ``["finance"]`` reads
                            # through ``->>`` as the *text* '["finance"]',
                            # which matches no value — so before this branch
                            # existed all three conditions were false and a
                            # correctly tagged document was hard-excluded
                            # from its own domain query. On the deployed
                            # Postgres backend, that was every tagged
                            # document. Mirrors the SQLite ``json_each`` path.
                            #
                            # The CASE is not defensive padding: the stored
                            # shape is genuinely either
                            # (``DocumentMetadata.domain`` is
                            # ``list[str] | str | None``), and
                            # ``jsonb_array_elements_text`` *raises* on a
                            # scalar rather than returning nothing — an error
                            # that would take down the whole query, not just
                            # mis-filter it. Wrapping a scalar into a
                            # one-element array makes both shapes take one
                            # path, which is what SQLite's ``json_each``
                            # already does.
                            exists = (
                                "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                                "CASE WHEN jsonb_typeof("
                                "metadata -> 'content_tags' -> %s) = 'array' "
                                "THEN metadata -> 'content_tags' -> %s "
                                "ELSE jsonb_build_array("
                                "metadata -> 'content_tags' -> %s) END"
                                f") AS v WHERE v IN ({placeholders}))"
                            )
                            if operator == "not_in":
                                exists = f"NOT {exists}"
                            conditions.append(
                                "(metadata -> 'content_tags' -> %s IS NULL "
                                "OR metadata -> 'content_tags' -> %s "
                                "= '[]'::jsonb "
                                f"OR {exists})"
                            )
                            params.extend(
                                [facet, facet, facet, facet, facet, *values_list]
                            )
                        else:
                            conditions.append(
                                "(metadata -> 'content_tags' ->> %s IS NULL "
                                "OR metadata -> 'content_tags' -> %s "
                                "= '[]'::jsonb "
                                f"OR metadata -> 'content_tags' ->> %s "
                                f"{membership} ({placeholders}))"
                            )
                            params.extend([facet, facet, facet, *values_list])
                elif isinstance(value, str | int | float | bool):
                    conditions.append("metadata->>%s = %s")
                    params.extend([key, str(value)])

        where_clause = " AND ".join(conditions)
        params.append(limit)

        rank_query = _OR_TSQUERY
        sql = f"""
            SELECT doc_id, content, content_hash, metadata,
                   created_at, updated_at,
                   ts_rank(tsv, {rank_query}) AS rank
            FROM documents
            WHERE {where_clause}
            ORDER BY rank DESC
            LIMIT %s
        """
        # The first %s in the SELECT is the ranking query param
        all_params: list[Any] = [query, *params]

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, all_params)
            rows = cur.fetchall()

        return [self._row_to_dict(row, include_rank=True) for row in rows]

    # ------------------------------------------------------------------
    # Listing / counting
    # ------------------------------------------------------------------

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count(self) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM documents")
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # Hash lookup
    # ------------------------------------------------------------------

    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents WHERE content_hash = %s
                """,
                (content_hash,),
            )
            row = cur.fetchone()
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
    def _row_to_dict(
        row: tuple[Any, ...],
        *,
        include_rank: bool = False,
    ) -> dict[str, Any]:
        metadata_raw = row[3]
        if isinstance(metadata_raw, str):
            metadata = json.loads(metadata_raw)
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            metadata = {}

        created = row[4]
        updated = row[5]
        doc: dict[str, Any] = {
            "doc_id": row[0],
            "content": row[1],
            "content_hash": row[2],
            "metadata": metadata,
            "created_at": (
                created.isoformat() if hasattr(created, "isoformat") else created
            ),
            "updated_at": (
                updated.isoformat() if hasattr(updated, "isoformat") else updated
            ),
        }
        if include_rank:
            doc["rank"] = row[6]
        return doc
