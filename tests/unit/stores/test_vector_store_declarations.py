"""#512 — a vector backend declares its own width and its own resettability.

Two facts about a backend were being decided *for* it, from outside the
package, by looking for private attributes: ``getattr(store,
"_dimensions", None)`` for the embedding width and a probe for ``_pool``
/ ``_conn`` for whether ``POST /api/v1/vectors/reset`` could drive it.
Both failed as **false statements rather than as errors** — a backend
that pinned a width under a different spelling had *"backend declares no
fixed dimensionality"* published about it as a fact, and renaming a
private attribute on any backend silently changed an API response.

Two kinds of test live here, and the second is the one that matters.

*Semantics of the declarations themselves*: that a backend cannot stay
silent about its width, and that ``supports_reset()`` is derived from the
``reset_storage`` override rather than declared beside it.

*That the new contract cases bind*. A contract case is only worth its
line count if some plausible store **fails** it, so each new case in
``VectorStoreContractTests`` is run here against a truthful store and
against a store that lies in exactly the way the case exists to catch.
Every default-selection backend is SQLite — which declares ``None`` — so
without these the pinned-width half of the contract would never execute
outside ``live-infra``'s pgvector job.
"""

from __future__ import annotations

import inspect
import math
import sys
from typing import Any

import pytest

from tests.unit.stores.contracts.vector_store_contract import (
    DIMS,
    VectorStoreContractTests,
)
from trellis.stores.base.vector import VectorStore

# The new cases, named once. Referenced by the binding tests below and
# asserted to still exist, so renaming a case cannot quietly retire the
# evidence that it binds.
_SHAPE_CASE = "test_declares_an_embedding_width"
_WIDTH_CASE = "test_declared_width_matches_the_widths_actually_accepted"
_STABILITY_CASE = "test_dimensions_is_a_declaration_not_a_measurement"
_RESET_CASE = "test_reset_storage_matches_supports_reset"

_DECLARATION_CASES = (_SHAPE_CASE, _WIDTH_CASE, _STABILITY_CASE, _RESET_CASE)


def _vec(x: float, y: float, z: float) -> list[float]:
    return [x, y, z]


def _run_case(name: str, store: VectorStore) -> bool:
    """Run one contract case against ``store``; ``True`` if it passed.

    A case can fail by ``assert`` (``AssertionError``), by a
    ``pytest.raises`` block that saw nothing (``pytest.fail.Exception``,
    which derives from ``BaseException`` and so is not caught by a plain
    ``except Exception``), or by the store raising where the case did not
    expect it. All three are "this store does not satisfy the contract",
    which is the only distinction these tests need.
    """
    case = getattr(VectorStoreContractTests(), name)
    try:
        case(store)
    except (Exception, pytest.fail.Exception):
        return False
    return True


def _module_vector_stores() -> list[type[VectorStore]]:
    """Every concrete ``VectorStore`` subclass defined in this module.

    Derived rather than listed so the biconditional below cannot be
    satisfied by leaving a class off it. Scoped to *this* module by
    ``__module__``, which is what keeps a shipped backend — whose
    ``reset_storage`` really drops a table — out of a sweep that
    constructs each class and calls it. Abstract classes are excluded
    because they cannot be constructed at all, which is a different
    test's subject.
    """
    module = sys.modules[__name__]
    return [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, VectorStore)
        and obj is not VectorStore
        and obj.__module__ == __name__
        and not inspect.isabstract(obj)
    ]


# ---------------------------------------------------------------------------
#  In-memory stores, honest and otherwise
# ---------------------------------------------------------------------------


