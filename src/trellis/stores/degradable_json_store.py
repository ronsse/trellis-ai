"""The read-leniently-refuse-to-write machinery, in one place.

Two stores whole-file-rewrite a small JSON document on every write —
:class:`~trellis.stores.policy_store.PolicyStore` over ``policies.json``
and :class:`~trellis.stores.advisory_store.AdvisoryStore` over
``advisories.json``. Both met the same failure, in the same shape, a
release apart (#393/#414, then #413/#423), and both answered it the same
way: **the read degrades and the write refuses.**

The laundering primitive
------------------------
A whole-file rewrite from an in-memory view that is *no longer the file*
deletes whatever the view is missing, and does it silently — the file it
leaves behind is well-formed, so every later reader accepts it and every
surface reports normal. Three routes reach that state:

1. **A degraded load.** The file could not be read in full, so the view
   holds only what parsed. Recorded as a :class:`LoadDegradation` and
   refused by :meth:`DegradableJsonStore.refuse_if_degraded`. Not
   transient: an operator has to look at the bytes.
2. **Another process wrote the file.** Both files have several writer
   processes on the reference deployment (a host CLI and a containerised
   API against the same bind-mounted data dir). Caught by comparing a
   fingerprint of the file as loaded — :meth:`DegradableJsonStore.refuse_if_stale`.
3. **The file appeared after construction.** A store built while the path
   was absent is not degraded, and its first write replaces a file it
   never read. Same fingerprint, same guard: ``None`` is a fingerprint
   value, not the absence of one.
4. **The fingerprint could not be taken.** ``stat`` failing is not the
   same fact as the file being absent, and #471 found the two collapsed
   into one ``None``: a store built while the path was absent, whose
   later ``stat`` failed, compared ``None`` against ``None``, found them
   equal, and let the write through — the compare-and-swap degrading to
   no check at all, in silence. :class:`UnknownFileIdentity` is what the
   two facts are now told apart by, and the guard refuses on it.

Routes 2, 3 and 4 are **transient** — re-read and redo — and the guard is
a compare-and-swap, not a lock: two writers can still interleave between
the check and the ``os.replace``. It closes the wide window and narrows
the tiny one. Last-writer-wins remains the model.

This file has now produced the same shape three times — an error
condition collapsing into an indistinguishable "nothing here" value, in
``_load``'s pre-#413 empty set, in ``__init__``'s pre-#444 ``exists()``,
and in ``_fingerprint``'s pre-#471 ``None``. The rule the third one
earns: **a guard that cannot see the filesystem refuses; it never
reports "unchanged".**

Why a base class rather than two copies
---------------------------------------
Because identical-by-copy had already started decaying inside the pull
request that created the second copy (#426): the two degenerate-shape
test tables covered *different* shapes against line-for-line identical
code, and the ``recovery`` docstrings had already diverged. The
five-branch load ladder is also the exact place #414's first attempt
shipped the ``raw.get(key, [])`` bug it was written to fix. Existing
twice, that branch is two chances to get it wrong.

What this base deliberately does **not** own
--------------------------------------------
The extraction is narrow on purpose, and the boundary is worth stating
because a base bent to absorb every difference would be worse than the
duplication it removed:

* **Every message and every store-specific docstring.** The two refusals
  argue different things — one about reviving suppressions the fitness
  loop made, the other about un-governing every mutation the missing
  policies covered. They are produced by the abstract hooks
  :meth:`DegradableJsonStore._degraded_write_message`,
  :meth:`DegradableJsonStore._stale_write_message` and
  :meth:`DegradableJsonStore._unreadable_write_message`, which exist so
  that every word of all three stays in the subclass that means it. The
  *recovery* for the third is the exception and lives in the base
  (:meth:`DegradableJsonStore._refuse_unreadable`): "look at the path and
  its parent" is a fact about a filesystem, not about policies or
  advisories, and there is nothing store-specific for a subclass to say.
* **The write methods.** ``add`` / ``remove`` / ``put`` / ``put_many`` /
  ``suppress`` / ``restore`` / ``clear`` keep their own explicit,
  paired ``refuse_if_degraded()`` / ``refuse_if_stale()`` calls. Hoisting
  those into a shared ``_commit`` helper would read tidier and would
  destroy the property the guards are tested for: several of those
  methods return a *wrong answer* from the in-memory view before they ever
  reach a write, so ``_save``'s own guards are too late, and each call
  site has to be independently removable — and therefore independently
  observable — for the mutation tests that pin them to mean anything.
* **Row-level policy.** ``PolicyStore`` treats a duplicate id as
  degradation while ``AdvisoryStore`` lets the last row win; that
  divergence is deliberate (the enforcement reader evaluates duplicates as
  a list) and lives in :meth:`DegradableJsonStore._reject_row`, whose
  default is to reject nothing.
* **Strictness.** Both readers here are lenient. The strict one is
  :mod:`trellis.mutate.policy_source`, a different module, and it must
  stay there: *display* degrades, *enforcement* fails closed.
"""

