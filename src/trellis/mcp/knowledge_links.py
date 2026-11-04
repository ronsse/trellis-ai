"""Link a ``save_knowledge`` note to the graph it is about.

Until now ``save_knowledge`` linked a note only when ``relates_to`` was an
exact node id, and it never read the note's own properties. Measured on the
reference deployment (2026-09-18): 178 ``entity.create`` writes from the
tool against 5 ``link.create``, and 165 knowledge notes with no edge at all.
All five links used exact ids; no caller ever passed a name. Meanwhile 58
notes carry a ``domain`` property naming what they are about, and 50 of
those name a ``domain:`` node that trace extraction had already minted,
under exactly the id its minting rule derives. The note knew what it was
about; the graph did not.

Two links, both through the governed pipeline, and neither mints a node:

* ``relates_to`` takes an id *or* a name, resolved by
  :func:`~trellis.extract.referents.resolve_referent`. An exact id behaves
  as it always did. A name that resolves to one node links to it, and the
  response names the node chosen. A name that resolves to several links to
  none of them and lists them all.
* ``properties["domain"]`` (a string or a list of strings) links the note
  ``appliesTo`` each ``domain:`` node it names. That is the edge trace
  extraction already writes from an Activity to its domain, so a domain
  node reaches the notes about it by the route it already reaches the work
  done in it. A domain with no node is reported and left alone. This
  module never creates one.

Hub growth was measured before deciding to link. Doing it for all 50
existing notes would add at most 11 edges to any one domain node, and the
largest would go from 24 edges to 27.

The note is already written by the time this runs, so nothing here
raises. Every failure — an unresolvable name, a store outage, a rejected
link — becomes a line in the tool's response. A caller that needs strict
link semantics calls ``execute_mutation`` with ``LINK_CREATE`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from trellis.core.error_sanitize import sanitize_error_message
from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.extract.referents import (
    ReferentMatch,
    ReferentResolution,
    ReferentStatus,
    resolve_referent,
    resolve_referents,
)
from trellis.extract.trace import normalize_slug
from trellis.mutate import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.retention import CURRENT_STATE
from trellis.schemas.classification import lifecycle_state_of
from trellis.schemas.well_known import (
    APPLIES_TO,
    CONCEPT,
    canonicalize_entity_type,
    normalize_entity_name,
    schema_alignment_for_edge_kind,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.mutate import MutationExecutor
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: A FAILED/REJECTED ``CommandResult.message`` can carry driver or caller
#: text; SUCCESS and DUPLICATE only restate the caller's own request, so
#: they pass through unsanitized, matching ``_results.py``'s
#: ``_SANITIZED_STATUSES`` on the REST boundary (#836).
_SANITIZED_STATUSES = frozenset({CommandStatus.FAILED, CommandStatus.REJECTED})


def _describe_unsuccessful(result: CommandResult) -> str:
    """Sanitize ``result.message`` for a non-SUCCESS link write, gated by
    status (#836's convention) rather than wrapped unconditionally."""
    if result.status in _SANITIZED_STATUSES:
        return sanitize_error_message(result.message)
    return result.message


#: ``requested_by`` on every link this module creates.
SAVE_KNOWLEDGE_REQUESTER: Final = "mcp:save_knowledge"

#: The note property naming the domain(s) the note is about.
DOMAIN_PROPERTY: Final = "domain"

#: Edge property recording that a link was derived from a note property
#: rather than asked for, so a derived link can be told apart and reverted.
DERIVED_FROM_KEY: Final = "derived_from"


def domain_values(properties: Mapping[str, Any]) -> list[str]:
    """The distinct non-blank strings under ``properties["domain"]``.

    A string is one domain and a list is several. Any other shape names no
    domain rather than being coerced into one.
    """
    raw = properties.get(DOMAIN_PROPERTY)
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list | tuple):
        items = [item for item in raw if isinstance(item, str)]
    else:
        return []
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))


