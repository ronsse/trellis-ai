"""Search strategies for pack assembly."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    NamedTuple,
    Protocol,
    TypedDict,
    runtime_checkable,
)

import structlog

from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.schemas.extraction import (
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
)
from trellis.schemas.pack import PackItem
from trellis.schemas.parameters import ParameterScope
from trellis.schemas.well_known import (
    canonicalize_entity_type,
    expand_entity_type_query,
    normalize_entity_name,
)
from trellis.stores.base.graph_query import FilterClause, NodeQuery

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from trellis.ops.registry import ParameterRegistry
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger()


#: Default half-life for recency decay (days). After this many days a
#: score is halved relative to its un-decayed value (above the floor).
DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0

#: Floor for recency decay — a very old item still retains at least this
#: fraction of its original relevance. Prevents high-importance archival
#: content from being suppressed entirely.
RECENCY_FLOOR = 0.3

#: Slack allowed before a recency stamp counts as being in the future and
#: is skipped by :func:`resolve_recency_stamp`. A source clock is naive
#: often enough that one timezone's offset must not read as hostile; a day
#: is generous against a 30-day half-life (0.977 of the multiplier).
_FUTURE_STAMP_TOLERANCE = timedelta(days=1)

#: Key on ``PackItem.metadata`` naming **which clock** recency decay actually
#: read for this item, and forwarded into ``PACK_ASSEMBLED.injected_items[]``
#: by :func:`~trellis.retrieve.pack_builder._item_attribution`.
#:
#: :func:`resolve_recency_stamp` picks one of three outcomes per item and the
#: three are not close together: the source clock and the row clock differed
#: by a median **2.20x** (max 3.17x) in the resulting multiplier across the
#: 148 rows #417 measured, and resolving to *nothing* fails **open** — up to
#: ``1 / floor`` (3.3x) more than the row's own clock would have given. An
#: item's rank can therefore be dominated by which branch fired, and until
#: #465 that was recoverable only by re-reading the code that ran.
#:
#: Same commitment as ``graph_selection`` (#371) and ``content_floor`` (#358):
#: which branch ran is a property of the **served record**, not an inference
#: about which build was deployed that week. Stamped after the metadata splat,
#: for the #433 reason — the bag is open, so a stored key of this name must
#: not get a vote on a fact about *this* search.
RECENCY_CLOCK_METADATA_KEY = "recency_clock"

#: :data:`RECENCY_CLOCK_METADATA_KEY` value when the winning stamp came from
#: the item's **metadata bag** — the *source's* clock, propagated by an ingest
#: path that knew the content predates its own write (#417).
RECENCY_CLOCK_SOURCE = "source"

#: :data:`RECENCY_CLOCK_METADATA_KEY` value when the winning stamp came from a
#: **store row column** — Trellis's own write clock for the row. Only the
#: keyword axis can produce this: ``VectorStore.query`` returns no columns, so
#: the semantic axis passes none.
RECENCY_CLOCK_ROW = "row"

#: :data:`RECENCY_CLOCK_METADATA_KEY` value when **no candidate was usable**
#: and the item was scored undecayed.
#:
#: Not a neutral outcome — undecayed is the *maximum* multiplier, strictly
#: above what the freshest real timestamp earns. This is the value that makes
#: the semantic-axis residual ``TestSemanticAxisResidual`` pins a one-query
#: finding instead of a code-reading one: a document whose own ``created_at``
#: is malformed or in the future reaches that axis with nothing to fall
#: through to and is served at maximum freshness. Measured live on 2026-09-02
#: it fires on zero rows; the field exists so the next surprise is visible.
RECENCY_CLOCK_NONE = "none"

#: Grace period before importance-score staleness decay starts. Below this
#: age (measured from ``importance_scored_at``) the legacy multiplier is
#: applied as-is; past it, the score decays with the same half-life math
#: as recency decay. See adr-importance-score-freshness §3.4.
DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS = 180.0

#: Floor for importance staleness decay — never zero out a stale score,
#: just dampen it. Same semantics as :data:`RECENCY_FLOOR`.
DEFAULT_IMPORTANCE_DECAY_FLOOR = 0.3

#: Only decay importance scores at or above this threshold. Low scores
#: barely move the multiplier already, so the freshness check would
#: cost more than it gains. See adr-importance-score-freshness §3.4.
DEFAULT_IMPORTANCE_DECAY_THRESHOLD = 0.5

#: Default scoring boosts inside :class:`GraphSearch`. Exposed as
#: module-level constants so they can be resolved through
#: :class:`ParameterRegistry` with these values as fallback defaults.
GRAPH_DOMAIN_MATCH_BOOST = 1.3
GRAPH_CURATED_BOOST = 1.3
GRAPH_DESCRIPTION_BOOST = 1.2
GRAPH_POSITION_DECAY_STEP = 0.05

# Component ids used when resolving registry overrides. Each SearchStrategy
# has its own scope so per-domain tuning stays isolated.
_KEYWORD_COMPONENT = "retrieve.strategies.KeywordSearch"
_SEMANTIC_COMPONENT = "retrieve.strategies.SemanticSearch"
_GRAPH_COMPONENT = "retrieve.strategies.GraphSearch"

#: Over-fetch multiplier for the semantic axis when a domain scope is active.
#: The vector stores cannot express the ``content_tags`` default-pass facet
#: filter (a scalar store-side filter would hard-exclude domain-less rows —
#: the #254 defect), so :class:`SemanticSearch` fetches extra candidates and
#: applies the Python-side default-pass post-filter, then slices back to
#: ``limit``. Over-fetching keeps a heavily-mismatched domain from thinning
#: semantic recall (the trade-off #254 accepted, closed here for #262).
_SEMANTIC_DOMAIN_OVERFETCH = 4

#: Over-fetch multiplier for the unseeded graph branch. ``GraphStore.query``
#: is ``ORDER BY created_at DESC LIMIT n`` on every shipped backend, so this
#: multiplier is not a recall knob — it is the *entire* candidate window, and
#: it is a fixed row count rather than a fraction of the graph. See
#: :data:`GRAPH_SELECTION_RECENCY_WINDOW`.
_GRAPH_RECENCY_OVERFETCH = 4

#: The node timestamp the graph axis runs on — for **selection and ranking
#: alike** (#420).
#:
#: The unseeded branch's entire candidate window is ``GraphStore.query``,
#: which is ``ORDER BY created_at DESC LIMIT n`` on every shipped backend
#: (``stores/sqlite/graph.py``, ``stores/postgres/graph.py``,
#: ``stores/bolt_opencypher/graph.py``). Ranking those same rows by
#: ``updated_at`` meant the axis *selected* on one clock and *ordered* on
#: another, so a re-versioned node could outrank a strictly newer neighbour
#: it could never have displaced from the window.
#:
#: SCD-2 carries ``created_at`` forward across versions, so a re-version —
#: a lifecycle stamp from ``retention.prune`` / ``retention.restore``, a
#: property merge from ``entity.update``, an extraction upsert — moves
#: ``updated_at`` and leaves this alone. That is the property #420 wanted
#: and the reason no writer-side ``preserve_updated_at`` equivalent is
#: needed on ``upsert_node``.
#:
#: Read as a single field, deliberately: falling back to a second column
#: when this one is absent is exactly how the two clocks drift apart again.
#: :func:`_apply_recency_decay` fails open on an unusable timestamp, and
#: ``GraphSearch.search`` logs once per search when that happens, so the
#: no-fallback rule does not buy silence.
#:
#: **This is not** :func:`resolve_recency_stamp`, and the difference is the
#: point of both. That function answers *how old is this information* for the
#: two document-backed axes, and answers it by preferring the **metadata
#: bag's** source clock over the store row's write clock (#417/#462). Its own
#: docstring excludes the graph axis explicitly: a node's ``properties`` is an
#: extractor-written bag with no source-clock convention and no writer
#: producing one. Routing this axis through it would resolve to the same
#: column via a longer path today, and would silently start reading an
#: unvalidated node property the moment any extractor wrote ``created_at``
#: into a bag. The two are consistent rather than duplicated — one clock per
#: axis, chosen for what that axis's rows actually carry — and neither is a
#: fallback chain. A graph-side source-clock convention would be a new
#: measurement, not a call to this resolver.
#:
#: The cost, stated because it was argued: "this entity was materially
#: updated yesterday" no longer boosts score on this axis. Reverting is one
#: line. Reopen #420 if the recency window widens *and* in-window nodes
#: routinely carry ``updated_at > created_at``.
GRAPH_RECENCY_CLOCK_FIELD = "created_at"

#: Value of ``PackItem.metadata["graph_selection"]`` when the graph axis
#: picked its candidates by recency because nothing supplied seeds. Stamped
#: on every item so the two selection modes are distinguishable in
#: ``PACK_ASSEMBLED.injected_items[]`` — the axis's query-independence is a
#: measurable property of a served pack, not a claim in a docstring.
GRAPH_SELECTION_RECENCY_WINDOW = "recency_window"

#: Value of ``PackItem.metadata["graph_selection"]`` when the graph axis
#: expanded a seed set — the only mode in which the axis is a function of
#: the caller's query.
GRAPH_SELECTION_SEEDED = "seeded"

#: Filter keys that are **retrieval controls addressed to the graph axis**,
#: not metadata predicates about a stored row.
#:
#: ``PackBuilder.build`` injects ``include_structural`` into the single
#: ``filters`` mapping it hands to *every* strategy, and a caller may pass
#: any of these through ``filters=`` directly. :class:`GraphSearch` ``pop``\ s
#: them; the document and vector stores do not know them, and both compile an
#: unknown filter key to **hard metadata equality**, which matches no row.
#: So a caller that asked for one extra category of graph node was silently
#: getting the keyword and semantic axes emptied: measured on a three-axis
#: pack, ``include_structural=True`` took it from 3 items to 1, with no
#: warning, no ``strategy_failures`` entry and no ``RejectedItem`` — the
#: strategies returned ``[]``, which is indistinguishable from "nothing
#: matched" (the #404 failure shape, one layer up).
#:
#: A closed allow-list of *controls* rather than a deny-list of metadata: the
#: opposite of :mod:`trellis.retrieve.servable`'s posture, and deliberately —
#: stored metadata keys are an open set that must stay servable by default,
#: while these are consumed by the graph axis and are added whenever it grows
#: a new ``pop``. That rule is enforced rather than asserted:
#: ``TestTheAllowListCoversEveryPop`` derives the popped keys from
#: ``GraphSearch``'s own AST, because the hand-written set this replaced
#: compared one literal against another and could not notice the three keys
#: it was already missing.
#:
#: ``seed_ids`` is *not* owned by :class:`GraphSearch` alone —
#: :class:`~trellis.retrieve.observation_strategy.ObservationSearch` reads it
#: as its subject set. That is why the stripping happens inside the two
#: strategies that forward filters to a store rather than at the collect
#: seam: a seam-level strip would take ``seed_ids`` away from a strategy that
#: needs it.
#:
#: Latent since the initial commit, and it stayed latent because nothing in
#: the repository passed one. #375/#436 changed that: surfacing a
#: newly-written meta-Activity now *requires* ``include_structural=True``
#: alongside ``include_meta=True``, so the documented escape hatch walked
#: straight onto it. The same escape hatch was still half-open after #443:
#: ``depth``, ``edge_types`` and ``node_type`` are advertised by
#: ``GraphSearch``'s own docstrings ("Consumes ``depth`` / ``edge_types``
#: from *filters*") and were omitted from this set, so ``filters={"depth": 3}``
#: emptied a measured three-axis pack to **zero** items — worse than the
#: original, because ``depth`` and ``edge_types`` are popped only in the
#: *seeded* branch and production always takes the unseeded one (#371), where
#: they fell through into ``query_props`` and hard-equality-filtered the graph
#: axis as well.
GRAPH_CONTROL_FILTER_KEYS = frozenset(
    {
        "seed_ids",
        "include_structural",
        "include_unconfirmed",
        "depth",
        "edge_types",
        "node_type",
    }
)


def _exclude_and_log(
    nodes: list[dict[str, Any]],
    keep: Callable[[dict[str, Any]], bool],
    event: str,
) -> list[dict[str, Any]]:
    """Apply one of ``GraphSearch``'s client-side node filters, counting it.

    These drops happen before a ``PackItem`` exists, so they produce no
    ``RejectedItem`` and never reach
    :func:`~trellis.retrieve.withholding.summarize_withheld`. The debug line
    is the only observable they have — which is not enough on its own (#404
    said so about exactly this shape) but is strictly more than nothing.
    Silent when the filter removed nothing, so an ``excluded=0`` line never
    dilutes the one that matters.
    """
    kept = [n for n in nodes if keep(n)]
    if len(kept) != len(nodes):
        logger.debug(event, excluded=len(nodes) - len(kept))
    return kept


def strip_graph_controls(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop :data:`GRAPH_CONTROL_FILTER_KEYS` before a store-side filter call.

    A mapping that held nothing *but* controls returns ``None``, matching
    what the stores expect for "no filters" — a ``{}`` is a filter that some
    backends read as a predicate over nothing. An input that was *already*
    empty is passed through unchanged (``None`` stays ``None``, ``{}`` stays
    ``{}``), which is what ``test_empty_input_passes_through`` pins; the
    caller had nothing to strip, so there is nothing to normalise.
    """
    if not filters:
        return filters
    return {
        k: v for k, v in filters.items() if k not in GRAPH_CONTROL_FILTER_KEYS
    } or None


