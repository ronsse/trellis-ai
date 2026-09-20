"""Graph shape — the base rates a graph actuator has to quote before it exists.

The graph is the one plane with no instrument. ``analyze health`` watches
writes and serves, ``analyze value`` watches retrieval, ``capture_coverage``
watches the capture sweep — and nothing at all reports what the graph
actually looks like. So every proposal to reshape it has been argued from a
hand-written probe that ran once, was quoted in prose, and then rotted.

`#594 <https://github.com/ronsse/trellis-ai/issues/594>`_ is the cautionary
case, and it is cautionary in both directions. Its measurement was real —
213 of 1896 nodes isolated, 163 of them the lowercase vocabulary
``save_knowledge`` writes, ``gotcha`` isolated at 72 of 72 — and it is the
reason ``save_knowledge`` now links by default. But the same document also
carried two *false* claims, both produced by inferring a sentence from two
adjacent result sets rather than querying for it: that three of the seven
cross-type name collisions were casing collisions on the type itself (the
true number is zero), and that the max-degree node was "the opposite
failure" from the orphans (it is an Agent carrying one ``wasAssociatedWith``
edge per activity it ran, which is the correct PROV-O shape).

Both false claims are things this module computes directly. That is the
point of it: the questions an operator asks about graph shape should be
answered by a command, not by a conjunction of two numbers that happen to
be printed near each other.

Four commitments shape the report.

**One read pass, and truncation is reported rather than hidden.** Two
counts and two queries: :meth:`~trellis.stores.base.graph.GraphStore.count_nodes`
and ``count_edges`` size the read, then one ``execute_node_query`` and one
``execute_edge_query`` with no filters. There is no per-node
``get_edges`` — an N+1 over a graph this report exists to measure the size
of is how a sensor becomes too expensive to run — and no cap, because a
capped shape report describes the newest ``limit`` rows and calls them the
graph. The DSL has no cursor, so the limit is sized from the count plus
:data:`READ_MARGIN`; if a query comes back at its limit the read raced a
writer, and :attr:`GraphShapeReport.status` is ``truncated`` rather than a
number computed over a prefix.

**Bucketing is derived from the shipped alias map, and so is its failure.**
Type counts are bucketed through
:func:`~trellis.schemas.well_known.canonicalize_entity_type`, the same
function retrieval uses. :attr:`GraphShapeReport.uncovered_splits` then
reports the raw types that *would* have bucketed together had their casing
matched a key in that map — ``ENTITY_TYPE_ALIASES`` is keyed on lowercase
legacy names, so ``system`` collapses onto ``SoftwareApplication`` while
``System`` stays its own type. This is computed by asking whether a group
of case-insensitively-equal raw types canonicalizes to more than one
bucket, not by listing the pairs known today: a roster of blessed pairs
would be stale the first time a new vocabulary lands.

**Collisions are reported on both keys, because the difference is the
answer.** A name carried by nodes of more than one *raw* type and a name
carried by nodes of more than one *canonical* bucket are different
questions, and the gap between the two counts is exactly "how many of these
collisions does the alias map already explain" — the question #594's false
claim was reaching for. Reporting one of them would leave the reader to
infer the other, which is the habit that produced the false claim.

**Absence is reported as absence.** External-referent coverage has two
mechanisms and this pass can read one of them. Node properties come free
with the node read, so both the well-known dataset routing properties and
a generic locator shape are counted, separately, never summed into a single
authoritative number. The ``entity_aliases`` table is the other mechanism —
an alias is literally "this node corresponds to object X in external system
Y" — and :meth:`~trellis.stores.base.graph.GraphStore.get_aliases` is keyed
by a single ``entity_id``, so reading it here would be the N+1 the first
commitment rules out. :attr:`ReferentCoverage.alias_table_read` says so in
the payload instead of letting a zero stand in for a thing never looked at.

Read-only throughout: no meta-Activity wrapper, no event emission, nothing
that would make running the sensor change the thing it measures.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Final

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.schemas.well_known import (
    DATASET_ROUTING_PROPERTIES,
    canonicalize_entity_type,
)
from trellis.stores.base.graph_query import EdgeQuery, NodeQuery

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: Headroom added to the row count when sizing the single read. The DSL
#: exposes no cursor, so the limit has to be chosen up front; this absorbs
#: writes landing between the count and the query. A read that still comes
#: back at its limit is reported as truncated, never silently prefixed.
READ_MARGIN: Final[int] = 1_000

#: Property key holding an entity's display name. The convention every
#: writer follows (``entity_resolution``, ``name_aliases``, both SQL graph
#: backends' owner-name lookup); nodes without it are excluded from the
#: collision pass rather than grouped under a shared empty key.
NAME_PROPERTY: Final[str] = "name"

#: Matches a property value that points outside Trellis: any URI scheme,
#: the authority-less schemes, or an absolute / home-relative filesystem
#: path. A *shape* test rather than a roster of key names, so a locator
#: parked under a key nobody anticipated is still counted — and the keys
#: that matched are reported, so the operator can see what it caught.
LOCATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""^(?:
        [a-z][a-z0-9+.\-]*://   # scheme with authority: https:// s3:// postgres://
      | (?:file|mailto|urn):    # schemes with no authority component
      | ~?/\S                   # absolute or home-relative filesystem path
    )""",
    re.VERBOSE | re.IGNORECASE,
)

