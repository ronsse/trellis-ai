"""Well-known promotion loop — surface-only schema evolution.

The loop reads the GraphStore for open-string ``node_type`` /
``edge_type`` values that have accumulated significant usage and emits
:attr:`~trellis.stores.base.event_log.EventType.WELL_KNOWN_CANDIDATE`
events for the ones that meet operator-tunable thresholds. It never
auto-mutates :mod:`trellis.schemas.well_known` — the canonical registry
is a one-way commitment (see ``adr-graph-ontology.md`` §5.4) and the
promotion path is a human-authored ADR amendment.

Design constraints from ``adr-well-known-promotion-loop.md`` and the POC
directive in ``plan-self-improvement-program.md`` §2:

* **Read-only against GraphStore.** The analyzer queries; it never
  upserts.
* **Threshold lookup from ParameterRegistry — missing key raises.** No
  silent defaults; misconfiguration surfaces at the earliest possible
  point.
* **Idempotent across runs.** A candidate that surfaced last week is
  suppressed unless its count grew by ≥ 20% or its cooldown elapsed.
* **Filters its own writes.** :data:`MUTATION_EXECUTED` events whose
  ``requested_by`` field starts with ``"trellis_meta_"`` are excluded
  from candidate counts so the dogfooding loop (Item 6) doesn't
  bootstrap itself.

See ``docs/design/plan-well-known-promotion-loop.md`` for the full
phase plan.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog

from trellis.meta.agents import META_AGENT_PREFIX
from trellis.schemas import well_known as wk
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.ops import ParameterRegistry
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Threshold keys + scope
# ---------------------------------------------------------------------------

#: Parameter-registry component id used for threshold resolution. The
#: registry has no concept of "well known promotion" outside this loop,
#: so the component id is the only axis required.
PARAM_COMPONENT_ID: str = "learning.schema_evolution"

#: Total writes-with-this-type that must accumulate before a candidate
#: is eligible.
PARAM_COUNT_THRESHOLD: str = "well_known_count_threshold"

#: Distinct extractor (``requested_by``) identifiers that must have
#: emitted writes for this type.
PARAM_DISTINCT_EXTRACTORS: str = "well_known_distinct_extractors"

#: Distinct ``ContentTags.domain`` values observed across attached
#: items. Multi-domain usage is the strongest signal that the
#: open-string value is genuinely shared vocabulary rather than a
#: single-extractor leak.
PARAM_DISTINCT_DOMAINS: str = "well_known_distinct_domains"

#: Minimum acceptable average ``signal_quality`` (controlled vocabulary:
#: ``"noise"`` < ``"low"`` < ``"standard"`` < ``"high"``). Candidates
#: whose attached items skew toward noise are filtered out.
PARAM_MIN_SIGNAL_QUALITY: str = "well_known_min_signal_quality"

#: Minimum time window (days) the candidate's evidence must span.
#: Filters out spikes that haven't proven themselves across multiple
#: ingestion sessions.
PARAM_WINDOW_DAYS: str = "well_known_window_days"

#: Days between re-emissions of the same ``candidate_id`` when the
#: underlying count hasn't grown materially. Per ADR §2.3.
PARAM_COOLDOWN_DAYS: str = "well_known_cooldown_days"


REQUIRED_PARAM_KEYS: tuple[str, ...] = (
    PARAM_COUNT_THRESHOLD,
    PARAM_DISTINCT_EXTRACTORS,
    PARAM_DISTINCT_DOMAINS,
    PARAM_MIN_SIGNAL_QUALITY,
    PARAM_WINDOW_DAYS,
    PARAM_COOLDOWN_DAYS,
)


#: Recommended seed values for a fresh ParameterRegistry. Documented
#: here (not in the analyzer) so operators can hand-seed without
#: needing to consult the ADR.
RECOMMENDED_SEED_VALUES: dict[str, float | int | str | bool] = {
    PARAM_COUNT_THRESHOLD: 500,
    PARAM_DISTINCT_EXTRACTORS: 2,
    PARAM_DISTINCT_DOMAINS: 2,
    PARAM_MIN_SIGNAL_QUALITY: "standard",
    PARAM_WINDOW_DAYS: 7,
    PARAM_COOLDOWN_DAYS: 7,
}


# Ordering of the SignalQuality literal — index 0 is the worst quality,
# the last index is the best. The comparison "avg_signal_quality >=
# min_signal_quality" is computed against this ordering.
_SIGNAL_QUALITY_ORDER: tuple[str, ...] = ("noise", "low", "standard", "high")


# Trigger for re-emission within the cooldown window: a candidate whose
# count grew by ≥ this fraction since the prior emission re-surfaces
# regardless of the cooldown. Per ADR §2.3.
_COOLDOWN_GROWTH_RATIO: float = 0.20


# Default cap for enumerating current nodes from the GraphStore. The
# loop is bounded by ``well_known_count_threshold`` * a comfortable
# multiple — at the default threshold of 500, 50k nodes covers the
# top-100 open-string types. Operators with larger graphs can raise
# via the ``node_scan_limit`` kwarg.
_DEFAULT_NODE_SCAN_LIMIT: int = 50_000


#: ``requested_by`` prefix that the analyzer filters out of candidate
#: counts. Reserves the ``"trellis_meta_"`` namespace for Item 6's
#: dogfooding loop so its own ``MUTATION_EXECUTED`` events don't make
#: Item 6-emitted open-string types into perpetual self-promotion
#: candidates. Re-exported alias of
#: :data:`trellis.meta.agents.META_AGENT_PREFIX` — the meta module owns
#: the namespace; this alias preserves the pre-Item-6 import path.
META_EXTRACTOR_PREFIX: str = META_AGENT_PREFIX


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------

CandidateKind = Literal["entity_type", "edge_kind"]


@dataclass(frozen=True, slots=True)
class WellKnownCandidate:
    """A surfaced open-string type that crossed promotion thresholds.

    Frozen / slotted so it's safe to hash + dedupe across runs by
    ``candidate_id`` (the stable hash of ``open_string_value`` +
    ``candidate_kind``). Per ADR §2.2 the suggested canonical name and
    alignment URI are advisory only — the human authoring the
    promotion ADR decides whether to accept, rename, or reject.
    """

    candidate_kind: CandidateKind
    open_string_value: str
    count: int
    distinct_extractors: tuple[str, ...]
    distinct_domains: tuple[str, ...]
    avg_signal_quality: str
    first_seen: datetime
    last_seen: datetime
    suggested_canonical_name: str
    suggested_alignment_uri: str | None
    candidate_id: str
    cooldown_until: datetime | None = None
    naming_collision: bool = False
    recurrence_count: int = 0
    #: Findings that the loop saw but did not let block emission —
    #: typically informational annotations consumed by downstream
    #: ADR authors. Frozen tuple so dataclass remains hashable.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_event_payload(self) -> dict[str, Any]:
        """Render the candidate as a ``WELL_KNOWN_CANDIDATE`` payload.

        Keys are stable wire contract — analyzers downstream of the
        EventLog read this directly. Lists are emitted as plain
        ``list[str]`` (not tuples) so JSON round-trips cleanly across
        every supported backend.
        """
        return {
            "candidate_id": self.candidate_id,
            "candidate_kind": self.candidate_kind,
            "open_string_value": self.open_string_value,
            "count": self.count,
            "distinct_extractors": list(self.distinct_extractors),
            "distinct_domains": list(self.distinct_domains),
            "avg_signal_quality": self.avg_signal_quality,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "suggested_canonical_name": self.suggested_canonical_name,
            "suggested_alignment_uri": self.suggested_alignment_uri,
            "naming_collision": self.naming_collision,
            "recurrence_count": self.recurrence_count,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Thresholds:
    """Resolved threshold bundle. Constructed once per analyzer call."""

    count: int
    distinct_extractors: int
    distinct_domains: int
    min_signal_quality: str
    window_days: int
    cooldown_days: int


def _resolve_thresholds(registry: ParameterRegistry) -> _Thresholds:
    """Resolve all required thresholds from the registry.

    Per the POC directive: a missing key raises :class:`KeyError`
    naming the absent key. No fallback to module-level constants —
    silent defaults are exactly the kind of misconfiguration the rule
    forbids.
    """
    from trellis.schemas.parameters import ParameterScope  # noqa: PLC0415

    scope = ParameterScope(component_id=PARAM_COMPONENT_ID)
    values = registry.get_values(scope)
    missing = [key for key in REQUIRED_PARAM_KEYS if key not in values]
    if missing:
        seed_hint = ", ".join(f"{k}={RECOMMENDED_SEED_VALUES[k]!r}" for k in missing)
        msg = (
            f"ParameterRegistry is missing required schema-evolution "
            f"thresholds: {sorted(missing)!r}. Seed defaults are: "
            f"{seed_hint}. See "
            "docs/design/adr-well-known-promotion-loop.md §2.1 and "
            "trellis.learning.schema_evolution.RECOMMENDED_SEED_VALUES."
        )
        raise KeyError(msg)

    sig_q = values[PARAM_MIN_SIGNAL_QUALITY]
    if not isinstance(sig_q, str) or sig_q not in _SIGNAL_QUALITY_ORDER:
        msg = (
            f"{PARAM_MIN_SIGNAL_QUALITY} must be one of "
            f"{list(_SIGNAL_QUALITY_ORDER)}, got {sig_q!r}"
        )
        raise ValueError(msg)

    return _Thresholds(
        count=int(values[PARAM_COUNT_THRESHOLD]),
        distinct_extractors=int(values[PARAM_DISTINCT_EXTRACTORS]),
        distinct_domains=int(values[PARAM_DISTINCT_DOMAINS]),
        min_signal_quality=sig_q,
        window_days=int(values[PARAM_WINDOW_DAYS]),
        cooldown_days=int(values[PARAM_COOLDOWN_DAYS]),
    )


# ---------------------------------------------------------------------------
# Naming heuristics
# ---------------------------------------------------------------------------


_SPLIT_RE = re.compile(r"[\s_\-./]+")


def suggest_canonical_name(open_string_value: str, kind: CandidateKind) -> str:
    """Return a heuristic canonical-name suggestion.

    Entity types → PascalCase. Edge kinds → camelCase. Underscores,
    hyphens, spaces, dots, and slashes split the input into tokens.
    Already-canonical inputs (no split tokens AND first letter case
    matches kind convention) pass through unchanged — emitting a
    suggestion identical to the input is noise.

    The suggestion is **advisory only** (per ADR §2.4). The human
    authoring the promotion ADR decides whether to accept it,
    rename, or reject the promotion entirely.
    """
    tokens = [t for t in _SPLIT_RE.split(open_string_value) if t]
    if not tokens:
        return open_string_value

    # Camelcase splits inside a single token if it already mixes case
    # (``dbtModel`` -> ``dbt`` + ``Model``). Keeps the heuristic stable
    # for inputs that came from a half-canonicalised source.
    expanded: list[str] = []
    for token in tokens:
        if re.search(r"[A-Z]", token[1:]):
            parts = re.split(r"(?<=[a-z0-9])(?=[A-Z])", token)
            expanded.extend(p for p in parts if p)
        else:
            expanded.append(token)

    if kind == "entity_type":
        return "".join(t[:1].upper() + t[1:].lower() for t in expanded)

    # edge_kind → camelCase: first token lowercase, rest PascalCase.
    first, *rest = expanded
    head = first.lower()
    tail = "".join(t[:1].upper() + t[1:].lower() for t in rest)
    return f"{head}{tail}"


def _suggest_alignment_uri(canonical_name: str, kind: CandidateKind) -> str | None:
    """Best-effort schema.org URI suggestion.

    Returns a ``schema.org/<Name>`` candidate if the suggested
    canonical name is a single Pascal/camel token. Multi-token names
    typically don't map onto a real schema.org class so we suppress
    the suggestion — emitting a fake URI for ``DbtModel`` would
    mislead the ADR author and downstream RDF consumers.
    """
    if " " in canonical_name or not canonical_name:
        return None
    # Only one capitalised hump? Then it might be a real schema.org term.
    humps = len(re.findall(r"[A-Z]", canonical_name))
    if humps != 1:
        return None
    if kind == "entity_type":
        return f"schema.org/{canonical_name}"
    # Edges: prov: namespace would need real ontology lookup; suppress.
    return None


def _detect_naming_collision(canonical_name: str, kind: CandidateKind) -> bool:
    """``True`` if ``canonical_name`` is already in the well-known registry.

    Case-insensitive match across the canonical set AND legacy alias
    map. A case-only difference (``"Person"`` vs. ``"person"``) counts
    as collision because the ADR amendment cannot add a new canonical
    that overlaps with an existing alias — the alias would be
    ambiguous.
    """
    lowered = canonical_name.lower()
    if kind == "entity_type":
        canonicals = wk.CANONICAL_ENTITY_TYPES
        aliases = set(wk.ENTITY_TYPE_ALIASES.keys()) | set(
            wk.ENTITY_TYPE_ALIASES.values()
        )
    else:
        canonicals = wk.CANONICAL_EDGE_KINDS
        aliases = set(wk.EDGE_KIND_ALIASES.keys()) | set(wk.EDGE_KIND_ALIASES.values())
    haystack = {name.lower() for name in canonicals} | {a.lower() for a in aliases}
    return lowered in haystack


def _compute_candidate_id(open_string_value: str, kind: CandidateKind) -> str:
    """Stable hash used as the cooldown key.

    SHA-256 truncated to 16 hex chars — enough entropy for the bounded
    candidate space (< 10k distinct open strings even on large graphs)
    while keeping the candidate_id short enough to embed in ADR
    filenames.
    """
    digest = hashlib.sha256(f"{kind}::{open_string_value}".encode()).hexdigest()
    return f"wkc_{kind[:3]}_{digest[:16]}"


# ---------------------------------------------------------------------------
# Graph-side enumeration
# ---------------------------------------------------------------------------


def _enumerate_node_types(
    graph_store: GraphStore, *, node_scan_limit: int
) -> dict[str, list[dict[str, Any]]]:
    """Group current nodes by ``node_type``.

    Returns ``dict[node_type, list[node_record]]``. Only the current
    version of each node (``valid_to IS NULL``) is considered — SCD-2
    history isn't relevant for promotion eligibility.
    """
    nodes = graph_store.query(limit=node_scan_limit)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        ntype = node.get("node_type")
        if not isinstance(ntype, str) or not ntype:
            continue
        grouped.setdefault(ntype, []).append(node)
    return grouped


def _enumerate_edge_types(
    graph_store: GraphStore, nodes_by_type: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Group current edges by ``edge_type``.

    The :class:`GraphStore` ABC exposes edges only via
    :meth:`~trellis.stores.base.graph.GraphStore.get_edges` keyed by
    node — so we walk every current node once and collect outgoing
    edges. This is O(N) in node count, which the ``_DEFAULT_NODE_SCAN_LIMIT``
    bound makes acceptable for POC-scale graphs.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_edge_ids: set[str] = set()
    for nodes in nodes_by_type.values():
        for node in nodes:
            node_id = node.get("node_id")
            if not isinstance(node_id, str):
                continue
            edges = graph_store.get_edges(node_id, direction="outgoing")
            for edge in edges:
                eid = edge.get("edge_id")
                if isinstance(eid, str):
                    if eid in seen_edge_ids:
                        continue
                    seen_edge_ids.add(eid)
                etype = edge.get("edge_type")
                if not isinstance(etype, str) or not etype:
                    continue
                grouped.setdefault(etype, []).append(edge)
    return grouped


# ---------------------------------------------------------------------------
# Event-side enrichment
# ---------------------------------------------------------------------------


def _index_mutation_extractors(
    event_log: EventLog,
    *,
    since: datetime | None,
    until: datetime | None,
    scan_limit: int,
) -> dict[str, set[str]]:
    """Map ``entity_type`` -> set of ``requested_by`` identifiers.

    ``MUTATION_EXECUTED`` events carry the canonical write trail. The
    payload's ``requested_by`` field follows the
    ``<surface>:<verb>`` convention (see
    :class:`trellis.mutate.commands.Command`). The analyzer treats
    that as the ``extractor_id``.

    Events from the meta-extractor namespace (prefix
    :data:`META_EXTRACTOR_PREFIX`) are excluded to preempt Item 6's
    dogfooding loop self-promotion.
    """
    events = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED,
        since=since,
        until=until,
        limit=scan_limit,
    )
    out: dict[str, set[str]] = {}
    for event in events:
        etype = event.entity_type
        if not isinstance(etype, str) or not etype:
            continue
        requested_by = event.payload.get("requested_by")
        if not isinstance(requested_by, str) or not requested_by:
            continue
        if requested_by.startswith(META_EXTRACTOR_PREFIX):
            continue
        out.setdefault(etype, set()).add(requested_by)
    return out


# Top-level keys reserved for ``ContentTags`` content. If any of these
# appear as a top-level attribute on a node row (instead of nested under
# ``properties``), the analyzer treats it as a shape-contract violation
# and raises — silent empty domains would let axis G stay at 0 with no
# diagnostic. See ``_summarize_tags`` for the contract.
_RESERVED_TAG_KEYS_AT_TOP_LEVEL: tuple[str, ...] = ("content_tags", "tags")


def _extract_content_tags(node: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a ``ContentTags`` dict out of a node record if present.

    Convention across the codebase: tags live under
    ``properties["content_tags"]`` (or ``properties["tags"]`` for older
    rows). The store ABC pins this shape (see
    :class:`trellis.stores.base.graph.GraphStore`) so every backend
    surfaces tags through the same path.

    Returns ``None`` when ``properties`` exists but carries no tag dict
    (a legitimate "this node was never classified" case — typically
    structural nodes). The shape-violation path (top-level
    ``content_tags`` instead of nested) raises in
    :func:`_summarize_tags` rather than silently returning ``None`` here,
    so the diagnostic is one frame closer to the caller.
    """
    props = node.get("properties") or {}
    if not isinstance(props, dict):
        return None
    for key in ("content_tags", "tags"):
        candidate = props.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _summarize_tags(
    nodes_or_edges: Iterable[dict[str, Any]],
) -> tuple[tuple[str, ...], str]:
    """Return ``(distinct_domains, avg_signal_quality)`` across items.

    **Shape contract.** Every item MUST expose its ``ContentTags`` as a
    dict at ``item["properties"]["content_tags"]`` (or ``["tags"]`` for
    legacy rows). Top-level columns are reserved for graph-invariant
    metadata (``node_type``, ``valid_from``, ``created_at``, ...) — see
    :class:`trellis.stores.base.graph.GraphStore` for the ABC-level
    pin. Backends that put ``content_tags`` outside ``properties`` would
    leave the analyzer with empty domains and no diagnostic, so this
    function raises :class:`TypeError` on detected violations: a
    top-level ``content_tags`` / ``tags`` key on a node row is the
    canonical footgun (Phase 5A finding). Genuinely untagged nodes
    (``properties`` present, no tag key) are tolerated silently because
    structural nodes legitimately ship without classification.

    The "avg" is the *minimum* signal quality observed — a single
    noisy item drags the bucket down to its level. This is conservative
    by design: promotion criteria want to surface types that are
    *reliably* useful, not types where the median item happens to be
    high-signal but the tail is full of noise.

    Raises:
        TypeError: when an item carries a top-level ``content_tags`` or
            ``tags`` key. That's a backend-shape violation, not a data
            problem the analyzer can paper over — surface it loud per
            the POC directive (no silent fallbacks).
    """
    domains: set[str] = set()
    qualities: list[str] = []
    for item in nodes_or_edges:
        # Loud on shape mismatch: a top-level ``content_tags`` /
        # ``tags`` attribute means some backend bypassed the
        # ``properties`` bag. Returning empty domains here would mask
        # the misconfiguration; raise instead so callers see the cause.
        for reserved in _RESERVED_TAG_KEYS_AT_TOP_LEVEL:
            if reserved in item:
                node_id = item.get("node_id") or item.get("edge_id") or "<unknown>"
                msg = (
                    f"_summarize_tags: item {node_id!r} carries top-level "
                    f"{reserved!r} key — ContentTags must live under "
                    f"item['properties']['content_tags'] per the GraphStore "
                    f"ABC. A backend that promotes tags to a top-level "
                    f"column would silently return zero domains here and "
                    f"axis G of the well-known analyzer would stay at 0 "
                    f"with no diagnostic. Fix the backend's read path to "
                    f"return tags nested under 'properties'."
                )
                raise TypeError(msg)
        tags = _extract_content_tags(item)
        if tags is None:
            continue
        domain_list = tags.get("domain")
        if isinstance(domain_list, list):
            for d in domain_list:
                if isinstance(d, str) and d:
                    domains.add(d)
        sig_q = tags.get("signal_quality")
        if isinstance(sig_q, str) and sig_q in _SIGNAL_QUALITY_ORDER:
            qualities.append(sig_q)
    if not qualities:
        # No classification signal at all — treat as "standard" so the
        # absence of a tag doesn't accidentally block promotion. ADR
        # §2.1 says the average must be >= standard; we honor that
        # exactly without inventing a worse-than-standard default.
        avg_quality = "standard"
    else:
        # Pick the worst observed (lowest rank).
        avg_quality = min(qualities, key=_SIGNAL_QUALITY_ORDER.index)
    return tuple(sorted(domains)), avg_quality


