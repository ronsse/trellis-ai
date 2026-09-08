"""Indexed name → entity resolution.

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

The resolver fixes (1), uses aliases maintained by governed entity writes and
the governed backfill for (2), and makes (3) loud. It is deliberately
read-only: retrieval must never mutate a store as a side effect.

Matching rule and its failure mode
----------------------------------

**Exact equality after :func:`~trellis.schemas.well_known.normalize_entity_name`**
(trim, collapse whitespace, case-fold, NFC) — nothing fuzzy.
Consequences, stated plainly because this code decides entity identity:

* Two *different* entities sharing a normalized name are **ambiguous**:
  the resolver returns both ids, ``AliasMatchExtractor`` treats anything
  other than a single hit as unresolved, and **no alias is minted**. A
  wrong merge is not recoverable; a skipped mention is.
* A bounded fallback scan never writes. If the scan hit its cap we may not
  have seen a same-named node in the tail, so truncation is logged.
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

Alias writes live in :mod:`trellis.mutate.handlers` and
:mod:`trellis.mutate.name_aliases`, where they traverse the governed mutation
pipeline and emit audit events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import structlog

from trellis.schemas.well_known import normalize_entity_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: ``entity_aliases.source_system`` namespace for display-name bindings
#: maintained by governed entity writes and the governed backfill. Kept
#: separate from the CLI's
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
#: wrong answer intact. Governed entity writes maintain the index and
#: :func:`trellis.mutate.name_aliases.backfill_name_aliases` repairs
#: existing nodes.
DEFAULT_NAME_SCAN_LIMIT = 2000


class _ScanResult(NamedTuple):
    """Outcome of the bounded fallback scan."""

    matches: list[str]


def build_name_alias_resolver(
    graph_store: GraphStore,
    *,
    scan_limit: int = DEFAULT_NAME_SCAN_LIMIT,
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
    2. On a miss, a bounded read-only scan of
       ``graph_store.query(limit=scan_limit)`` comparing normalized
       ``properties["name"]``.

    Args:
        graph_store: The knowledge-plane graph store. Read for both steps.
        scan_limit: Node cap for the step-2 bootstrap scan. See
            :data:`DEFAULT_NAME_SCAN_LIMIT`.

    Returns:
        A ``Callable[[str], list[str]]``. It never raises — not for an
        unresolvable name, not for a store outage (logged, treated as no
        match, so one bad mention cannot fail the ingest that triggered it).
    """

    def resolve(mention: str) -> list[str]:
        key = normalize_entity_name(mention)
        if not key:
            return []

        bound = _resolve_via_index(graph_store, key=key, mention=mention)
        if bound is not None:
            return [bound]

        return _scan_for_name(
            graph_store, key=key, mention=mention, scan_limit=scan_limit
        ).matches

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
        return _ScanResult([])

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
    return _ScanResult(matches)