from __future__ import annotations

import inspect
import json
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, NoReturn, TypeVar

import structlog
from pydantic import BaseModel

from trellis.core.atomic_write import atomic_write_text
from trellis.errors import DegradedStoreWriteError, StaleStoreWriteError

logger = structlog.get_logger(__name__)

#: The row type a concrete store files. Bound to :class:`~pydantic.BaseModel`
#: so the base can serialise a row without asking the subclass how.
RowT = TypeVar("RowT", bound=BaseModel)

#: How many per-row failures to name in the degradation detail. Enough to
#: recognise a pattern (one renamed field shows up identically on every
#: row), short enough that a cron log line stays readable.
_MAX_REPORTED_ROWS = 3


@dataclass(frozen=True, slots=True, eq=False)
class UnknownFileIdentity:
    """``stat`` failed, so the file's identity is *unknown* — not absent.

    The distinction is the whole of #471. :meth:`DegradableJsonStore._fingerprint`
    used to answer ``None`` for both "there is no file" and "I could not
    look", and :meth:`DegradableJsonStore.refuse_if_stale` compares the
    fingerprint taken at load against one taken before the write. A store
    built while the path was absent (a normal fresh deployment) records
    ``None``; if a later ``stat`` also failed — ``EACCES`` from a parent
    that lost its execute bit, ``ELOOP`` from a symlink cycle, ``EIO`` or
    ``ESTALE`` from a network mount — it recorded ``None`` again, the two
    compared **equal**, and the compare-and-swap passed. The guard that
    stands between a stale in-memory view and #413's fail-open on access
    control disarmed itself precisely when it could not see the file.

    Only one of those two facts is safe to write over. An absent file is
    the ordinary first-write case and must stay writable, or every fresh
    install refuses. An unreadable one is a state this process cannot
    reason about, and :meth:`DegradableJsonStore.refuse_if_stale` refuses
    on it — cheaply, because that refusal is documented as transient and
    retryable, so the cost of being wrong is one retry against the cost of
    a silent whole-file rewrite.

    ``eq=False`` is load-bearing, not tidiness. With the generated
    ``__eq__`` two independently-derived records of the *same* failure —
    which is exactly what :meth:`DegradableJsonStore.refuse_if_stale` holds
    when a store's ``stat`` keeps failing the same way — carry equal
    ``detail`` and compare equal, reproducing the defect verbatim one type
    later. Identity equality makes "unknown == unknown" impossible to
    write by accident. The explicit ``isinstance`` branches in the guard
    are the primary defence and this is the backstop; both are pinned by
    test, because a backstop nothing exercises is a comment.
    """

    #: ``"PermissionError: [Errno 13] ..."`` — the exception, for the operator.
    detail: str

    def __str__(self) -> str:
        return self.detail


#: What :meth:`DegradableJsonStore._fingerprint` answers with. Three
#: distinct facts, deliberately not two: a tuple is *this* file, ``None``
#: is *no* file, and :class:`UnknownFileIdentity` is *don't know*.
FileIdentity = tuple[int, int, int] | None | UnknownFileIdentity