def _first_last_seen(items: Iterable[dict[str, Any]]) -> tuple[datetime, datetime]:
    """Return the earliest and latest ``valid_from`` across items.

    **Shape contract.** Reads ``item["valid_from"]`` (and ``created_at``
    as legacy fallback) as top-level keys on the node / edge row. This
    is the inverse of the ``content_tags`` contract pinned in
    :func:`_summarize_tags`: SCD-2 temporal columns live at the top
    level of the row dict, while retrieval-shaping tags live nested
    inside ``properties``. The GraphStore ABC fixes both shapes — see
    :meth:`trellis.stores.base.graph.GraphStore.get_node` for the
    return-dict schema.

    ``valid_from`` is populated by every backend's SCD-2 write path.
    Falls back to ``created_at`` for items that pre-date the SCD
    columns (legacy fixtures).
    """
    timestamps: list[datetime] = []
    for item in items:
        for key in ("valid_from", "created_at"):
            value = item.get(key)
            if isinstance(value, datetime):
                timestamps.append(value)
                break
            if isinstance(value, str):
                try:
                    timestamps.append(datetime.fromisoformat(value))
                    break
                except ValueError:
                    continue
    if not timestamps:
        now = datetime.now(tz=UTC)
        return now, now
    return min(timestamps), max(timestamps)