def link_knowledge_node(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    properties: Mapping[str, Any],
    relates_to: str | None,
    edge_kind: str,
    requested_by: str = SAVE_KNOWLEDGE_REQUESTER,
    link_or_mint: bool = False,
) -> list[str]:
    """Create the links a freshly written note asks for.

    Args:
        executor: The governed executor that created the note.
        graph_store: The knowledge-plane graph store, read to resolve
            targets.
        node_id: The note's node id. It is never a link target.
        properties: The note's properties, read for ``domain``.
        relates_to: An id or a name to link to, or ``None``.
        edge_kind: The edge kind for the ``relates_to`` link.
        requested_by: ``requested_by`` on each ``LINK_CREATE``.
        link_or_mint: The ``knowledge_link_or_mint`` write-behaviour knob.
            ``False`` (the default) is the behaviour above, unchanged. ``True``
            mints a missing ``domain:<slug>`` node, resolves ``relates_to``
            through external-system aliases, and reports an unresolvable
            value by count and position instead of echoing it.

    Returns:
        Response lines, in the order the links were attempted.
    """
    if link_or_mint:
        return _link_or_mint(
            executor,
            graph_store,
            node_id=node_id,
            properties=properties,
            relates_to=relates_to,
            edge_kind=edge_kind,
            requested_by=requested_by,
        )
    lines: list[str] = []
    linked: set[tuple[str, str]] = set()
    if relates_to:
        lines.extend(
            _link_relates_to(
                executor,
                graph_store,
                node_id=node_id,
                relates_to=relates_to,
                edge_kind=edge_kind,
                requested_by=requested_by,
                linked=linked,
            )
        )
    lines.extend(
        _link_domains(
            executor,
            graph_store,
            node_id=node_id,
            values=domain_values(properties),
            requested_by=requested_by,
            linked=linked,
        )
    )
    return lines


def _link_relates_to(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    relates_to: str,
    edge_kind: str,
    requested_by: str,
    linked: set[tuple[str, str]],
) -> list[str]:
    try:
        resolution = resolve_referent(graph_store, relates_to, exclude_ids=(node_id,))
    except Exception:
        logger.exception(
            "save_knowledge_relates_to_lookup_failed", relates_to=relates_to
        )
        return [
            (
                f"Warning: could not resolve relates_to {relates_to!r} (store error)"
                " — edge not created"
            )
        ]

    match = resolution.match
    if resolution.status is ReferentStatus.AMBIGUOUS:
        return [
            (
                f"Warning: relates_to {relates_to!r} names "
                f"{len(resolution.matches)} nodes ({_describe(resolution)})"
                " — edge not created; pass one id"
            )
        ]
    if match is None:
        return [f"Warning: target entity not found: {relates_to} — edge not created"]

    lines: list[str] = []
    if resolution.status is ReferentStatus.RESOLVED:
        lines.append(
            f"Resolved relates_to {relates_to!r} -> {match.node_id} "
            f"({match.node_type}, via {match.via})"
        )
    result = executor.execute(
        Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": node_id,
                "target_id": match.node_id,
                "edge_kind": edge_kind,
            },
            requested_by=requested_by,
        )
    )
    if result.status == CommandStatus.SUCCESS:
        linked.add((match.node_id, edge_kind))
        lines.append(
            f"Edge created: {result.created_id} --[{edge_kind}]--> {match.node_id}"
        )
    else:
        lines.append(f"Warning: edge not created: {_describe_unsuccessful(result)}")
    return lines


def _link_domains(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    values: list[str],
    requested_by: str,
    linked: set[tuple[str, str]],
) -> list[str]:
    if not values:
        return []
    try:
        resolutions = resolve_referents(
            graph_store,
            values,
            namespaces=("domain",),
            allow_exact=False,
            use_name_alias=False,
        )
    except Exception:
        logger.exception("save_knowledge_domain_lookup_failed", domains=values)
        return [
            "Warning: could not look up domain nodes (store error) — domain not linked"
        ]

    edge_properties: dict[str, Any] = {
        DERIVED_FROM_KEY: f"properties.{DOMAIN_PROPERTY}"
    }
    alignment = schema_alignment_for_edge_kind(APPLIES_TO)
    if alignment is not None:
        edge_properties["schema_alignment"] = alignment

    lines: list[str] = []
    for resolution in resolutions:
        match = resolution.match
        if match is None:
            lines.append(
                f"Note: domain {resolution.value!r} has no domain node — not linked"
            )
            continue
        # A domain node naming itself is not a missing domain, so it is
        # skipped here rather than excluded from the lookup.
        if match.node_id == node_id or (match.node_id, APPLIES_TO) in linked:
            continue
        result = executor.execute(
            Command(
                operation=Operation.LINK_CREATE,
                args={
                    "source_id": node_id,
                    "target_id": match.node_id,
                    "edge_kind": APPLIES_TO,
                    "properties": dict(edge_properties),
                },
                requested_by=requested_by,
            )
        )
        if result.status == CommandStatus.SUCCESS:
            linked.add((match.node_id, APPLIES_TO))
            lines.append(
                f"Domain link: {result.created_id} --[{APPLIES_TO}]--> {match.node_id}"
            )
        else:
            lines.append(
                "Warning: domain link to "
                f"{match.node_id} not created: {_describe_unsuccessful(result)}"
            )
    return lines