@runtime_checkable
class GraphSeedExtractor(Protocol):
    """Turns a retrieval intent into graph node ids to expand from.

    This is the *only* place query relevance can enter
    :class:`GraphSearch`. The unseeded branch calls
    ``GraphStore.query``, which every shipped backend implements as
    ``ORDER BY created_at DESC LIMIT n`` — it takes no query argument and
    has no text index to consult. So an extractor is not an optimisation
    on top of a search; without one there is no search.

    Implementations must be **total and cheap**: ``extract`` runs inline
    on the pack-assembly path, once per pack. Returning ``[]`` is a valid
    answer meaning "this intent anchors on no entity I can name", and
    :class:`GraphSearch` treats it as such — it falls back to the recency
    window rather than emptying the axis, because a seeding miss must not
    cost the caller an axis it had before.

    Known implementations:

    * :class:`~trellis.retrieve.semantic_seeds.SemanticSeedExtractor` —
      embeds the intent and maps top-K vector hits back to entity ids.
      Requires entity-summary documents in the vector store; read that
      module's docstring before wiring it, because a corpus without them
      makes it a measured no-op (#371); a production path needs #375 first.
    * :class:`NamespaceSeedExtractor` — resolves intent words to node ids
      through the alias index and Trellis's deterministic id prefixes.
      Indexed reads only, no embedder, and the one wired by default
      (#375).
    """

    def extract(self, intent: str) -> list[str]:
        """Return graph node ids to seed traversal from, best first."""
        ...  # pragma: no cover - protocol declaration


#: Node-id namespaces :class:`NamespaceSeedExtractor` probes by default.
#:
#: Trellis mints deterministic, prefixed node ids — ``domain:<name>``,
#: ``tool:<name>``, ``artifact:<path>``, ``agent:<name>``, ``team:<name>``
#: — so a name-to-id lookup is a primary-key read rather than a search.
#: That is the whole reason this extractor can exist without the scan
#: #375 refuses.
#:
#: The roster is **measured, not enumerated from the schema**. On the
#: reference deployment (1,696 current nodes, 2026-09-12), replaying the
#: 85 real intents this deployment has assembled packs for:
#:
#: ==========================  ================
#: namespaces probed           intents seeded
#: ==========================  ================
#: all five (the default)      **51/85**
#: ``domain`` alone            38/85
#: ``tool`` alone              10/85
#: ``artifact`` alone          3/85
#: ==========================  ================
#:
#: The combination is worth more than its best member, which is the
#: reason it is a roster and not a single prefix.
#:
#: ``trace:`` is deliberately **absent** and is the roster's negative
#: control: 165 such nodes exist, and **0** of them carry an id derivable
#: from their name (the id is a ULID; the name is the task's prose
#: title). Probing it would add a namespace's worth of round-trip payload
#: for a structurally impossible hit.
#:
#: An empty string is a legal member and probes the bare key as a whole
#: node id, for a deployment whose ids are not namespaced. It is not in
#: the default because it was measured and yields nothing here: 51/85
#: either way, same 18 distinct seeds.
DEFAULT_SEED_NAMESPACES: tuple[str, ...] = (
    "domain",
    "team",
    "artifact",
    "agent",
    "tool",
)

#: Upper bound on distinct intent keys probed for one pack.
#:
#: This is the extractor's cost bound, and it is **where the yield
#: saturates**, not a round number. Over the same 85 intents: a cap of 16
#: seeds 46, 24 seeds 49, and **32 seeds 51 — identical to no cap at
#: all**, down to the same 18 distinct seed ids. The median intent
#: produces 20 keys, so the cap does not bind on a typical pack; it
#: bounds the tail (the longest real intent produces 54).
DEFAULT_MAX_SEED_KEYS = 32

#: Upper bound on seeds handed back for expansion.
#:
#: Every seed is a BFS root, so this bounds the subgraph the seeded
#: branch asks for. Measured over the same intents the ceiling is **3**
#: seeds, so this never binds today — it exists so that a future corpus
#: whose names collide with common words cannot turn one pack into a
#: whole-graph traversal.
DEFAULT_MAX_SEEDS = 8

#: Traversal depth for seeds an extractor derived, as opposed to seeds a
#: caller named through ``filters["seed_ids"]``.
#:
#: A caller who names a seed is making a claim about that entity and gets
#: the historical default of 2. A *derived* seed is an inference from a
#: word in the intent, and the measurement says one hop is where its value
#: is. Across the 18 distinct seeds this deployment's intents resolve to,
#: depth 1 yields **90** non-structural nodes in total and depth 2 yields
#: **344** — and the extra 254 are not more of the same. The seeds that
#: resolve are provenance hubs (``domain:fincore`` alone goes 13 nodes ->
#: 105 at depth 2), so the second hop reaches the hub's whole cohort
#: rather than the intent's neighbourhood.
#:
#: Latency is *not* the reason (1.4-6.5 ms either way). The reason is that
#: :meth:`GraphSearch.search` finishes with ``scored = nodes[:limit]`` over
#: an **unordered** subgraph, so a depth-2 hub expansion does not rank the
#: generic nodes below the on-topic ones — it hands the slice ~100 of them
#: and displaces the session Activities depth 1 returns cleanly. A wider
#: net plus an arbitrary slice is a worse pack, not a bigger one.
EXTRACTED_SEED_DEPTH = 1