#: Degree histogram buckets as ``(label, low, high)``; ``high=None`` is
#: open-ended. Doubling widths because degree is heavy-tailed — #594's
#: top hub carried 207 edges against a median of 1, and linear buckets
#: would render that as one populated cell and a long tail of zeros.
DEGREE_BUCKETS: Final[tuple[tuple[str, int, int | None], ...]] = (
    ("0", 0, 0),
    ("1", 1, 1),
    ("2-3", 2, 3),
    ("4-7", 4, 7),
    ("8-15", 8, 15),
    ("16-31", 16, 31),
    ("32-63", 32, 63),
    ("64+", 64, None),
)

#: How many hubs :attr:`DegreeDistribution.top_hubs` names. Enough to see
#: whether a star graph is one agent or a family of them (#594 needed the
#: next four to tell), short enough that the text render stays readable.
TOP_HUB_COUNT: Final[int] = 5

#: The threshold both "this disagrees with itself" passes use: a split or a
#: collision needs at least two distinct members to exist at all.
MIN_DISAGREEMENT: Final[int] = 2

#: Document counts above this fold into the histogram's open-ended bucket.
#: #594 measured zero nodes carrying more than one document, so the interesting
#: cells are 0 and 1 and everything past 2 is the tail that claim predicts empty.
DOCUMENT_HISTOGRAM_TAIL: Final[int] = 3

#: How many collision groups the report enumerates. The counts are always
#: complete; only the per-group detail is capped, and
#: :attr:`NameCollisions.groups_listed` says how many of how many are shown.
MAX_COLLISION_GROUPS: Final[int] = 20


class ScanWindow(TrellisModel):
    """What the single read actually saw, beside what it was told to expect."""

    nodes_counted: int = Field(description="count_nodes() before the read")
    nodes_read: int = Field(description="Rows the node query returned")
    node_limit: int = Field(description="Limit the node query was issued with")
    edges_counted: int = Field(description="count_edges() before the read")
    edges_read: int = Field(description="Rows the edge query returned")
    edge_limit: int = Field(description="Limit the edge query was issued with")
    truncated: bool = Field(
        default=False,
        description="A query returned at its limit; every rate below is over a prefix",
    )
    note: str = Field(
        default="",
        description="Why the read is truncated, when it is. Empty otherwise.",
    )


class TypeBucket(TrellisModel):
    """One canonical type, with the raw spellings that landed in it."""

    canonical: str
    total: int
    share: float
    raw_types: dict[str, int] = Field(
        default_factory=dict,
        description="Raw node_type -> count, for the raw types bucketed here",
    )


