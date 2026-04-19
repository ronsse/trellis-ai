"""Pure-wire markdown formatters for SDK skill helpers.

These are token-budgeted stringifiers over plain dicts — zero
dependency on ``trellis`` core.  Ported from
``trellis.retrieve.formatters`` with ``estimate_tokens`` inlined as
the same 4-chars-per-token heuristic; advisory rendering reduced to
dict access (no ``Advisory`` Pydantic model required).

The core formatters in ``trellis.retrieve.formatters`` remain in
place — they're used by the MCP server and internal reporting.
These duplicates exist so the SDK can stay ``trellis.*``-free.  If
the two ever drift meaningfully, surface it as an observable bug —
both sides format from the same wire-level dict shape so divergence
is easy to test against fixtures.
"""

from __future__ import annotations

from typing import Any

_CHARS_PER_TOKEN = 4  # conservative estimate matching core


def _estimate_tokens(text: str) -> int:
    """Rough token count at 4 chars/token."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def format_pack_as_markdown(
    items: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 2000,
    *,
    pack_id: str | None = None,
) -> str:
    """Format pack items as concise markdown for LLM consumption.

    Token-budgeted: stops emitting items once the running budget is
    exceeded and appends a ``[N more items omitted]`` marker.
    """
    lines = [f"# Context for: {intent}"]
    if pack_id:
        lines.append(f"**pack_id:** `{pack_id}`")
    lines.append("")
    token_budget = max_tokens - _estimate_tokens(lines[0]) - 10
    used = 0
    included = 0

    for item in items:
        item_type = item.get("item_type", "item")
        excerpt = item.get("excerpt", "")
        score = item.get("relevance_score", 0.0)
        item_id = item.get("item_id", "")

        header = f"## [{item_type}] `{item_id}`"
        if score > 0:
            header += f" (relevance: {score:.2f})"

        block = f"{header}\n{excerpt}\n"
        block_tokens = _estimate_tokens(block)
        if used + block_tokens > token_budget:
            remaining = len(items) - included
            if remaining > 0:
                lines.append(f"\n*[{remaining} more items omitted]*")
            break
        lines.append(block)
        used += block_tokens
        included += 1

    if pack_id and included > 0:
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
    """Format trace summaries as markdown."""
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


def format_sectioned_pack_as_markdown(
    sections: list[dict[str, Any]],
    intent: str,
    max_tokens: int = 4000,
) -> str:
    """Format a list of pack sections (as dicts) as markdown.

    Each section is expected to be a dict with keys ``name``, ``items``,
    and optionally ``max_tokens``.  Items within each section are
    rendered via :func:`format_pack_as_markdown` with a proportional
    slice of the overall budget.
    """
    if not sections:
        return f"# Context for: {intent}\n\n*No sections available.*"

    lines = [f"# Context for: {intent}", ""]
    per_section = max(200, max_tokens // max(1, len(sections)))
    for section in sections:
        name = section.get("name", "section")
        items = section.get("items", [])
        lines.append(f"## Section: {name}")
        if not items:
            lines.append("*No items in this section.*")
            continue
        rendered = format_pack_as_markdown(items, name, max_tokens=per_section)
        # Strip the inner H1 — the outer H1 already names the intent.
        rendered = "\n".join(
            line for line in rendered.splitlines() if not line.startswith("# ")
        )
        lines.append(rendered)
    return "\n".join(lines)


__all__ = [
    "format_pack_as_markdown",
    "format_sectioned_pack_as_markdown",
    "format_traces_as_markdown",
]
