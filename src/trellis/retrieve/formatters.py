"""Response formatters for token-efficient output."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.core.hashing import estimate_tokens as _estimate_tokens
from trellis.schemas.advisory import Advisory

logger = structlog.get_logger(__name__)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token budget."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def format_pack_as_markdown(
    items: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 2000,
    *,
    pack_id: str | None = None,
) -> str:
    """Format pack items as concise markdown for LLM consumption.

    Args:
        items: List of pack item dicts with item_id, item_type, excerpt,
            relevance_score, metadata.
        intent: The original query intent.
        max_tokens: Maximum token budget for the response.
        pack_id: Optional pack identifier to surface for citation.

    Returns:
        Markdown-formatted string within token budget.
    """
    lines = [f"# Context for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    lines.append("")
    token_budget = max_tokens - _estimate_tokens(lines[0]) - 10  # reserve overhead
    used = 0
    included = 0

    for item in items:
        item_type = item.get("item_type", "item")
        excerpt = item.get("excerpt", "")
        score = item.get("relevance_score", 0.0)
        item_id = item.get("item_id", "")

        # Build item block — full item_id in backticks so it's copy-pastable
        header = f"## [{item_type}] `{item_id}`"
        if score > 0:
            header += f" (relevance: {score:.2f})"

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


def format_subgraph_as_markdown(
    entity: dict[str, Any],
    subgraph: dict[str, Any],
    max_tokens: int = 2000,
) -> str:
    """Format an entity and its subgraph neighborhood as markdown.

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

    # Add entity properties
    for k, v in props.items():
        if k != "name":
            lines.append(f"- **{k}**: {str(v)[:200]}")

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
            lines.append(f"- **{nname}** ({ntype})")

    result = "\n".join(lines)
    return _truncate_to_tokens(result, max_tokens)


def format_sectioned_pack_as_markdown(
    sections: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 8000,
    *,
    pack_id: str | None = None,
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

    Returns:
        Markdown-formatted string within token budget.
    """
    lines = [f"# Context for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
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
            score = item.get("relevance_score", 0.0)

            # Full item_id in backticks so it's copy-pastable for feedback
            block = f"- `{item_id}` ({item_type}"
            if score > 0:
                block += f", {score:.2f}"
            block += f"): {excerpt}"
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

    for i, adv in enumerate(advisories, start=1):
        ev = adv.evidence
        effect_str = f"{ev.effect_size:+.0%}" if ev.effect_size else ""
        lines.append(
            f"{i}. `{adv.advisory_id}` **[{adv.category.value}]** {adv.message}"
            f" (n={ev.sample_size}, effect={effect_str})"
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