class UncoveredSplit(TrellisModel):
    """Raw types that differ only in case and did *not* bucket together.

    ``ENTITY_TYPE_ALIASES`` is keyed on lowercase legacy names, so an
    entity written as ``System`` misses the ``system -> SoftwareApplication``
    alias and becomes its own type. The alias map covers the vocabulary it
    was written for; this names where the data left it behind.
    """

    lowercase_key: str = Field(
        description="The canonical bucket these types share once lowercased"
    )
    buckets: dict[str, int] = Field(
        description="Canonical bucket -> node count, one entry per split half"
    )
    raw_types: dict[str, int] = Field(description="Raw node_type -> node count")
    nodes: int = Field(description="Total nodes across every half of the split")
    share: float = Field(description="Those nodes as a fraction of the graph")


class CollisionGroup(TrellisModel):
    """One name carried by nodes that disagree about what kind of thing it is."""

    name: str
    nodes: int
    canonical_types: dict[str, int] = Field(
        description="Canonical bucket -> node count for nodes carrying this name"
    )
    raw_types: dict[str, int] = Field(description="Raw node_type -> node count")
    node_ids: list[str] = Field(default_factory=list)


class NameCollisions(TrellisModel):
    """Cross-type name collisions, counted on both keys.

    ``canonical`` is the honest count of referent-identity disagreement:
    nodes sharing a name that bucket to more than one canonical type.
    ``raw_type`` is the same count before the alias map is applied. The
    difference is how many collisions the alias map already explains —
    the number #594 guessed at and got wrong.
    """

    named_nodes: int = Field(description="Nodes carrying a non-empty name property")
    distinct_names: int
    canonical: int = Field(description="Names spanning >1 canonical bucket")
    raw_type: int = Field(description="Names spanning >1 raw node_type")
    explained_by_alias_map: int = Field(
        description="raw_type - canonical: collisions the alias map resolves"
    )
    same_type_duplicates: int = Field(
        description="Names carried by >1 node that all bucket to one type"
    )
    groups_listed: int = Field(description="How many of `canonical` are enumerated")
    groups: list[CollisionGroup] = Field(default_factory=list)


class HubNode(TrellisModel):
    """One of the highest-degree nodes, named so a star can be recognised."""

    node_id: str
    name: str = ""
    canonical_type: str = ""
    degree: int


class DegreeDistribution(TrellisModel):
    """Degree over current nodes, computed from the single edge read."""

    nodes: int
    edges: int
    degree_min: int = 0
    degree_p50: int = 0
    degree_p90: int = 0
    degree_p99: int = 0
    degree_max: int = 0
    mean: float = 0.0
    histogram: dict[str, int] = Field(default_factory=dict)
    top_hubs: list[HubNode] = Field(default_factory=list)
    dangling_endpoints: int = Field(
        default=0,
        description=(
            "Edge endpoints naming a node absent from the current node read. "
            "Non-zero means edges outlived their nodes, or the read was truncated."
        ),
    )


class TypeIsolation(TrellisModel):
    """Isolated share for one canonical bucket."""

    canonical: str
    total: int
    isolated: int
    share: float


class IsolationReport(TrellisModel):
    """Degree-zero nodes overall and per canonical type.

    The base rate #594 was built on, and the one an actuator that reshapes
    neighbourhoods has to quote: it cannot rebalance a node that has none.
    """

    nodes: int
    isolated: int
    share: float
    by_type: list[TypeIsolation] = Field(default_factory=list)