#: Tokenizer for intent text. Deliberately permissive about the
#: characters that appear *inside* a Trellis name — ``_ . : / # + -`` —
#: because the names being matched are file paths, tool names and domain
#: slugs, not English words.
_SEED_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/#+-]+")


class NamespaceSeedExtractor:
    """Resolve intent words to node ids with indexed reads only (#375).

    The graph axis's unseeded branch is ``ORDER BY created_at DESC LIMIT
    n``, so without an extractor the axis never consults the intent and
    its coverage decays as 1/N. This is the production-viable extractor:
    it needs no embedder, no entity-summary corpus, and — the load-bearing
    property — **no scan**.

    How it resolves, per normalized key, in this order:

    1. ``resolve_alias("name", key)`` — the governed display-name index
       #530 maintains. One indexed read; the authoritative binding.
    2. ``f"{namespace}:{key}"`` for each of :data:`DEFAULT_SEED_NAMESPACES`
       — candidate primary keys under Trellis's deterministic id scheme.

    Every candidate from step 2 is then confirmed in **one**
    ``get_subgraph(candidates, depth=0)`` call, which the
    ``GraphStoreContractTests`` pin to return exactly the seed nodes that
    exist. That is the batch primary-key read this design rests on, and
    it is why the cost does not grow with the number of namespaces.

    **What this costs, stated against the mechanism it replaces.**
    Measured on the reference deployment by running *this class* over all
    85 real intents: ``extract`` is a **median 7.7 ms** per pack (p90
    11.9, max 15.9), which splits **6.0 ms step 1 / 1.3 ms step 2 / 0.14
    ms CPU**. Read that split before optimising anything here — **78% of
    the cost is the alias half, which resolves nothing on this deployment
    today** (see *What it does not buy* below). The confirm call is the
    cheap half at a median 1.3 ms (p90 2.5, max 4.1) for a median 100
    candidate ids; the same 100 ids fetched one at a time cost **29.8
    ms**, and the whole-table scan #375 refuses costs **24.4 ms** at
    1,696 rows. So the batched form is ~19x cheaper than the refused scan
    *and* O(1) in graph size, where the scan is O(N) and silently
    truncates past ``DEFAULT_NAME_SCAN_LIMIT``. Both properties matter;
    only the second one was the stated reason for the refusal.

    For scale: a pack on this deployment assembles in ~2.2 s, so the
    whole extractor is ~0.35% of it. Nothing here is a latency problem —
    the split is recorded because the *shape* of the cost is surprising,
    not because the total is.

    **What it buys.** Over the 85 real intents this deployment has
    assembled packs for, **51** resolve to at least one seed, and every
    one of those seeds is expandable by construction (it was confirmed to
    exist). The alternative the issue refused — an ideal client-side name
    match, i.e. the scan performing perfectly — reaches 40 intents and
    only **35** with a seed that has any neighbourhood to expand.

    **What it does not buy, today.** Step 1 resolves nothing on this
    deployment: ``entity_aliases`` holds 0 rows, because the alias-minting
    code (#530) is merged but not yet deployed here and the #369 backfill
    has not been run. It is wired anyway because it is the *durable* half
    — the id-prefix convention is a property of today's writers, while
    the alias index is the governed mechanism that survives a node whose
    id is a ULID. This is stated rather than elided: an extractor that
    were *only* step 1 would be a measured no-op and must not ship as
    one.

    **And it is not free, which is the part worth carrying forward.** It
    is one indexed read *per key*, unbatched — 1,624 round trips across
    the 85 intents, a median of 20 per pack — so on a deployment with an
    empty ``entity_aliases`` it is **78% of this extractor's runtime for
    zero seeds**. That buys nothing until #530 deploys and the #369
    backfill runs, after which it is the half that keeps working when an
    id stops being a readable name. It is left unbatched here because
    ``resolve_alias`` is a single-id API and giving it a batch form is a
    store-side change, outside what this issue touches; the numbers are
    recorded so that change can be justified without re-deriving them.

    Unigrams only. Bigrams and trigrams were measured and add nothing
    (51/85 at every n), while tripling the candidate count.

    Never raises, per :class:`GraphSeedExtractor`: a store failure
    returns ``[]`` and the axis falls back to its recency window.
    """

    def __init__(
        self,
        graph_store: Any,
        *,
        namespaces: Sequence[str] = DEFAULT_SEED_NAMESPACES,
        max_keys: int = DEFAULT_MAX_SEED_KEYS,
        max_seeds: int = DEFAULT_MAX_SEEDS,
    ) -> None:
        """Wire an extractor against one graph store.

        Args:
            graph_store: The knowledge plane's ``GraphStore``. Only
                ``resolve_alias`` and ``get_subgraph`` are used, and both
                are read-only.
            namespaces: Id prefixes to probe, best first. An empty-string
                member probes the bare key as a whole node id. See
                :data:`DEFAULT_SEED_NAMESPACES` for why this roster.
            max_keys: Cost bound — distinct intent keys probed per pack.
            max_seeds: Bound on the seeds returned, i.e. on BFS roots.
        """
        self._store = graph_store
        self._namespaces = tuple(namespaces)
        self._max_keys = max_keys
        self._max_seeds = max_seeds

    def extract(self, intent: str) -> list[str]:
        """Return live node ids the intent names, best first.

        ``[]`` means "this intent anchors on no entity I can name" and is
        a valid answer — :class:`GraphSearch` reads it as *run the recency
        window*, never as *serve nothing*.
        """
        keys = self._keys(intent)
        if not keys:
            return []

        # Step 1. One indexed alias read per key. ``alias_by_id`` also
        # records which key produced an id, so the liveness confirm below
        # can re-check the binding's *name* — the check
        # ``entity_resolution._binding_is_live`` makes, done here for free
        # from properties the confirm call already returned.
        alias_by_id: dict[str, str] = {}
        for key in keys:
            entity_id = self._alias_id(key)
            if entity_id and entity_id not in alias_by_id:
                alias_by_id[entity_id] = key

        # Step 2. Candidate primary keys under the deterministic id
        # scheme. Ordered key-major so the returned seeds follow the order
        # the entities were mentioned in, which is the only ranking signal
        # available at this layer.
        ordered: list[str] = []
        seen: set[str] = set()
        for key in keys:
            for candidate in self._candidates(key, alias_by_id):
                if candidate not in seen:
                    seen.add(candidate)
                    ordered.append(candidate)

        live = self._confirm(ordered)
        seeds: list[str] = []
        for candidate in ordered:
            node = live.get(candidate)
            if node is None:
                continue
            alias_key = alias_by_id.get(candidate)
            if alias_key is not None and not _alias_binding_matches(node, alias_key):
                continue
            seeds.append(candidate)
            if len(seeds) >= self._max_seeds:
                break

        logger.debug(
            "namespace_seed_extractor_resolved",
            keys=len(keys),
            candidates=len(ordered),
            seeds=len(seeds),
        )
        return seeds

    def _keys(self, intent: str) -> list[str]:
        """Normalized, de-duplicated intent unigrams, capped and in order."""
        keys: list[str] = []
        seen: set[str] = set()
        for token in _SEED_TOKEN_RE.findall(intent or ""):
            key = normalize_entity_name(token)
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
            if len(keys) >= self._max_keys:
                break
        return keys

    def _candidates(self, key: str, alias_by_id: dict[str, str]) -> list[str]:
        """Candidate ids for one key: its alias binding, then namespaces."""
        candidates = [i for i, k in alias_by_id.items() if k == key]
        candidates.extend(f"{ns}:{key}" if ns else key for ns in self._namespaces)
        return candidates

    def _alias_id(self, key: str) -> str | None:
        """One indexed alias read, or ``None`` on a miss or an outage."""
        try:
            row = self._store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
        # GRACEFUL-DEGRADATION: the alias index is one of two resolution
        # paths and the other needs no index at all. An outage here must
        # cost the miss, not the pack.
        except Exception:
            logger.warning("namespace_seed_alias_lookup_failed", exc_info=True)
            return None
        entity_id = row.get("entity_id") if isinstance(row, dict) else None
        return entity_id if isinstance(entity_id, str) and entity_id else None

    def _confirm(self, candidates: list[str]) -> dict[str, dict[str, Any]]:
        """Which candidates exist, in one batched primary-key read.

        ``get_subgraph(ids, depth=0)`` returns exactly the seed nodes that
        are current — pinned by
        ``GraphStoreContractTests.test_subgraph_seed_only_at_depth_zero``,
        so every shipped backend answers it the same way. This is the call
        that keeps the extractor's cost independent of graph size.
        """
        if not candidates:
            return {}
        try:
            subgraph = self._store.get_subgraph(candidates, depth=0)
        # GRACEFUL-DEGRADATION: seeding is additive. A confirm failure
        # yields no seeds and the axis serves its recency window, which is
        # exactly what it served before this extractor existed.
        except Exception:
            logger.warning("namespace_seed_confirm_failed", exc_info=True)
            return {}
        nodes = subgraph.get("nodes", []) if isinstance(subgraph, dict) else []
        resolved: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = node.get("node_id")
            if isinstance(node_id, str) and node_id:
                resolved.setdefault(node_id, node)
        return resolved


