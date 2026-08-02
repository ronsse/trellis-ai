"""Indexed name → entity resolution for the extraction write paths.

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
   ``entity_aliases`` table — an indexed, SCD-2, unique-per-current
   ``(source_system, raw_id)`` mapping that already exists on all four
   backends — was populated only by callers of the bulk-ingest API.
3. **They degraded silently.** Past 2000 nodes the scan simply stopped
   seeing the tail and reported "no match", which is indistinguishable
   from a genuine miss.

The resolver here fixes (1), fixes (2) by *minting* an alias whenever a
scan resolves a name unambiguously (so the next lookup is a single indexed
row read), and fixes (3) by logging a warning when the scan is truncated
instead of pretending the tail was empty.

Matching rule and its failure mode
----------------------------------

**Exact equality after :func:`~trellis.schemas.well_known.normalize_entity_name`**
(trim, collapse whitespace, case-fold) — nothing fuzzy. Consequences,
stated plainly because this code decides entity identity:

* Two *different* entities sharing a normalized name are **ambiguous**:
  the resolver returns both ids, ``AliasMatchExtractor`` treats anything
  other than a single hit as unresolved, and **no alias is minted**. A
  wrong merge is not recoverable; a skipped mention is.
* An alias is only minted from an **untruncated** scan. If the scan hit
  its cap we may not have seen a same-named node in the tail, so the match
  is used for this call but never cached.
* Once an alias exists, it wins. A later-created entity with the same name
  will not be considered — mentions keep resolving to the bound entity.
  This binds *edges*, not identity: the alias path never merges nodes and
  never rewrites an entity, so the worst case is a ``mentions`` edge
  pointing at the wrong one of two same-named entities, which is a
  deletable edge rather than a destroyed node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
#: successful resolution removes one future scan.
DEFAULT_NAME_SCAN_LIMIT = 2000


def build_name_alias_resolver(
    graph_store: GraphStore,
    *,
    scan_limit: int = DEFAULT_NAME_SCAN_LIMIT,
    mint: bool = True,
    on_scan_error: Callable[[Exception, str], None] | None = None,
) -> Callable[[str], list[str]]:
    """Build an :data:`~trellis.extract.alias_match.AliasResolver`.

    The returned callable maps a mention string to zero-or-more entity ids:
    empty for no match, one element for an unambiguous match, several when
    the name is ambiguous (the caller must not guess).

    Resolution order:

    1. ``graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)`` — one
       indexed row read on the store's unique-current alias index.
    2. On a miss, a bounded scan of ``graph_store.query(limit=scan_limit)``
       comparing normalized ``properties["name"]``. An unambiguous hit from
       an untruncated scan is written back to the alias index so step 1
       answers it next time.

    Args:
        graph_store: The knowledge-plane graph store. Read for both steps
            and written (aliases only) when *mint* is on.
        scan_limit: Node cap for the step-2 bootstrap scan.
        mint: Set ``False`` to make the resolver read-only — used by
            callers that must not write, and by tests asserting the
            scan/index split.
        on_scan_error: Invoked as ``(exc, mention)`` when the scan raises.
            Callers that want the failure to be loud (the MCP server)
            raise from inside it; the default logs and yields no match.

    Returns:
        A ``Callable[[str], list[str]]``. It never raises for an
        unresolvable name, and never raises from the mint step — a failed
        alias write costs a future scan, not a resolution.
    """

    def resolve(mention: str) -> list[str]:
        key = normalize_entity_name(mention)
        if not key:
            return []

        bound = _resolve_via_index(graph_store, key, mention)
        if bound is not None:
            return [bound]

        matches, truncated = _scan_for_name(
            graph_store,
            key=key,
            mention=mention,
            scan_limit=scan_limit,
            on_scan_error=on_scan_error,
        )
        if mint and len(matches) == 1 and not truncated:
            _mint_alias(graph_store, entity_id=matches[0], key=key, mention=mention)
        return matches

    return resolve


def _resolve_via_index(graph_store: GraphStore, key: str, mention: str) -> str | None:
    """Look *key* up in the alias index, or ``None`` on a miss.

    Never raises: an alias-index outage must degrade to the scan, not fail
    the ingest that triggered it.
    """
    try:
        row = graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
    except Exception:
        logger.exception("entity_resolution_index_lookup_failed", mention=mention)
        return None
    if not row:
        return None
    entity_id = row.get("entity_id")
    return str(entity_id) if entity_id else None


def _scan_for_name(
    graph_store: GraphStore,
    *,
    key: str,
    mention: str,
    scan_limit: int,
    on_scan_error: Callable[[Exception, str], None] | None,
) -> tuple[list[str], bool]:
    """Bootstrap scan: ``(matching entity ids, scan was truncated)``.

    ``truncated`` is ``True`` when the store returned a full page, i.e. the
    tail of the graph was not examined and the match set may be incomplete.
    """
    try:
        nodes = graph_store.query(limit=scan_limit)
    except Exception as exc:
        logger.exception("entity_resolution_scan_failed", mention=mention)
        if on_scan_error is not None:
            on_scan_error(exc, mention)
        return [], False

    truncated = len(nodes) >= scan_limit
    matches: list[str] = []
    for node in nodes:
        properties = node.get("properties") or {}
        name = properties.get("name")
        if isinstance(name, str) and normalize_entity_name(name) == key:
            node_id = node.get("node_id")
            if node_id:
                matches.append(str(node_id))

    if truncated and len(matches) != 1:
        # LOUD-DEGRADATION: past the cap a miss is not evidence of absence
        # and an ambiguity count is not trustworthy. Say so rather than
        # letting the graph quietly stop resolving as it grows.
        logger.warning(
            "entity_resolution_scan_truncated",
            mention=mention,
            scan_limit=scan_limit,
            matches=len(matches),
        )
    return matches, truncated


def _mint_alias(
    graph_store: GraphStore, *, entity_id: str, key: str, mention: str
) -> None:
    """Bind *key* to *entity_id* so the next resolution is an index read.

    Written straight to the graph store rather than through the
    ``MutationExecutor``: there is no alias mutation operation, the same
    call shape the bulk-ingest route uses (``trellis_api.routes.ingest``),
    and ``upsert_alias`` is idempotent via SCD-2 versioning.

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
        return
    logger.info(
        "entity_resolution_alias_minted",
        mention=mention,
        alias_key=key,
        entity_id=entity_id,
    )