@dataclass(frozen=True, slots=True)
class LoadDegradation:
    """What a load could not read, and what an operator should do about it.

    Constructed only when something was unreadable — a store whose file
    parsed in full (or was simply absent) has ``degradation is None``, so
    the presence of this record is itself the signal.
    """

    #: The file that could not be read in full.
    path: str
    #: Machine-readable cause: ``unreadable_file``, ``malformed_json``,
    #: ``malformed_envelope``, ``invalid_rows`` or ``load_failed``.
    reason: str
    #: Human-readable specifics — the exception text, or the first few
    #: per-row validation failures.
    detail: str
    #: Rows that *did* parse and are being served.
    rows_loaded: int = 0
    #: Rows that did not — or ``None`` when the count is *unknowable*, which
    #: is every whole-file failure: nothing got as far as counting rows.
    #: Deliberately not ``0``. "0 could not be read" tells an operator at
    #: 03:00 that nothing was lost, when the whole file may be sitting
    #: unread; :attr:`rows_skipped_display` is what the surfaces render so
    #: the two cases cannot present identically.
    rows_skipped: int | None = None

    @property
    def recovery(self) -> str:
        """The shell command that clears the degraded state.

        Deliberately concrete. An operator meets this in a 03:00 cron log
        or a failed deploy, where a diagnosis is worth much less than the
        fix. Moving the file aside rather than deleting it keeps the bytes
        for inspection — what that buys is store-specific and each store
        says so where it builds its refusal message.

        Both operands are ``shlex.quote``d (#427). A data dir containing a
        space — ``~/Library/Application Support/…`` — otherwise word-splits
        into an ``mv`` with **four** operands rather than two, and one more
        pair per extra space (``/tmp/my staging dir/`` reaches six), so what
        the operator pastes is at best a refusal and at worst a move of
        three real paths into a fourth. That is the same failure as the
        Rich-markup and hard-wrap cases the CLI renderer already guards:
        an unrunnable command printed to the operator *as* the fix. It is
        the one string here that must survive every layer byte-for-byte,
        and the shell is the last of them.
        """
        quoted = shlex.quote(self.path)
        return f"mv {quoted} {shlex.quote(self.path + '.corrupt')}"

    @property
    def rows_skipped_display(self) -> str:
        """``rows_skipped`` for humans — ``"unknown"`` rather than ``0``."""
        return "unknown" if self.rows_skipped is None else str(self.rows_skipped)

    def to_dict(self) -> dict[str, Any]:
        """Flat view for ``--format json`` payloads and structured logs."""
        return {
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
            "rows_loaded": self.rows_loaded,
            "rows_skipped": self.rows_skipped,
            "rows_skipped_display": self.rows_skipped_display,
            "recovery": self.recovery,
        }