def _alias_binding_matches(node: dict[str, Any], key: str) -> bool:
    """Is *node* still named *key*?

    The retrieval-side half of
    :func:`trellis.extract.entity_resolution._binding_is_live`. An alias
    row binds a *normalized name* to an id, so a renamed node leaves a
    binding that points somewhere real and wrong — and seeding a
    neighbourhood on it would serve a caller an entity they did not name.
    Checked only for alias-derived ids: a namespace candidate is its own
    evidence (the id was constructed from the key), and Trellis slugifies
    some ids away from their display name (``merge_prs`` mints
    ``tool:merge-prs``), so the same check there would reject good seeds.

    Costs nothing extra — the properties come from the confirm call.
    """
    name = (node.get("properties") or {}).get("name")
    if isinstance(name, str) and normalize_entity_name(name) == key:
        return True
    logger.debug(
        "namespace_seed_stale_alias_skipped",
        alias_key=key,
        entity_id=node.get("node_id"),
    )
    return False


def _passes_domain_scope(metadata: dict[str, Any], domain: str) -> bool:
    """Default-pass domain check for semantic-axis hits (#254 / #262).

    Mirrors the keyword axis's ``content_tags`` facet semantics at the
    Python boundary — the vector stores only offer a hard-equality scalar
    metadata filter, which would hard-exclude domain-less rows. Vector rows
    carry full document metadata (``build_vector_row`` copies it), so a hit
    may hold ``domain`` in either storage location: scalar ``metadata.domain``
    or the ``metadata.content_tags.domain`` facet (a list).

    Semantics: pass when no domain is present in either location
    (default-pass — a domain-less memory is never hard-excluded); pass when
    either location matches; exclude only on explicit mismatch.
    """
    values: list[Any] = []
    scalar = metadata.get("domain")
    if scalar is not None:
        values.append(scalar)
    tags = metadata.get("content_tags")
    if isinstance(tags, dict):
        facet = tags.get("domain")
        if isinstance(facet, list):
            values.extend(facet)
        elif facet is not None:
            values.append(facet)
    if not values:
        return True
    return domain in values


def _resolve_param(
    registry: ParameterRegistry | None,
    component_id: str,
    domain: str | None,
    key: str,
    default: Any,
) -> Any:
    """Resolve a scoring param via registry, or fall back to ``default``.

    Scope is ``(component_id, domain)`` — per-(intent_family, tool_name)
    tuning is deferred to a follow-up when strategies gain intent-family
    awareness.
    """
    if registry is None:
        return default
    return registry.get(
        ParameterScope(component_id=component_id, domain=domain),
        key,
        default,
    )


class SearchStrategy(ABC):
    """Base class for retrieval strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for reporting."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        """Execute search and return ranked PackItems."""


def _apply_importance(
    base_score: float,
    metadata: dict[str, Any],
    *,
    now: datetime | None = None,
    fresh_horizon_days: float = DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
    floor: float = DEFAULT_IMPORTANCE_DECAY_FLOOR,
    decay_threshold: float = DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> float:
    """Apply importance weighting with bounded staleness decay.

    Decay is applied *only* when:

    * the raw importance is at or above ``decay_threshold``, AND
    * ``importance_scored_at`` (located on ``metadata["content_tags"]`` or
      directly on ``metadata``) is past the ``fresh_horizon_days`` horizon.

    Below those thresholds the function returns the legacy behavior:
    ``base_score * (1.0 + clamp(importance, 0, 1))``.

    Greenfield writer contract (adr-importance-score-freshness §3.5): if
    ``auto_importance`` is set above ``decay_threshold`` but
    ``importance_scored_at`` is missing, raises ``ValueError``. There is
    no fallback to ``classified_at`` and no "treat as fresh" path —
    every code path that writes ``auto_importance`` must also stamp.
    """
    importance = float(metadata.get("auto_importance", 0.0))
    if importance == 0.0:
        # No importance score → no multiplier, no freshness check needed.
        return base_score
    importance = max(0.0, min(1.0, importance))  # clamp 0-1
    if importance < decay_threshold:
        # Sub-threshold scores skip the freshness check entirely — the
        # multiplier is small enough that staleness barely moves it.
        return base_score * (1.0 + importance)

    # Above threshold: locate the freshness witness. ContentTags is the
    # canonical home; `metadata["importance_scored_at"]` is supported as
    # a flat alias for stores that flatten tags into top-level metadata.
    tags = metadata.get("content_tags") or {}
    raw_stamp = tags.get("importance_scored_at") if isinstance(tags, dict) else None
    if raw_stamp is None:
        raw_stamp = metadata.get("importance_scored_at")
    if raw_stamp is None:
        msg = (
            "auto_importance is set but importance_scored_at is missing — "
            "writer path is broken. Every code path that writes "
            "auto_importance must also stamp importance_scored_at "
            "(see adr-importance-score-freshness.md §3.5). "
            f"Item metadata keys={sorted(metadata.keys())}"
        )
        raise ValueError(msg)
    decayed = _decay_importance_if_stale(
        importance,
        raw_stamp,
        now=now,
        fresh_horizon_days=fresh_horizon_days,
        half_life_days=half_life_days,
        floor=floor,
    )
    return base_score * (1.0 + decayed)


class _ImportanceParams(TypedDict):
    """Typed bag for the per-(component, domain) importance-decay tunables.

    Mirrors the keyword arguments of :func:`_apply_importance` so callers
    can ``**`` -spread the registry-resolved values without losing types.
    """

    fresh_horizon_days: float
    floor: float
    decay_threshold: float
    half_life_days: float


def _resolve_importance_params(
    registry: ParameterRegistry | None,
    component_id: str,
    domain: str | None,
) -> _ImportanceParams:
    """Resolve per-(component, domain) importance-decay overrides.

    Mirrors the resolution shape of the recency params so callers can
    spread the result into ``_apply_importance(**params)``.
    """
    return _ImportanceParams(
        fresh_horizon_days=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_fresh_horizon_days",
            DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
        ),
        floor=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_decay_floor",
            DEFAULT_IMPORTANCE_DECAY_FLOOR,
        ),
        decay_threshold=_resolve_param(
            registry,
            component_id,
            domain,
            "importance_decay_threshold",
            DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
        ),
        half_life_days=_resolve_param(
            registry,
            component_id,
            domain,
            "recency_half_life_days",
            DEFAULT_RECENCY_HALF_LIFE_DAYS,
        ),
    )


def _parse_stamp(value: Any) -> datetime | None:
    """Parse one recency stamp, or ``None`` when it is absent/unusable.

    Heterogeneous by necessity: SQLite hands back ISO strings and Postgres
    hands back ``datetime`` objects for the very same column.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _decay_importance_if_stale(
    importance: float,
    raw_stamp: str | datetime,
    *,
    now: datetime | None = None,
    fresh_horizon_days: float,
    half_life_days: float,
    floor: float,
) -> float:
    """Decay an importance score past the freshness horizon.

    Mirrors :func:`_apply_recency_decay` but with a no-op grace period:
    inside ``fresh_horizon_days`` the score is returned unchanged; past
    it, the score decays with the same half-life math, capped at
    ``floor``. Unparseable stamps return the score unchanged (the caller
    enforces non-None at a higher level).
    """
    ts = _parse_stamp(raw_stamp)
    if ts is None:
        return importance
    reference = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400.0)
    if age_days <= fresh_horizon_days:
        return importance
    # Past horizon: decay the *excess* age over the horizon with the
    # standard half-life formula, floored.
    excess = age_days - fresh_horizon_days
    decay: float = 0.5 ** (excess / half_life_days)
    return importance * (floor + (1.0 - floor) * decay)


class ResolvedRecency(NamedTuple):
    """The stamp recency decay should read, **and which clock it came from**.

    Returned as a pair rather than resolved once and labelled again by the
    caller: a second traversal of the same candidates is how two readers of
    one rule drift apart, which is the failure class #325/#326/#443 are each
    an instance of. There is exactly one walk, and the label is what that walk
    did.

    ``stamp`` is deliberately ``Any`` — a candidate is whatever the store or
    the metadata bag held (SQLite hands back ISO strings, Postgres hands back
    ``datetime``), and the resolver returns it *verbatim* rather than
    normalising, so :func:`_apply_recency_decay` keeps parsing exactly what
    was stored.
    """

    stamp: Any
    clock: str


