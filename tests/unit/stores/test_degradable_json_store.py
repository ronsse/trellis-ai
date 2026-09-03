"""Contract tests for :class:`DegradableJsonStore` and its two subclasses.

Same idea as ``tests/unit/stores/contracts/``, one layer down: the shared
read-leniently-refuse-to-write machinery is asserted **once, over both
stores**, instead of twice over two copies that had already begun to
disagree (#426).

Two properties live here that no per-store suite can express:

* **The shape table is one table.** Every degenerate file in
  :mod:`tests.degradable_shapes` must degrade *and* refuse on *every*
  store. Before #426 each store had its own table and neither was a
  superset of the other, against line-for-line identical code.
* **Every write path carries both guards, structurally.** #423 and #444
  both found that deleting a guard from a single write method killed zero
  tests, because ``_save``'s own guard plus the rollback make it
  invisible. Those suites answered with per-method tests that stub
  ``_save``; :class:`TestEveryWritePathIsGuarded` answers with an AST walk,
  so a *new* write method added to either store is covered the day it is
  written rather than the day someone remembers to write its test.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from tests.degradable_shapes import degenerate_files
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.degradable_json_store import (
    DegradableJsonStore,
    UnknownFileIdentity,
)
from trellis.stores.policy_store import PolicyStore


@dataclass(frozen=True)
class StoreCase:
    """One concrete store, with just enough to drive it generically."""

    label: str
    cls: type[DegradableJsonStore]
    envelope_key: str
    #: Perform one ordinary write. Must reach ``_save``.
    write: Callable[[DegradableJsonStore], object]
    #: A row this store would accept, as the envelope list holds it.
    row: Callable[[], dict]
    #: The id a ``row()`` files under.
    row_id: Callable[[dict], str]
    #: **Every** public method that can reach a write, bound to a store and
    #: to the id of a row that store has loaded. Cross-checked against the
    #: AST walk below, so a method added to a store without an entry here
    #: fails rather than going quietly uncovered.
    attempts: Callable[[DegradableJsonStore, str], dict[str, Callable[[], object]]]


def _policy() -> Policy:
    return Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global"),
        rules=[PolicyRule(operation="entity.create", action="deny")],
        enforcement=Enforcement.ENFORCE,
    )


def _advisory() -> Advisory:
    return Advisory(
        category=AdvisoryCategory.ENTITY,
        message="Test advisory",
        confidence=0.7,
        scope="global",
        evidence=AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
    )


CASES = [
    StoreCase(
        label="policy",
        cls=PolicyStore,
        envelope_key="policies",
        write=lambda store: store.add(_policy()),
        row=lambda: _policy().model_dump(mode="json"),
        row_id=lambda row: row["policy_id"],
        attempts=lambda store, seeded: {
            "add": lambda: store.add(_policy()),
            "remove": lambda: store.remove(seeded),
        },
    ),
    StoreCase(
        label="advisory",
        cls=AdvisoryStore,
        envelope_key="advisories",
        write=lambda store: store.put(_advisory()),
        row=lambda: _advisory().model_dump(mode="json"),
        row_id=lambda row: row["advisory_id"],
        attempts=lambda store, seeded: {
            "put": lambda: store.put(_advisory()),
            "put_many": lambda: store.put_many([_advisory()]),
            "suppress": lambda: store.suppress(seeded, reason="x"),
            "restore": lambda: store.restore(seeded),
            "remove": lambda: store.remove(seeded),
            "clear": store.clear,
        },
    ),
]
CASE_IDS = [c.label for c in CASES]

#: ``(case, shape id, contents, expected reason)`` — the cross product, so a
#: shape added to the table is immediately asserted against every store.
SHAPE_PARAMS = [
    pytest.param(case, text, reason, id=f"{case.label}-{shape_id}")
    for case in CASES
    for shape_id, text, reason in degenerate_files(case.envelope_key)
]


class TestOneShapeTableForEveryStore:
    """Every degenerate shape degrades the load and refuses the write."""

    @pytest.mark.parametrize(("case", "text", "reason"), SHAPE_PARAMS)
    def test_shape_degrades_with_the_expected_reason(
        self, tmp_path: Path, case: StoreCase, text: str, reason: str
    ) -> None:
        path = tmp_path / "store.json"
        path.write_text(text, encoding="utf-8")
        store = case.cls(path)
        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.reason == reason

    @pytest.mark.parametrize(("case", "text", "reason"), SHAPE_PARAMS)
    def test_shape_refuses_the_write_and_keeps_the_bytes(
        self, tmp_path: Path, case: StoreCase, text: str, reason: str
    ) -> None:
        """Degrading alone was the defect: the *next* write is what deletes."""
        path = tmp_path / "store.json"
        path.write_text(text, encoding="utf-8")
        store = case.cls(path)
        with pytest.raises(DegradedStoreWriteError):
            case.write(store)
        assert path.read_text(encoding="utf-8") == text

    @pytest.mark.parametrize(("case", "text", "reason"), SHAPE_PARAMS)
    def test_the_refusal_carries_a_runnable_recovery(
        self, tmp_path: Path, case: StoreCase, text: str, reason: str
    ) -> None:
        """The operator meets this in a cron log; the ``mv`` has to run."""
        path = tmp_path / "a dir with spaces" / "store.json"
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
        store = case.cls(path)
        with pytest.raises(DegradedStoreWriteError) as excinfo:
            case.write(store)
        recovery = excinfo.value.recovery
        assert recovery is not None
        assert recovery.startswith("mv ")
        assert f"'{path}'" in recovery
        assert f"'{path}.corrupt'" in recovery

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_an_empty_envelope_list_is_a_clean_store(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """The negative control: what ``_save`` writes must load clean."""
        path = tmp_path / "store.json"
        path.write_text(json.dumps({case.envelope_key: []}), encoding="utf-8")
        store = case.cls(path)
        assert store.is_degraded is False
        case.write(store)  # must not raise

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_an_absent_file_is_a_clean_store(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """ "No file" and "unreadable file" are different states."""
        store = case.cls(tmp_path / "never-written.json")
        assert store.is_degraded is False
        assert store.degradation is None
        case.write(store)  # must not raise

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_a_round_trip_loads_clean(self, tmp_path: Path, case: StoreCase) -> None:
        path = tmp_path / "store.json"
        case.write(case.cls(path))
        assert case.cls(path).is_degraded is False


class TestStaleWritesAreRefusedOnEveryStore:
    """The #438/#423 guard, asserted once rather than per store."""

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_a_second_writer_is_not_silently_overwritten(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        path = tmp_path / "store.json"
        case.write(case.cls(path))
        store = case.cls(path)
        theirs = json.dumps({case.envelope_key: [case.row(), case.row()]})
        path.write_text(theirs, encoding="utf-8")

        with pytest.raises(StaleStoreWriteError):
            case.write(store)
        assert path.read_text(encoding="utf-8") == theirs

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_a_file_that_appeared_after_construction_is_not_overwritten(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """Route 3: never degraded, never read, and the first write replaces it."""
        path = tmp_path / "store.json"
        store = case.cls(path)  # absent at construction
        assert store.is_degraded is False
        theirs = json.dumps({case.envelope_key: [case.row()]})
        path.write_text(theirs, encoding="utf-8")

        with pytest.raises(StaleStoreWriteError):
            case.write(store)
        assert path.read_text(encoding="utf-8") == theirs

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_consecutive_writes_from_one_store_do_not_trip_the_guard(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """A store must not be made stale by its own write."""
        store = case.cls(tmp_path / "store.json")
        case.write(store)
        case.write(store)  # must not raise

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_fingerprint_catches_an_edit_that_only_changed_the_size(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """``st_size`` is load-bearing, and until now nothing measured it.

        #444 found ``st_mtime_ns`` unmeasured on ``AdvisoryStore`` and
        pinned it; ``st_size`` was left in the same state, and #426's
        mutant run found that dropping it survives the whole suite on
        ``main`` — on **both** stores, because the two copies of
        ``_fingerprint`` were identical. Extraction is what makes one test
        answer for both.

        The shape it covers is an in-place edit whose mtime is restored —
        ``cp -p`` or ``rsync --times`` over the bind-mounted data dir, or
        two writes landing inside one filesystem timestamp tick. The
        assertions on ``st_ino`` and ``st_mtime_ns`` are the test: without
        them it would pass on whichever field happened to move.
        """
        path = tmp_path / "store.json"
        case.write(case.cls(path))
        store = case.cls(path)
        before = path.stat()

        theirs = json.dumps({case.envelope_key: [case.row(), case.row()]})
        path.write_text(theirs, encoding="utf-8")  # truncates in place
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        after = path.stat()
        assert after.st_ino == before.st_ino, "inode moved; not a size-only edit"
        assert after.st_mtime_ns == before.st_mtime_ns, "mtime moved"
        assert after.st_size != before.st_size

        with pytest.raises(StaleStoreWriteError):
            case.write(store)
        assert path.read_text(encoding="utf-8") == theirs

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_stale_refusal_names_a_command_that_shows_state(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """A stale write is transient: recovery is a read, never an ``mv``.

        Advising the operator to move the file aside here would delete rows
        that are perfectly readable and belong to whoever wrote them.
        """
        path = tmp_path / "store.json"
        case.write(case.cls(path))
        store = case.cls(path)
        path.write_text(json.dumps({case.envelope_key: [case.row()]}), encoding="utf-8")

        with pytest.raises(StaleStoreWriteError) as excinfo:
            case.write(store)
        recovery = excinfo.value.recovery
        assert recovery is not None
        assert recovery.startswith("trellis ")
        assert not recovery.startswith("mv ")


class TestEveryWritePathIsGuarded:
    """Both guards, first, on every method that can reach a write.

    The masking finding from #423 and #444: removing ``refuse_if_stale()``
    from a single write method killed **zero** tests, because ``_save``
    calls it too and ``_save_or_roll_back`` restores memory on the raise.
    Both suites answered with per-method tests that stub ``_save`` to a
    recorder. Those stay — they prove the guard runs *at* the call site.
    This proves the call site *exists*, for methods nobody has written yet,
    and it is the property #426's extraction most plausibly breaks: hoisting
    the pair into a shared helper would read tidier and would make every
    one of those per-method mutants survive.

    ``_save``'s own guards cannot substitute. Several of these methods
    return a *wrong answer* out of the in-memory view — ``False`` for a row
    that is in the file and merely failed to parse, or an idempotent no-op
    read from a file another process has replaced — without ever reaching a
    write.
    """

    GUARDS = ("refuse_if_degraded", "refuse_if_stale")

    @staticmethod
    def _leading_guard_calls(func: ast.FunctionDef) -> list[str]:
        body = func.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]  # the docstring
        names = []
        for stmt in body:
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                break
            call = stmt.value.func
            if (
                not isinstance(call, ast.Attribute)
                or not isinstance(call.value, ast.Name)
                or call.value.id != "self"
            ):
                break
            names.append(call.attr)
        return names

    @staticmethod
    def _methods(target: type) -> dict[str, ast.FunctionDef]:
        tree = ast.parse(inspect.getsource(inspect.getmodule(target)))
        classdef = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == target.__name__
        )
        return {
            node.name: node
            for node in classdef.body
            if isinstance(node, ast.FunctionDef)
        }

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_every_persisting_method_opens_with_both_guards(
        self, case: StoreCase
    ) -> None:
        persisting = self._persisting(case)
        # A store with no write path would pass this vacuously.
        assert persisting, f"{case.cls.__name__} has no persisting method"
        for name, node in persisting.items():
            assert self._leading_guard_calls(node)[:2] == list(self.GUARDS), (
                f"{case.cls.__name__}.{name} must open with "
                f"{self.GUARDS[0]}() then {self.GUARDS[1]}()"
            )

    @staticmethod
    def _persisting(case: StoreCase) -> dict[str, ast.FunctionDef]:
        return {
            name: node
            for name, node in TestEveryWritePathIsGuarded._methods(case.cls).items()
            if "_save_or_roll_back" in ast.dump(node)
        }

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_a_degraded_write_never_enters_the_write_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: StoreCase
    ) -> None:
        """The behavioural half, for the guard that only had the AST one.

        #444 pinned this for ``refuse_if_stale`` and did it per store;
        nothing did it for ``refuse_if_degraded``, and measuring the gap
        was what found it — on ``main``, deleting ``refuse_if_degraded``
        from **any** of ``AdvisoryStore``'s six write methods killed zero
        tests. ``_save`` calls the same guard and ``_save_or_roll_back``
        undoes the mutation, so the exception and the surviving file are
        identical either way. Stubbing ``_save`` to a recorder is what
        makes each call site observable: with its guard gone the stub
        records a call and this fires.

        A file of one good row and one bad one is what makes the lookups
        reachable — the store degrades *and* still holds a row, so
        ``suppress`` / ``restore`` / ``remove`` get past their id check
        instead of returning the wrong answer early.
        """
        good = case.row()
        path = tmp_path / "store.json"
        path.write_text(
            json.dumps({case.envelope_key: [good, {"nope": 1}]}), encoding="utf-8"
        )
        store = case.cls(path)
        assert store.is_degraded is True
        assert store.degradation is not None
        assert store.degradation.rows_loaded == 1

        attempts = case.attempts(store, case.row_id(good))
        assert set(attempts) == set(self._persisting(case)), (
            "the attempt map and the store's write methods have diverged"
        )

        entered: list[str] = []
        monkeypatch.setattr(
            store,
            "_save",
            lambda: entered.append("save"),  # type: ignore[method-assign]
        )
        for name, attempt in attempts.items():
            with pytest.raises(DegradedStoreWriteError):
                attempt()
            assert entered == [], f"{name} entered the write path on a degraded store"

    def test_save_opens_with_both_guards(self) -> None:
        """The backstop for a direct ``_save``, and only the backstop."""
        node = self._methods(DegradableJsonStore)["_save"]
        assert self._leading_guard_calls(node)[:2] == list(self.GUARDS)

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_guards_are_not_inherited_shims(self, case: StoreCase) -> None:
        """The subclass must call the base's guards, not shadow them.

        An override that forgot to raise would make every assertion above
        pass while disarming the store completely.
        """
        for guard in self.GUARDS:
            assert getattr(case.cls, guard) is getattr(DegradableJsonStore, guard)


class TestAnIncompleteSubclassIsRefusedAtImport:
    """``@abstractmethod`` covers the hooks; nothing covered the parameters.

    Every ``_`` class attribute the base reads is read on a *failure* path,
    so an unset one does not surface as a broken store — it surfaces as a
    traceback from inside a guard, or (for ``_degraded_impact``, read inside
    ``_degrade``, which runs under ``_load``'s broad handler) as
    ``load_failed: AttributeError`` on a perfectly readable file. A wiring
    mistake would present as file corruption.
    """

    @staticmethod
    def _subclass(**parameters: str) -> type[DegradableJsonStore]:
        namespace: dict[str, object] = {
            "_parse_row": staticmethod(Policy.model_validate),
            "_row_id": staticmethod(lambda row: row.policy_id),
            "_degraded_write_message": lambda self, degradation: "degraded",
            "_stale_write_message": lambda self: "stale",
            "_unreadable_write_message": lambda self, detail: "unreadable",
            **parameters,
        }
        return type("Probe", (DegradableJsonStore,), namespace)

    COMPLETE: ClassVar[dict[str, str]] = {
        "_envelope_key": "things",
        "_store_label": "thing",
        "_loaded_event": "things_loaded",
        "_degraded_event": "thing_load_degraded",
        "_degraded_impact": "Things that parsed are still served.",
        "_stale_recovery": "trellis thing list",
    }

    def test_a_complete_subclass_is_accepted(self) -> None:
        assert self._subclass(**self.COMPLETE) is not None

    @pytest.mark.parametrize("omitted", sorted(COMPLETE))
    def test_omitting_any_one_parameter_raises_at_definition(
        self, omitted: str
    ) -> None:
        parameters = {k: v for k, v in self.COMPLETE.items() if k != omitted}
        with pytest.raises(TypeError, match=omitted):
            self._subclass(**parameters)

    def test_an_abstract_intermediate_is_exempt(self) -> None:
        """A shared partial base declares nothing and must still import."""

        class Intermediate(DegradableJsonStore[Policy]):
            @staticmethod
            def _row_id(row: Policy) -> str:
                return row.policy_id

        assert Intermediate is not None

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_shipped_stores_declare_every_parameter(self, case: StoreCase) -> None:
        for name in DegradableJsonStore._REQUIRED_PARAMETERS:
            assert getattr(case.cls, name)


def _symlink_loop(path: Path) -> None:
    """Make ``stat(path)`` fail with a real ``ELOOP``, no mocking anywhere.

    Two symlinks pointing at each other. Chosen over the obvious
    ``chmod 000`` on the parent because that one is a no-op for root and
    so would skip silently in a container, and over a mocked ``stat``
    because the claim under test is about a filesystem this code cannot
    read — a mock proves the branch runs, not that the state exists.

    Note what ``Path.exists()`` says about this path afterwards:
    ``False``. That is #444's finding, and it is why the constructor
    stopped using it.
    """
    partner = path.parent / f"{path.name}.loop"
    path.symlink_to(partner)
    partner.symlink_to(path)


class TestAnUnreadableFingerprintRefusesOnEveryStore:
    """#471: ``None`` meant both "no file" and "could not look", and the
    compare-and-swap compared them equal.

    The reachable shape is two ordinary states in sequence, neither of
    which degrades the store: constructed while the path was **absent** (a
    deployment that has never written a policy — ``_loaded_fingerprint``
    is ``None``), and a later ``stat`` that **fails** (``EACCES`` from a
    parent that lost its execute bit, ``ELOOP``, ``EIO`` or ``ESTALE``
    from a network mount — also ``None``). ``None == None``, the guard
    returned, and the whole-file rewrite that
    :class:`~trellis.errors.StaleStoreWriteError` exists to prevent went
    through.

    Why one *failing* ``stat`` was never enough to catch it, and why the
    mutant sweeps in #470 did not: a single failure compares a real
    fingerprint against ``None``, which is unequal, so the guard refuses
    for the right reason by accident. Only a **double** failure reaches
    the defect, and nothing modelled one.
    """

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_a_double_stat_failure_refuses_rather_than_passing(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """The headline. Both ``_fingerprint`` calls fail; the write refuses."""
        path = tmp_path / "store.json"
        store = case.cls(path)  # absent at construction: a normal fresh install
        assert store.is_degraded is False
        assert store._loaded_fingerprint is None

        _symlink_loop(path)
        assert isinstance(store._fingerprint(), UnknownFileIdentity), (
            "the double failure this test exists for was not constructed"
        )

        with pytest.raises(StaleStoreWriteError):
            store.refuse_if_stale()

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_write_path_surfaces_the_refusal_and_not_a_traceback(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """A caller must meet the guard, not the writer's internals.

        Before the fix the guard passed and the call fell through to
        ``atomic_write_text``, which raised ``RuntimeError`` from
        ``Path.resolve``. So the *refusal* was silent and what the operator
        actually got was a crash from three frames below the guard that
        should have stopped them — with none of the recovery advice every
        surface renders off :class:`~trellis.errors.StoreWriteRefusedError`.
        """
        path = tmp_path / "store.json"
        store = case.cls(path)
        _symlink_loop(path)

        with pytest.raises(StaleStoreWriteError):
            case.write(store)

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_an_unreadable_fingerprint_recorded_at_load_also_refuses(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """The *loaded* operand, and the message is what makes it observable.

        A store constructed over an unreadable path records
        :class:`UnknownFileIdentity` as its loaded identity. It is also
        degraded, so every shipped write path refuses one guard earlier;
        the stale guard is public and documented as independently callable,
        so it is asserted directly.

        The path is then cleared **completely**, leaving it absent, which
        is the arrangement that makes this discriminating in both
        directions. Before the fix the recorded identity was ``None`` and
        the current one is ``None``, so the guard passed — a store that
        never read its file being told it is unchanged. And a build that
        kept the fix but dropped the ``loaded`` branch would still refuse
        (``UnknownFileIdentity`` equals nothing) while *saying* the file
        changed after this process read it, which is a claim nothing here
        is entitled to make. Only the message separates those two, so the
        message is the assertion.
        """
        path = tmp_path / "store.json"
        _symlink_loop(path)
        store = case.cls(path)
        assert store.is_degraded is True
        assert isinstance(store._loaded_fingerprint, UnknownFileIdentity)

        path.unlink()
        (path.parent / f"{path.name}.loop").unlink()
        assert store._fingerprint() is None, "the path must now read as absent"

        with pytest.raises(StaleStoreWriteError) as excinfo:
            store.refuse_if_stale()
        assert "could not be read" in str(excinfo.value)
        assert "changed after this process read it" not in str(excinfo.value)

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_an_absent_file_stays_writable(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """The anti-vacuity half, and the reason the fix is not "refuse on ``None``".

        Absence is the ordinary first-write state. A ``_fingerprint`` that
        answered :class:`UnknownFileIdentity` for it would make every test
        above pass while refusing every write on every fresh install, which
        is the failure #444's own fix had to avoid in the constructor.
        """
        path = tmp_path / "store.json"
        store = case.cls(path)

        assert store._fingerprint() is None
        store.refuse_if_stale()  # must not raise
        case.write(store)  # and the first write must land
        assert path.exists()

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_the_refusal_names_the_cause_and_a_command_that_shows_the_path(
        self, tmp_path: Path, case: StoreCase
    ) -> None:
        """What the operator reads has to separate this from a stale write.

        The stale message asserts the file *changed*; here nothing knows
        that, and printing it would send someone hunting a concurrent
        writer that may not exist. The recovery is neither the stale one (a
        re-read, which meets the same unreadable path and reports the same
        nothing) nor the degraded one (an ``mv``, which would move a file
        this process cannot even see).
        """
        path = tmp_path / "store.json"
        store = case.cls(path)
        _symlink_loop(path)

        with pytest.raises(StaleStoreWriteError) as excinfo:
            store.refuse_if_stale()

        message = str(excinfo.value)
        assert "could not be read" in message
        assert "OSError" in message, "the stat failure itself must reach the operator"
        assert "changed after this process read it" not in message

        recovery = excinfo.value.recovery
        assert recovery is not None
        assert recovery.startswith("ls -ld -- ")
        assert str(path) in recovery
        assert str(path.parent) in recovery

    def test_a_data_dir_containing_a_space_stays_one_command(
        self, tmp_path: Path
    ) -> None:
        """#427's rule, applied to the one recovery string this fix adds.

        An unquoted path with a space word-splits into an ``ls`` over four
        operands rather than two — an unrunnable command handed to the
        operator *as* the fix.
        """
        directory = tmp_path / "my staging dir"
        directory.mkdir()
        path = directory / "store.json"
        store = PolicyStore(path)
        _symlink_loop(path)

        with pytest.raises(StaleStoreWriteError) as excinfo:
            store.refuse_if_stale()

        recovery = excinfo.value.recovery
        assert recovery is not None
        assert shlex.split(recovery) == ["ls", "-ld", "--", str(path), str(directory)]

    def test_two_records_of_the_same_failure_are_never_equal(self) -> None:
        """``eq=False`` on :class:`UnknownFileIdentity`, pinned.

        The explicit ``isinstance`` branches in ``refuse_if_stale`` are the
        primary defence; this is the backstop, and a backstop nothing
        exercises is a comment. With the dataclass-generated ``__eq__``,
        two records of the *same* ``stat`` failure — which is precisely
        what the guard holds when a path stays unreadable between load and
        write — compare equal, and the defect returns verbatim one type
        later.
        """
        detail = "PermissionError: [Errno 13] Permission denied: '/x'"
        assert UnknownFileIdentity(detail) != UnknownFileIdentity(detail)
        one = UnknownFileIdentity(detail)
        # Identity equality is retained: the *same* record still compares
        # equal, which is what keeps ``eq=False`` a narrowing of equality
        # rather than a type that is simply broken. It is only two
        # separately-derived records that must not collapse.
        same_record = one
        assert same_record == one
        assert str(one) == detail