# ---------------------------------------------------------------------------
# Cooldown bookkeeping
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PriorCandidate:
    """Snapshot of the most recent emission for a ``candidate_id``."""

    emitted_at: datetime
    count: int
    recurrence_count: int


def _load_prior_candidates(
    event_log: EventLog, *, scan_limit: int = 5_000
) -> dict[str, _PriorCandidate]:
    """Index the latest WELL_KNOWN_CANDIDATE event per ``candidate_id``.

    Per the swarm directive's "If you hit a blocker": payload predicate
    push-down is awkward across backends, so we read with ``order=desc``
    and filter Python-side. The unique candidate space is bounded by
    the sample size we'd produce anyway.
    """
    events = event_log.get_events(
        event_type=EventType.WELL_KNOWN_CANDIDATE,
        limit=scan_limit,
        order="desc",
    )
    out: dict[str, _PriorCandidate] = {}
    for event in events:
        cid = event.payload.get("candidate_id")
        if not isinstance(cid, str) or cid in out:
            # Only the most recent emission counts; ``order=desc``
            # means the first sighting per id is the freshest.
            continue
        prior_count = event.payload.get("count")
        prior_recurrence = event.payload.get("recurrence_count")
        out[cid] = _PriorCandidate(
            emitted_at=event.occurred_at,
            count=int(prior_count) if isinstance(prior_count, int | float) else 0,
            recurrence_count=int(prior_recurrence)
            if isinstance(prior_recurrence, int | float)
            else 0,
        )
    return out


