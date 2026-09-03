"""Response formatters for token-efficient output."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.core.hashing import estimate_tokens as _estimate_tokens
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.withholding import WithholdingSummary, format_withholding_note
from trellis.schemas.advisory import Advisory

logger = structlog.get_logger(__name__)

#: Marker for response-level trimming. Plain ASCII, unlike the excerpt
#: ellipsis — these strings are rendered markdown, not pack item previews.
_TRIM_MARKER = "..."

#: Character budget for the label half of an index line. Long enough for a
#: title or a first clause of the excerpt, short enough that an index pack
#: stays one order of magnitude cheaper than the full pack it stands for.
_INDEX_LABEL_MAX_CHARS = 80

#: Maximum evidence-document pointers rendered per graph entity. Doc-link
#: provenance is unbounded in storage (#301 stamps every mint); the rendered
#: pointer list is not — the agent batch-fetches the rest by id if needed.
_MAX_EVIDENCE_POINTERS = 10

#: Maximum doc pointers appended inline to a single neighbor line in
#: ``format_subgraph_as_markdown`` — neighbors are a survey, not the subject.
_MAX_NEIGHBOR_POINTERS = 3

#: Headroom an index rendering keeps back for the ``pack_id`` line and the
#: follow-up footer, on top of the heading. See
#: :func:`index_render_overhead_tokens`.
_INDEX_RENDER_RESERVE = 10


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget.

    Deliberately *not*
    :func:`~trellis.retrieve.excerpts.truncate_excerpt`: this is a
    last-resort budget enforcer over a whole rendered response, where
    retaining the maximum number of characters matters more than a
    readable break, whereas an excerpt is a preview where the opposite is
    true. The marker is charged against the budget rather than appended on
    top of it, so a function documented as trimming *to fit* no longer
    overshoots the limit it enforces.
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(_TRIM_MARKER)] + _TRIM_MARKER


def format_pack_as_markdown(
    items: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 2000,
    *,
    pack_id: str | None = None,
    withholding: WithholdingSummary | None = None,
) -> str:
    """Format pack items as concise markdown for LLM consumption.

    ``withholding`` (#404) is rendered as one line **in the header**, above
    the item blocks. Never appended after them: the item loop below
    ``break``\\ s the moment the token budget runs out, so a note appended
    after it would print only for packs that happen to fit — "honest in
    JSON alone", which is the failure this reports on.

    The note is **not** charged against the item budget. Charging it looks
    tidier and is wrong: the builder sized its walk before any of these
    rejections were summarized, so every token the note takes from the item
    budget drops an item the ``PACK_ASSEMBLED`` payload already records as
    *served* — suppressed by session dedup for the rest of the session and
    graded by the learning join, while the agent never saw its id. #305
    made that a pinned invariant on the index path
    (``test_every_id_charged_as_served_is_an_id_the_agent_is_shown``), and
    it was this function that broke it when the note was charged. One line
    of response-level overhead is the caller's to reserve — the same
    doctrine :func:`index_render_overhead_tokens` states — and the
    alternative is a report about withheld items that withholds one.

    Args:
        items: List of pack item dicts with item_id, item_type, excerpt,
            relevance_score, metadata.
        intent: The original query intent.
        max_tokens: Maximum token budget for the response.
        pack_id: Optional pack identifier to surface for citation.
        withholding: What the builder removed and did not serve. Counts and
            reasons only — see :mod:`trellis.retrieve.withholding`.

    Returns:
        Markdown-formatted string within token budget.
    """
    lines = [f"# Context for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    note = format_withholding_note(withholding)
    if note:
        lines.append(note)
    lines.append("")
    token_budget = max_tokens - _estimate_tokens(lines[0]) - 10  # reserve overhead
    used = 0
    included = 0

    for item in items:
        item_type = item.get("item_type", "item")
        excerpt = item.get("excerpt", "")
        item_id = item.get("item_id", "")

        # Build item block - full item_id in backticks so it's copy-pastable.
        # No relevance number is shown: after RRF fusion the score is a
        # reciprocal-rank sum (typically ~0.01 to 0.02), not a calibrated 0-1
        # relevance. Rendering it as "relevance: 0.02" reads as low-confidence
        # on every item and leads agents to discount the whole pack. Item
        # order already conveys ranking; exact scores and per-strategy
        # breakdowns live in the PACK_ASSEMBLED telemetry for offline analysis.
        header = f"## [{item_type}] `{item_id}`"

        block = f"{header}\n{excerpt}\n"
        block_tokens = _estimate_tokens(block)

        if used + block_tokens > token_budget:
            remaining = len(items) - included
            if remaining > 0:
                lines.append(
                    f"\n*[{remaining} more items omitted — use CLI for full results]*"
                )
            break

        lines.append(block)
        used += block_tokens
        included += 1

    if included == 0 and items:
        # At least include a truncated first item
        first = items[0]
        excerpt = _truncate_to_tokens(first.get("excerpt", ""), token_budget - 50)
        lines.append(
            f"## [{first.get('item_type', 'item')}] `{first.get('item_id', '')}`"
        )
        lines.append(excerpt)
        remaining = len(items) - 1
        if remaining > 0:
            lines.append(f"\n*[{remaining} more items omitted]*")

    if pack_id:
        lines.append(
            "\n---\n"
            '*Cite feedback via `record_feedback(pack_id="' + pack_id + '", '
            "success=..., helpful_item_ids=[...], unhelpful_item_ids=[...])`.*"
        )

    return "\n".join(lines)


def item_label(item: dict[str, Any], limit: int = _INDEX_LABEL_MAX_CHARS) -> str:
    """One-line human label for a pack item, at most ``limit`` characters.

    The item's title when its metadata carries one (``title`` /
    ``capture_title`` / ``name`` — the same derivation
    :func:`trellis.retrieve.pack_builder._item_attribution` uses), else the
    first clause of its excerpt. Newlines are flattened first: every caller
    renders this inside a single line.

    Shared by :func:`format_index_line` and
    :func:`trellis.retrieve.disclosure.apply_disclosure` so an item stands
    for itself identically whether it is surveyed in an index pack or
    demoted to a pointer in the tail of a full one. Without the shared
    helper the two renderings would drift, and an agent comparing a
    pointer against the index line for the same id would see two different
    descriptions of one memory.
    """
    meta = item.get("metadata") or {}
    title = meta.get("title") or meta.get("capture_title") or meta.get("name")
    if isinstance(title, str) and title.strip():
        return truncate_excerpt(" ".join(title.split()), limit)
    flattened = " ".join((item.get("excerpt") or "").split())
    return truncate_excerpt(flattened, limit)


def format_index_line(item: dict[str, Any]) -> str:
    """Render one compact index line for a pack item.

    The line carries what an agent needs to *decide whether to fetch*:
    the copy-pastable ``item_id``, the item type, the estimated read cost
    (the item's excerpt token estimate — what serving it in a full pack
    would have charged), and a label. The label is the item's title when
    the metadata carries one (``title`` / ``capture_title`` / ``name`` —
    the same derivation the ``PACK_ASSEMBLED`` attribution uses), else the
    first clause of the excerpt via :func:`truncate_excerpt`. No excerpt
    bodies: this is the index layer of progressive disclosure (#305) —
    the bodies come from a follow-up fetch by id.

    Shared between the markdown renderer below and the ``PackBuilder``
    index-mode budget walk, so the tokens the builder charges are the
    tokens the rendered line actually costs.
    """
    item_id = item.get("item_id", "")
    item_type = item.get("item_type", "item")
    title = item_label(item)

    read_tokens = item.get("estimated_tokens")
    cost = (
        f", ~{read_tokens} tok"
        if isinstance(read_tokens, int) and read_tokens > 0
        else ""
    )
    label = f" {title}" if title else ""
    return f"- `{item_id}` ({item_type}{cost}){label}"


def index_render_overhead_tokens(intent: str) -> int:
    """Tokens :func:`format_pack_as_index_markdown` spends before any line.

    The heading plus a small reserve for the ``pack_id`` line and the
    follow-up footer. A caller that budgets the item lines *upstream* —
    ``PackBuilder(index_mode=True)`` charges each item its exact rendered
    line — must subtract this from the response budget it hands the
    builder, or the two walks disagree and the tail of the pack is
    recorded as served while never being rendered.
    """
    return _estimate_tokens(f"# Context index for: {intent}") + _INDEX_RENDER_RESERVE


def format_pack_as_index_markdown(
    items: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 2000,
    *,
    pack_id: str | None = None,
    withholding: WithholdingSummary | None = None,
) -> str:
    """Format pack items as an id index — one line per item, no bodies.

    The index layer of progressive disclosure (#305): the agent surveys
    cheap lines, follows ``get_graph`` pointers, then batch-fetches the
    ids it actually wants via ``get_items``. ``pack_id`` and the stable
    ``item_id``s keep ``record_feedback`` attribution identical to the
    full-pack rendering.

    ``max_tokens`` is the budget for the whole rendered response, so an
    index-mode ``PackBuilder`` must be given ``max_tokens`` less
    :func:`index_render_overhead_tokens` — see that function.

    Args:
        items: Pack item dicts with ``item_id``, ``item_type``,
            ``excerpt``, and optionally ``metadata`` / ``estimated_tokens``
            (see :func:`format_index_line`).
        intent: The original query intent.
        max_tokens: Maximum token budget for the response.
        pack_id: Optional pack identifier to surface for citation.
        withholding: What the builder removed and did not serve. An index
            pack is all pointers, so the distinction it draws — served but
            demoted vs. withheld entirely — matters here most.

    Returns:
        Markdown-formatted index within token budget.
    """
    lines = [f"# Context index for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    note = format_withholding_note(withholding)
    if note:
        lines.append(note)
    lines.append("")
    token_budget = max_tokens - index_render_overhead_tokens(intent)
    used = 0
    rendered: list[str] = []

    for item in items:
        line = format_index_line(item)
        line_tokens = _estimate_tokens(line)
        if used + line_tokens > token_budget:
            break
        rendered.append(line)
        used += line_tokens

    if not rendered and items:
        # An index with no id is a dead end — the agent has nothing to
        # fetch. One line costs ~15 tokens, so render the first anyway
        # (same posture as format_pack_as_markdown's truncated-first-item
        # fallback).
        rendered.append(format_index_line(items[0]))

    lines.extend(rendered)
    omitted = len(items) - len(rendered)
    if omitted > 0:
        lines.append(f"\n*[{omitted} more items omitted]*")

    if pack_id:
        lines.append(
            "\n---\n"
            "*Fetch bodies via `get_items(item_ids=[...], "
            'pack_id="' + pack_id + '")`. Cite feedback via '
            '`record_feedback(pack_id="' + pack_id + '", success=..., '
            "helpful_item_ids=[...], unhelpful_item_ids=[...])`.*"
        )

    return "\n".join(lines)


def format_fetched_items_as_markdown(
    items: list[dict[str, Any]],
    *,
    not_found: list[str] | None = None,
    max_tokens: int = 4000,
    pack_id: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Render batch-fetched item bodies within a token budget (#305).

    The fetch layer of progressive disclosure: full bodies for the ids an
    agent chose off an index pack or a ``get_graph`` evidence pointer.
    Items that do not fit the budget are *omitted, never truncated* —
    their ids are listed so the agent re-fetches them with a bigger
    budget, and they are reported as omitted rather than served. That
    holds without exception, including when nothing fits at all: a
    half-runbook the agent cannot tell is half is worse than an empty
    response naming the id to re-fetch, and recording a trimmed prefix as
    ``served`` would make the audit event lie. The index line the agent
    chose from carries the item's read cost, so an over-budget fetch is a
    correctable call, not a dead end. ``not_found`` ids are always
    listed: a silently absent id would read as a serving decision.

    Args:
        items: Resolved item dicts with ``item_id``, ``kind`` (document /
            entity / trace), and ``body`` (pre-rendered markdown).
        not_found: Ids that resolved to nothing in any store.
        max_tokens: Maximum token budget for the response.
        pack_id: Originating pack, when the caller supplied one.

    Returns:
        ``(markdown, served_item_ids, omitted_item_ids)`` — the id lists
        feed the ``PACK_ITEMS_FETCHED`` telemetry so the fetch stays
        attributable to the pack.
    """
    lines = ["# Fetched items"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    lines.append("")
    token_budget = max_tokens - _estimate_tokens(lines[0]) - 10  # reserve overhead
    used = 0
    served: list[str] = []
    omitted: list[str] = []

    for item in items:
        item_id = item.get("item_id", "")
        block = f"## [{item.get('kind', 'item')}] `{item_id}`\n{item.get('body', '')}\n"
        block_tokens = _estimate_tokens(block)
        if used + block_tokens > token_budget:
            omitted.append(item_id)
            continue
        lines.append(block)
        used += block_tokens
        served.append(item_id)

    if omitted:
        listed = ", ".join(f"`{item_id}`" for item_id in omitted)
        lines.append(
            f"\n*[{len(omitted)} items over token budget — re-fetch with a "
            f"larger max_tokens: {listed}]*"
        )
    if not_found:
        listed = ", ".join(f"`{item_id}`" for item_id in not_found)
        lines.append(f"\n*[not found: {listed}]*")

    return "\n".join(lines), served, omitted


def format_traces_as_markdown(
    traces: list[dict[str, Any]],
    max_tokens: int = 2000,
) -> str:
    """Format trace summaries as markdown.

    Args:
        traces: List of trace summary dicts.
        max_tokens: Maximum token budget.

    Returns:
        Markdown-formatted string.
    """
    if not traces:
        return "No traces found."

    lines = [f"# Recent Traces ({len(traces)})", ""]
    used = _estimate_tokens(lines[0])
    included = 0

    for t in traces:
        outcome = t.get("outcome", "unknown")
        domain = t.get("domain", "")
        intent = t.get("intent", "")[:120]
        created = t.get("created_at", "")[:10]

        line = f"- **{outcome}** | {domain or 'general'} | {intent} ({created})"
        line_tokens = _estimate_tokens(line)

        if used + line_tokens > max_tokens:
            remaining = len(traces) - included
            lines.append(f"\n*[{remaining} more traces omitted]*")
            break

        lines.append(line)
        used += line_tokens
        included += 1  # noqa: SIM113

    return "\n".join(lines)


def format_entities_as_markdown(
    entities: list[dict[str, Any]],
    max_tokens: int = 2000,
) -> str:
    """Format entities as markdown.

    Args:
        entities: List of entity/node dicts.
        max_tokens: Maximum token budget.

    Returns:
        Markdown-formatted string.
    """
    if not entities:
        return "No entities found."

    lines = [f"# Entities ({len(entities)})", ""]
    used = _estimate_tokens(lines[0])
    included = 0

    for e in entities:
        props = e.get("properties", {})
        name = props.get("name", e.get("node_id", "unknown"))
        node_type = e.get("node_type", "unknown")
        desc = props.get("description", "")[:200]

        line = f"- **{name}** ({node_type})"
        if desc:
            line += f": {desc}"

        line_tokens = _estimate_tokens(line)
        if used + line_tokens > max_tokens:
            remaining = len(entities) - included
            lines.append(f"\n*[{remaining} more entities omitted]*")
            break

        lines.append(line)
        used += line_tokens
        included += 1  # noqa: SIM113

    return "\n".join(lines)


def format_lessons_as_markdown(
    lessons: list[dict[str, Any]],
    max_tokens: int = 2000,
) -> str:
    """Format precedent/lessons as markdown.

    Args:
        lessons: List of lesson/precedent dicts.
        max_tokens: Maximum token budget.

    Returns:
        Markdown-formatted string.
    """
    if not lessons:
        return "No lessons found."

    lines = [f"# Lessons Learned ({len(lessons)})", ""]
    used = _estimate_tokens(lines[0])
    included = 0

    for lesson in lessons:
        title = lesson.get("title", "Untitled")
        desc = lesson.get("description", "")[:300]
        domain = lesson.get("domain", "")

        block = f"## {title}"
        if domain:
            block += f" [{domain}]"
        block += f"\n{desc}\n"

        block_tokens = _estimate_tokens(block)
        if used + block_tokens > max_tokens:
            remaining = len(lessons) - included
            lines.append(f"\n*[{remaining} more lessons omitted]*")
            break

        lines.append(block)
        used += block_tokens
        included += 1  # noqa: SIM113

    return "\n".join(lines)


def _entity_property_lines(properties: dict[str, Any]) -> list[str]:
    """Property lines for a graph node, minus the ``name`` in its heading.

    Values are cut at 200 chars: a node property is a pointer, not a
    body — the body is a document the agent fetches by id.
    """
    return [f"- **{k}**: {str(v)[:200]}" for k, v in properties.items() if k != "name"]


def _capped_doc_pointers(document_ids: list[str], limit: int) -> tuple[list[str], int]:
    """Backtick-quoted evidence pointers up to ``limit``, plus the remainder.

    One cap for every surface that renders ``document_ids`` — the graph
    root block, a neighbor line, a fetched entity — so a node's evidence
    list cannot be unbounded on one surface and capped on another.
    """
    shown = [f"`{doc_id}`" for doc_id in document_ids[:limit]]
    return shown, max(len(document_ids) - limit, 0)


def format_entity_as_markdown(node: dict[str, Any]) -> str:
    """Render one graph node as a standalone body (#305 ``get_items``).

    The same three parts as the root-entity block of
    :func:`format_subgraph_as_markdown` — name/type heading, properties,
    evidence pointers — so an entity fetched by id reads like the entity
    an agent traversed to, and a fetched entity stays a hop rather than a
    dead end. Headings are bold rather than ``#``: the fetch renderer
    owns the heading levels around this block.
    """
    props = node.get("properties", {}) or {}
    name = props.get("name", node.get("node_id", "unknown"))
    lines = [f"**{name}** ({node.get('node_type', 'unknown')})"]
    lines.extend(_entity_property_lines(props))

    shown, more = _capped_doc_pointers(
        node.get("document_ids") or [], _MAX_EVIDENCE_POINTERS
    )
    if shown:
        suffix = f" (+{more} more)" if more else ""
        lines.append(f"Evidence documents: {', '.join(shown)}{suffix}")
    return "\n".join(lines)


def format_subgraph_as_markdown(
    entity: dict[str, Any],
    subgraph: dict[str, Any],
    max_tokens: int = 2000,
) -> str:
    """Format an entity and its subgraph neighborhood as markdown.

    ``document_ids`` — the entity → evidence doc-link provenance the graph
    stores on every node (``save_knowledge`` pointer-not-prose, #301
    extraction stamps) — is rendered as copy-pastable pointers (#305): an
    ``Evidence documents`` section for the root entity and an inline
    ``docs:`` suffix on each neighbor line. That makes ``get_graph`` the
    traversal layer between an index pack and a ``get_items`` fetch —
    without it an agent could see an entity but never follow it to the
    documents that attest it.

    Args:
        entity: The root entity dict.
        subgraph: Dict with "nodes" and "edges" lists.
        max_tokens: Maximum token budget.

    Returns:
        Markdown-formatted string.
    """
    props = entity.get("properties", {})
    name = props.get("name", entity.get("node_id", "unknown"))
    node_type = entity.get("node_type", "unknown")

    lines = [f"# {name} ({node_type})", ""]
    lines.extend(_entity_property_lines(props))

    doc_ids = entity.get("document_ids") or []
    shown, more = _capped_doc_pointers(doc_ids, _MAX_EVIDENCE_POINTERS)
    if shown:
        lines.append("")
        lines.append(f"## Evidence documents ({len(doc_ids)})")
        lines.extend(f"- {pointer}" for pointer in shown)
        if more:
            lines.append(f"*[{more} more omitted]*")

    nodes = subgraph.get("nodes", [])
    edges = subgraph.get("edges", [])

    if edges:
        lines.append("")
        lines.append(f"## Relationships ({len(edges)})")
        for edge in edges[:20]:  # cap at 20 edges
            source = edge.get("source_id", "?")[:12]
            target = edge.get("target_id", "?")[:12]
            etype = edge.get("edge_type", "related")
            lines.append(f"- {source}... --[{etype}]--> {target}...")

    if len(nodes) > 1:
        lines.append("")
        lines.append(f"## Neighbors ({len(nodes) - 1})")
        for node in nodes[:15]:
            if node.get("node_id") == entity.get("node_id"):
                continue
            nprops = node.get("properties", {})
            nname = nprops.get("name", node.get("node_id", "?")[:12])
            ntype = node.get("node_type", "?")
            line = f"- **{nname}** ({ntype})"
            nshown, nmore = _capped_doc_pointers(
                node.get("document_ids") or [], _MAX_NEIGHBOR_POINTERS
            )
            if nshown:
                suffix = f" (+{nmore})" if nmore else ""
                line += f" — docs: {', '.join(nshown)}{suffix}"
            lines.append(line)

    result = "\n".join(lines)
    return _truncate_to_tokens(result, max_tokens)


def format_file_context_as_markdown(
    file_context: dict[str, Any],
    max_tokens: int = 2000,
) -> str:
    """Format a :func:`~trellis.retrieve.file_context.build_file_context` result.

    Timestamps are rendered verbatim (ISO) on every item and as the
    per-path ``Newest memory`` line, so a client staleness gate can parse
    them straight out of the markdown.

    Args:
        file_context: The ``{"paths": [...]}`` result dict.
        max_tokens: Maximum token budget.

    Returns:
        Markdown-formatted string.
    """
    path_results = file_context.get("paths", [])
    if not path_results:
        return "No file paths queried."

    lines = [f"# File Context ({len(path_results)} paths)"]
    if file_context.get("graph_scan_truncated"):
        lines.append("")
        lines.append("_Graph scan hit its cap — entity lists below may be incomplete._")
    for result in path_results:
        documents = result.get("documents", [])
        entities = result.get("entities", [])
        lines.append("")
        lines.append(f"## {result.get('path', '?')}")
        if not documents and not entities:
            lines.append("No stored context for this path.")
            continue
        newest = result.get("newest_item_at")
        if newest:
            lines.append(f"Newest memory: {newest}")
        if documents:
            lines.append("")
            lines.append(f"### Documents ({len(documents)})")
            for doc in documents:
                label = doc.get("title") or doc.get("source_path") or "untitled"
                stamp = doc.get("updated_at") or doc.get("created_at") or "?"
                lines.append(f"- **{label}** `{doc.get('doc_id')}` (updated {stamp})")
                excerpt = doc.get("excerpt")
                if excerpt:
                    lines.append(f"  {excerpt}")
        if entities:
            lines.append("")
            lines.append(f"### Entities ({len(entities)})")
            for entity in entities:
                name = entity.get("name") or entity.get("entity_id") or "?"
                etype = entity.get("entity_type") or "?"
                stamp = entity.get("updated_at") or entity.get("created_at") or "?"
                line = (
                    f"- **{name}** ({etype}) `{entity.get('entity_id')}`"
                    f" (updated {stamp})"
                )
                if entity.get("description"):
                    line += f": {entity['description']}"
                lines.append(line)

    return _truncate_to_tokens("\n".join(lines), max_tokens)


def format_sectioned_pack_as_markdown(
    sections: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 8000,
    *,
    pack_id: str | None = None,
    withholding: WithholdingSummary | None = None,
) -> str:
    """Format a sectioned pack as markdown with section headings.

    Each section becomes a ``## Section Name`` heading with its items
    rendered underneath. Empty sections are omitted.

    When ``pack_id`` is provided, the output includes a reference header
    and a citation footer so agents can cite specific items or advisories
    when calling ``record_feedback`` — enabling element-level attribution
    in the fitness loops.

    Args:
        sections: List of section dicts, each with ``name`` and ``items``
            (list of item dicts with item_id, item_type, excerpt, relevance_score).
        intent: The original query intent.
        max_tokens: Total token budget across all sections.
        pack_id: Optional pack identifier to surface for citation.
        withholding: What the builder removed and did not serve, across the
            whole candidate pool — rendered in the header, above the first
            section, for the reason given in
            :func:`format_pack_as_markdown`.

    Returns:
        Markdown-formatted string within token budget.
    """
    lines = [f"# Context for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    note = format_withholding_note(withholding)
    if note:
        lines.append(note)
    lines.append("")
    used = _estimate_tokens(lines[0]) + 10  # overhead

    for section in sections:
        section_name = section.get("name", "Section")
        items = section.get("items", [])
        if not items:
            continue

        heading = f"## {section_name}"
        heading_tokens = _estimate_tokens(heading)
        if used + heading_tokens > max_tokens:
            lines.append("\n*[sections omitted — token budget reached]*")
            break

        lines.append(heading)
        lines.append("")
        used += heading_tokens

        for item in items:
            excerpt = item.get("excerpt", "")
            item_type = item.get("item_type", "item")
            item_id = item.get("item_id", "")

            # Full item_id in backticks so it's copy-pastable for feedback.
            # Score intentionally omitted — the RRF-fused value is ordinal, not
            # a calibrated relevance; see format_pack_as_markdown for rationale.
            block = f"- `{item_id}` ({item_type}): {excerpt}"
            block_tokens = _estimate_tokens(block)

            if used + block_tokens > max_tokens:
                remaining = len(items) - items.index(item)
                lines.append(f"  *[{remaining} more items omitted]*")
                break

            lines.append(block)
            used += block_tokens

        lines.append("")

    if pack_id:
        lines.append(
            "---\n"
            '*Cite feedback via `record_feedback(pack_id="' + pack_id + '", '
            "success=..., helpful_item_ids=[...], unhelpful_item_ids=[...])`.*"
        )

    return "\n".join(lines).rstrip()


def format_advisories_as_markdown(
    advisories: list[Advisory],
) -> str:
    """Format advisories as a markdown section for pack output.

    Each advisory renders its ``advisory_id`` in backticks so the agent
    can cite it in feedback (``record_feedback(..., followed_advisory_ids=
    [...])``).  The fitness loop uses these IDs to attribute outcomes to
    specific advisories.

    **The evidence is rendered once, by the message** (#392). Every
    :class:`~trellis.retrieve.advisory_generator.AdvisoryGenerator`
    analysis already writes its numbers into ``message`` — *"Packs using
    the 'graph' strategy succeeded 60% of the time vs 0% without (n=5,
    effect=+60%)."* — and this formatter appended a second
    ``(n=…, effect=…)`` to it, so every line printed the same two figures
    twice. The suffix is removed rather than made conditional on the
    message text: guessing whether a sentence already carries its own
    evidence is a string heuristic over content the generator owns.
    Nothing structured is lost — the numbers ride ``Advisory.evidence``,
    which ``POST /api/v1/packs`` serialises in full.

    The list is expected to be pre-capped by
    :meth:`~trellis.retrieve.pack_builder.PackBuilder._select_advisories`;
    this function renders what it is given and imposes no bound of its
    own, so the two surfaces cannot disagree about what the pack holds.

    Args:
        advisories: List of Advisory objects to render.

    Returns:
        Markdown string with advisory suggestions and evidence.
        Empty string if no advisories.
    """
    if not advisories:
        return ""

    lines = [
        f"## Advisories ({len(advisories)} suggestions based on past outcomes)",
        "",
    ]

    lines.extend(
        f"{i}. `{adv.advisory_id}` **[{adv.category.value}]** {adv.message}"
        for i, adv in enumerate(advisories, start=1)
    )

    lines.append("")
    return "\n".join(lines)


def auto_trim_response(
    text: str,
    max_tokens: int,
    *,
    strategy: str = "tail",
) -> tuple[str, bool]:
    """Trim a response to fit within token budget.

    This is a safety-net for edge cases where the primary formatters
    (which stop adding items at the budget boundary) still produce
    output that exceeds the budget.

    Args:
        text: The response text to potentially trim.
        max_tokens: Maximum allowed token count.
        strategy: Trimming strategy.
            ``"tail"`` removes content from the end (default).
            ``"low_relevance"`` removes the lowest-scored markdown
            sections first (identified by ``## `` headers).

    Returns:
        Tuple of (trimmed_text, was_trimmed).
    """
    current_tokens = _estimate_tokens(text)
    if current_tokens <= max_tokens:
        return text, False

    if strategy == "low_relevance":
        trimmed = _trim_low_relevance(text, max_tokens)
    else:
        trimmed = _truncate_to_tokens(text, max_tokens)

    logger.debug(
        "auto_trim_applied",
        strategy=strategy,
        original_tokens=current_tokens,
        max_tokens=max_tokens,
        trimmed_tokens=_estimate_tokens(trimmed),
    )
    return trimmed, True


def _trim_low_relevance(text: str, max_tokens: int) -> str:
    """Remove lowest-relevance sections (by position) until within budget.

    Sections are identified by ``## `` headers. Later sections are
    assumed to be lower relevance and are dropped first.
    """
    lines = text.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    # Remove sections from the end until we fit
    while len(sections) > 1:
        candidate = "\n".join(line for section in sections for line in section)
        if _estimate_tokens(candidate) <= max_tokens:
            return candidate
        sections.pop()

    # Down to one section — fall back to hard truncation
    result = "\n".join(sections[0]) if sections else ""
    return _truncate_to_tokens(result, max_tokens)