def _describe(resolution: ReferentResolution) -> str:
    return ", ".join(f"{m.node_id} [{m.node_type}]" for m in resolution.matches)


# ---------------------------------------------------------------------------
# Link-or-mint (``knowledge_link_or_mint`` knob).  Everything below runs only
# with the knob on; the functions above are the knob-off behaviour and are not
# reached from here, so they stay byte-for-byte what they were.
# ---------------------------------------------------------------------------

#: ``source_system`` values ``relates_to`` is probed under when a bare value
#: is not a node id or a name.  A value may also name its system itself
#: (``"unity_catalog:main.sales.orders"``), which reaches any system.
EXTERNAL_ALIAS_SYSTEMS: Final = (
    "dbt",
    "openlineage_dataset",
    "openlineage_job",
    "unity_catalog",
)

#: Property stamped on a ``domain:<slug>`` node this module minted, so a
#: minted node can be told apart from one trace extraction wrote.
MINTED_BY_KEY: Final = "minted_by"


def find_attach_target(
    graph_store: GraphStore, *, name: str, entity_type: str
) -> dict[str, Any] | None:
    """The existing note a new ``save_knowledge`` call should attach to.

    The ``name`` alias (``normalize_entity_name``) must resolve to a node that
    is of the same type, semantic, current, still named that, and not a
    namespaced node (``domain:``, ``tool:``, ``trace:`` ... ids): a note
    called ``finance`` is not the ``domain:finance`` node. ``None`` means
    create as usual.
    """
    key = normalize_entity_name(name)
    if not key:
        return None
    row = graph_store.resolve_alias(NAME_ALIAS_SOURCE_SYSTEM, key)
    if row is None:
        return None
    node = graph_store.get_node(str(row["entity_id"]))
    if node is None or ":" in str(node["node_id"]):
        return None
    props = node.get("properties") or {}
    if (
        canonicalize_entity_type(str(node.get("node_type", "")))
        != canonicalize_entity_type(entity_type)
        or node.get("node_role", "semantic") != "semantic"
        or lifecycle_state_of(props) != CURRENT_STATE
        or normalize_entity_name(str(props.get("name", ""))) != key
    ):
        return None
    return node


def attach_to_note(
    executor: MutationExecutor,
    node: Mapping[str, Any],
    *,
    properties: Mapping[str, Any],
    document_ids: list[str] | None,
    requested_by: str = SAVE_KNOWLEDGE_REQUESTER,
) -> list[str]:
    """Attach a new save to the note ``node`` instead of creating a duplicate.

    Writes at most one ``ENTITY_UPDATE`` and nothing else: no node, no alias.
    The update carries only the properties the node does not already have
    (an existing value is never overwritten) and the node's ``document_ids``
    plus any new evidence document, deduplicated. When there is nothing to
    add, nothing is written. Returns warning lines (empty on success).
    """
    existing = node.get("properties") or {}
    new_props = {k: v for k, v in properties.items() if k not in existing}
    old_docs = [str(d) for d in node.get("document_ids") or []]
    merged_docs = list(dict.fromkeys([*old_docs, *(document_ids or [])]))
    args: dict[str, Any] = {"entity_id": str(node["node_id"])}
    if new_props:
        args["properties"] = new_props
    if merged_docs != old_docs:
        args["document_ids"] = merged_docs
    if len(args) == 1:
        return []
    result = executor.execute(
        Command(
            operation=Operation.ENTITY_UPDATE,
            args=args,
            target_id=str(node["node_id"]),
            target_type=str(node.get("node_type", "")),
            requested_by=requested_by,
        )
    )
    if result.status == CommandStatus.SUCCESS:
        return []
    return [f"Warning: attach update not applied: {_describe_unsuccessful(result)}"]