def resolve_recency_stamp(
    metadata: Any, *row_stamps: Any, now: datetime | None = None
) -> ResolvedRecency:
    """Resolve the one timestamp recency decay should read for an item.

    **The metadata bag wins over the store row's own columns**, and that
    ordering is the point of the function (#417).

    ``updated_at`` / ``created_at`` exist in two places with two different
    meanings. As *store columns* they are the row's write clock — when
    Trellis last touched this row. As *metadata keys* they are **the
    source's** clock, put there by an ingest path that knows the content
    predates its own write. That is the rule, not a roster of writers: any
    reader whose source carries a timestamp may propagate one, and two
    already do — :mod:`trellis.ingest_corpus.conversations` copies a
    claude.ai conversation's stamps, and the markdown handler passes YAML
    frontmatter through flat (neither key is reserved). Both then reach the
    vector row, because
    :func:`~trellis.retrieve.embed_ingest_hook.build_vector_row` splats
    document metadata and only ``setdefault``s its own ``created_at``.

    Recency decay asks *how old is this information*, and for an imported
    corpus only the source's clock can answer it. Measured on the reference
    deployment (2026-09-02): all **148** conversation documents were written
    by one import batch inside a **72-second** window on 2026-08-07 (the 148
    column values are distinct, and distinct by microseconds), across a corpus
    whose source stamps span **28 months**. The column therefore ranks a 2024
    conversation exactly as fresh as one from last week. Reading it there is
    not a conservative default; it is reading a stamp with no information in
    it. The 16 rows since re-written by a metadata-only pass are the sharpest
    case rather than an exception — their column now says 2026-08-27/30, so
    the keyword axis was scoring 2024 content as three days old.

    Before this function the two document-backed axes disagreed about which of
    the two they meant: ``KeywordSearch`` read the column, ``SemanticSearch``
    read the bag. Same document, two ages, decided by which strategy retrieved
    it — a median **2.20x** (max 3.17x) difference in the resulting recency
    multiplier across those 148 rows, which supplied 152 of 917 injected
    servings over 29 of 56 assembled packs. Both axes now call this, so a
    third document-backed axis cannot re-open the split by picking a side.

    Two neighbours deliberately keep the column, and neither is an oversight.
    ``GraphSearch`` is not routed through here: a graph node's ``properties``
    is an extractor-written bag with no source-clock convention and no writer
    producing one (0 of 1111 live production nodes carry either key on
    2026-09-02 — the count moves daily, the zero has not), so extending a
    document-corpus rule to it would be an unmeasured change to a different
    store rather than consistency. And :mod:`trellis.mutate.retention` keeps
    reading the column for its ``older_than_days`` gate, because that gate
    asks a *different* question — how long Trellis has held this row, not how
    old the content is — and it answers it by deleting. Switching it would
    take that gate's 30-day age criterion on these documents from **0/148 to
    146/148**. Stated as the criterion and not as the gate, because it is one
    conjunct of two: ``_classify_document`` consults it only on the
    ``lifecycle_states`` branch, and none of the 148 carries a lifecycle state
    today, so nothing would be deleted the instant it was switched. What the
    switch removes is the only thing standing between an imported corpus and
    a destructive sweep the moment any of it acquires a targeted state.
    Recency decay is a score with a floor; retention is destructive. They do
    not have to agree.

    Chunk rows are the third exclusion, and unlike the other two this one is
    an *asymmetry this function creates*. ``_write_chunks`` propagates only
    :data:`~trellis.classify.ingest.CLASSIFY_METADATA_KEYS` from parent to
    chunk, so a chunk of a 2024 conversation carries no source clock and
    decays off the import column. Before #417 the keyword axis read the
    column for parent and chunk alike and they agreed; now they do not
    (production: 735 chunk rows under 74 stamped parents, 147 servings, and
    6 of 56 assembled packs already served a stamped parent together with one
    of its own chunks). **On the keyword axis that disagreement is new here**
    — the pre-existing half is narrower than it first looks: the semantic axis
    has always read the bag, so it already scored stamped parents off the
    source clock and everything else off a write clock, but a stamped parent
    and its own chunks are never *both* servable there (a conversation is
    either chunked or embedded whole — the intersection is 0 of 148), so that
    axis never exercised the parent-versus-chunk case. Inheriting the stamp is
    not obviously right either: the parent is the worse-cited half of that
    corpus (1 helpful / 64 unhelpful against the chunks' 7 / 38; P(cited
    helpful | served) 0.015 vs 0.081, against 0.123 for the rest of the
    corpus), so propagating it would demote the better half. Left as measured,
    not as taste, and tracked in #463.

    Candidates are tried in order and the first one that is *usable* wins —
    not the first one merely present. Two ways a candidate is unusable, and
    both fall through to the next one rather than reaching
    :func:`_apply_recency_decay`:

    * **It does not parse.** The decay fails *open* on an unparseable value:
      it returns the score undecayed, which is not neutral — it is the
      maximum multiplier, strictly above what the freshest real timestamp
      earns. That is the wrong direction to fail for a value from outside.
    * **It is in the future.** Parsing is not enough, because the decay
      clamps age at zero, so a stamp dated 2099 buys exactly the same maximum
      multiplier a malformed one would — up to ``1 / floor`` (3.3x) more than
      the row's own clock would have given the same item. Rejecting only the
      malformed half would leave the guarantee open on its easier route:
      both live producers of these keys copy them verbatim out of a file
      (a claude.ai export's JSON, a note's YAML frontmatter), so the value
      is caller-supplied either way. :data:`_FUTURE_STAMP_TOLERANCE` of slack
      is allowed first — a source clock is naive often enough that one
      timezone's offset must not read as hostile.

    **The guard is a preference, not a guarantee, and it is weaker on the
    semantic axis** — say so rather than let the word "guard" imply more.
    Falling through only helps if a later candidate is usable. The keyword
    axis always has one (a store row's columns are never absent), so a
    hostile stamp there really is decayed off the row's clock. The semantic
    axis passes *no* row stamps, so its candidates are the bag's two keys;
    ``build_vector_row`` normally supplies embed time via ``setdefault``, but
    ``setdefault`` is a no-op when the key is *present and unusable*. A
    document carrying a malformed or future ``created_at`` therefore still
    reaches the semantic axis with nothing to fall through to, resolves to
    ``None``, and scores undecayed — exactly what it would have done with no
    guard at all. Closing that would mean returning the vector row's own
    ``created_at`` column from ``VectorStore.query``, which today returns
    ``item_id`` / ``score`` / ``metadata`` on every backend; a contract
    change across four backends is not this function's business.
    ``TestSemanticAxisResidual`` pins the limit so it stays a known one.

    Args:
        metadata: The item's metadata bag (document or vector row).
        *row_stamps: The store row's own stamps, most-preferred first
            (typically ``updated_at`` then ``created_at``). ``VectorStore.query``
            returns no columns, so the semantic axis passes none.
        now: Reference clock for the future check. Defaults to wall time.

    Returns:
        A :class:`ResolvedRecency` — the first usable stamp with
        :data:`RECENCY_CLOCK_SOURCE` or :data:`RECENCY_CLOCK_ROW` naming which
        of the two clocks it came from, or a ``None`` stamp labelled
        :data:`RECENCY_CLOCK_NONE` when no candidate was usable.
        ``None`` decays nothing, which is the same maximum multiplier a future
        stamp would have produced — so rejecting every candidate costs
        nothing; the guard only ever *prefers* a non-future candidate that
        exists. The label is carried onto the served item under
        :data:`RECENCY_CLOCK_METADATA_KEY` (#465), because those three
        outcomes are up to 3.3x apart and were otherwise unrecoverable from
        the record.
    """
    bag = metadata if isinstance(metadata, dict) else {}
    horizon = (now or datetime.now(UTC)) + _FUTURE_STAMP_TOLERANCE
    if horizon.tzinfo is None:
        horizon = horizon.replace(tzinfo=UTC)
    candidates = (
        (bag.get("updated_at"), RECENCY_CLOCK_SOURCE),
        (bag.get("created_at"), RECENCY_CLOCK_SOURCE),
        *((stamp, RECENCY_CLOCK_ROW) for stamp in row_stamps),
    )
    for candidate, clock in candidates:
        parsed = _parse_stamp(candidate)
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed > horizon:
            continue
        return ResolvedRecency(candidate, clock)
    return ResolvedRecency(None, RECENCY_CLOCK_NONE)


def _apply_recency_decay(
    base_score: float,
    # Not ``str | None``: the document store's columns come back as ``str``
    # from SQLite and ``datetime`` from Postgres, and always have. Not ``Any``
    # either, since #465 — :func:`resolve_recency_stamp` now returns a
    # :class:`ResolvedRecency` pair, and ``Any`` would let a caller hand the
    # whole pair to this function, where ``_parse_stamp`` would shrug it off
    # as unparseable and fail *open* at maximum freshness. Naming the three
    # types the stores actually produce makes that a type error instead.
    timestamp: str | datetime | None,
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
    floor: float = RECENCY_FLOOR,
) -> float:
    """Apply exponential recency decay to a relevance score.

    Items lose half their un-floored weight every ``half_life_days``.
    A floor ensures an old-but-relevant item still surfaces. Missing or
    unparseable timestamps leave the score unchanged (fail-open).

    Formula:
        decay = 0.5 ** (age_days / half_life_days)
        score = base_score * (floor + (1 - floor) * decay)
    """
    ts = _parse_stamp(timestamp)
    if ts is None:
        return base_score
    reference = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age_days = max(0.0, (reference - ts).total_seconds() / 86400.0)
    decay: float = 0.5 ** (age_days / half_life_days)
    return base_score * (floor + (1.0 - floor) * decay)


