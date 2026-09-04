"""VectorStore — abstract interface for vector storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


def format_vector_literal(vec: list[float], *, separator: str = ",") -> str:
    """Render a list of floats as a SQL list literal ``"[v1, v2, ...]"``.

    Used by backends whose typed-vector columns reject parameter-bound
    Python lists (pgvector's ``::vector`` cast wants a literal;
    ArcadeDB's ``LIST OF FLOAT`` rejects bound ``ARRAY_OF_FLOATS``).
    ``repr(float(x))`` is injection-safe — only numeric literals end
    up in the SQL.
    """
    return "[" + separator.join(repr(float(x)) for x in vec) + "]"


def as_float_list(value: Any) -> list[float]:
    """Normalise whatever a driver hands back for a vector column.

    The inverse of :func:`format_vector_literal`, and it lives here for the
    same reason: the shape is a *driver* concern, not a backend one, and
    this module has no third-party imports so the behaviour stays testable
    without the optional postgres extras installed.

    The shape genuinely varies. ``pgvector`` is not pinned and changed it
    between releases — 0.4.x returns a plain ``list``, 0.5.0 returns a
    ``pgvector.Vector`` implementing neither ``__iter__`` nor ``__len__``,
    so a bare ``list(value)`` raises ``TypeError: 'Vector' object is not
    iterable``. That split was live and asymmetric: a host install on 0.4.2
    worked while containers rebuilt onto 0.5.0 raised, which reads as
    "works on my machine" because neither environment is wrong on its own.

    ``to_list()`` is the documented accessor and is tried first; a real list
    passes through; anything else falls back to iteration so a future driver
    shape still works.
    """
    to_list = getattr(value, "to_list", None)
    if callable(to_list):
        return [float(x) for x in to_list()]
    return [float(x) for x in value]


class VectorStore(ABC):
    """Abstract interface for vector storage.

    Stores embedding vectors with metadata and supports
    similarity search via cosine distance.
    """

    # ------------------------------------------------------------------
    # Declarations — what a backend says about itself
    # ------------------------------------------------------------------
    #
    # Both members below exist because a caller outside this package was
    # deciding these two facts *for* the backend by looking for private
    # attributes on it (#511, #512). ``POST /api/v1/vectors/reset`` read
    # ``getattr(store, "_dimensions", None)`` and probed for ``_pool`` /
    # ``_conn``. Neither fact was declared anywhere, so a backend that
    # pinned a width under some other spelling had *"backend declares no
    # fixed dimensionality"* published about it, as a fact, with no error
    # — and renaming a private attribute on any backend silently changed
    # an API response. What is asked of a backend here is that it answer,
    # not that a reader guess.

    @property
    @abstractmethod
    def dimensions(self) -> int | None:
        """The embedding width every vector in this store must have.

        ``None`` is a real answer and not a fallback: it means the
        backend pins **no** width at storage level. That is the truth for
        :class:`~trellis.stores.sqlite.vector.SQLiteVectorStore`, which
        keeps a ``dimensions`` column per row and will store a 3-wide and
        a 1536-wide vector side by side.

        This is **abstract on purpose**. A concrete default returning
        ``None`` would relocate #512 rather than fix it: a new backend
        that pins a width and forgets to override would still have "no
        fixed dimensionality" said on its behalf, silently and
        plausibly. Abstract means a backend that has not answered cannot
        be instantiated at all.

        It is a **declaration, not a measurement**. Implementations
        return a value fixed for the lifetime of the store: no I/O, no
        dependence on what has been written, and it does not raise.
        Callers report it beside destructive work, so a property that
        could fail would put an avoidable failure next to an operation
        that cannot be undone. ``VectorStoreContractTests`` pins the
        stability and pins the declaration against the widths the store
        actually accepts.
        """

    @classmethod
    def supports_reset(cls) -> bool:
        """Whether this backend implements :meth:`reset_storage`.

        **Derived from the override, never declared separately.** A
        ``supports_reset`` flag a backend sets by hand is a second fact
        that can disagree with the first: a backend could claim support
        and not implement, or implement and be refused. Here the answer
        *is* the implementation, so the question "can this be reset?" and
        the code that does the resetting cannot come apart.

        This is a classmethod and reads only the type, so asking it costs
        nothing and touches no instance state. That matters at its call
        site: the caller asks before it acts, and
        ``SQLiteStoreBase._conn`` — what the old probe reached for — is a
        *property that opens a connection*, so the shape question used to
        do I/O and could raise ``sqlite3.DatabaseError`` out of a probe
        whose whole job was to decide whether anything should happen yet.
        """
        return cls.reset_storage is not VectorStore.reset_storage

    def reset_storage(self) -> None:
        """Drop this store's backing storage and recreate it empty.

        **Destructive and total**: every vector in the store is gone
        afterwards, and the store is usable again immediately (a caller
        may ``upsert`` into it without reconstructing anything).

        The default implementation refuses, which is the honest answer
        for a backend that keeps no storage of its own —
        ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` hold embeddings
        as properties on the graph store's ``(:Node)`` rows, so there is
        nothing here to drop and dropping the nodes is the graph store's
        business, not this one's. Overriding is how a backend says it can
        do this; see :meth:`supports_reset`.

        Callers that must not touch a store they cannot reset ask
        :meth:`supports_reset` **first**. Implementations may still fail
        part-way — a ``DROP`` that succeeded followed by a recreate that
        did not leaves the store empty and unusable — so a raise from
        here is *not* a promise that nothing changed.

        Raises:
            NotImplementedError: when the backend does not implement it.
        """
        # Says only what is true of every non-overriding backend — that
        # there is no route through this interface — rather than the
        # ArcadeDB/Neo4j *reason* for it, which would be invented about
        # any other backend that simply has not implemented it. Stating a
        # borrowed reason as a fact is #512 one level down.
        msg = (
            f"{type(self).__name__} does not implement reset_storage(), so "
            "there is no way to drop and recreate its storage through this "
            "interface. Ask supports_reset() before calling."
        )
        raise NotImplementedError(msg)

    @abstractmethod
    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store or update a vector with optional metadata."""

    @abstractmethod
    def upsert_bulk(
        self,
        items: list[dict[str, Any]],
    ) -> None:
        """Bulk variant of :meth:`upsert`.

        Each entry in ``items`` is a dict with the same fields the
        single-row method accepts:

        - ``item_id`` (``str``, required).
        - ``vector`` (``list[float]``, required).
        - ``metadata`` (``dict | None``, optional; defaults to ``{}``).

        Semantics match :meth:`upsert` per row: existing vectors with
        the same ``item_id`` are replaced; metadata is overwritten.
        Within-batch duplicate ``item_id`` values are **rejected** — see
        :meth:`_pre_validate_bulk_item_ids`. Sequential per-row calls
        with the same ``item_id`` would have collapsed (last-write-wins
        deterministically); bulk paths can't make the same guarantee
        across all backends (Neo4j's UNWIND fires SET twice
        non-deterministically; other backends' merge-style writes are
        similarly implementation-defined). Reject up-front rather than
        ship divergent semantics.

        On backends with network round-trip cost (Neo4j),
        implementations SHOULD consolidate the work into a small
        constant number of round trips per batch — typically one
        UNWIND-style write. On in-process backends a simple loop over
        :meth:`upsert` is acceptable.

        Raises:
            ValueError: with the offending list index when a row's
                vector dimensions don't match the store's configured
                dimensions, when a required field is missing, or when
                two rows in the batch share an ``item_id``.
        """

    @staticmethod
    def _validate_bulk_required_keys(
        items: list[dict[str, Any]],
        required_keys: tuple[str, ...],
        method_name: str,
    ) -> None:
        """Raise ``ValueError`` on the first row missing any required key.

        A key is "missing" if it's absent OR present with ``None`` —
        bulk callers must ship every required field with a real value
        since downstream writes have NOT NULL constraints. Errors are
        tagged with the offending row index so callers can map them
        back to input.
        """
        for i, spec in enumerate(items):
            for key in required_keys:
                if key not in spec or spec[key] is None:
                    msg = f"{method_name}[{i}]: missing required key {key!r}"
                    raise ValueError(msg)

    def _pre_validate_bulk_item_ids(self, items: list[dict[str, Any]]) -> None:
        """Reject duplicate ``item_id`` values in a bulk batch.

        Implementations call this from :meth:`upsert_bulk` before any
        write so divergent within-batch-duplicate behavior across
        backends doesn't surface as a silent correctness gap. Errors
        are tagged with the offending row index (the second occurrence)
        so callers can map them back to input.
        """
        seen: set[str] = set()
        for i, spec in enumerate(items):
            item_id = spec.get("item_id")
            if item_id is None:
                continue
            if item_id in seen:
                msg = (
                    f"upsert_bulk[{i}]: duplicate item_id {item_id!r} in batch; "
                    "deduplicate before calling — last-write-wins is "
                    "non-deterministic across backends"
                )
                raise ValueError(msg)
            seen.add(item_id)

    @abstractmethod
    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Find similar vectors.

        Returns:
            List of ``{item_id, score, metadata}`` sorted by score descending.
        """

    @abstractmethod
    def get(self, item_id: str) -> dict[str, Any] | None:
        """Get a vector by ID.

        Returns:
            ``{item_id, vector, dimensions, metadata}`` or ``None``.
        """

    @abstractmethod
    def delete(self, item_id: str) -> bool:
        """Delete a vector. Returns ``True`` if it existed."""

    @abstractmethod
    def count(self) -> int:
        """Return the total number of stored vectors."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
