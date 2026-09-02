"""Policy store — JSON file-based persistence for governance policies.

Failure posture — read leniently, refuse to write
-------------------------------------------------
This store whole-file-rewrites on every write: ``_save`` serialises the
in-memory rows over the path it loaded from. Until #413 it also degraded an
unreadable file to an *empty* set, and those two behaviours in sequence are
how a damaged access-control file becomes a transparent one:

1. ``_load`` fails, logs, leaves the store empty.
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

This is #393's resolution for :class:`~trellis.stores.advisory_store.AdvisoryStore`
(landed in #414) applied here, and the generalisation is worth stating: **the axis is
read-vs-write, not policy-vs-advisory.** ``policy_source`` has no write
path, so it never had to answer the write question; this store, which
does, was never held to the same standard. Since #426 the machinery the two
stores share lives in
:class:`~trellis.stores.degradable_json_store.DegradableJsonStore`; what
stays here is what only makes sense about *policies*.

Per-row, not per-file
---------------------
Validation is per row. One unparseable entry — a hand-edit, a renamed
field — costs that entry rather than the ruleset, so ``policy list`` still
shows the policies that *are* readable. The pre-fix blast radius was
identical in both stores — the old handler wrapped the whole loop, so a
single bad row discarded the lot — but it costs more here: for advisories
the operator loses hints, for policies they lose their only view of what
the deployment is enforcing.

Per-row leniency is only safe *because* the write refuses — a partial load
followed by a permitted write rewrites the file without the skipped rows,
which is the same laundering at a narrower granularity. The two halves are
a pair; neither is safe alone.

Degradation is not the only stale view
--------------------------------------
The laundering primitive is *a whole-file rewrite from an in-memory view
that is no longer the file*, and a degraded load is only one way to get
one. Two others reach the identical end state with nothing degraded:

* **Another process wrote the file.** The reference deployment runs a host
  CLI (``trellis policy add``) and a containerised API (``POST
  /api/policies``) against the same bind-mounted ``policies.json``, so
  "this store is the file's only writer" was never true. A store that
  loaded ``[A]`` and then rewrites the file after the CLI has made it
  ``[A, B]`` deletes ``B`` — from disk and from Stage 2 — with no error
  anywhere.
* **The file appeared after construction.** A store built while the path
  was absent is not degraded, and its first write replaces a file it never
  read.

A third takes a different route to the same place, and is handled in the
load instead: a **duplicate ``policy_id``**. This store keys by id while
``policy_source`` builds a *list* and evaluates every duplicate (deny
wins), so collapsing them silently made the two readers disagree about what
the file says — and the next permitted write would have made the file match
the smaller view. It is treated as an invalid row, so the read degrades and
the write refuses like any other partial load. That is the one row-level
rule this store adds to the shared load ladder
(:meth:`PolicyStore._reject_row`).

The first two are closed by one guard: ``refuse_if_stale`` records a
fingerprint of the file as loaded and refuses
(:class:`~trellis.errors.StaleStoreWriteError`) if it no longer matches.
Unlike a degraded load this one is **transient** — re-read and redo, rather
than go and look at the file. It is a compare-and-swap, not a lock: two
writers can still interleave between the check and the ``os.replace``, so
it closes the wide window and narrows the tiny one. Last-writer-wins
remains the model.

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

from typing import Any, ClassVar

import structlog

from trellis.schemas.policy import Policy
from trellis.stores.degradable_json_store import DegradableJsonStore, LoadDegradation

logger = structlog.get_logger(__name__)


class PolicyStore(DegradableJsonStore[Policy]):
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
    :class:`~trellis.errors.DegradedStoreWriteError`. The mechanism is
    :class:`~trellis.stores.degradable_json_store.DegradableJsonStore`;
    everything an operator reads about *policies* is below.
    """

    _envelope_key: ClassVar[str] = "policies"
    _store_label: ClassVar[str] = "policy"
    _loaded_event: ClassVar[str] = "policies_loaded"
    _degraded_event: ClassVar[str] = "policy_load_degraded"
    _degraded_impact: ClassVar[str] = (
        "Policies that parsed are still listed; every write is refused "
        "so the unreadable file cannot be replaced by the partial view. "
        "Enforcement reads this file separately and strictly, and is "
        "failing closed on it."
    )
    _stale_recovery: ClassVar[str] = "trellis policy list"

    # -- Row handling --

    @staticmethod
    def _parse_row(entry: Any) -> Policy:
        return Policy.model_validate(entry)

    @staticmethod
    def _row_id(row: Policy) -> str:
        return row.policy_id

    def _reject_row(self, row: Policy) -> str | None:
        """A duplicate id is degradation, not a last-one-wins overwrite.

        This store keys by ``policy_id`` while the enforcement reader
        builds a *list* and evaluates every duplicate (deny wins), so
        collapsing them silently makes the two readers disagree about what
        the file says — and the next permitted write would rewrite the file
        with the collapsed view, deleting a rule the gate was enforcing.
        Same laundering, different route in.

        :class:`~trellis.stores.advisory_store.AdvisoryStore` deliberately
        does *not* do this: nothing else reads ``advisories.json`` as a
        list, so there is no second reader for it to disagree with.
        """
        if row.policy_id in self._rows:
            return "duplicate policy_id"
        return None

    # -- Public API --

    def list(self) -> list[Policy]:
        """Return all policies, in file order.

        Works on a degraded store, serving whatever parsed. That is the
        lenient half of the posture and the whole reason this reader
        differs from the enforcement one: an operator whose policy file
        just broke needs ``trellis policy list`` to still tell them what
        state they are in. **A degraded store's list is not the ruleset** —
        callers rendering it must say so (:attr:`degradation`).
        """
        return list(self._rows.values())

    def get(self, policy_id: str) -> Policy | None:
        """Get a policy by ID.

        **A ``None`` from a degraded store does not mean "no such
        policy"** — it may mean "the row was unreadable". Callers that
        report absence to a human (``policy show``, ``DELETE
        /policies/{id}``) must check :attr:`is_degraded` before calling it
        a 404.
        """
        return self._rows.get(policy_id)

    def add(self, policy: Policy) -> Policy:
        """Add or replace a policy. Persists immediately."""
        self.refuse_if_degraded()
        self.refuse_if_stale()
        restore = self._snapshot()
        self._rows[policy.policy_id] = policy
        self._save_or_roll_back(restore)
        logger.info("policy_stored", policy_id=policy.policy_id)
        return policy

    def remove(self, policy_id: str) -> bool:
        """Remove a policy by ID. Returns ``True`` if found.

        Refuses *before* the membership check, not after — for both
        reasons. On a degraded store the check would answer from a partial
        view and report ``False`` ("no such policy") for a policy that
        exists in the file and merely failed to parse. On a **stale** store
        it would do the same for a policy another process added since this
        one loaded. Both are wrong answers rather than unhelpful ones, and
        ``_save``'s guards are too late to prevent them: this path returns
        before reaching a write at all.
        """
        self.refuse_if_degraded()
        self.refuse_if_stale()
        if policy_id not in self._rows:
            return False
        restore = self._snapshot()
        del self._rows[policy_id]
        self._save_or_roll_back(restore)
        logger.info("policy_removed", policy_id=policy_id)
        return True

    # -- Refusal messages --

    def _degraded_write_message(self, degradation: LoadDegradation) -> str:
        """What rewriting a partially-read ``policies.json`` would cost.

        Note what taking the ``mv`` advice means. Usually the canonical path
        is then *absent*, which is a legitimate, transparent, zero-policy
        deployment — right only if the operator re-declares the policies
        afterwards. But if a file also sits at the **legacy** path
        (``<data_dir>/policies.json``), ``resolve_policy_path`` falls back
        to it, and the deployment silently starts enforcing that stale
        ruleset instead. Check ``trellis policy list`` after the move; it
        names the file actually in force.
        """
        return (
            f"Refusing to write the Trellis policy file at {degradation.path}: "
            f"it loaded degraded ({degradation.reason}: {degradation.detail}). "
            f"{degradation.rows_loaded} policy/policies parsed and are being "
            f"shown; {degradation.rows_skipped_display} could not be read. "
            "Writing would replace the file with only what parsed — which the "
            "strict enforcement reader would then accept as the whole ruleset, "
            "silently un-governing every mutation the missing policies covered. "
            "To reset:"
        )

    def _stale_write_message(self) -> str:
        """What rewriting a ``policies.json`` that moved under us would cost."""
        return (
            f"Refusing to write the Trellis policy file at {self._path}: it "
            "changed after this process read it, so writing would replace "
            "whatever landed in between — silently un-governing every "
            "mutation those policies covered. Re-read and retry:"
        )