def _link_or_mint(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    properties: Mapping[str, Any],
    relates_to: str | None,
    edge_kind: str,
    requested_by: str,
) -> list[str]:
    lines: list[str] = []
    linked: set[tuple[str, str]] = set()
    resolved: list[bool] = []
    if relates_to:
        rel_lines, rel_ok = _lom_relates_to(
            executor,
            graph_store,
            node_id=node_id,
            relates_to=relates_to,
            edge_kind=edge_kind,
            requested_by=requested_by,
            linked=linked,
        )
        lines.extend(rel_lines)
        resolved.append(rel_ok)
    values = domain_values(properties)
    if values:
        dom_lines, dom_ok = _lom_domains(
            executor,
            graph_store,
            node_id=node_id,
            values=values,
            requested_by=requested_by,
            linked=linked,
        )
        lines.extend(dom_lines)
        resolved.extend(dom_ok)
    missing = [i for i, ok in enumerate(resolved, start=1) if not ok]
    if missing:
        lines.append(
            f"Unresolved: {len(missing)} of {len(resolved)} references "
            f"(positions {', '.join(map(str, missing))}) \u2014 not linked"
        )
    return lines


def _resolve_external_alias(
    graph_store: GraphStore, value: str, *, exclude_id: str
) -> ReferentResolution:
    """Resolve ``value`` as a raw id under an external ``source_system``."""
    raw = value.strip()
    probes: list[tuple[str, str]] = []
    system, sep, tail = raw.partition(":")
    if sep and system.strip() and tail.strip() and system != NAME_ALIAS_SOURCE_SYSTEM:
        probes.append((system.strip(), tail.strip()))
    probes.extend((s, raw) for s in EXTERNAL_ALIAS_SYSTEMS)
    found: dict[str, str] = {}
    for source_system, raw_id in probes:
        row = graph_store.resolve_alias(source_system, raw_id)
        if row is None:
            continue
        entity_id = str(row["entity_id"])
        if entity_id != exclude_id:
            found.setdefault(entity_id, f"alias:{source_system}")
    nodes = (
        {str(n["node_id"]): n for n in graph_store.get_nodes_bulk(list(found))}
        if found
        else {}
    )
    matches = tuple(
        ReferentMatch(entity_id, str(nodes[entity_id].get("node_type", "")), via)
        for entity_id, via in found.items()
        if entity_id in nodes
        and lifecycle_state_of(nodes[entity_id].get("properties") or {})
        == CURRENT_STATE
    )
    if not matches:
        return ReferentResolution(value, ReferentStatus.MISSING)
    status = ReferentStatus.RESOLVED if len(matches) == 1 else ReferentStatus.AMBIGUOUS
    return ReferentResolution(value, status, matches)


def _lom_relates_to(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    relates_to: str,
    edge_kind: str,
    requested_by: str,
    linked: set[tuple[str, str]],
) -> tuple[list[str], bool]:
    try:
        resolution = resolve_referent(graph_store, relates_to, exclude_ids=(node_id,))
        if resolution.status is ReferentStatus.MISSING:
            resolution = _resolve_external_alias(
                graph_store, relates_to, exclude_id=node_id
            )
    except Exception:
        logger.exception("save_knowledge_relates_to_lookup_failed")
        return ["Warning: could not resolve relates_to (store error)"], False

    match = resolution.match
    if resolution.status is ReferentStatus.AMBIGUOUS:
        return [
            (
                f"Warning: relates_to (position 1) names {len(resolution.matches)} "
                f"nodes ({_describe(resolution)}) \u2014 edge not created; pass one id"
            )
        ], False
    if match is None:
        return [], False

    lines: list[str] = []
    if resolution.status is ReferentStatus.RESOLVED:
        lines.append(
            f"Resolved relates_to {relates_to!r} -> {match.node_id} "
            f"({match.node_type}, via {match.via})"
        )
    result = executor.execute(
        Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": node_id,
                "target_id": match.node_id,
                "edge_kind": edge_kind,
            },
            requested_by=requested_by,
        )
    )
    if result.status == CommandStatus.SUCCESS:
        linked.add((match.node_id, edge_kind))
        lines.append(
            f"Edge created: {result.created_id} --[{edge_kind}]--> {match.node_id}"
        )
        return lines, True
    lines.append(f"Warning: edge not created: {_describe_unsuccessful(result)}")
    return lines, False