def _cooldown_blocks_emission(
    *,
    candidate_id: str,
    current_count: int,
    prior: _PriorCandidate | None,
    cooldown_days: int,
    now: datetime,
) -> tuple[bool, datetime | None, int]:
    """Return ``(blocked, cooldown_until, recurrence_count)``.

    Per ADR §2.3:

    * No prior emission → not blocked, recurrence_count = 0.
    * Prior emission, count grew by >= 20% → not blocked, recurrence
      increments.
    * Prior emission within cooldown window AND count didn't grow →
      blocked, cooldown_until = prior_emitted_at + cooldown_days.
    * Prior emission past cooldown → not blocked, recurrence increments
      (per ADR §4.2, a persistent candidate is a persistent signal).
    """
    if prior is None:
        return False, None, 0

    growth_ratio = (
        (current_count - prior.count) / prior.count if prior.count > 0 else 1.0
    )
    if growth_ratio >= _COOLDOWN_GROWTH_RATIO:
        return False, None, prior.recurrence_count + 1

    cooldown_until = prior.emitted_at + timedelta(days=cooldown_days)
    if now < cooldown_until:
        logger.info(
            "well_known.candidate_suppressed_cooldown",
            candidate_id=candidate_id,
            cooldown_until=cooldown_until.isoformat(),
            current_count=current_count,
            prior_count=prior.count,
        )
        return True, cooldown_until, prior.recurrence_count

    return False, None, prior.recurrence_count + 1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_well_known_candidates(
    *,
    graph_store: GraphStore,
    event_log: EventLog,
    registry: ParameterRegistry,
    since: datetime | None = None,
    until: datetime | None = None,
    candidate_kinds: tuple[CandidateKind, ...] = ("entity_type", "edge_kind"),
    emit_events: bool = True,
    node_scan_limit: int = _DEFAULT_NODE_SCAN_LIMIT,
    now: datetime | None = None,
) -> list[WellKnownCandidate]:
    """Identify open-string types eligible for canonical promotion.

    Reads the GraphStore for current ``node_type`` / ``edge_type``
    values and the EventLog for the corresponding ``MUTATION_EXECUTED``
    history. Returns surfaced candidates and, when ``emit_events`` is
    ``True``, appends a :data:`WELL_KNOWN_CANDIDATE` event per surfaced
    candidate to ``event_log``.

    The analyzer is **read-only against the GraphStore**. The only
    writes are EventLog events.

    Args:
        graph_store: Source of node + edge type observations.
        event_log: Source of ``MUTATION_EXECUTED`` history for
            extractor counts, and destination for emitted
            ``WELL_KNOWN_CANDIDATE`` events.
        registry: Threshold resolver. Must carry every key in
            :data:`REQUIRED_PARAM_KEYS`; missing keys raise
            :class:`KeyError` rather than fall back to module defaults.
        since: Lower bound on the analysis window. Defaults to
            ``now - well_known_window_days`` resolved from the
            registry.
        until: Upper bound on the analysis window. Defaults to ``now``.
        candidate_kinds: Filter to ``"entity_type"`` and/or
            ``"edge_kind"``. Default analyses both.
        emit_events: When ``False`` the analyzer runs in dry-run mode —
            returns the candidate list but never emits.
        node_scan_limit: Cap on the number of current nodes pulled per
            backend. The default suits POC-scale graphs.
        now: Test seam for the cooldown clock. ``None`` uses
            ``datetime.now(tz=UTC)``.

    Returns:
        List of :class:`WellKnownCandidate`. Sorted by
        ``candidate_kind`` then descending ``count``.

    Raises:
        KeyError: When the registry lacks any required threshold key.
        ValueError: When ``min_signal_quality`` is not a recognised
            ``SignalQuality`` literal.
    """
    thresholds = _resolve_thresholds(registry)
    eval_now = now if now is not None else datetime.now(tz=UTC)
    # ``window_start`` gates the evidence-span filter (was a candidate
    # seen across enough recent history?). It is *not* used to bound
    # the MUTATION_EXECUTED scan — extractor counts span the entire
    # available history because a single-event extractor from years ago
    # is still a real distinct extractor for promotion purposes.
    window_start = (
        since
        if since is not None
        else eval_now - timedelta(days=thresholds.window_days)
    )
    # Pad the event scan with one day past ``eval_now`` so the test
    # seam (``now=`` kwarg) doesn't accidentally exclude events whose
    # ``recorded_at`` is microseconds past ``now``.
    event_scan_until = until if until is not None else eval_now + timedelta(days=1)

    nodes_by_type = _enumerate_node_types(graph_store, node_scan_limit=node_scan_limit)

    extractors_by_entity_type = _index_mutation_extractors(
        event_log,
        since=None,
        until=event_scan_until,
        scan_limit=node_scan_limit,
    )

    prior_candidates = _load_prior_candidates(event_log)

    surfaced: list[WellKnownCandidate] = []

    if "entity_type" in candidate_kinds:
        surfaced.extend(
            _analyze_kind(
                items_by_value=nodes_by_type,
                extractors_by_value=extractors_by_entity_type,
                kind="entity_type",
                thresholds=thresholds,
                prior_candidates=prior_candidates,
                window_start=window_start,
                eval_now=eval_now,
            )
        )

    if "edge_kind" in candidate_kinds:
        edges_by_type = _enumerate_edge_types(graph_store, nodes_by_type)
        # Edge writes have no canonical entity_type column on
        # MUTATION_EXECUTED rows — the existing pipeline records the
        # entity_type of the target. We approximate distinct extractors
        # by walking the per-edge ``properties.requested_by`` if set,
        # otherwise fall back to a single placeholder "unknown_edge".
        extractors_by_edge_type = _index_edge_extractors(edges_by_type)
        surfaced.extend(
            _analyze_kind(
                items_by_value=edges_by_type,
                extractors_by_value=extractors_by_edge_type,
                kind="edge_kind",
                thresholds=thresholds,
                prior_candidates=prior_candidates,
                window_start=window_start,
                eval_now=eval_now,
            )
        )

    surfaced.sort(key=lambda c: (c.candidate_kind, -c.count, c.open_string_value))

    if emit_events:
        for candidate in surfaced:
            event_log.emit(
                EventType.WELL_KNOWN_CANDIDATE,
                source="learning.schema_evolution",
                entity_id=candidate.candidate_id,
                entity_type=candidate.candidate_kind,
                payload=candidate.to_event_payload(),
            )

    return surfaced


