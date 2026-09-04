"""Indexed name → entity resolution and write-time alias maintenance.

Both write paths that resolve ``@mention`` tokens against the existing
graph — the MCP ``save_memory`` tool and the CLI bulk-ingest hook
(:mod:`trellis.extract.memory_ingest_hook`) — build their resolver here
so the matching rule cannot drift between them.

Why this module exists
----------------------

The previous implementations were a case-insensitive ``query(limit=2000)``
full-node scan duplicated in both callers. Three problems:

1. **They never matched.** Both compared ``node["name"]`` against the
   mention, but ``GraphStore.query`` returns the display name inside
   ``node["properties"]["name"]`` — the top-level key does not exist. Every
   mention fell through to the LLM residue stage, which is how the live
   graph accumulated seven separate ``hermes`` nodes with an empty
   ``entity_aliases`` table.
2. **They rescanned from scratch every time.** Nothing learned. The
   ``entity_aliases`` table — an indexed, SCD-2 ``(source_system, raw_id)``
   mapping that already exists on all four backends — was populated only
   by callers of the bulk-ingest API.

   *How strongly uniqueness is enforced differs by backend*, and this
   module is a consumer of that difference rather than a guarantor of it.
   SQLite and Postgres carry a real partial unique index
   (``idx_aliases_current ON entity_aliases(source_system, raw_id)
   WHERE valid_to IS NULL``), so a duplicate current binding is
   *impossible*. The two Bolt backends (Neo4j, ArcadeDB — neither
   overrides ``SCHEMA_STATEMENTS``) index the lookup
   (``alias_lookup_idx FOR (a:Alias) ON (a.source_system, a.raw_id)``)
   but constrain uniqueness only on ``version_id``; one-current-per-pair
   there rests on ``upsert_alias``'s close-then-insert, not on DDL. The
   resolver is safe either way — it reads a *single* row
   (``resolve_alias`` returns one record) and re-validates the binding
   before use — but do not read "the store's unique-current alias index"
   below as a claim that every backend enforces it.
3. **They degraded silently.** Past 2000 nodes the scan simply stopped
   seeing the tail and reported "no match", which is indistinguishable
   from a genuine miss.

The resolver fixes (1), fixes (2) by *minting* an alias whenever a complete
scan resolves a name unambiguously, and makes (3) loud. The write-time binder
and bounded backfill below keep the index complete without raising the scan
cap.

Matching rule and its failure mode
----------------------------------

**Exact equality after :func:`~trellis.schemas.well_known.normalize_entity_name`**
(trim, collapse whitespace, case-fold, NFC) — nothing fuzzy.
Consequences, stated plainly because this code decides entity identity:

* Two *different* entities sharing a normalized name are **ambiguous**:
  the resolver returns both ids, ``AliasMatchExtractor`` treats anything
  other than a single hit as unresolved, and **no alias is minted**. A
  wrong merge is not recoverable; a skipped mention is.
* An alias is only minted from a **complete** scan. If the scan hit its
  cap we may not have seen a same-named node in the tail, so the match is
  used for this call but never cached, and the truncation is logged.
* A binding is re-validated on every hit against the node it points at
  (:func:`_binding_is_live`): if that node was deleted, or renamed so it
  no longer normalizes to the key, the binding is dropped and the scan
  re-runs. What that check *cannot* see is a **second** entity created
  with the same name after the binding — the bound node still matches, so
  the ambiguity the scan would have refused to guess at is bypassed and
  mentions keep resolving to the first one. Accepted: this binds *edges*,
  not identity — the alias path never merges nodes and never rewrites an
  entity, so the worst case is a ``mentions`` edge pointing at the wrong
  one of two same-named entities, which is a deletable edge rather than a
  destroyed node. Detecting it would mean rescanning on every hit, which
  is the O(n) cost this module exists to remove.
  ``get_aliases(entity_id, source_system="name")`` is the audit surface
  for what got bound; there is no revoke command yet (see the PR's known
  gaps) — ``delete_node`` on the entity is the only unbind today.

Governance note
---------------

The mint is a **direct ``graph_store.upsert_alias`` call**, not a
``MutationExecutor`` command, because the pipeline has no alias verb —
:mod:`trellis_api.routes.ingest` already writes aliases directly for the
same reason. Adding a governed alias verb must convert that route, this
binder, and the backfill together. The alias row is itself SCD-2 and carries
``raw_name`` / ``valid_from``, so what was bound and when is recoverable
from the store even though no event was emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple

import structlog

from trellis.schemas.well_known import normalize_entity_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: ``entity_aliases.source_system`` namespace for display-name bindings
#: minted by the extraction write paths. Kept separate from the CLI's
#: ``"local"`` namespace so an operator-curated alias and an inferred one
#: are never confused, and so the store's unique-current index on
#: ``(source_system, raw_id)`` gives us exactly one entity per normalized
#: name in this namespace.
NAME_ALIAS_SOURCE_SYSTEM = "name"

#: Upper bound on the bootstrap scan. Only reached on an index miss; every
#: successful resolution removes one future scan. No production caller
#: overrides it — the ``scan_limit`` kwarg exists so tests can drive the
#: truncation branch without building a 2000-node graph.
#:
#: Two things the "self-extinguishing bootstrap" framing does *not* cover,
#: both measured on the reference deployment (2026-08-27):
#:
#: * **Only a resolvable name extinguishes.** A mention that matches zero
#:   nodes, or matches ambiguously, never mints — so it scans once per
#:   occurrence, forever. Of 119 ``@mention`` occurrences across 1239
#:   production documents, **118 match nothing**: they are email-address
#:   and package-scope fragments (``@gmail`` x21, ``@modelcontextprotocol``
#:   x6, ``@upstash``, ``@react-native-async-storage``). Minting is
#:   structurally unable to retire those; not repeating them within a
#:   document is (see :mod:`trellis.extract.alias_match`).
#: * **Past the cap the resolver stops learning, and starts being wrong.**
#:   ``GraphStore.query`` is ``ORDER BY created_at DESC LIMIT n``, so above
#:   ``scan_limit`` current nodes the scan sees only the newest window:
#:   an older entity reports a clean "no match" — the duplicate-``hermes``
#:   failure this module was built to end, returning by a different door —
#:   and ``mintable`` is permanently ``False``, so the index can never
#:   bootstrap. The reference graph held 964 current nodes growing
#:   ~30/day (~17/day excluding a one-off backfill), i.e. the cliff is
#:   weeks out, not years.
#:
#: Raising this number is deliberately **not** the fix: it delays the
#: cliff, multiplies the per-mention fetch linearly, and leaves the silent
#: wrong answer intact. :func:`bind_name_alias` runs after governed entity
#: writes, and :func:`backfill_name_aliases` repairs existing nodes.
DEFAULT_NAME_SCAN_LIMIT = 2000


class _ScanResult(NamedTuple):
    """Outcome of the bootstrap scan.

    ``mintable`` is the single predicate the caller needs: a *complete*
    scan that found *exactly one* name match. Deriving it here rather than
    re-testing ``truncated``/``len(matches)`` at the call site keeps the
    mint rule and the truncation warning from drifting apart.
    """

    matches: list[str]
    mintable: bool


NameAliasBindResult = Literal[
    "bound",
    "already_bound",
    "kept_existing",
    "contested",
    "skipped",
]


@dataclass(frozen=True)
class BackfillReport:
    """Outcome of one bounded name-alias backfill pass."""

    bound: int
    already_bound: int
    contested_keys: list[str]
    skipped: int
    truncated: bool


def bind_name_alias(  # noqa: PLR0911 - each return is a distinct outcome
    graph_store: GraphStore,
    *,
    entity_id: str,
    name: str,
) -> NameAliasBindResult:
    """Best-effort bind of a normalized display name, preserving first-wins.

    The exact-name twin check is the strongest check the current
    :meth:`GraphStore.query` contract permits. It does not prove uniqueness
    across case variants; callers must not treat this alias as an identity
    merge.
    """
    if not isinstance(name, str):
        return "skipped"
    key = normalize_entity_name(name)
    if not key:
        return "skipped"

    try:
        row = graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
    except Exception:
        logger.exception(
            "entity_resolution_name_alias_lookup_failed", entity_id=entity_id
        )
        return "skipped"

    bound_id = str(row.get("entity_id")) if row and row.get("entity_id") else None
    if bound_id == entity_id:
        return "already_bound"
    if bound_id is not None and _binding_is_live(
        graph_store, entity_id=bound_id, key=key
    ):
        logger.warning(
            "entity_resolution_name_alias_kept_existing",
            alias_key=key,
            entity_id=entity_id,
            existing_entity_id=bound_id,
        )
        return "kept_existing"

    try:
        twins = graph_store.query(properties={"name": name}, limit=2)
    except Exception:
        logger.exception(
            "entity_resolution_name_alias_twin_check_failed",
            entity_id=entity_id,
        )
        return "skipped"
    if any(str(node.get("node_id")) != entity_id for node in twins):
        logger.warning(
            "entity_resolution_name_alias_contested",
            alias_key=key,
            entity_id=entity_id,
        )
        return "contested"

    if not _mint_alias(graph_store, entity_id=entity_id, key=key, mention=name):
        return "skipped"
    return "bound"


def backfill_name_aliases(
    graph_store: GraphStore,
    *,
    max_nodes: int,
) -> BackfillReport:
    """Bind unique current names from one bounded, complete graph snapshot.

    A result larger than ``max_nodes`` is discarded before any alias write.
    Re-running with a larger bound resumes safely; already-bound keys are
    counted without creating another alias version.
    """
    if max_nodes < 1:
        msg = "max_nodes must be >= 1"
        raise ValueError(msg)

    nodes = graph_store.query(limit=max_nodes + 1)
    if len(nodes) > max_nodes:
        return BackfillReport(
            bound=0,
            already_bound=0,
            contested_keys=[],
            skipped=0,
            truncated=True,
        )

    by_key: dict[str, list[tuple[str, str]]] = {}
    skipped = 0
    for node in nodes:
        name = (node.get("properties") or {}).get("name")
        node_id = node.get("node_id")
        if not isinstance(name, str) or not node_id:
            skipped += 1
            continue
        key = normalize_entity_name(name)
        if not key:
            skipped += 1
            continue
        by_key.setdefault(key, []).append((str(node_id), name))

    contested = {key for key, rows in by_key.items() if len(rows) > 1}
    bound = 0
    already_bound = 0
    for key, rows in by_key.items():
        if len(rows) != 1:
            continue
        entity_id, name = rows[0]
        result = bind_name_alias(graph_store, entity_id=entity_id, name=name)
        if result == "bound":
            bound += 1
        elif result == "already_bound":
            already_bound += 1
        elif result in {"contested", "kept_existing"}:
            contested.add(key)
        else:
            skipped += 1

    return BackfillReport(
        bound=bound,
        already_bound=already_bound,
        contested_keys=sorted(contested),
        skipped=skipped,
        truncated=False,
    )


def build_name_alias_resolver(
    graph_store: GraphStore,
    *,
    scan_limit: int = DEFAULT_NAME_SCAN_LIMIT,
    mint: bool = True,
) -> Callable[[str], list[str]]:
    """Build an :data:`~trellis.extract.alias_match.AliasResolver`.

    The returned callable maps a mention string to zero-or-more entity ids:
    empty for no match, one element for an unambiguous match, several when
    the name is ambiguous (the caller must not guess).

    Resolution order:

    1. ``graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)`` — one
       indexed row read on the store's unique-current alias index, then one
       ``get_node`` to confirm the binding still points at a live node that
       still carries the name.
    2. On a miss, a bounded scan of ``graph_store.query(limit=scan_limit)``
       comparing normalized ``properties["name"]``. An unambiguous hit from
       a complete scan is written back to the alias index so step 1 answers
       it next time.

    Args:
        graph_store: The knowledge-plane graph store. Read for both steps
            and written (aliases only) when *mint* is on.
        scan_limit: Node cap for the step-2 bootstrap scan. See
            :data:`DEFAULT_NAME_SCAN_LIMIT`.
        mint: Set ``False`` to make the resolver read-only — used by
            callers that must not write, and by tests asserting the
            scan/index split.

    Returns:
        A ``Callable[[str], list[str]]``. It never raises — not for an
        unresolvable name, not for a store outage (logged, treated as no
        match, so one bad mention cannot fail the ingest that triggered
        it), and not from the mint step, where a failed write costs a
        future scan rather than a resolution.
    """

    def resolve(mention: str) -> list[str]:
        key = normalize_entity_name(mention)
        if not key:
            return []

        bound = _resolve_via_index(graph_store, key=key, mention=mention)
        if bound is not None:
            return [bound]

        scan = _scan_for_name(
            graph_store, key=key, mention=mention, scan_limit=scan_limit
        )
        if mint and scan.mintable:
            _mint_alias(
                graph_store, entity_id=scan.matches[0], key=key, mention=mention
            )
        return scan.matches

    return resolve


def _resolve_via_index(
    graph_store: GraphStore, *, key: str, mention: str
) -> str | None:
    """Look *key* up in the alias index, or ``None`` on a miss.

    Never raises: an alias-index outage must degrade to the scan, not fail
    the ingest that triggered it. A hit whose node has since been deleted
    or renamed is treated as a miss so the scan can re-bind — otherwise a
    stale binding would keep emitting edges to a node that is gone.
    """
    try:
        row = graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
    except Exception:
        logger.exception("entity_resolution_index_lookup_failed", mention=mention)
        return None
    entity_id: str | None = row.get("entity_id") if row else None
    if not entity_id:
        return None
    if not _binding_is_live(graph_store, entity_id=entity_id, key=key):
        return None
    return entity_id


def _binding_is_live(graph_store: GraphStore, *, entity_id: str, key: str) -> bool:
    """Is the bound node still present and still named *key*?

    One indexed ``get_node`` — the cost that keeps a binding from
    outliving its entity. A lookup failure counts as live: an outage on
    the validation read should not silently discard a good binding.
    """
    try:
        node = graph_store.get_node(entity_id)
    except Exception:
        logger.exception("entity_resolution_binding_check_failed", entity_id=entity_id)
        return True
    if node is not None:
        name = (node.get("properties") or {}).get("name")
        if isinstance(name, str) and normalize_entity_name(name) == key:
            return True
    logger.warning(
        "entity_resolution_stale_binding_dropped",
        alias_key=key,
        entity_id=entity_id,
        reason="node_missing" if node is None else "name_changed",
    )
    return False


def _scan_for_name(
    graph_store: GraphStore, *, key: str, mention: str, scan_limit: int
) -> _ScanResult:
    """Bootstrap scan over node display names."""
    try:
        nodes = graph_store.query(limit=scan_limit)
    except Exception:
        logger.exception("entity_resolution_scan_failed", mention=mention)
        return _ScanResult([], mintable=False)

    truncated = len(nodes) >= scan_limit
    matches: list[str] = []
    for node in nodes:
        properties = node.get("properties") or {}
        name = properties.get("name")
        if isinstance(name, str) and normalize_entity_name(name) == key:
            node_id = node.get("node_id")
            if node_id:
                matches.append(str(node_id))

    if truncated:
        # LOUD-DEGRADATION: past the cap the tail was never examined, so a
        # miss is not evidence of absence *and* a single hit is not
        # evidence of uniqueness — the same-named node may be in the tail.
        # Warn on every truncated scan, including the one that looks
        # unambiguous; that is the case where acting on it binds an edge
        # to possibly the wrong entity.
        logger.warning(
            "entity_resolution_scan_truncated",
            mention=mention,
            scan_limit=scan_limit,
            matches=len(matches),
        )
    return _ScanResult(matches, mintable=not truncated and len(matches) == 1)


def _mint_alias(
    graph_store: GraphStore, *, entity_id: str, key: str, mention: str
) -> bool:
    """Bind *key* to *entity_id* so the next resolution is an index read.

    Written straight to the graph store rather than through the
    ``MutationExecutor`` — see the module docstring's governance note.
    ``upsert_alias`` is idempotent via SCD-2 versioning.

    Best-effort — a failed write only costs a future scan.
    """
    try:
        graph_store.upsert_alias(
            entity_id=entity_id,
            source_system=NAME_ALIAS_SOURCE_SYSTEM,
            raw_id=key,
            raw_name=mention,
        )
    except Exception:
        logger.exception(
            "entity_resolution_alias_mint_failed",
            mention=mention,
            entity_id=entity_id,
        )
        return False
    logger.info(
        "entity_resolution_alias_minted",
        mention=mention,
        alias_key=key,
        entity_id=entity_id,
    )
    return True