class ReferentCoverage(TrellisModel):
    """How many nodes point at something outside Trellis.

    Two independent measures over node properties, reported separately and
    never summed into one authoritative figure, plus ``any`` for the union
    an actuator needs as a base rate. The alias table is the third
    mechanism and is not read here — see the module docstring.
    """

    nodes: int
    well_known: int = Field(
        description="Nodes carrying any DATASET_ROUTING_PROPERTIES key"
    )
    well_known_share: float = 0.0
    locator_shape: int = Field(
        description="Nodes with any property value matching LOCATOR_PATTERN"
    )
    locator_share: float = 0.0
    any_referent: int = Field(description="Nodes matched by either measure")
    any_share: float = 0.0
    by_property: dict[str, int] = Field(
        default_factory=dict,
        description="Property key -> node count, for every key that matched either",
    )
    alias_table_read: bool = Field(
        default=False,
        description=(
            "Always False: get_aliases is keyed by entity_id, so reading the "
            "alias table here would be one query per node. Absence of alias "
            "evidence in this report is not evidence of absent aliases."
        ),
    )


class DocumentLinkage(TrellisModel):
    """The graph-to-memory join, which is what makes this a provenance map.

    #594 measured 180 of 1896 nodes carrying any ``document_ids``, and none
    carrying more than one. ``max_per_node`` is here so that second half is
    a reading rather than a recollection.
    """

    nodes: int
    linked: int
    share: float
    max_per_node: int = 0
    total_links: int = 0
    histogram: dict[str, int] = Field(
        default_factory=dict,
        description="Document-count -> node count ('0', '1', '2', '3+')",
    )
    by_type: dict[str, int] = Field(
        default_factory=dict,
        description="Canonical bucket -> linked node count",
    )


class GraphShapeReport(TrellisModel):
    """One read pass over the graph, rendered as base rates."""

    status: str = Field(
        default="ok", description="'ok' or 'truncated' — see ScanWindow.note"
    )
    scan: ScanWindow
    nodes: int
    edges: int
    node_roles: dict[str, int] = Field(default_factory=dict)
    type_buckets: list[TypeBucket] = Field(default_factory=list)
    uncovered_splits: list[UncoveredSplit] = Field(default_factory=list)
    edge_types: dict[str, int] = Field(
        default_factory=dict,
        description="Canonical edge kind -> count, bucketed through the alias map",
    )
    collisions: NameCollisions
    degree: DegreeDistribution
    isolation: IsolationReport
    referents: ReferentCoverage
    documents: DocumentLinkage


def _share(part: int, whole: int) -> float:
    """Fraction, with zero for an empty population rather than a ZeroDivisionError."""
    return (part / whole) if whole else 0.0


def _percentile(sorted_values: list[int], fraction: float) -> int:
    """Nearest-rank percentile over an ascending list. ``0`` when empty."""
    if not sorted_values:
        return 0
    index = round(fraction * (len(sorted_values) - 1))
    return sorted_values[index]


def _bucket_label(degree: int) -> str:
    """Histogram bucket for one degree value."""
    for label, low, high in DEGREE_BUCKETS:
        if degree >= low and (high is None or degree <= high):
            return label
    return DEGREE_BUCKETS[-1][0]


def _read_limit(counted: int) -> int:
    """Limit for a single full-table read, sized from the row count."""
    return max(counted + READ_MARGIN, 1)


def _node_name(node: dict[str, Any]) -> str:
    """Display name for a node, or ``""`` when it carries none."""
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return ""
    name = properties.get(NAME_PROPERTY)
    return name.strip() if isinstance(name, str) else ""


def _collect_type_buckets(nodes: list[dict[str, Any]]) -> list[TypeBucket]:
    """Canonical buckets with the raw spellings that landed in each."""
    raw_by_canonical: dict[str, Counter[str]] = defaultdict(Counter)
    for node in nodes:
        raw = str(node.get("node_type") or "")
        raw_by_canonical[canonicalize_entity_type(raw)][raw] += 1
    buckets = [
        TypeBucket(
            canonical=canonical,
            total=sum(raws.values()),
            share=_share(sum(raws.values()), len(nodes)),
            raw_types=dict(raws.most_common()),
        )
        for canonical, raws in raw_by_canonical.items()
    ]
    buckets.sort(key=lambda bucket: (-bucket.total, bucket.canonical))
    return buckets