class DegradableJsonStore(ABC, Generic[RowT]):
    """A ``{"<key>": [<row>, ...]}`` file that degrades on read and refuses
    on write.

    Subclasses supply the envelope key, the row model, the id attribute
    and the strings, and keep their own public API. See the module
    docstring for the boundary: everything an operator *reads* stays in the
    subclass, everything the file *does* lives here.

    File format::

        {"<envelope_key>": [<row.model_dump()>, ...]}

    A store whose file could not be read in full is **degraded**: reads
    serve what parsed, writes raise
    :class:`~trellis.errors.DegradedStoreWriteError`. A store whose file
    changed underneath it is **stale**: writes raise
    :class:`~trellis.errors.StaleStoreWriteError`.
    """

    # -- Subclass parameters --

    #: The single key :meth:`_save` emits and :meth:`_load_rows` requires.
    _envelope_key: ClassVar[str]
    #: Grammar for the envelope-shape messages, nothing more: ``"a"`` for
    #: ``a "policies" list``, ``"an"`` for ``an "advisories" list``.
    _envelope_article: ClassVar[str] = "a"
    #: ``store=`` on both refusal errors.
    _store_label: ClassVar[str]
    #: structlog event names. Literals rather than f-strings built from the
    #: key, so that grepping an event name from a log line finds its emitter.
    _loaded_event: ClassVar[str]
    #: Emitted at ``error`` by :meth:`_degrade`.
    _degraded_event: ClassVar[str]
    #: The ``impact=`` line on that event: what a reader of the log should
    #: understand is now true of the deployment.
    _degraded_impact: ClassVar[str]
    #: ``recovery=`` on :class:`~trellis.errors.StaleStoreWriteError`. A
    #: stale write is transient, so this names a command that shows current
    #: state rather than one that moves the file aside.
    _stale_recovery: ClassVar[str]

    #: Every parameter above, checked at class-definition time.
    _REQUIRED_PARAMETERS: ClassVar[tuple[str, ...]] = (
        "_envelope_key",
        "_store_label",
        "_loaded_event",
        "_degraded_event",
        "_degraded_impact",
        "_stale_recovery",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse an incomplete subclass at import, not mid-refusal.

        ``@abstractmethod`` covers the hooks; nothing covers a class
        attribute a subclass forgot to set. Left unchecked, a missing
        ``_store_label`` surfaces as an ``AttributeError`` raised *from
        inside* :meth:`refuse_if_degraded` — the guard fails while doing
        its job, and the caller gets a traceback instead of the refusal
        and its recovery advice. Worse, a missing ``_degraded_impact``
        raises inside :meth:`_degrade`, which runs under :meth:`_load`'s
        broad handler: a wiring mistake would present as file corruption
        (``load_failed: AttributeError``) on a perfectly good file.
        """
        super().__init_subclass__(**kwargs)
        # ``inspect.isabstract`` is the right call here specifically because
        # it handles being asked mid-``__init_subclass__``: ``ABCMeta`` has
        # not computed ``cls.__abstractmethods__`` yet, so it falls back to
        # scanning for unimplemented hooks by hand. ``cls.__abstractmethods__``
        # read directly would raise, and the inherited-looking truthiness of
        # the base's copy is not what it looks like.
        if inspect.isabstract(cls):
            return
        missing = [name for name in cls._REQUIRED_PARAMETERS if not hasattr(cls, name)]
        if missing:
            msg = (
                f"{cls.__name__} is a DegradableJsonStore but does not set "
                f"{', '.join(missing)}. Every parameter is read on a failure "
                "path, so an unset one would surface as a traceback from "
                "inside a guard."
            )
            raise TypeError(msg)

    # -- Subclass hooks --

    @staticmethod
    @abstractmethod
    def _parse_row(entry: Any) -> RowT:
        """Validate one entry of the envelope list, raising on a bad row."""

    @staticmethod
    @abstractmethod
    def _row_id(row: RowT) -> str:
        """The key this store files ``row`` under."""

    @abstractmethod
    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        """What this store loses if it rewrites a file it could not read."""

    @abstractmethod
    def _stale_write_message(self) -> str:
        """What this store loses if it rewrites a file that moved under it."""

    @abstractmethod
    def _unreadable_write_message(self, detail: str) -> str:
        """What this store risks if it rewrites a file it cannot ``stat``.

        Separate from :meth:`_stale_write_message` because that one asserts
        the file *changed*, which here is not known and may be false. The
        two refusals are the same stakes reached by different facts, and
        printing the wrong fact to an operator sends them to look for a
        concurrent writer that does not exist.

        ``detail`` is the ``stat`` failure — ``"PermissionError: [Errno 13]
        ..."`` — and every implementation must render it: it is the only
        part of the message that says *why*, and it is the part that
        distinguishes a permissions problem from a symlink cycle from a
        flaky mount.
        """

    def _reject_row(self, row: RowT) -> str | None:  # noqa: ARG002
        """Why ``row`` must not be filed, or ``None`` to file it.

        Called after :meth:`_parse_row` succeeds and before the row lands in
        ``self._rows``, so an implementation may consult the rows already
        loaded. A returned reason is recorded like any other per-row failure:
        the load degrades, the row is not served, and the write refuses.

        Rejects nothing by default. Overriding it is a claim that this
        store's *key* carries meaning some other reader of the same file
        does not share — see
        :meth:`~trellis.stores.policy_store.PolicyStore._reject_row`, which
        is the one case.
        """
        return None

    # -- Construction --

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._rows: dict[str, RowT] = {}
        self._degradation: LoadDegradation | None = None
        # ``stat`` rather than ``exists()``, which splits an unreadable file
        # two ways and gets both wrong. ``Path.exists`` swallows the errnos
        # in ``pathlib._ignore_error`` — ``ENOENT``, ``ENOTDIR``, ``EBADF``,
        # ``ELOOP`` — and re-raises the rest. So a file behind a symlink
        # loop, or under a path component that is a regular file, presented
        # as *absent*: the one state that is neither degraded nor stale and
        # so is freely writable. That is the laundering primitive again, by
        # a different door. And the errnos it does *not* ignore — ``EACCES``
        # from an unsearchable parent is the one that happens — escaped the
        # constructor, breaking the promise both stores make unconditionally,
        # that constructing one never raises. Every failure to stat now
        # degrades: writes refused, reads still served, one record either way.
        try:
            self._path.stat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._degrade("unreadable_file", f"{type(exc).__name__}: {exc}")
        else:
            self._load()
        self._loaded_fingerprint: FileIdentity = self._fingerprint()

    # -- Public API --

    @property
    def path(self) -> Path:
        """The file this store reads and writes.

        Public because the CRUD surfaces have to tell an operator *which*
        file produced an empty answer: an absent file is a normal
        never-written deployment, a file declaring an empty list is a
        deliberate declaration, and the two are worth distinguishing
        wherever a human asks.
        """
        return self._path

    @property
    def degradation(self) -> LoadDegradation | None:
        """What the load could not read, or ``None`` when it read cleanly.

        An *absent* file is not degradation — a deployment that has never
        written this file is a normal empty store, and conflating the two
        would refuse every write on every fresh install.
        """
        return self._degradation

    @property
    def is_degraded(self) -> bool:
        """Whether this store refuses writes because its load was partial."""
        return self._degradation is not None

    def refuse_if_degraded(self) -> None:
        """Raise rather than rewrite a file this store could not read.

        Public because a caller about to make a *batch* of writes should
        fail before it starts rather than part-way through. Every write path
        calls it too, so calling it is never a way to get a write past the
        refusal, and not calling it is never a way to avoid one.

        The message comes from :meth:`_degraded_write_message`, so what an
        operator reads is written by the store that knows what is at stake.
        """
        degradation = self._degradation
        if degradation is None:
            return
        raise DegradedStoreWriteError(
            self._degraded_write_message(degradation),
            store=self._store_label,
            path=degradation.path,
            recovery=degradation.recovery,
        )

    def refuse_if_stale(self) -> None:
        """Raise rather than rewrite a file that changed after we read it.

        See the module docstring: a degraded load is one way an in-memory
        view stops matching the file, another process writing it is a
        second, and a file appearing after construction is a third. All
        three end the same way, and only the first of them degrades.

        Public for the same reason :meth:`refuse_if_degraded` is, and it is
        a **compare-and-swap, not a lock**: two writers can still interleave
        between this check and the ``os.replace`` inside :meth:`_save`. It
        closes the wide window (a store that loaded minutes ago, which is
        every nightly run) and narrows the tiny one; it does not make the
        write exclusive.

        A fourth way the view stops matching the file is that we cannot
        tell whether it does (#471), and that is checked **first and on
        both operands**, before any comparison runs. Either side being
        :class:`UnknownFileIdentity` means this process never learned what
        the file is, and the only honest answer to "did it change?" is a
        refusal. Deliberately not folded into the equality check: an
        equality that happens to be false for the right reason is one
        refactor away from being true for the wrong one, which is the
        defect being fixed.
        """
        loaded = self._loaded_fingerprint
        current = self._fingerprint()
        # Two branches, not one over a tuple: each has to be independently
        # removable to be independently observable, which is what the
        # mutation tests on this guard rest on.
        if isinstance(loaded, UnknownFileIdentity):
            self._refuse_unreadable(loaded)
        if isinstance(current, UnknownFileIdentity):
            self._refuse_unreadable(current)
        if current == loaded:
            return
        raise StaleStoreWriteError(
            self._stale_write_message(),
            store=self._store_label,
            path=str(self._path),
            recovery=self._stale_recovery,
        )

    def _refuse_unreadable(self, identity: UnknownFileIdentity) -> NoReturn:
        """Refuse a write whose file could not be identified.

        :class:`~trellis.errors.StaleStoreWriteError` rather than a new
        error class or a bare ``OSError``: every surface that writes these
        files already catches it (``trellis policy``, ``trellis analyze``,
        ``POST /api/policies``) and renders its ``recovery``, and the fix
        is the same shape — look, then retry. A fresh exception type would
        escape all of them as a traceback, which is how a guard that
        refuses correctly still ends up looking like a crash.

        The ``recovery`` is **not** ``_stale_recovery``. That one names a
        command that re-reads the store, which here would meet the same
        unreadable path and report the same nothing. What an operator
        needs is the path and its parent: mode, owner and symlink target
        are between them the answer for ``EACCES``, ``ELOOP`` and
        ``ENOTDIR`` alike. Both operands are ``shlex.quote``d for #427's
        reason — a data dir containing a space otherwise word-splits into
        a command that lists three wrong paths.
        """
        raise StaleStoreWriteError(
            self._unreadable_write_message(identity.detail),
            store=self._store_label,
            path=str(self._path),
            recovery=(
                f"ls -ld -- {shlex.quote(str(self._path))} "
                f"{shlex.quote(str(self._path.parent))}"
            ),
        )

    # -- Persistence --

    def _snapshot(self) -> dict[str, RowT]:
        """Shallow copy of the in-memory rows, for rollback on a failed save.

        Rows are only ever replaced wholesale, never mutated in place, so a
        shallow copy is a complete undo. Both corpora are tens of rows; this
        is not a cost worth avoiding.
        """
        return dict(self._rows)

    def _save_or_roll_back(self, restore: dict[str, RowT]) -> None:
        """Persist, and put memory back the way it was if the write fails.

        Without this a refused write still mutated the object: a ``clear()``
        that raised had already emptied ``list()``, and a ``restore()`` that
        raised had already un-suppressed the row in memory — #393's own
        symptom, surviving in-process, landing on exactly the caller the
        store-level refusal exists for (one that catches the error and keeps
        serving from the same store; the API route holds its store in a
        module-level cache across requests). The same applies off the
        degraded path: a full disk must not leave a row in memory that no
        file records.
        """
        try:
            self._save()
        except Exception:
            self._rows = restore
            raise

    def _load(self) -> None:
        """Load rows, recording anything that could not be read.

        The outer catch is broad on purpose, and that breadth was never the
        defect. The defect was that the handler left a state
        indistinguishable from an empty deployment and then let the next
        write act on it. Breadth is what keeps the unconditional promise
        both stores make — constructing one never raises, whatever shape the
        corruption takes, so neither retrieval nor ``policy list`` falls
        over on this file. What changed is that every failure path now
        *records* itself, and the record is what refuses the write.
        """
        try:
            self._load_rows()
        # Degrades, never swallows: the record below is what refuses the write.
        except Exception as exc:
            self._degrade("load_failed", f"{type(exc).__name__}: {exc}")

    def _load_rows(self) -> None:
        """Parse the file into ``self._rows``, degrading per failure."""
        key = self._envelope_key
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError is a ValueError, not an OSError, and is the
            # shape a truncated or partially-binary write actually takes.
            self._degrade("unreadable_file", f"{type(exc).__name__}: {exc}")
            return

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            self._degrade("malformed_json", str(exc))
            return

        if not isinstance(raw, dict):
            self._degrade(
                "malformed_envelope",
                f'expected a JSON object with {self._envelope_article} "{key}" '
                f"list, got {type(raw).__name__}",
            )
            return

        # A *missing* key is degradation, not an empty store. ``_save`` always
        # emits the key, so a dict without it is by construction not a file
        # this store produced: a hand-edit, a renamed field, the wrong file at
        # this path, or a future schema. Defaulting it to ``[]`` — which the
        # old code did, and which #414's first attempt at the same fix also
        # did — loads ``{}`` and a typo'd key as a *clean* empty store and
        # leaves the whole defect intact for those shapes: degrade to empty
        # in silence, then let the next write replace the file.
        if key not in raw:
            self._degrade(
                "malformed_envelope",
                f'JSON object has no "{key}" key (keys: '
                f"{sorted(raw)[:_MAX_REPORTED_ROWS]})",
            )
            return

        if not isinstance(raw[key], list):
            self._degrade(
                "malformed_envelope",
                f'expected "{key}" to be a list, got {type(raw[key]).__name__}',
            )
            return

        skipped: list[str] = []
        for index, entry in enumerate(raw[key]):
            try:
                row = self._parse_row(entry)
            # Broad on purpose: one bad row costs one row, not the file.
            except Exception as exc:
                skipped.append(f"row {index}: {type(exc).__name__}")
                continue
            rejection = self._reject_row(row)
            if rejection is not None:
                skipped.append(f"row {index}: {rejection}")
                continue
            self._rows[self._row_id(row)] = row

        if skipped:
            detail = "; ".join(skipped[:_MAX_REPORTED_ROWS])
            if len(skipped) > _MAX_REPORTED_ROWS:
                detail += f"; (+{len(skipped) - _MAX_REPORTED_ROWS} more)"
            self._degrade("invalid_rows", detail, rows_skipped=len(skipped))
            return

        logger.info(
            self._loaded_event,
            count=len(self._rows),
            path=str(self._path),
        )

    def _degrade(
        self, reason: str, detail: str, *, rows_skipped: int | None = None
    ) -> None:
        """Mark the store degraded and say so at a level operators see.

        ``error``, not ``info``. The CLI's root callback *defaults*
        ``TRELLIS_LOG_LEVEL`` to ``WARNING`` when the env var is absent
        (``trellis_cli.main._root``; an explicit env var always wins), so on
        a default invocation an ``info`` line here is filtered out of the
        surface an operator is most likely to meet this on — the same class
        of no-op as a ``logger.debug`` under an INFO filter.
        """
        self._degradation = LoadDegradation(
            path=str(self._path),
            reason=reason,
            detail=detail,
            rows_loaded=len(self._rows),
            rows_skipped=rows_skipped,
        )
        logger.error(
            self._degraded_event,
            **self._degradation.to_dict(),
            impact=self._degraded_impact,
        )

    def _fingerprint(self) -> FileIdentity:
        """Identity of the file as this store last saw it.

        Three answers, never two (#471). A ``(st_ino, st_mtime_ns,
        st_size)`` tuple is *this* file; ``None`` is *no* file, and only
        ``FileNotFoundError`` produces it; :class:`UnknownFileIdentity` is
        every other ``OSError`` — *don't know*. Collapsing the last two
        into ``None`` is what let ``refuse_if_stale`` compare ``None``
        against ``None`` and pass.

        ``st_ino`` is the load-bearing part: every write here lands through
        ``os.replace`` from a fresh temp file, so a completed write by any
        process changes the inode even if size and mtime happen to collide.
        Size and mtime are kept because they catch the other shape — an
        in-place edit that keeps the inode, which is what ``sed -i`` and
        an editor configured to write through produce.

        Inode *reuse* — a replacement landing on the number this store
        recorded, with mtime and size colliding too — is unreachable by
        construction rather than defended against: ``atomic_write_text``
        creates its temp file while the target still exists, so the
        target's inode is never free to be handed back.

        ``None`` is a value, not the absence of one: a store built while the
        path was absent compares ``None`` against the fingerprint of a file
        that has since appeared, and refuses.

        ``FileNotFoundError`` is caught ahead of ``OSError`` and is the
        **only** shape read as absence. ``NotADirectoryError`` in
        particular is not: a path component that turned into a regular
        file is a broken path, not an empty deployment, and it is one of
        the errnos ``Path.exists`` swallows — the #444 door, which this
        keeps shut on the guard side too.
        """
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return UnknownFileIdentity(f"{type(exc).__name__}: {exc}")
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _save(self) -> None:
        """Persist the current rows to the JSON file.

        Atomic, via :func:`~trellis.core.atomic_write.atomic_write_text`.
        A direct ``write_text`` truncates the destination and *then* writes,
        so a crash, a full disk or a killed process between the two produces
        exactly the half-written file the rest of this module has to
        survive. No other *code* writes these files, so closing that window
        closes the main way the degraded state gets created.

        Atomicity is not a concurrency guarantee, and both files have
        several writer **processes** against the same bind-mounted data dir
        (each store's module docstring names its own). ``os.replace`` makes
        their writes atomic, not ordered; :meth:`refuse_if_stale` is what
        stops one of them rewriting another's work from a stale view.

        Guards first, unconditionally. Every public write path calls them
        too — that is the point, not a redundancy: several of those paths
        return a wrong answer from the in-memory view before they would
        ever reach here, so this guard cannot cover them, and they cannot
        cover a direct call to this one.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            self._envelope_key: [
                # ``mode="json"`` rather than a bare dump: this goes straight
                # into :func:`json.dumps`, whose ``default=str`` is a backstop
                # and not the encoder.
                row.model_dump(mode="json")
                for row in self._rows.values()
            ]
        }
        atomic_write_text(self._path, json.dumps(data, indent=2, default=str))
        # What we just wrote is now what we "loaded": a second write from the
        # same store instance must not trip its own guard.
        self._loaded_fingerprint = self._fingerprint()