class _MemoryVectorStore(VectorStore):
    """A truthful minimal backend. Subclasses vary one thing each.

    ``_declared`` is a class attribute rather than a constructor
    argument so a liar can be a two-line subclass, and so no fixture
    shares an implementation with the thing under test.
    """

    _declared: int | None = None

    def __init__(self) -> None:
        self._rows: dict[str, tuple[list[float], dict[str, Any]]] = {}

    @property
    def dimensions(self) -> int | None:
        return self._declared

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._check_width(vector)
        self._rows[item_id] = (list(vector), dict(metadata or {}))

    def _check_width(self, vector: list[float]) -> None:
        if self._declared is not None and len(vector) != self._declared:
            msg = (
                f"vector has {len(vector)} dimensions but store was "
                f"configured for {self._declared}"
            )
            raise ValueError(msg)

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        self._validate_bulk_required_keys(items, ("item_id", "vector"), "upsert_bulk")
        self._pre_validate_bulk_item_ids(items)
        for spec in items:
            self.upsert(spec["item_id"], spec["vector"], spec.get("metadata"))

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        def cosine(other: list[float]) -> float:
            if len(other) != len(vector):
                return -1.0
            dot = sum(a * b for a, b in zip(vector, other, strict=True))
            norms = math.dist(vector, [0.0] * len(vector)) * math.dist(
                other, [0.0] * len(other)
            )
            return dot / norms if norms else 0.0

        scored = [
            {"item_id": item_id, "score": cosine(vec), "metadata": meta}
            for item_id, (vec, meta) in self._rows.items()
            if not filters or all(meta.get(k) == v for k, v in filters.items())
        ]
        scored.sort(key=lambda row: row["score"], reverse=True)
        return scored[:top_k]

    def get(self, item_id: str) -> dict[str, Any] | None:
        row = self._rows.get(item_id)
        if row is None:
            return None
        vec, meta = row
        return {
            "item_id": item_id,
            "vector": list(vec),
            "dimensions": len(vec),
            "metadata": dict(meta),
        }

    def delete(self, item_id: str) -> bool:
        return self._rows.pop(item_id, None) is not None

    def count(self) -> int:
        return len(self._rows)

    def close(self) -> None:
        return None


class _UnpinnedStore(_MemoryVectorStore):
    """Truthful: declares no width and accepts any (the SQLite shape)."""


class _PinnedStore(_MemoryVectorStore):
    """Truthful: declares ``DIMS`` and refuses anything else (pgvector's)."""

    _declared = DIMS


class _SilentlyPinnedStore(_MemoryVectorStore):
    """The #512 defect itself: pins a width, declares ``None``.

    This is the backend the old ``getattr(store, "_dimensions", None)``
    read produced a false statement about — it enforces a width and had
    "declares no fixed dimensionality" said on its behalf.
    """

    _declared = None

    def _check_width(self, vector: list[float]) -> None:
        if len(vector) != DIMS:
            msg = f"vector has {len(vector)} dimensions but store wants {DIMS}"
            raise ValueError(msg)


class _OverdeclaringStore(_MemoryVectorStore):
    """Declares ``DIMS`` and stores whatever it is handed."""

    _declared = DIMS

    def _check_width(self, vector: list[float]) -> None:
        return None


class _MisdeclaringStore(_MemoryVectorStore):
    """Declares a width, enforces a *different* one.

    The wrong-positive-value liar. ``_SilentlyPinnedStore`` and
    ``_OverdeclaringStore`` both get the ``None`` / not-``None`` split
    wrong; this one gets the split right and the number wrong, which is
    the only thing ``declared == DIMS`` and ``row["dimensions"] ==
    declared`` can catch.
    """

    _declared = DIMS + 2

    def _check_width(self, vector: list[float]) -> None:
        if len(vector) != DIMS:
            msg = f"vector has {len(vector)} dimensions but store wants {DIMS}"
            raise ValueError(msg)


class _DriftingStore(_MemoryVectorStore):
    """Answers with a measurement of the last write rather than a declaration."""

    @property
    def dimensions(self) -> int | None:
        if not self._rows:
            return None
        return len(next(reversed(self._rows.values()))[0])


class _ResettableStore(_MemoryVectorStore):
    """Truthful: implements ``reset_storage`` and really empties itself."""

    def reset_storage(self) -> None:
        self._rows = {}


class _PretendResetStore(_MemoryVectorStore):
    """Overrides ``reset_storage`` and does nothing, so it declares support."""

    def reset_storage(self) -> None:
        return None


class _DropOnlyResetStore(_MemoryVectorStore):
    """Empties itself and leaves nothing to write into again.

    The realistic half-failure for the two SQL backends: the ``DROP``
    runs and ``_init_schema`` does not, so the store is empty *and*
    unusable. ``count() == 0`` alone cannot see it — "recreated, not
    merely dropped" is the assertion that can.
    """

    def reset_storage(self) -> None:
        self._rows = {}
        self._dropped = True

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if getattr(self, "_dropped", False):
            msg = "no such table: vectors"
            raise RuntimeError(msg)
        super().upsert(item_id, vector, metadata)


class _RebindsTheBaseStore(_MemoryVectorStore):
    """Names ``reset_storage`` in its body without implementing it.

    The one way a class can look like it overrides and not: the attribute
    is present in ``__dict__`` but is the base function, so it refuses
    when called. An identity check against the base sees through it; a
    ``"reset_storage" in cls.__dict__`` check would not.
    """

    reset_storage = VectorStore.reset_storage