def _collect_uncovered_splits(
    buckets: list[TypeBucket], total_nodes: int
) -> list[UncoveredSplit]:
    """Raw types that differ only in case and landed in different buckets.

    Derived, not listed: group every raw type by the bucket its *lowercase*
    form canonicalizes to, then report the groups whose actual buckets
    number more than one. A group of size one is a type the alias map
    handles; a group of size two or more is a split it does not cover.
    """
    raw_to_bucket: dict[str, str] = {}
    raw_counts: Counter[str] = Counter()
    for bucket in buckets:
        for raw, count in bucket.raw_types.items():
            raw_to_bucket[raw] = bucket.canonical
            raw_counts[raw] += count

    grouped: dict[str, set[str]] = defaultdict(set)
    for raw in raw_to_bucket:
        grouped[canonicalize_entity_type(raw.lower())].add(raw)

    splits: list[UncoveredSplit] = []
    for lowercase_key, raws in grouped.items():
        landed = {raw_to_bucket[raw] for raw in raws}
        if len(landed) < MIN_DISAGREEMENT:
            continue
        per_bucket: Counter[str] = Counter()
        for raw in raws:
            per_bucket[raw_to_bucket[raw]] += raw_counts[raw]
        nodes = sum(per_bucket.values())
        splits.append(
            UncoveredSplit(
                lowercase_key=lowercase_key,
                buckets=dict(per_bucket.most_common()),
                raw_types={raw: raw_counts[raw] for raw in sorted(raws)},
                nodes=nodes,
                share=_share(nodes, total_nodes),
            )
        )
    splits.sort(key=lambda split: (-split.nodes, split.lowercase_key))
    return splits