def _index_edge_extractors(
    edges_by_type: dict[str, list[dict[str, Any]]],
) -> dict[str, set[str]]:
    """Best-effort per-edge extractor index.

    Looks at ``edge.properties.requested_by`` (or ``"extractor"``,
    falling back to ``"source"``) to gather the set of distinct emitters
    per edge type. Edges from the meta-extractor namespace are
    excluded.
    """
    out: dict[str, set[str]] = {}
    for etype, edges in edges_by_type.items():
        for edge in edges:
            props = edge.get("properties") or {}
            if not isinstance(props, dict):
                continue
            for key in ("requested_by", "extractor", "source"):
                value = props.get(key)
                if isinstance(value, str) and value:
                    if value.startswith(META_EXTRACTOR_PREFIX):
                        break
                    out.setdefault(etype, set()).add(value)
                    break
    return out


def _analyze_kind(
    *,
    items_by_value: dict[str, list[dict[str, Any]]],
    extractors_by_value: dict[str, set[str]],
    kind: CandidateKind,
    thresholds: _Thresholds,
    prior_candidates: dict[str, _PriorCandidate],
    window_start: datetime,
    eval_now: datetime,
) -> list[WellKnownCandidate]:
    """Evaluate every open-string value of one kind against the thresholds."""
    is_known = (
        wk.is_known_entity_type if kind == "entity_type" else wk.is_known_edge_kind
    )
    surfaced: list[WellKnownCandidate] = []

    for value, items in items_by_value.items():
        if is_known(value):
            # Already canonical or a registered alias — not a promotion
            # candidate. Per ADR §3.1 the registry never auto-mutates,
            # so existing canonicals are out of scope.
            continue

        count = len(items)
        if count < thresholds.count:
            continue

        extractors = extractors_by_value.get(value, set())
        if len(extractors) < thresholds.distinct_extractors:
            continue

        distinct_domains, avg_signal_quality = _summarize_tags(items)
        if len(distinct_domains) < thresholds.distinct_domains:
            continue
        avg_rank = _SIGNAL_QUALITY_ORDER.index(avg_signal_quality)
        min_rank = _SIGNAL_QUALITY_ORDER.index(thresholds.min_signal_quality)
        if avg_rank < min_rank:
            continue

        first_seen, last_seen = _first_last_seen(items)
        # Defer candidates whose evidence span is shorter than the
        # configured minimum AND that started inside the window —
        # filters short-lived spikes without rejecting older types
        # that haven't been written to recently. The criterion is the
        # *span* of evidence, not the start of the window.
        span = last_seen - first_seen
        if span < timedelta(days=thresholds.window_days) and first_seen > window_start:
            continue

        candidate_id = _compute_candidate_id(value, kind)
        prior = prior_candidates.get(candidate_id)
        blocked, cooldown_until, recurrence_count = _cooldown_blocks_emission(
            candidate_id=candidate_id,
            current_count=count,
            prior=prior,
            cooldown_days=thresholds.cooldown_days,
            now=eval_now,
        )
        if blocked:
            continue

        canonical_name = suggest_canonical_name(value, kind)
        naming_collision = _detect_naming_collision(canonical_name, kind)
        alignment_uri = _suggest_alignment_uri(canonical_name, kind)

        notes: list[str] = []
        if naming_collision:
            notes.append(
                f"suggested canonical name {canonical_name!r} collides "
                "with an existing canonical or alias; ADR author must rename "
                "or alias explicitly"
            )

        surfaced.append(
            WellKnownCandidate(
                candidate_kind=kind,
                open_string_value=value,
                count=count,
                distinct_extractors=tuple(sorted(extractors)),
                distinct_domains=distinct_domains,
                avg_signal_quality=avg_signal_quality,
                first_seen=first_seen,
                last_seen=last_seen,
                suggested_canonical_name=canonical_name,
                suggested_alignment_uri=alignment_uri,
                candidate_id=candidate_id,
                cooldown_until=cooldown_until,
                naming_collision=naming_collision,
                recurrence_count=recurrence_count,
                notes=tuple(notes),
            )
        )

    return surfaced