def _mint_domain(
    executor: MutationExecutor,
    graph_store: GraphStore,
    value: str,
    *,
    requested_by: str,
) -> tuple[str | None, list[str]]:
    """Mint ``domain:<slug>`` through the executor; ``(node id | None, lines)``.

    The id is the one trace extraction mints (``normalize_slug``), the type
    is the one it uses (``Concept``) and the name is the caller's own string,
    so a later trace for the same domain updates this node rather than
    forking it.  An id that already exists is never minted over: the
    resolver passed it by, so it is not a current domain, and recreating it
    would resurrect it.
    """
    slug = normalize_slug(value)
    if not slug:
        return None, []
    domain_id = f"domain:{slug}"
    if graph_store.get_node(domain_id) is not None:
        return None, []
    result = executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_id": domain_id,
                "entity_type": CONCEPT,
                "name": value,
                "properties": {MINTED_BY_KEY: requested_by},
            },
            target_type=CONCEPT,
            requested_by=requested_by,
        )
    )
    if result.status == CommandStatus.SUCCESS:
        return domain_id, [f"Domain minted: {domain_id}"]
    return None, [f"Warning: domain node not minted: {_describe_unsuccessful(result)}"]


def _lom_domains(
    executor: MutationExecutor,
    graph_store: GraphStore,
    *,
    node_id: str,
    values: list[str],
    requested_by: str,
    linked: set[tuple[str, str]],
) -> tuple[list[str], list[bool]]:
    try:
        resolutions = resolve_referents(
            graph_store,
            values,
            namespaces=("domain",),
            allow_exact=False,
            use_name_alias=False,
        )
    except Exception:
        logger.exception("save_knowledge_domain_lookup_failed")
        return [
            (
                "Warning: could not look up domain nodes (store error)"
                " \u2014 domain not linked"
            )
        ], [False] * len(values)

    edge_properties: dict[str, Any] = {
        DERIVED_FROM_KEY: f"properties.{DOMAIN_PROPERTY}"
    }
    alignment = schema_alignment_for_edge_kind(APPLIES_TO)
    if alignment is not None:
        edge_properties["schema_alignment"] = alignment

    lines: list[str] = []
    ok: list[bool] = []
    minted: dict[str, str] = {}
    for resolution in resolutions:
        match = resolution.match
        target = match.node_id if match is not None else None
        if target is None:
            slug = normalize_slug(resolution.value)
            target = minted.get(slug)
            if target is None:
                target, mint_lines = _mint_domain(
                    executor, graph_store, resolution.value, requested_by=requested_by
                )
                lines.extend(mint_lines)
                if target is not None:
                    minted[slug] = target
        if target is None:
            ok.append(False)
            continue
        ok.append(True)
        if target == node_id or (target, APPLIES_TO) in linked:
            continue
        result = executor.execute(
            Command(
                operation=Operation.LINK_CREATE,
                args={
                    "source_id": node_id,
                    "target_id": target,
                    "edge_kind": APPLIES_TO,
                    "properties": dict(edge_properties),
                },
                requested_by=requested_by,
            )
        )
        if result.status == CommandStatus.SUCCESS:
            linked.add((target, APPLIES_TO))
            lines.append(
                f"Domain link: {result.created_id} --[{APPLIES_TO}]--> {target}"
            )
        else:
            ok[-1] = False
            lines.append(
                f"Warning: domain link to {target} not created: "
                f"{_describe_unsuccessful(result)}"
            )
    return lines, ok
