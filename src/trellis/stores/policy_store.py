"""Policy store — JSON file-based persistence for governance policies.

Failure posture — read leniently, refuse to write
-------------------------------------------------
This store whole-file-rewrites on every write: :meth:`PolicyStore._save`
serialises ``self._policies.values()`` over the path it loaded from. Until
#413 it also degraded an unreadable file to an *empty* set, and those two
behaviours in sequence are how a damaged access-control file becomes a
transparent one:

1. ``_load`` fails, logs, leaves ``self._policies == {}``.
2. Any CRUD write lands — ``trellis policy add``, ``POST /api/policies``.
3. ``_save`` writes a file containing only what survived the load, which
   for a whole-file failure is **nothing**.

The bite is in what happens next. :mod:`trellis.mutate.policy_source` is
deliberately *strict* — it raises :class:`~trellis.errors.ConfigError`
rather than degrade, on the reasoning that a corrupt access-control file
must not silently disable access control. That reasoning is right and it
is **insufficient**, because it reasons about the read and the exposure
comes from the write: after step 3 the file is valid JSON with a valid
``policies`` list, so the strict reader parses it without complaint,
:meth:`~trellis.mutate.policy_gate.DefaultPolicyGate.check` allows
everything that matches no policy, and Stage 2 is a no-op again. Nothing in
that chain errors and every surface reports normal. The write launders the
corruption past the strict reader.

So: **the read degrades and the write refuses.** A store that could not
read its file in full still serves what it did read — ``trellis policy
list`` is the reason this reader is lenient at all, and an operator whose
file just broke is exactly who needs it — and raises
:class:`~trellis.errors.DegradedStoreWriteError` from every write path. The
damaged bytes stay on disk, where an operator can look at them and where
the strict enforcement reader keeps failing closed on them.

This is #414's resolution for :class:`~trellis.stores.advisory_store.AdvisoryStore`
applied here, and the generalisation is worth stating: **the axis is
read-vs-write, not policy-vs-advisory.** ``policy_source`` has no write
path, so it never had to answer the write question; this store, which
does, was never held to the same standard.

Per-row, not per-file
---------------------
Validation is per row. One unparseable entry — a hand-edit, a renamed
field — costs that entry rather than the ruleset, so ``policy list`` still
shows the policies that *are* readable. That matters more here than for
advisories, where the pre-fix blast radius was the same: the old handler
wrapped the whole loop, so a single bad row discarded every policy from
the operator's view of their own governance.

Per-row leniency is only safe *because* the write refuses — a partial load
followed by a permitted write rewrites the file without the skipped rows,
which is the same laundering at a narrower granularity. The two halves are
a pair; neither is safe alone.

The two readers disagree on purpose
-----------------------------------
On a file with one bad row this store serves the good rows while
``policy_source`` raises and takes the mutation pipeline down. That
disagreement is the design, and it is safe in that direction only:
*display* degrades, *enforcement* fails closed. It must never be
"corrected" by making the enforcement reader lenient, and this store must
never write back the partial view it is showing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from trellis.core.atomic_write import atomic_write_text
from trellis.errors import DegradedStoreWriteError
from trellis.schemas.policy import Policy

logger = structlog.get_logger(__name__)

#: How many per-row failures to name in the degradation detail. Enough to
#: recognise a pattern (one renamed field shows up identically on every
#: row), short enough that a cron log line stays readable.
_MAX_REPORTED_ROWS = 3


@dataclass(frozen=True, slots=True)
class PolicyLoadDegradation:
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
    #: Deliberately not ``0``. "0 could not be read" tells an operator that
    #: nothing was lost, when the whole ruleset may be sitting unread;
    #: :attr:`rows_skipped_display` is what the surfaces render so the two
    #: cases cannot present identically.
    rows_skipped: int | None = None

    @property
    def recovery(self) -> str:
        """The shell command that clears the degraded state.

        Deliberately concrete: an operator meets this in a cron log or a
        failed deploy, where a diagnosis is worth much less than the fix.
        Moving the file aside rather than deleting it keeps the bytes,
        which for an access-control file are the only record of what the
        deployment was enforcing — no machine can rebuild them.

        Note what taking this advice means: the canonical path is then
        *absent*, which is a legitimate, transparent, zero-policy
        deployment. That is the right end state only if the operator
        re-declares the policies afterwards.
        """
        return f"mv {self.path} {self.path}.corrupt"

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


class PolicyStore:
    """Load and save policies from a JSON file.

    Lightweight persistence suitable for local and single-node deployments.
    Policies are small, rarely change, and are loaded in full at startup —
    a JSON file is the right weight class.

    This is the **CRUD** store: it backs ``trellis policy`` and
    ``/api/policies``. It is emphatically *not* the enforcement path — the
    mutation pipeline loads through
    :func:`trellis.mutate.policy_source.load_policies`, which is strict and
    raises. See the module docstring for why the two readers differ and why
    that difference is only safe while this store refuses to write.

    File format::

        {"policies": [<Policy.model_dump()>, ...]}

    A store whose file could not be read in full is **degraded**: reads
    serve what parsed, writes raise
    :class:`~trellis.errors.DegradedStoreWriteError`.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._policies: dict[str, Policy] = {}
        self._degradation: PolicyLoadDegradation | None = None
        if self._path.exists():
            self._load()

    # -- Public API --

    @property
    def path(self) -> Path:
        """The file this store reads and writes.

        Public because the CRUD surfaces have to tell an operator *which*
        file produced an empty answer: an absent file is the shipped
        default posture, a file declaring ``{"policies": []}`` is a
        deliberate declaration, and the two are worth distinguishing where
        a human asks — see :mod:`trellis.mutate.policy_source` on why
        enforcement deliberately does not distinguish them.
        """
        return self._path

    @property
    def degradation(self) -> PolicyLoadDegradation | None:
        """What the load could not read, or ``None`` when it read cleanly.

        An *absent* file is not degradation — Trellis ships zero default
        policies, so a deployment that has never declared one is a normal
        empty store, and the transparent gate that follows from it is the
        documented default posture (see
        :mod:`trellis.mutate.policy_source`).
        """
        return self._degradation

    @property
    def is_degraded(self) -> bool:
        """Whether this store refuses writes because its load was partial."""
        return self._degradation is not None

    def list(self) -> list[Policy]:
        """Return all policies, ordered by creation time.

        Works on a degraded store, serving whatever parsed. That is the
        lenient half of the posture and the whole reason this reader
        differs from the enforcement one: an operator whose policy file
        just broke needs ``trellis policy list`` to still tell them what
        state they are in. **A degraded store's list is not the ruleset** —
        callers rendering it must say so (:attr:`degradation`).
        """
        return list(self._policies.values())

    def get(self, policy_id: str) -> Policy | None:
        """Get a policy by ID.

        **A ``None`` from a degraded store does not mean "no such
        policy"** — it may mean "the row was unreadable". Callers that
        report absence to a human (``policy show``, ``DELETE
        /policies/{id}``) must check :attr:`is_degraded` before calling it
        a 404.
        """
        return self._policies.get(policy_id)

    def add(self, policy: Policy) -> Policy:
        """Add or replace a policy. Persists immediately."""
        self.refuse_if_degraded()
        restore = self._snapshot()
        self._policies[policy.policy_id] = policy
        self._save_or_roll_back(restore)
        logger.info("policy_stored", policy_id=policy.policy_id)
        return policy

    def remove(self, policy_id: str) -> bool:
        """Remove a policy by ID. Returns ``True`` if found.

        Refuses on a degraded store *before* the membership check, not
        after. The check would otherwise answer from a partial view and
        report ``False`` — "no such policy" — for a policy that exists in
        the file and merely failed to parse.
        """
        self.refuse_if_degraded()
        if policy_id not in self._policies:
            return False
        restore = self._snapshot()
        del self._policies[policy_id]
        self._save_or_roll_back(restore)
        logger.info("policy_removed", policy_id=policy_id)
        return True

    def refuse_if_degraded(self) -> None:
        """Raise rather than rewrite a file this store could not read.

        Public so a caller about to make a *batch* of writes can fail
        before it starts rather than part-way through. Every write path
        calls it too, so calling it is never a way to get a write past the
        refusal, and not calling it is never a way to avoid one.
        """
        degradation = self._degradation
        if degradation is None:
            return
        msg = (
            f"Refusing to write the Trellis policy file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} policy/policies parsed and are being "
            f"shown; {degradation.rows_skipped_display} could not be read. "
            "Writing would replace the file with only what parsed — which the "
            "strict enforcement reader would then accept as the whole ruleset, "
            "silently un-governing every mutation the missing policies covered. "
            "To reset:"
        )
        raise DegradedStoreWriteError(
            msg,
            store="policy",
            path=degradation.path,
            recovery=degradation.recovery,
        )

    # -- Persistence --

    def _snapshot(self) -> dict[str, Policy]:
        """Shallow copy of the in-memory rows, for rollback on a failed save.

        :class:`Policy` is only ever replaced wholesale, never mutated in
        place, so a shallow copy is a complete undo. A ruleset is tens of
        rows; this is not a cost worth avoiding.
        """
        return dict(self._policies)

    def _save_or_roll_back(self, restore: dict[str, Policy]) -> None:
        """Persist, and put memory back the way it was if the write fails.

        Without this a failed write still mutated the object, so a process
        that catches the error and keeps serving — the API route holds its
        store in a module-level cache across requests — would go on
        answering ``policy list`` from a ruleset that is not on disk. That
        applies off the degraded path too: a full disk must not leave a
        policy in memory that no file records.
        """
        try:
            self._save()
        except Exception:
            self._policies = restore
            raise

    def _load(self) -> None:
        """Load policies, recording anything that could not be read.

        The outer catch is broad on purpose, and that breadth was never the
        defect. Breadth is what keeps ``trellis policy list`` working
        whatever shape the corruption takes — constructing this store never
        raises. What changed in #413 is that every failure path now
        *records* itself, and the record is what refuses the write.
        """
        try:
            self._load_rows()
        # Degrades, never swallows: the record below is what refuses the write.
        except Exception as exc:
            self._degrade("load_failed", f"{type(exc).__name__}: {exc}")

    def _load_rows(self) -> None:
        """Parse the file into ``self._policies``, degrading per failure."""
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
                'expected a JSON object with a "policies" list, got '
                f"{type(raw).__name__}",
            )
            return

        # A *missing* key is degradation, not an empty ruleset. ``_save``
        # always emits ``policies``, so a dict without it is by construction
        # not a file this store produced: a hand-edit, a renamed field, the
        # wrong file at this path, or a future schema. Defaulting it to
        # ``[]`` — which the old code did, and which #414's first attempt at
        # the same fix also did — loads ``{}`` and ``{"policys": [...]}`` as
        # a *clean* empty store and leaves the whole defect intact for those
        # shapes: degrade to empty in silence, then let the next write
        # replace the file.
        if "policies" not in raw:
            self._degrade(
                "malformed_envelope",
                'JSON object has no "policies" key (keys: '
                f"{sorted(raw)[:_MAX_REPORTED_ROWS]})",
            )
            return

        if not isinstance(raw["policies"], list):
            self._degrade(
                "malformed_envelope",
                'expected "policies" to be a list, got '
                f"{type(raw['policies']).__name__}",
            )
            return

        skipped: list[str] = []
        for index, entry in enumerate(raw["policies"]):
            try:
                policy = Policy.model_validate(entry)
            # Broad on purpose: one bad row costs one row, not the ruleset.
            except Exception as exc:
                skipped.append(f"row {index}: {type(exc).__name__}")
                continue
            self._policies[policy.policy_id] = policy

        if skipped:
            detail = "; ".join(skipped[:_MAX_REPORTED_ROWS])
            if len(skipped) > _MAX_REPORTED_ROWS:
                detail += f"; (+{len(skipped) - _MAX_REPORTED_ROWS} more)"
            self._degrade("invalid_rows", detail, rows_skipped=len(skipped))
            return

        logger.info(
            "policies_loaded",
            count=len(self._policies),
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
        self._degradation = PolicyLoadDegradation(
            path=str(self._path),
            reason=reason,
            detail=detail,
            rows_loaded=len(self._policies),
            rows_skipped=rows_skipped,
        )
        logger.error(
            "policy_load_degraded",
            **self._degradation.to_dict(),
            impact=(
                "Policies that parsed are still listed; every write is refused "
                "so the unreadable file cannot be replaced by the partial view. "
                "Enforcement reads this file separately and strictly, and is "
                "failing closed on it."
            ),
        )

    def _save(self) -> None:
        """Persist current policies to the JSON file.

        Atomic, via :func:`~trellis.core.atomic_write.atomic_write_text`.
        A direct ``write_text`` truncates the destination and *then* writes,
        so a crash, a full disk or a killed process between the two produces
        exactly the half-written file the rest of this module has to
        survive. This store is the file's only writer, so closing that
        window closes the main way the state gets created.
        """
        self.refuse_if_degraded()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "policies": [p.model_dump(mode="json") for p in self._policies.values()]
        }
        atomic_write_text(self._path, json.dumps(data, indent=2, default=str))
