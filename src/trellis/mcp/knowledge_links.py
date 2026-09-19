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

from trellis.extract.referents import (
    ReferentResolution,
    ReferentStatus,
    resolve_referent,
    resolve_referents,
)
from trellis.mutate import Command, CommandStatus, Operation
from trellis.schemas.well_known import APPLIES_TO, schema_alignment_for_edge_kind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.mutate import MutationExecutor
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

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

    Returns:
        Response lines, in the order the links were attempted.
    """
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
        lines.append(f"Warning: edge not created: {result.message}")
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
                f"Warning: domain link to {match.node_id} not created: {result.message}"
            )
    return lines


def _describe(resolution: ReferentResolution) -> str:
    return ", ".join(f"{m.node_id} [{m.node_type}]" for m in resolution.matches)