def _collect_collisions(nodes: list[dict[str, Any]]) -> NameCollisions:
    """Names carried by more than one node, split by whether the types agree.

    Grouped on the casefolded name: ``Hermes`` and ``hermes`` are one
    referent for federation purposes, and treating them as two would hide
    exactly the disagreement this measures. The raw spellings survive in
    each group's ``raw_types`` / ``node_ids``.
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        name = _node_name(node)
        if name:
            by_name[name.casefold()].append(node)

    canonical_groups: list[CollisionGroup] = []
    raw_type_collisions = 0
    same_type_duplicates = 0
    for members in by_name.values():
        if len(members) < MIN_DISAGREEMENT:
            continue
        raw_counts: Counter[str] = Counter(
            str(member.get("node_type") or "") for member in members
        )
        canonical_counts: Counter[str] = Counter(
            canonicalize_entity_type(raw) for raw in raw_counts.elements()
        )
        if len(raw_counts) > 1:
            raw_type_collisions += 1
        if len(canonical_counts) > 1:
            canonical_groups.append(
                CollisionGroup(
                    name=_node_name(members[0]),
                    nodes=len(members),
                    canonical_types=dict(canonical_counts.most_common()),
                    raw_types=dict(raw_counts.most_common()),
                    node_ids=[str(member.get("node_id") or "") for member in members],
                )
            )
        else:
            same_type_duplicates += 1

    canonical_groups.sort(key=lambda group: (-group.nodes, group.name))
    return NameCollisions(
        named_nodes=sum(len(members) for members in by_name.values()),
        distinct_names=len(by_name),
        canonical=len(canonical_groups),
        raw_type=raw_type_collisions,
        explained_by_alias_map=raw_type_collisions - len(canonical_groups),
        same_type_duplicates=same_type_duplicates,
        groups_listed=min(len(canonical_groups), MAX_COLLISION_GROUPS),
        groups=canonical_groups[:MAX_COLLISION_GROUPS],
    )


def _collect_degrees(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[dict[str, int], DegreeDistribution]:
    """Degree per node plus the distribution over it.

    Computed from the edge list, never from ``get_edges``: one pass over
    the edges is the whole cost, against one query per node otherwise.
    Both endpoints of an edge earn a degree, and an endpoint naming a node
    the read did not return is counted as dangling rather than dropped.
    """
    degrees: dict[str, int] = {str(node.get("node_id") or ""): 0 for node in nodes}
    dangling = 0
    for edge in edges:
        for key in ("source_id", "target_id"):
            endpoint = str(edge.get(key) or "")
            if endpoint in degrees:
                degrees[endpoint] += 1
            else:
                dangling += 1

    values = sorted(degrees.values())
    histogram = {label: 0 for label, _, _ in DEGREE_BUCKETS}
    for degree in values:
        histogram[_bucket_label(degree)] += 1

    names = {str(node.get("node_id") or ""): node for node in nodes}
    top = sorted(degrees.items(), key=lambda item: (-item[1], item[0]))[:TOP_HUB_COUNT]
    hubs = [
        HubNode(
            node_id=node_id,
            name=_node_name(names.get(node_id, {})),
            canonical_type=canonicalize_entity_type(
                str(names.get(node_id, {}).get("node_type") or "")
            ),
            degree=degree,
        )
        for node_id, degree in top
        if degree > 0
    ]

    distribution = DegreeDistribution(
        nodes=len(nodes),
        edges=len(edges),
        degree_min=values[0] if values else 0,
        degree_p50=_percentile(values, 0.50),
        degree_p90=_percentile(values, 0.90),
        degree_p99=_percentile(values, 0.99),
        degree_max=values[-1] if values else 0,
        mean=_share(sum(values), len(values)),
        histogram=histogram,
        top_hubs=hubs,
        dangling_endpoints=dangling,
    )
    return degrees, distribution


def _collect_isolation(
    nodes: list[dict[str, Any]], degrees: dict[str, int]
) -> IsolationReport:
    """Degree-zero share overall and per canonical bucket."""
    totals: Counter[str] = Counter()
    isolated: Counter[str] = Counter()
    for node in nodes:
        canonical = canonicalize_entity_type(str(node.get("node_type") or ""))
        totals[canonical] += 1
        if degrees.get(str(node.get("node_id") or ""), 0) == 0:
            isolated[canonical] += 1

    by_type = [
        TypeIsolation(
            canonical=canonical,
            total=total,
            isolated=isolated[canonical],
            share=_share(isolated[canonical], total),
        )
        for canonical, total in totals.items()
    ]
    by_type.sort(key=lambda row: (-row.isolated, -row.total, row.canonical))
    total_isolated = sum(isolated.values())
    return IsolationReport(
        nodes=len(nodes),
        isolated=total_isolated,
        share=_share(total_isolated, len(nodes)),
        by_type=by_type,
    )


def _is_locator(value: Any) -> bool:
    """Does this property value point outside Trellis?

    Only strings are examined, and only at the top level of the property
    bag: a locator nested inside a list or dict is a different convention
    and counting it would make the measure depend on how deeply an
    extractor happened to nest its output.
    """
    return isinstance(value, str) and bool(LOCATOR_PATTERN.match(value.strip()))


def _collect_referents(nodes: list[dict[str, Any]]) -> ReferentCoverage:
    """External-referent coverage from node properties.

    Two measures, kept apart. ``well_known`` counts the dataset routing
    properties the repo already blessed for this purpose; ``locator_shape``
    counts property values that look like a locator whatever key they sit
    under. Summing them would double-count the nodes both fire on, and
    picking one would hide the other's misses.
    """
    well_known = 0
    locator = 0
    any_referent = 0
    by_property: Counter[str] = Counter()
    for node in nodes:
        properties = node.get("properties")
        if not isinstance(properties, dict):
            continue
        hit_well_known = False
        hit_locator = False
        for key, value in properties.items():
            if key in DATASET_ROUTING_PROPERTIES and value not in (None, ""):
                hit_well_known = True
                by_property[key] += 1
            elif _is_locator(value):
                hit_locator = True
                by_property[key] += 1
        well_known += int(hit_well_known)
        locator += int(hit_locator)
        any_referent += int(hit_well_known or hit_locator)

    total = len(nodes)
    return ReferentCoverage(
        nodes=total,
        well_known=well_known,
        well_known_share=_share(well_known, total),
        locator_shape=locator,
        locator_share=_share(locator, total),
        any_referent=any_referent,
        any_share=_share(any_referent, total),
        by_property=dict(by_property.most_common()),
        alias_table_read=False,
    )


def _collect_documents(nodes: list[dict[str, Any]]) -> DocumentLinkage:
    """The graph-to-memory join: how many nodes carry ``document_ids``."""
    histogram = {"0": 0, "1": 0, "2": 0, "3+": 0}
    by_type: Counter[str] = Counter()
    linked = 0
    total_links = 0
    max_per_node = 0
    for node in nodes:
        document_ids = node.get("document_ids") or []
        count = len(document_ids) if isinstance(document_ids, list) else 0
        total_links += count
        max_per_node = max(max_per_node, count)
        if count:
            linked += 1
            by_type[canonicalize_entity_type(str(node.get("node_type") or ""))] += 1
        histogram[str(count) if count < DOCUMENT_HISTOGRAM_TAIL else "3+"] += 1

    return DocumentLinkage(
        nodes=len(nodes),
        linked=linked,
        share=_share(linked, len(nodes)),
        max_per_node=max_per_node,
        total_links=total_links,
        histogram=histogram,
        by_type=dict(by_type.most_common()),
    )


def analyze_graph_shape(graph_store: GraphStore) -> GraphShapeReport:
    """Read the graph once and report its shape.

    Args:
        graph_store: The graph store to read. Never written to.

    Returns:
        A :class:`GraphShapeReport`. When either query came back at its
        limit the report's ``status`` is ``"truncated"`` and every rate in
        it was computed over a prefix of the graph — callers that branch on
        the numbers must branch on ``status`` first.
    """
    node_count = graph_store.count_nodes()
    edge_count = graph_store.count_edges()
    node_limit = _read_limit(node_count)
    edge_limit = _read_limit(edge_count)

    nodes = graph_store.execute_node_query(NodeQuery(filters=(), limit=node_limit))
    edges = graph_store.execute_edge_query(EdgeQuery(filters=(), limit=edge_limit))

    truncated_parts: list[str] = []
    if len(nodes) >= node_limit:
        truncated_parts.append(f"nodes hit the read limit ({node_limit})")
    if len(edges) >= edge_limit:
        truncated_parts.append(f"edges hit the read limit ({edge_limit})")
    truncated = bool(truncated_parts)
    note = (
        " and ".join(truncated_parts)
        + "; every rate below was computed over the newest rows only"
        if truncated
        else ""
    )

    scan = ScanWindow(
        nodes_counted=node_count,
        nodes_read=len(nodes),
        node_limit=node_limit,
        edges_counted=edge_count,
        edges_read=len(edges),
        edge_limit=edge_limit,
        truncated=truncated,
        note=note,
    )

    buckets = _collect_type_buckets(nodes)
    degrees, degree_distribution = _collect_degrees(nodes, edges)
    edge_types: Counter[str] = Counter()
    for edge in edges:
        edge_types[str(edge.get("edge_type") or "")] += 1

    roles: Counter[str] = Counter(
        str(node.get("node_role") or "semantic") for node in nodes
    )

    report = GraphShapeReport(
        status="truncated" if truncated else "ok",
        scan=scan,
        nodes=len(nodes),
        edges=len(edges),
        node_roles=dict(roles.most_common()),
        type_buckets=buckets,
        uncovered_splits=_collect_uncovered_splits(buckets, len(nodes)),
        edge_types=dict(edge_types.most_common()),
        collisions=_collect_collisions(nodes),
        degree=degree_distribution,
        isolation=_collect_isolation(nodes, degrees),
        referents=_collect_referents(nodes),
        documents=_collect_documents(nodes),
    )
    logger.info(
        "graph_shape_analyzed",
        nodes=report.nodes,
        edges=report.edges,
        isolated=report.isolation.isolated,
        truncated=truncated,
    )
    return report