class _UndeclaredWidthStore(VectorStore):
    """Implements everything except ``dimensions``. Must be unconstructible."""

    upsert = _MemoryVectorStore.upsert
    upsert_bulk = _MemoryVectorStore.upsert_bulk
    query = _MemoryVectorStore.query
    get = _MemoryVectorStore.get
    delete = _MemoryVectorStore.delete
    count = _MemoryVectorStore.count
    close = _MemoryVectorStore.close


# ---------------------------------------------------------------------------
#  The declarations themselves
# ---------------------------------------------------------------------------


class TestDimensionsIsAbstract:
    """A backend cannot fail to declare and get an answer made for it.

    This is the whole of #512 in one property, and it is why the ABC
    member is abstract rather than a concrete default returning ``None``.
    A default would have *relocated* the defect: a new backend that pins a
    width and forgets to override would still publish "no fixed
    dimensionality" about itself, silently and plausibly — the same false
    statement, now sanctioned by the abstraction instead of guessed at by
    a route.
    """

    def test_a_backend_that_declares_nothing_cannot_be_instantiated(self):
        with pytest.raises(TypeError, match="dimensions"):
            _UndeclaredWidthStore()  # type: ignore[abstract]

    def test_not_even_by_skipping_init(self):
        """``object.__new__`` is where the abstract check lives, so it
        catches the spelling that skips ``__init__`` too — which is the
        one a registry or a test helper reaches for."""
        with pytest.raises(TypeError, match="abstract"):
            object.__new__(_UndeclaredWidthStore)

    def test_declaring_it_is_enough_to_become_constructible(self):
        """The floor against a vacuous pass above: the two tests would
        also pass if ``VectorStore`` were unconstructible for some
        unrelated reason, so pin that the *only* thing missing was the
        declaration."""

        class _Declared(_UndeclaredWidthStore):
            @property
            def dimensions(self) -> int | None:
                return 7

        assert _Declared().dimensions == 7


class TestSupportsResetIsDerivedFromTheOverride:
    """The reset capability is the implementation, not a flag beside it.

    #520 established that "is this supported?" and "which code runs?"
    must be one decision — a mutant reintroducing a second, independent
    check survived its entire 7,388-test suite, because the two spellings
    agree for every shipped backend. #512 removes the possibility rather
    than testing around it: there is no dispatch left, and the capability
    answer *is* ``reset_storage``'s identity.
    """

    def test_a_backend_that_does_not_override_declines(self):
        assert _MemoryVectorStore.supports_reset() is False
        with pytest.raises(NotImplementedError, match="reset_storage"):
            _MemoryVectorStore().reset_storage()

    def test_a_backend_that_overrides_declares_support(self):
        assert _ResettableStore.supports_reset() is True

    def test_inheriting_an_override_declares_support(self):
        """A backend built on another backend does not have to re-declare."""

        class _Child(_ResettableStore):
            pass

        assert _Child.supports_reset() is True

    def test_naming_the_base_implementation_is_not_an_override(self):
        """The identity check, attacked with the shape that defeats the
        obvious ``"reset_storage" in cls.__dict__`` alternative."""
        assert "reset_storage" in _RebindsTheBaseStore.__dict__
        assert _RebindsTheBaseStore.supports_reset() is False
        with pytest.raises(NotImplementedError):
            _RebindsTheBaseStore().reset_storage()

    def test_declining_and_raising_cannot_disagree(self):
        """The invariant, over every class in this module: declining is
        exactly raising the base refusal.

        Asserted as a biconditional rather than a table, so a new class
        cannot be added to one side only. The right-hand side is the
        store **called**, not ``cls.reset_storage is
        VectorStore.reset_storage`` — that spelling is the body of
        ``supports_reset`` itself, so a biconditional built on it is a
        tautology that restates the implementation instead of checking
        it against behaviour.

        The roster is **derived**, not hand-written, which is what makes
        the first sentence true and the second one hold. A hand-written
        list named six of this module's twelve stores, so the four liars
        and the drop-only store were outside the invariant they exist to
        exercise and a new class could be added to the module and to
        neither side — the failure the sentence promises cannot happen.
        Every concrete ``VectorStore`` subclass *defined here* is swept;
        the derivation is scoped to this module deliberately, so a real
        backend can never be swept into a test that constructs it and
        calls ``reset_storage()`` on it.

        The residual, stated because the identity rule cannot see it: an
        override that *delegates* to the base
        (``def reset_storage(self): return super().reset_storage()``)
        declares support and then refuses. That is out of contract and
        ``VectorStoreContractTests.test_reset_storage_matches_supports_reset``
        is what catches it — by calling the thing — which is why the
        contract case exists beside this one rather than instead of it.
        """
        classes = _module_vector_stores()
        # Floor against a derivation that silently collects nothing, and
        # against one that quietly drops the shapes the module was
        # written to carry. Both spellings of "does not override" have to
        # be in it, or the biconditional is only being asked about
        # overriders.
        assert len(classes) >= 10, classes
        assert _MemoryVectorStore in classes
        assert _RebindsTheBaseStore in classes
        assert _ResettableStore in classes
        # ...and the one class that legitimately cannot be swept is
        # excluded for the stated reason rather than by omission: it is
        # abstract, so constructing it is the ``TypeError`` that
        # ``TestDimensionsIsAbstract`` asserts, not a verdict about
        # ``reset_storage``.
        assert _UndeclaredWidthStore not in classes
        assert inspect.isabstract(_UndeclaredWidthStore)

        for cls in classes:
            declines = not cls.supports_reset()
            try:
                cls().reset_storage()
            except NotImplementedError:
                raises = True
            else:
                raises = False
            assert declines == raises, cls
        # Floor: both sides of the biconditional are actually exercised,
        # so it is not satisfied by every class answering the same way.
        verdicts = {cls.supports_reset() for cls in classes}
        assert verdicts == {True, False}

    def test_asking_touches_no_instance_state(self):
        """It is a classmethod, so the question costs nothing and does no
        I/O — which is what makes it safe above the ``try`` in the route.

        The old probe's hazard was concrete: ``SQLiteStoreBase._conn`` is
        a *property that opens a connection*, so ``hasattr(store,
        "_conn")`` answered a question about shape by touching the disk
        and could raise ``sqlite3.DatabaseError`` out of a decision about
        whether anything should happen yet.
        """

        class _AngryStore(_ResettableStore):
            def __getattribute__(self, name: str) -> Any:
                msg = "no instance attribute may be read to classify me"
                raise AssertionError(msg)

        assert _AngryStore.supports_reset() is True
        assert type(_AngryStore.__new__(_AngryStore)).supports_reset() is True


