"""Resolve a free-text referent to an existing graph node — never mint.

A caller that wants to point at something already in the graph — the
``relates_to`` of a ``save_knowledge`` note, the ``domain`` it names —
usually knows a *name*, not a node id. Trace extraction
(:mod:`trellis.extract.trace`) mints the ids those names would need, by a
rule the caller cannot see: ``domain:``, ``team:``, ``agent:`` and ``tool:``
take :func:`~trellis.extract.trace.normalize_slug` of the name, and
``artifact:`` takes the artifact id verbatim. This module applies the same
rule in reverse and asks the store which of the candidates exist.

Why the minting rule, not the name key
--------------------------------------

:func:`~trellis.schemas.well_known.normalize_entity_name` decides whether
two *display names* denote one entity, and it deliberately leaves
separators alone — ``"alice-b"`` and ``"Alice B"`` stay distinct. That is
right for identity and wrong for rebuilding an id: a slug joins every run
of non-word characters with ``-``, so ``mcp__trellis__search`` mints
``tool:mcp-trellis-search`` and no name key reaches it. Measured on the
reference deployment (2026-09-18, the 1,219 current nodes in these five
namespaces), probing ``<namespace>:<name key>`` finds 506 of them from
their own display name; the minting rule finds 1,129; the minting rule
plus the one legacy spelling below finds all 1,219.

That legacy spelling is ``tool:<name verbatim>``: 90 tool nodes predate the
slug rule, 88 of them with no slug-form sibling. The forms of a namespace
are tried in preference order and the first one present wins, so the two
tools that exist in both spellings resolve to the slug form trace
extraction writes to today, rather than to an ambiguity between a tool and
itself.

Matching rule and its failure mode
----------------------------------

* **An exact id wins outright.** A value that is itself a current node id
  is that node, whatever else it would derive to.
* **Otherwise one read confirms every derived candidate**: the namespace
  forms plus the ``name`` alias binding
  (:data:`~trellis.extract.entity_resolution.NAME_ALIAS_SOURCE_SYSTEM`),
  which counts only while the bound node's current name still normalizes
  to the key — the liveness rule
  :func:`~trellis.extract.entity_resolution.build_name_alias_resolver`
  applies.
* **One distinct node is a resolution; two or more is ambiguous, and an
  ambiguous referent resolves to nothing.** ``trellis`` naming both
  ``domain:trellis`` and a note called "Trellis" is two different things,
  and a wrong edge is worse than a missing one. No id tail occurs in two
  of these namespaces on the reference deployment, so this is a guard
  rather than a measured rate.
* **Nothing is ever minted.** A value that matches nothing is reported as
  missing. Creating the node it names is the caller's decision, through the
  governed pipeline, not a side effect of looking it up.

Store failures propagate: a lookup that could not run is not a miss, and
the caller decides what an outage means for its write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.extract.trace import normalize_slug
from trellis.schemas.well_known import normalize_entity_name

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from trellis.stores.base.graph import GraphStore

#: ``ReferentMatch.via`` for a value that was itself a node id.
VIA_ID: Final = "id"

#: ``ReferentMatch.via`` for a match found through the ``name`` alias index.
VIA_NAME_ALIAS: Final = "name-alias"


def _verbatim(value: str) -> str:
    return value.strip()


@dataclass(frozen=True)
class IdForm:
    """One way a namespace derives an id tail from a name."""

    namespace: str
    label: str
    tail: Callable[[str], str]

    @property
    def via(self) -> str:
        """The ``ReferentMatch.via`` label for a match found by this form."""
        return f"{self.namespace}:{self.label}"

    def candidate(self, name: str) -> str | None:
        """The id this form derives from *name*, or ``None`` for an empty tail."""
        tail = self.tail(name)
        return f"{self.namespace}:{tail}" if tail else None


#: Per namespace, the forms trace extraction has minted ids with, in
#: preference order: the current rule first, legacy spellings after it.
ID_FORMS: Final[Mapping[str, tuple[IdForm, ...]]] = {
    "domain": (IdForm("domain", "slug", normalize_slug),),
    "team": (IdForm("team", "slug", normalize_slug),),
    "agent": (IdForm("agent", "slug", normalize_slug),),
    "tool": (
        IdForm("tool", "slug", normalize_slug),
        IdForm("tool", "verbatim", _verbatim),
    ),
    "artifact": (IdForm("artifact", "verbatim", _verbatim),),
}

#: Every namespace with a name-derived id. ``trace:`` and ``evidence:`` are
#: absent on purpose: their tails are opaque ids, never names, so a caller
#: holding one passes it as an exact id.
DEFAULT_REFERENT_NAMESPACES: Final[tuple[str, ...]] = tuple(ID_FORMS)


class ReferentStatus(StrEnum):
    """How a value resolved."""

    EXACT = "exact"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


@dataclass(frozen=True)
class ReferentMatch:
    """A current node a value resolved to, and the route that found it."""

    node_id: str
    node_type: str
    via: str


@dataclass(frozen=True)
class ReferentResolution:
    """The outcome of resolving one value."""

    value: str
    status: ReferentStatus
    matches: tuple[ReferentMatch, ...] = ()

    @property
    def match(self) -> ReferentMatch | None:
        """The one node this value denotes, or ``None`` unless it resolved."""
        if self.status in (ReferentStatus.EXACT, ReferentStatus.RESOLVED):
            return self.matches[0]
        return None


def namespace_candidates(
    value: str,
    namespaces: Sequence[str] = DEFAULT_REFERENT_NAMESPACES,
) -> tuple[tuple[IdForm, str], ...]:
    """Every id the minting rules would derive from *value*, with its form.

    Pure — no store access. Ordered by namespace, then by each namespace's
    form preference. A value already carrying a known namespace prefix
    (``tool:mcp__trellis__search``) is derived within that namespace only,
    and only if it is one of *namespaces*.
    """
    text = value.strip()
    prefix, sep, tail = text.partition(":")
    scoped: Sequence[str] = namespaces
    name = text
    if sep and prefix.strip().casefold() in ID_FORMS:
        namespace = prefix.strip().casefold()
        scoped = (namespace,) if namespace in namespaces else ()
        name = tail
    derived: list[tuple[IdForm, str]] = []
    for namespace in scoped:
        for form in ID_FORMS[namespace]:
            candidate = form.candidate(name)
            if candidate is not None:
                derived.append((form, candidate))
    return tuple(derived)


@dataclass(frozen=True)
class _Plan:
    """The candidates one value is resolved against."""

    value: str
    exact: str | None
    derived: tuple[tuple[IdForm, str], ...]
    alias_key: str | None

    def candidate_ids(self) -> list[str]:
        ids = [candidate for _, candidate in self.derived]
        return [self.exact, *ids] if self.exact is not None else ids


def _plan(
    value: str,
    *,
    namespaces: Sequence[str],
    allow_exact: bool,
    use_name_alias: bool,
) -> _Plan:
    text = value.strip()
    if not text:
        return _Plan(value=value, exact=None, derived=(), alias_key=None)
    alias_key = normalize_entity_name(text) if use_name_alias else ""
    return _Plan(
        value=value,
        exact=text if allow_exact else None,
        derived=namespace_candidates(text, namespaces),
        alias_key=alias_key or None,
    )


def _as_match(node: Mapping[str, Any], via: str) -> ReferentMatch:
    return ReferentMatch(
        node_id=str(node["node_id"]),
        node_type=str(node.get("node_type") or ""),
        via=via,
    )


def _still_named(node: Mapping[str, Any], key: str) -> bool:
    name = (node.get("properties") or {}).get("name")
    return isinstance(name, str) and normalize_entity_name(name) == key


def _classify(
    plan: _Plan,
    *,
    found: Mapping[str, Mapping[str, Any]],
    bound: Mapping[str, str],
) -> ReferentResolution:
    if plan.exact is not None and plan.exact in found:
        return ReferentResolution(
            value=plan.value,
            status=ReferentStatus.EXACT,
            matches=(_as_match(found[plan.exact], VIA_ID),),
        )

    matches: dict[str, ReferentMatch] = {}
    matched_namespaces: set[str] = set()
    for form, candidate in plan.derived:
        if form.namespace in matched_namespaces or candidate not in found:
            continue
        matched_namespaces.add(form.namespace)
        matches.setdefault(candidate, _as_match(found[candidate], form.via))

    if plan.alias_key is not None:
        entity_id = bound.get(plan.alias_key)
        node = found.get(entity_id) if entity_id is not None else None
        if node is not None and _still_named(node, plan.alias_key):
            matches.setdefault(str(node["node_id"]), _as_match(node, VIA_NAME_ALIAS))

    if not matches:
        status = ReferentStatus.MISSING
    elif len(matches) == 1:
        status = ReferentStatus.RESOLVED
    else:
        status = ReferentStatus.AMBIGUOUS
    return ReferentResolution(
        value=plan.value, status=status, matches=tuple(matches.values())
    )


def resolve_referents(
    graph_store: GraphStore,
    values: Sequence[str],
    *,
    namespaces: Sequence[str] = DEFAULT_REFERENT_NAMESPACES,
    allow_exact: bool = True,
    use_name_alias: bool = True,
    exclude_ids: Collection[str] = (),
) -> list[ReferentResolution]:
    """Resolve each of *values* to the current node it names, if exactly one.

    Read-only: one ``resolve_alias`` per distinct name key (skipped when
    *use_name_alias* is false), then a single ``get_nodes_bulk`` over every
    candidate of every value.

    Args:
        graph_store: The knowledge-plane graph store.
        values: Ids or names, resolved independently, in order.
        namespaces: Which of :data:`ID_FORMS` to derive candidates in.
        allow_exact: Whether a value may match as a node id in its own
            right. Off when only the namespaces should answer — a
            ``domain`` property must name a ``domain:`` node, not whatever
            node happens to have that id.
        use_name_alias: Whether to consult the ``name`` alias index.
        exclude_ids: Node ids that never match — the caller's own node, so
            a note can never resolve a referent to itself.

    Returns:
        One :class:`ReferentResolution` per value, in input order.

    Raises:
        ValueError: *namespaces* names a namespace with no id forms.
        Exception: Whatever the store raises. A lookup that could not run
            is not reported as a miss.
    """
    unknown = [namespace for namespace in namespaces if namespace not in ID_FORMS]
    if unknown:
        msg = f"no id forms for namespace(s) {unknown!r}; known: {list(ID_FORMS)!r}"
        raise ValueError(msg)

    plans = [
        _plan(
            value,
            namespaces=namespaces,
            allow_exact=allow_exact,
            use_name_alias=use_name_alias,
        )
        for value in values
    ]

    bound: dict[str, str] = {}
    for key in dict.fromkeys(plan.alias_key for plan in plans if plan.alias_key):
        row = graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
        entity_id = row.get("entity_id") if row else None
        if isinstance(entity_id, str) and entity_id:
            bound[key] = entity_id

    excluded = set(exclude_ids)
    wanted = [
        node_id
        for node_id in dict.fromkeys(
            [*(i for plan in plans for i in plan.candidate_ids()), *bound.values()]
        )
        if node_id not in excluded
    ]
    found = (
        {str(node["node_id"]): node for node in graph_store.get_nodes_bulk(wanted)}
        if wanted
        else {}
    )
    return [_classify(plan, found=found, bound=bound) for plan in plans]


def resolve_referent(
    graph_store: GraphStore,
    value: str,
    *,
    namespaces: Sequence[str] = DEFAULT_REFERENT_NAMESPACES,
    allow_exact: bool = True,
    use_name_alias: bool = True,
    exclude_ids: Collection[str] = (),
) -> ReferentResolution:
    """Resolve one value. See :func:`resolve_referents`."""
    return resolve_referents(
        graph_store,
        [value],
        namespaces=namespaces,
        allow_exact=allow_exact,
        use_name_alias=use_name_alias,
        exclude_ids=exclude_ids,
    )[0]