class KeywordSearch(SearchStrategy):
    """Full-text keyword search via DocumentStore."""

    def __init__(
        self,
        document_store: Any,
        *,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
    ) -> None:
        self._store = document_store
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry

    @property
    def name(self) -> str:
        return "keyword"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        domain = (filters or {}).get("domain")
        half_life = _resolve_param(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _KEYWORD_COMPONENT,
            domain,
        )
        # ``domain`` is a scoping hint routed onto the ``content_tags`` facet
        # (default-pass, store-side) by :meth:`PackBuilder._apply_domain_scope`.
        # The scalar key is consumed here for per-(component, domain) param
        # resolution; forwarding it to the document store would re-introduce
        # the #254 scalar hard-equality that hard-excludes untagged rows.
        # ``domain`` is dropped for the #254 reason above; the graph-axis
        # control keys are dropped because the document store would read
        # them as metadata equality and return nothing (see
        # :data:`GRAPH_CONTROL_FILTER_KEYS`).
        store_filters = filters
        if filters and "domain" in filters:
            store_filters = {k: v for k, v in filters.items() if k != "domain"}
        store_filters = strip_graph_controls(store_filters)
        results = self._store.search(query, limit=limit, filters=store_filters)
        items = []
        for doc in results:
            metadata = doc.get("metadata", {})
            base_score = abs(doc.get("rank", 0.0))
            score = _apply_importance(base_score, metadata, **importance_params)
            # Source clock first, row clock second — see
            # :func:`resolve_recency_stamp`. The document store's columns are
            # this row's *write* clock; a conversation import's metadata
            # stamps are the content's own, and are what the semantic axis
            # has always read.
            recency = resolve_recency_stamp(
                metadata, doc.get("updated_at"), doc.get("created_at")
            )
            score = _apply_recency_decay(
                score,
                recency.stamp,
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=doc["doc_id"],
                    item_type="document",
                    excerpt=truncate_excerpt(doc.get("content", "")),
                    relevance_score=score,
                    metadata={
                        "source_strategy": "keyword",
                        **metadata,
                        # Stamped after the splat, for the #433 reason:
                        # which clock this search read is a fact about the
                        # search, and document metadata is an open bag that
                        # must not get a vote on it.
                        RECENCY_CLOCK_METADATA_KEY: recency.clock,
                    },
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


class SemanticSearch(SearchStrategy):
    """Vector similarity search via VectorStore."""

    def __init__(
        self,
        vector_store: Any,
        embedding_fn: Any = None,
        *,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
    ) -> None:
        self._store = vector_store
        self._embedding_fn = embedding_fn  # callable(str) -> list[float]
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry

    @property
    def name(self) -> str:
        return "semantic"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        if self._embedding_fn is None:
            logger.warning("semantic_search_no_embedding_fn")
            return []

        domain = (filters or {}).get("domain")
        half_life = _resolve_param(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _SEMANTIC_COMPONENT,
            domain,
        )
        # The vector store speaks neither the ``content_tags`` default-pass
        # facet nor a default-pass scalar ``domain`` filter — a store-side
        # filter on either compiles to hard equality and hard-excludes
        # domain-less rows (#254). Strip both from the store call and apply
        # domain scoping as a Python-side default-pass post-filter over the
        # materialized hits (which carry full document metadata). Over-fetch
        # so a heavily-mismatched domain doesn't thin recall (#262).
        # The graph-axis control keys go too — the vector store compiles an
        # unknown filter key to hard metadata equality and returns nothing
        # (see :data:`GRAPH_CONTROL_FILTER_KEYS`).
        store_filters = None
        if filters:
            store_filters = {
                k: v for k, v in filters.items() if k not in ("domain", "content_tags")
            } or None
        store_filters = strip_graph_controls(store_filters)
        fetch_k = limit * _SEMANTIC_DOMAIN_OVERFETCH if domain else limit
        query_vector = self._embedding_fn(query)
        results = self._store.query(query_vector, top_k=fetch_k, filters=store_filters)
        if domain:
            results = [
                r
                for r in results
                if _passes_domain_scope(r.get("metadata", {}), domain)
            ][:limit]
        items = []
        for result in results:
            metadata = result.get("metadata", {})
            base_score = result.get("score", 0.0)
            score = _apply_importance(base_score, metadata, **importance_params)
            # A vector row has no columns of its own — its recency stamp is
            # inside the metadata snapshot, either the source's (splatted
            # from the document) or ``build_vector_row``'s embed-time
            # ``setdefault``. Same resolver as the keyword axis so the two
            # cannot decay off different clocks for one document (#417).
            recency = resolve_recency_stamp(metadata)
            score = _apply_recency_decay(
                score,
                recency.stamp,
                half_life_days=half_life,
                floor=floor,
            )
            items.append(
                PackItem(
                    item_id=result["item_id"],
                    item_type="vector",
                    # Vector metadata is written already-truncated by
                    # ``build_vector_row``, which is the last place the full
                    # document is in hand; this only bounds rows from some
                    # other producer.
                    excerpt=truncate_excerpt(
                        metadata.get("content", metadata.get("excerpt", ""))
                    ),
                    relevance_score=score,
                    metadata={
                        "source_strategy": "semantic",
                        **metadata,
                        # After the splat, same reason as the keyword axis —
                        # and it matters more here: a vector row's metadata is
                        # an embed-time *snapshot* of the document's bag
                        # (#338), so anything the document carried is in it.
                        RECENCY_CLOCK_METADATA_KEY: recency.clock,
                    },
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


class GraphSearch(SearchStrategy):
    """Graph traversal search via GraphStore.

    **Query-independent unless seeded — read this before trusting the axis
    to answer an intent** (#371). The strategy has two branches, and only
    one of them is a search:

    * **Seeded** — ``filters["seed_ids"]`` was supplied, or a
      :class:`GraphSeedExtractor` was injected and produced ids. The
      strategy expands ``get_subgraph(seeds, depth=...)``. The seeds are
      where query relevance enters; everything downstream is scoring.
    * **Recency window** — no seeds. The strategy calls
      ``GraphStore.query``, which is ``ORDER BY created_at DESC LIMIT n``
      on every shipped backend (`stores/sqlite/graph.py`,
      `stores/postgres/graph.py`, `stores/bolt_opencypher/graph.py`). It
      takes no query argument, and there is no text index behind it. The
      axis therefore returns **the most recently created nodes**, filtered
      structurally and scored — without consulting what was asked.

    **Since #375 the first branch has a production producer**, and that
    is the one change worth reading carefully if you knew this class
    before. :func:`~trellis.retrieve.builder_factory.build_pack_builder`
    wires a :class:`NamespaceSeedExtractor` by default, so an intent that
    names an entity Trellis can resolve now expands that entity's
    neighbourhood. Two things it does **not** change. ``build_strategies``
    still defaults to ``graph_seed_extractor=None`` — the refusal #371
    recorded there was about a *specific* extractor measured as a no-op,
    and it stands. And ``seed_ids`` still has no in-repo producer: the
    entity-neighbourhood surfaces (``GET /entities/{id}``, MCP
    ``get_graph``) call ``graph_store.get_subgraph`` *directly* and never
    reach this class, and a section's ``entity_ids`` is a
    :class:`~trellis.retrieve.tier_mapping.TierMapper` routing filter over
    items already retrieved, not a seed.

    **The recency window is still the branch that runs when the intent
    names nothing resolvable**, which on the reference deployment is 34 of
    85 real intents. Everything below therefore remains live for those
    packs, and is stated where a reader meets it rather than in the issue
    that measured it:

    * The reachable set is a **fixed row count**
      (``limit * _GRAPH_RECENCY_OVERFETCH``), not a fraction of the graph,
      so **coverage decays as 1/N as the graph grows**. Measured on the
      reference deployment across the 37 packs assembled in the 30 days to
      2026-08-28: the window covered a median **8.6%** of servable nodes
      (range 7.2%-15.0%, falling monotonically as the graph grew from 286
      to 665 servable nodes) and spanned a median of **58 hours**.
    * An old, perfectly on-topic entity is **unreachable**, at any rank,
      for every intent.
    * A single bulk ingest of ``limit * _GRAPH_RECENCY_OVERFETCH`` nodes
      evicts the whole window. One pack in that measurement had a window
      spanning **0.0 hours** — every candidate came from one write batch.

    Every item carries ``metadata["graph_selection"]``
    (:data:`GRAPH_SELECTION_SEEDED` / :data:`GRAPH_SELECTION_RECENCY_WINDOW`)
    so which branch ran is legible in ``PACK_ASSEMBLED.injected_items[]``
    rather than inferred from the wiring.

    **Selection and ranking share one clock**
    (:data:`GRAPH_RECENCY_CLOCK_FIELD`, ``created_at``). Ranking by
    ``updated_at`` while selecting by ``created_at`` let an SCD-2
    re-version — a lifecycle stamp, a property merge — read as freshness on
    an axis whose window that re-version cannot widen (#420).

    Structural nodes (``node_role == "structural"``) are excluded by default
    — they represent fine-grained plumbing (columns, parameters, file
    lines) that is retrieved only as part of its parent's context. Pass
    ``include_structural=True`` via filters to surface them anyway.

    Unconfirmed extraction mints (``properties.extraction_status ==
    "unconfirmed"``) are likewise excluded by default: extraction from
    prose attests only that something was *mentioned*, and serving those
    nodes teaches downstream agents claims the source never made
    (trellis-ai#300 — the same claims-are-gated principle as the
    ``signal_quality="noise"`` document filter). Pass
    ``include_unconfirmed=True`` via filters to surface them (curation /
    review tooling), or confirm the entity via ``entity.update`` to make
    it retrievable for good.

    Curated nodes (``node_role == "curated"``) are retained and receive a
    relevance boost (``curated_boost``, default 1.3) because they are
    pre-digested synthesis — the highest information density per token.
    """

    def __init__(
        self,
        graph_store: Any,
        *,
        curated_boost: float = GRAPH_CURATED_BOOST,
        recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS,
        registry: ParameterRegistry | None = None,
        seed_extractor: GraphSeedExtractor | None = None,
    ) -> None:
        """Build the graph axis.

        Args:
            graph_store: Any :class:`~trellis.stores.base.graph.GraphStore`.
            curated_boost: Score multiplier for ``node_role == "curated"``.
            recency_half_life_days: Half-life for the recency decay applied
                to *scores*. Distinct from the recency *selection* the
                unseeded branch performs, but measured off the same column
                (:data:`GRAPH_RECENCY_CLOCK_FIELD`) so the two cannot order
                the same rows differently.
            registry: Optional :class:`ParameterRegistry` for per-domain
                scoring overrides.
            seed_extractor: Optional :class:`GraphSeedExtractor`. **Default
                ``None`` is the query-independent recency window** described
                in the class docstring. Supply one to make the axis a
                function of the intent. An extractor that returns ``[]`` or
                raises falls back to the recency window — seeding is
                additive, and a seeding miss must never cost the caller an
                axis it would otherwise have had.
        """
        self._store = graph_store
        self._curated_boost = curated_boost
        self._recency_half_life_days = recency_half_life_days
        self._registry = registry
        self._seed_extractor = seed_extractor

    @property
    def name(self) -> str:
        return "graph"

    def _query_nodes(
        self,
        *,
        node_type: str | None,
        properties: dict[str, Any] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Run a node query, expanding ``node_type`` to include legacy aliases.

        ADR Phase 2 (graph-ontology): a query for ``"Person"`` must
        bucket alongside legacy ``"person"`` rows during the migration
        period. We expand the requested type via
        :func:`~trellis.schemas.well_known.expand_entity_type_query`
        and, when the expansion yields more than one value, route
        through the canonical DSL with an ``in`` clause. Single-value
        types (open-string or canonicals with no aliases) keep using
        the legacy ``query`` path so backends that haven't shipped a
        DSL compiler yet still work.
        """
        # ``self._store`` is typed ``Any`` (graph store backends share
        # an open ABC), so we annotate locally to keep the ``Any`` taint
        # from leaking into ``GraphSearch.search``'s caller chain.
        rows: list[dict[str, Any]]
        if node_type is None:
            rows = self._store.query(
                node_type=None,
                properties=properties,
                limit=limit,
            )
            return rows

        expanded = expand_entity_type_query(node_type)
        if len(expanded) == 1:
            # No alias fan-out — the legacy single-string filter is
            # sufficient and avoids the DSL hop.
            rows = self._store.query(
                node_type=expanded[0],
                properties=properties,
                limit=limit,
            )
            return rows

        # Multi-value expansion routes through the DSL so backends
        # compile a single ``node_type IN (...)`` query rather than
        # forcing N round-trips. All shipped backends (sqlite,
        # postgres, neo4j) implement Phase 2 of the canonical-graph-
        # layer ADR.
        clauses: list[FilterClause] = [
            FilterClause(field="node_type", op="in", value=tuple(expanded)),
        ]
        for key, value in (properties or {}).items():
            clauses.append(
                FilterClause(field=f"properties.{key}", op="eq", value=value)
            )
        rows = self._store.execute_node_query(
            NodeQuery(filters=tuple(clauses), limit=limit),
        )
        return rows

    def _resolve_seeds(self, query: str, filters: dict[str, Any]) -> list[str]:
        """Decide which seeds this search expands, consuming ``filters``.

        Mutates ``filters`` — it pops the ``seed_ids`` control key and,
        on the derived path only, defaults ``depth``. Both are the
        caller's own copy, made at the top of :meth:`search`.

        An explicit seed set always wins: a caller that passed
        ``seed_ids`` has already decided which neighbourhood it wants, and
        re-deriving seeds from prose would silently widen a deliberately
        narrow request. No in-repo production caller does this today — see
        the class docstring.
        """
        if "seed_ids" in filters:
            # Annotated rather than returned straight out of the bag: the
            # value is caller-supplied and unvalidated, and this is the
            # same ``list[str]`` assertion the pre-#375 code made at this
            # same point. Nothing here starts trusting it more than before.
            named_seeds: list[str] = filters.pop("seed_ids")
            return named_seeds

        seed_ids = self._seeds_from_extractor(query)
        if seed_ids:
            # A derived seed gets one hop, a named one keeps the historical
            # two — see :data:`EXTRACTED_SEED_DEPTH`. ``setdefault`` and
            # not an assignment: a caller who passed ``depth`` explicitly
            # asked for that depth, and this path is about the *absence* of
            # an instruction.
            filters.setdefault("depth", EXTRACTED_SEED_DEPTH)
        return seed_ids

    def _seeds_from_extractor(self, query: str) -> list[str]:
        """Ask the injected extractor for seeds; never let it break a pack.

        Returns ``[]`` when no extractor is configured, when the extractor
        declines, or when it raises. The caller reads an empty list as "run
        the recency window" — the seeding path is additive by contract.
        """
        if self._seed_extractor is None:
            return []
        try:
            seeds = list(self._seed_extractor.extract(query))
        # GRACEFUL-DEGRADATION: an extractor typically embeds the intent
        # and queries the vector store. Neither is required for the graph
        # axis to return something, so a seeding failure degrades to the
        # unseeded branch rather than costing the caller an axis. Mirrors
        # PackBuilder's per-strategy failure handling one level down.
        except Exception:
            logger.exception(
                "graph_seed_extractor_failed",
                extractor=type(self._seed_extractor).__name__,
            )
            return []
        if not seeds:
            logger.debug(
                "graph_seed_extractor_returned_none",
                extractor=type(self._seed_extractor).__name__,
            )
        return seeds

    def _expand_seeds(
        self,
        seed_ids: list[str],
        *,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The seeded branch: expand a neighbourhood around known ids.

        This is the only branch in which the caller's intent has reached
        the store — via ``filters["seed_ids"]`` or a
        :class:`GraphSeedExtractor`. Consumes ``depth`` / ``edge_types``
        from *filters*.
        """
        depth = filters.pop("depth", 2)
        edge_types = filters.pop("edge_types", None)
        subgraph = self._store.get_subgraph(
            seed_ids,
            depth=depth,
            edge_types=edge_types,
        )
        nodes: list[dict[str, Any]] = subgraph.get("nodes", [])
        logger.debug(
            "graph_search_seeded",
            seed_count=len(seed_ids),
            depth=depth,
            nodes_returned=len(nodes),
        )
        return nodes

    def _recency_window_nodes(
        self,
        *,
        filters: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        """The unseeded branch: the newest ``limit * overfetch`` node rows.

        **The caller's query is not an input here and cannot be.**
        ``GraphStore.query`` is ``ORDER BY created_at DESC LIMIT n`` on
        every shipped backend and has no text index behind it, so this
        method selects on recency and structure alone. See the class
        docstring for what that costs. Consumes ``node_type`` from
        *filters*.
        """
        node_type = filters.pop("node_type", None)
        # ``domain`` and ``content_tags`` are scoping hints, not graph
        # properties: ``domain`` is applied client-side with default-pass
        # semantics by the caller (a domain-less node is never
        # hard-excluded, mirroring the other axes for #262), and
        # ``content_tags`` is a document-store facet the graph store cannot
        # interpret. Neither is forwarded as a property filter — a
        # store-side property filter compiles to hard equality and would
        # hard-exclude every domain-less node (#254).
        # Graph-axis controls are excluded too. ``node_type`` is popped just
        # above, and ``seed_ids`` / ``include_*`` are popped in ``search``
        # before either branch — but ``depth`` and ``edge_types`` are popped
        # only in the *seeded* branch, so on the unseeded branch (the one
        # production always takes, #371) they would otherwise arrive here and
        # become hard-equality node-property filters, emptying the graph axis
        # for a caller who was configuring a traversal.
        query_props = {
            k: v
            for k, v in filters.items()
            if k not in ("domain", "content_tags")
            and k not in GRAPH_CONTROL_FILTER_KEYS
        }
        # The multiplier is the whole candidate window (see the class
        # docstring), not headroom over a relevance-ordered result — it
        # exists so the client-side structural / unconfirmed filters have
        # rows to discard before the slice to ``limit``.
        scan_limit = limit * _GRAPH_RECENCY_OVERFETCH
        nodes = self._query_nodes(
            node_type=node_type,
            properties=query_props or None,
            limit=scan_limit,
        )
        logger.debug(
            "graph_search_recency_window",
            scan_limit=scan_limit,
            rows_returned=len(nodes),
            # True means the window was saturated: every row older than the
            # oldest one returned is unreachable, for *any* intent.
            window_saturated=len(nodes) >= scan_limit,
        )
        return nodes

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        filters = dict(filters) if filters else {}
        seed_ids = self._resolve_seeds(query, filters)

        include_structural = bool(filters.pop("include_structural", False))
        include_unconfirmed = bool(filters.pop("include_unconfirmed", False))

        # Extract domain for scoring (keep in filters for graph query too)
        request_domain = filters.get("domain")

        if seed_ids:
            selection = GRAPH_SELECTION_SEEDED
            nodes = self._expand_seeds(seed_ids, filters=filters)
        else:
            selection = GRAPH_SELECTION_RECENCY_WINDOW
            nodes = self._recency_window_nodes(filters=filters, limit=limit)

        # Filter structural nodes client-side unless explicitly requested.
        #
        # This drop happens *before* a ``PackItem`` exists, so it produces no
        # ``RejectedItem`` and is invisible to
        # :func:`~trellis.retrieve.withholding.summarize_withheld` — see that
        # module's "What this cannot see" section. Since #375/#436 the
        # population it removes includes every newly-written meta-Activity,
        # which used to be counted by ``PACK_ASSEMBLED.meta_filtered_count``
        # and no longer is. The debug line is the only observable the drop
        # has; it exists for parity with the ``include_unconfirmed`` filter
        # below, which has had one since #301.
        if not include_structural:
            nodes = _exclude_and_log(
                nodes,
                lambda n: n.get("node_role") != "structural",
                "graph_search_structural_excluded",
            )

        # Filter unconfirmed extraction mints unless explicitly requested
        # — client-side like the structural filter, so both the query and
        # subgraph branches are covered and a store-side property filter
        # can't hard-exclude the (status-less) majority of nodes.
        if not include_unconfirmed:
            nodes = _exclude_and_log(
                nodes,
                lambda n: (
                    (n.get("properties") or {}).get(EXTRACTION_STATUS_PROPERTY)
                    != EXTRACTION_STATUS_UNCONFIRMED
                ),
                "graph_search_unconfirmed_excluded",
            )

        # Domain scoping — the same default-pass contract as the keyword
        # facet and the semantic post-filter (#254): a node carrying an
        # explicitly mismatched domain (scalar ``properties.domain`` or the
        # ``properties.content_tags.domain`` facet) is excluded; a
        # domain-less node passes; a match passes and keeps the
        # ``domain_match_boost`` below.
        if request_domain:
            nodes = [
                n
                for n in nodes
                if _passes_domain_scope(n.get("properties", {}), request_domain)
            ]

        # Resolve all tuneable scoring params once per .search() call.
        domain_match_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "domain_match_boost",
            GRAPH_DOMAIN_MATCH_BOOST,
        )
        curated_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "curated_boost",
            self._curated_boost,
        )
        description_boost = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "description_boost",
            GRAPH_DESCRIPTION_BOOST,
        )
        position_decay_step = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "position_decay_step",
            GRAPH_POSITION_DECAY_STEP,
        )
        half_life = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "recency_half_life_days",
            self._recency_half_life_days,
        )
        floor = _resolve_param(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
            "recency_floor",
            RECENCY_FLOOR,
        )
        importance_params = _resolve_importance_params(
            self._registry,
            _GRAPH_COMPONENT,
            request_domain,
        )

        scored = nodes[:limit]
        # ``_apply_recency_decay`` fails open on an unusable timestamp, which
        # is the right per-item behaviour and the wrong thing to leave silent
        # in bulk: a backend that stores no ``created_at`` (the bolt adapter
        # reads it off the property bag, so a node written outside Trellis can
        # lack it) would rank purely on position with every item undecayed,
        # and nothing in the result would say the clock was gone.
        #
        # The predicate is ``_parse_stamp(...) is None`` — the *exact*
        # condition the decay fails open on — rather than a truthiness check.
        # A present-but-unparseable stamp earns the identical undecayed score,
        # so counting absences alone would report zero on the case an operator
        # most needs to see. Aggregated per search, not per item: one line to
        # find, not one per row.
        unusable_clock = sum(
            1
            for node in scored
            if _parse_stamp(node.get(GRAPH_RECENCY_CLOCK_FIELD)) is None
        )
        if unusable_clock:
            logger.warning(
                "graph_search_recency_clock_unusable",
                field=GRAPH_RECENCY_CLOCK_FIELD,
                rows_without_usable_clock=unusable_clock,
                rows_scored=len(scored),
                selection=selection,
            )

        items = []
        for i, node in enumerate(scored):
            props = node.get("properties", {})
            node_type_val = node.get("node_type", "")
            node_role_val = node.get("node_role") or "semantic"

            # Base score from position (decays)
            base_score = max(0.0, 1.0 - (i * position_decay_step))

            # Domain match boost: nodes matching requested domain score higher
            if request_domain and props.get("domain") == request_domain:
                base_score *= domain_match_boost

            # Curated nodes are pre-digested synthesis — boost them.
            if node_role_val == "curated":
                base_score *= curated_boost

            # Importance boost
            score = _apply_importance(base_score, props, **importance_params)

            # Prefer entities with descriptions — they carry more context
            if props.get("description") or props.get("comment"):
                score *= description_boost

            # Recency decay — older nodes score progressively lower.
            # Same clock the window was selected on; see
            # :data:`GRAPH_RECENCY_CLOCK_FIELD` for why it is not
            # ``updated_at`` and not a fallback chain.
            score = _apply_recency_decay(
                score,
                node.get(GRAPH_RECENCY_CLOCK_FIELD),
                half_life_days=half_life,
                floor=floor,
            )

            excerpt = props.get(
                "description",
                props.get("name", props.get("title", "")),
            )
            # ADR Phase 2: stamp the canonical bucket key alongside the
            # raw stored type so downstream group-by analytics don't
            # need to call canonicalize themselves.
            canonical_type = canonicalize_entity_type(node_type_val)
            items.append(
                PackItem(
                    item_id=node["node_id"],
                    item_type="entity",
                    excerpt=truncate_excerpt(str(excerpt)),
                    relevance_score=score,
                    metadata={
                        "source_strategy": "graph",
                        **{
                            k: v
                            for k, v in props.items()
                            if k not in ("name", "description", "comment")
                        },
                        # All four sit after the property spread,
                        # deliberately: ``properties`` is an open bag, so a
                        # node is free to carry a key of any of these names,
                        # and each is a fact about the *row* or about *this*
                        # search that a stored property must not get a vote on.
                        #
                        # ``node_role`` is the load-bearing one — PackBuilder
                        # reads it back as a *decision*, dropping items whose
                        # ``metadata["node_role"] == "structural"``. Spread
                        # last, a stored property could hide a structural row
                        # from that filter, or forge a structural verdict for
                        # a semantic one. ``node_type`` gates the
                        # meta-Activity filter the same way, and since #375
                        # gate 4 both also land in
                        # ``PACK_ASSEMBLED.injected_items[]``.
                        "node_type": node_type_val,
                        "node_type_canonical": canonical_type,
                        "node_role": node_role_val,
                        "graph_selection": selection,
                    },
                )
            )
        return sorted(items, key=lambda x: x.relevance_score, reverse=True)


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def build_strategies(
    registry: StoreRegistry,
    embedding_fn: Any | None = None,
    *,
    parameter_registry: ParameterRegistry | None = None,
    graph_seed_extractor: GraphSeedExtractor | None = None,
) -> list[SearchStrategy]:
    """Build the standard strategy list from a registry.

    Always includes KeywordSearch and GraphSearch.  Adds SemanticSearch when
    both a VectorStore and an ``embedding_fn`` callable are available.

    **The graph axis is query-independent unless you pass
    ``graph_seed_extractor``**, and the default here is deliberately
    ``None`` even though production now seeds. The seeding wiring lives
    one layer up in
    :func:`~trellis.retrieve.builder_factory.build_pack_builder`, which
    passes a
    :class:`NamespaceSeedExtractor` (#375). This default stays ``None``
    because the reasoning below is about a **different extractor** that
    was measured and refused — it is not a general argument against
    seeding, and collapsing the two would re-open a question that has been
    answered twice in opposite directions for opposite reasons (#371):

    * The obvious wiring — construct a
      :class:`~trellis.retrieve.semantic_seeds.SemanticSeedExtractor`
      whenever ``embedding_fn`` resolves — was **measured, not argued**.
      Replayed over all 37 real intents from the reference deployment's
      30-day ``PACK_ASSEMBLED`` history, against that deployment's own
      Postgres graph + pgvector stores and a live embedder, it produced
      **0 seeds on 37/37 intents** and changed the returned item set on
      **0/37**. The extractor filters vector hits to entity-summary
      documents and the corpus holds none, so it costs one embed per pack
      and returns the recency window regardless. Wiring it by default
      would have shipped a change that reports success and does nothing —
      exactly the failure shape `docs/design/swarm-handoff.md` §8 is about.
    * Auto-wiring on ``embedding_fn`` would also couple the graph axis to
      an embedder that this function otherwise treats as strictly
      optional, so a deployment without one would silently get a
      *differently-behaved* graph axis under the same name.
    * The axis measures well today (``useful_token_fraction`` 0.1744 vs
      semantic 0.1069, keyword 0.0241, 30d to 2026-08-28), so a
      speculative change risks a regression the headline would not show —
      the axis is only 7% of injected tokens.

    Making it non-``None`` is therefore an explicit, per-deployment
    decision, and the corpus has to be able to satisfy the extractor
    before it is worth making.

    Args:
        registry: The StoreRegistry providing stores.
        embedding_fn: Optional ``(str) -> list[float]`` callable.  When
            *None*, the helper checks ``registry.embedding_fn`` (which reads
            the ``embeddings`` config section).  If neither source provides
            one, SemanticSearch is skipped.
        parameter_registry: Optional :class:`ParameterRegistry` that
            strategies consult at call-time for per-(component, domain)
            scoring overrides.  When ``None`` the module-level defaults
            apply unchanged.
        graph_seed_extractor: Optional :class:`GraphSeedExtractor` handed
            to :class:`GraphSearch`.  ``None`` (the default) keeps the
            recency-window behaviour; ``build_pack_builder`` supplies a
            :class:`NamespaceSeedExtractor`, and it is the only in-repo
            caller that supplies one.  Never derived from ``embedding_fn``
            — see above.
    """
    strategies: list[SearchStrategy] = [
        KeywordSearch(registry.knowledge.document_store, registry=parameter_registry),
        GraphSearch(
            registry.knowledge.graph_store,
            registry=parameter_registry,
            seed_extractor=graph_seed_extractor,
        ),
    ]

    fn = embedding_fn or getattr(registry, "embedding_fn", None)
    if fn is not None:
        try:
            strategies.append(
                SemanticSearch(
                    registry.knowledge.vector_store,
                    fn,
                    registry=parameter_registry,
                )
            )
            logger.info("semantic_search_enabled")
        # GRACEFUL-DEGRADATION: semantic search is optional; a vector
        # backend that fails init must not block keyword + graph search
        # — log and continue without it.
        except Exception:
            logger.warning("semantic_search_init_failed", exc_info=True)

    return strategies