# ---------------------------------------------------------------------------
#  Do the new contract cases bind?
# ---------------------------------------------------------------------------


class TestTheContractCasesBind:
    """Each new contract case, run against a truthful store and a liar.

    A contract case that no plausible store fails is decoration. These
    also supply the pinned-width coverage the default selection cannot:
    every backend in it is SQLite, which declares ``None``, so the
    ``declared is not None`` half of the width case would otherwise run
    only in ``live-infra``'s pgvector job.
    """

    def test_the_named_cases_all_exist(self):
        """The roster is hand-written, so pin it back to the suite."""
        for name in _DECLARATION_CASES:
            assert hasattr(VectorStoreContractTests, name), name

    @pytest.mark.parametrize("case", _DECLARATION_CASES)
    def test_a_truthful_unpinned_backend_passes_every_case(self, case):
        assert _run_case(case, _UnpinnedStore()) is True

    @pytest.mark.parametrize("case", _DECLARATION_CASES)
    def test_a_truthful_pinned_backend_passes_every_case(self, case):
        assert _run_case(case, _PinnedStore()) is True

    @pytest.mark.parametrize("case", _DECLARATION_CASES)
    def test_a_truthful_resettable_backend_passes_every_case(self, case):
        assert _run_case(case, _ResettableStore()) is True

    def test_a_backend_that_pins_a_width_and_declares_none_is_caught(self):
        """The #512 shape exactly: the store enforces ``DIMS`` and says
        it enforces nothing. Only the width case can see it, and it does."""
        store = _SilentlyPinnedStore()
        assert _run_case(_WIDTH_CASE, store) is False
        # And it is caught for the right reason — the declaration is
        # syntactically fine, so the cheap case cannot tell.
        assert _run_case(_SHAPE_CASE, store) is True

    def test_a_backend_that_declares_a_width_it_does_not_enforce_is_caught(self):
        """The mirror image, which a one-sided case would miss."""
        store = _OverdeclaringStore()
        assert _run_case(_WIDTH_CASE, store) is False
        assert _run_case(_SHAPE_CASE, store) is True

    def test_a_measured_width_is_caught_as_not_a_declaration(self):
        """A store answering from its rows passes the width case on an
        empty-then-3-wide store and still fails, because the stability
        case is separate."""
        store = _DriftingStore()
        assert _run_case(_STABILITY_CASE, store) is False

    def test_a_reset_that_does_not_reset_is_caught(self):
        """Declaring support by overriding is cheap; the contract makes
        the override prove it emptied the store."""
        assert _run_case(_RESET_CASE, _PretendResetStore()) is False

    def test_a_reset_that_drops_without_recreating_is_caught(self):
        """The realistic half-failure, which ``count() == 0`` cannot see.

        A backend whose ``DROP`` ran and whose recreate did not is empty
        and unusable. That is why the case writes into the store *after*
        resetting instead of stopping at the count — a store can satisfy
        "there is nothing in it" by being broken.
        """
        assert _run_case(_RESET_CASE, _DropOnlyResetStore()) is False

    def test_a_backend_declaring_the_wrong_width_is_caught(self):
        """The wrong-*value* liar, distinct from the wrong-*kind* ones.

        ``_SilentlyPinnedStore`` and ``_OverdeclaringStore`` get the
        ``None`` / not-``None`` split wrong and are caught by the branch
        they land in. This one lands in the right branch with the wrong
        number, which only the value assertions can catch — and a case
        that checked only *which* branch to take would pass it.
        """
        assert _run_case(_WIDTH_CASE, _MisdeclaringStore()) is False
        assert _run_case(_SHAPE_CASE, _MisdeclaringStore()) is True

    def test_the_cases_are_not_satisfied_by_any_single_constant(self):
        """The property the issue asks for, stated directly.

        Neither constant answer survives: ``None`` fails against a store
        that pins a width, ``DIMS`` fails against one that does not. So
        the case cannot be passed by declaring a value — only by
        declaring the *right* value.
        """

        class _AlwaysNone(_PinnedStore):
            @property
            def dimensions(self) -> int | None:
                return None

        class _AlwaysDims(_UnpinnedStore):
            @property
            def dimensions(self) -> int | None:
                return DIMS

        assert _run_case(_WIDTH_CASE, _AlwaysNone()) is False
        assert _run_case(_WIDTH_CASE, _AlwaysDims()) is False
        # ... while the same two stores with honest declarations pass.
        assert _run_case(_WIDTH_CASE, _PinnedStore()) is True
        assert _run_case(_WIDTH_CASE, _UnpinnedStore()) is True


class TestShippedBackendsDeclare:
    """The four registry backends, and what each one says.

    ``SQLiteVectorStore`` is the only one that answers ``None``, and it is
    the only one in the default test selection — which is why the liars
    above exist. The other three are asserted on the class, without
    connecting to anything.
    """

    def test_sqlite_declares_no_width_and_can_be_reset(self, tmp_path):
        from trellis.stores.sqlite.vector import SQLiteVectorStore

        store = SQLiteVectorStore(tmp_path / "vec.db")
        try:
            assert store.dimensions is None
            assert SQLiteVectorStore.supports_reset() is True
        finally:
            store.close()

    def test_sqlite_reset_is_durable_inside_an_open_write_transaction(self, tmp_path):
        """The reset survives the process, even mid-transaction.

        The property, not a mechanism. ``sqlite3`` in legacy transaction
        mode opens an implicit transaction for DML and **not** for DDL, so
        a ``DROP`` issued on an idle connection auto-commits; issued while
        a write transaction is already live it joins that transaction and
        is invisible to every other connection until something commits.
        Two things could be that something here, and the test deliberately
        does not care which: ``reset_storage``'s explicit ``commit()``, or
        ``_init_schema``'s ``executescript``, which commits any pending
        transaction before running its script.

        **So the explicit ``commit()`` is currently redundant and deleting
        it leaves this test green** — measured, not assumed. It is kept
        because the redundancy is a coincidence of how ``_init_schema``
        happens to be spelled: rewrite that as ``execute`` calls and the
        commit becomes load-bearing with nothing else changing. What this
        test pins is the outcome an operator depends on, which holds
        either way.

        Provoked with a raw uncommitted ``INSERT`` because no store method
        leaves that state today. Read back through a *separate* connection
        — the store's own would see its own uncommitted transaction and
        report success regardless.
        """
        import sqlite3

        from trellis.stores.sqlite.vector import SQLiteVectorStore

        db_path = tmp_path / "vec.db"
        store = SQLiteVectorStore(db_path)
        try:
            store.upsert("a", _vec(1, 0, 0))
            store._conn.execute(
                "INSERT INTO vectors (item_id, vector_blob, dimensions) "
                "VALUES ('b', X'00', 3)"
            )
            assert store._conn.in_transaction, "premise: a write tx is live"

            store.reset_storage()

            other = sqlite3.connect(str(db_path), timeout=5)
            try:
                rows = other.execute("SELECT count(*) FROM vectors").fetchone()[0]
            finally:
                other.close()
            assert rows == 0
        finally:
            store.close()

    def test_the_graph_node_backends_declare_a_width_and_no_reset(self):
        """Pinning a width and owning storage are unrelated answers.

        ArcadeDB and Neo4j hold embeddings as properties on the graph
        store's ``(:Node)`` rows: they pin a width (the vector index is
        created at one) and there is no table here to drop. A design that
        folded the two into one capability flag would have to pick a
        wrong answer for both.
        """
        from trellis.stores.arcadedb.vector import ArcadeDBVectorStore
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        for cls in (ArcadeDBVectorStore, Neo4jVectorStore):
            assert cls.supports_reset() is False, cls
            store = object.__new__(cls)
            store._dimensions = 1536
            assert store.dimensions == 1536, cls

    def test_the_declaration_cannot_be_steered_on_an_instance(self, tmp_path):
        """A backend declares its width on the **class**, read-only.

        The mutant this exists for survived the whole 7,428-test suite:
        replace ``SQLiteVectorStore``'s ``dimensions`` property with a
        plain class attribute ``dimensions: int | None = None`` and every
        test still passes — because nothing reads the *shape* of the
        declaration, only its value, and the value is identical.

        What that would give back is #512's own mechanism. The route the
        fix is about reads ``vector_store.dimensions`` off the live
        instance and prints it in the body of a 200 that reports a
        destructive operation; against a plain attribute
        ``store.dimensions = 1536`` steers that report, which is exactly
        what setting ``_dimensions`` did before #512 and what
        ``TestVectorsResetStatusLine`` asserts is no longer possible
        ("``dimensions`` is a property on the *class* now, so the only
        way to make a backend report a width is for the backend to
        declare one"). That claim was true of all four shipped backends
        and pinned on none of them.

        Scoped to the backends in ``_BUILTIN_BACKENDS`` rather than
        promoted into ``VectorStoreContractTests``: a read-only
        descriptor is how *these* backends declare, not something a
        third-party backend has to promise (a ``cached_property`` or a
        frozen-dataclass field would be a legitimate declaration and is
        settable). The roster is derived from the registry so a fifth
        vector backend is covered without editing this test.
        """
        import importlib

        from trellis.stores.registry import _BUILTIN_BACKENDS
        from trellis.stores.sqlite.vector import SQLiteVectorStore

        backends = _BUILTIN_BACKENDS["knowledge"]["vector"]
        checked: list[str] = []
        unimportable: list[str] = []
        for name, (module_path, class_name) in backends.items():
            try:
                module = importlib.import_module(module_path)
            # The optional-driver extras are not installed in the default
            # selection; live-infra installs `[cloud]` and covers pgvector
            # there. Recorded rather than swallowed — see the floor below.
            except ImportError:
                unimportable.append(name)
                continue
            cls = getattr(module, class_name)
            declaration = inspect.getattr_static(cls, "dimensions")
            assert isinstance(declaration, property), (name, declaration)
            assert declaration.fset is None, name
            checked.append(name)

        # Floor: the sweep found the registry's backends and actually
        # checked most of them, so a registry that resolved to nothing —
        # or an `except ImportError` that swallowed every one — cannot
        # pass this by checking zero.
        assert len(backends) >= 4, backends
        assert len(checked) >= 3, (checked, unimportable)
        assert "sqlite" in checked, checked

        # And the consequence, on the default backend, end to end: the
        # width an operator is told about cannot be set from outside.
        store = SQLiteVectorStore(tmp_path / "vec.db")
        try:
            with pytest.raises(AttributeError):
                store.dimensions = 1536  # type: ignore[misc]
            assert store.dimensions is None
        finally:
            store.close()

    def test_pgvector_declares_its_column_width_and_can_be_reset(self):
        pytest.importorskip("psycopg")
        pytest.importorskip("psycopg_pool")
        from trellis.stores.pgvector.store import PgVectorStore

        assert PgVectorStore.supports_reset() is True
        store = object.__new__(PgVectorStore)
        store._dimensions = 1536
        assert store.dimensions == 1536
