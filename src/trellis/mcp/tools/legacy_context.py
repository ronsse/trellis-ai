"""The ``search`` tool and the legacy context trio.

``get_objective_context``, ``get_task_context`` and ``get_sectioned_context``.
"""

from __future__ import annotations

from typing import Any

from trellis.auth import SCOPE_READ
from trellis.core.knobs import resolve_search_max_tokens
from trellis.mcp.app import PACK_TOOL_TAG, mcp
from trellis.mcp.auth import trellis_scope
from trellis.mcp.boundary import _raise_invalid_params
from trellis.mcp.tools.context import _flat_context, _sectioned_context
from trellis.mcp.wiring import _get_registry
from trellis.stores.settings_store import load_settings_values

# ---------------------------------------------------------------------------
# Macro Tool 8: search
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def search(
    query: str,
    limit: int = 10,
    max_tokens: int | None = None,
    index: bool = False,
    retrieval_phase: str | None = None,
) -> str:
    """Search the experience graph for documents and entities.

    A targeted flat lookup over the same one retrieval path as
    ``get_context`` (keyword + graph + semantic fused with RRF), returning
    a token-budgeted markdown pack with a citable ``pack_id``.

    Args:
        query: Search query.
        limit: Maximum results (default 10).
        max_tokens: Maximum response size in tokens. Leave unset to use the
            deployment's configured default (2000 unless an operator has
            overridden it via settings).
        index: Return an id index instead of excerpt bodies (default
            False) — one compact line per item; raise ``limit`` to survey
            more. Fetch chosen bodies with ``get_items``; feedback
            attribution via the ``pack_id`` is unchanged. The index is a
            real pack, so a survey counts as a serve for session dedup —
            use ``get_context(..., refresh=True)`` if a later retrieval
            in the same session needs the surveyed items back.
        retrieval_phase: Optional — when in the task this search happens:
            one of "pre_work", "mid_task", "post_hoc". Pass "post_hoc"
            when searching after the work is done, e.g. to check
            conclusions. Leave unset when you don't know.
    """
    if not query or not query.strip():
        _raise_invalid_params(
            "query must not be empty",
            data={"field": "query"},
        )

    registry = _get_registry()
    resolved_max_tokens = (
        max_tokens
        if max_tokens is not None
        else resolve_search_max_tokens(load_settings_values(registry.stores_dir))
    )
    return _flat_context(
        registry,
        query,
        domain=None,
        max_tokens=resolved_max_tokens,
        session_id="",
        max_items=limit,
        title=f"Search: {query}",
        empty_message=f"No results found for: {query}",
        operation="search",
        index=index,
        retrieval_phase=retrieval_phase,
    )


# ---------------------------------------------------------------------------
# Macro Tool 9: get_objective_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_objective_context(
    intent: str,
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
    retrieval_phase: str | None = None,
) -> str:
    """Retrieve objective-level context for a workflow.

    Assembles domain knowledge (conventions, ownership, precedents,
    governance) and operational context (prior traces, known failures)
    for a user's business objective. Designed to be called once at
    workflow start and shared across all downstream agent phases.

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context`` with a ``sections`` layout — this
        tool is a fixed two-section preset and will be removed.

    Args:
        intent: The user's original business objective in their own words.
        domain: Optional domain filter (e.g., "orders", "data-pipeline").
        max_tokens: Token budget override. Pass ``0`` (default) to use the
            configured budget from ``retrieval.budgets`` in ``config.yaml``;
            pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded from the result, preventing repetition across calls.
        retrieval_phase: Optional — when in the task this retrieval
            happens: one of "pre_work", "mid_task", "post_hoc". Pass
            "post_hoc" when retrieving after the work is done, e.g. to
            check conclusions. Leave unset when you don't know.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_objective_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    resolved_tokens = budget.max_tokens
    section_specs = [
        {
            "name": "Domain Knowledge",
            "retrieval_affinities": ["domain_knowledge"],
            "max_tokens": resolved_tokens // 2,
            "max_items": 10,
        },
        {
            "name": "Operational Context",
            "retrieval_affinities": ["operational"],
            "max_tokens": resolved_tokens // 3,
            "max_items": 8,
        },
    ]
    return _sectioned_context(
        registry,
        intent,
        section_specs=section_specs,
        resolved_tokens=resolved_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_objective_context",
        retrieval_phase=retrieval_phase,
    )


# ---------------------------------------------------------------------------
# Macro Tool 10: get_task_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_task_context(
    intent: str,
    entity_ids: list[str] | None = None,
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
    retrieval_phase: str | None = None,
) -> str:
    """Retrieve task-level context for a specific agent step.

    Assembles technical patterns and reference data relevant to a
    specific task (e.g., SQL generation, validation). Complements
    objective context with step-specific details.

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context`` with a ``sections`` layout (anchor
        via each section's ``entity_ids``) — this fixed two-section preset
        will be removed.

    Args:
        intent: Description of the specific task being performed.
        entity_ids: Entity IDs being touched (e.g., table URIs). They also
            seed the graph axis, so a precedent attached to one is reachable.
        domain: Optional domain filter.
        max_tokens: Token budget override. Pass ``0`` (default) to use the
            configured budget from ``retrieval.budgets`` in ``config.yaml``;
            pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.
        retrieval_phase: Optional — when in the task this retrieval
            happens: one of "pre_work", "mid_task", "post_hoc". Pass
            "post_hoc" when retrieving after the work is done, e.g. to
            check conclusions. Leave unset when you don't know.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_task_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    resolved_tokens = budget.max_tokens
    section_specs = [
        {
            "name": "Technical Patterns",
            "retrieval_affinities": ["technical_pattern"],
            "max_tokens": resolved_tokens // 2,
            "max_items": 10,
        },
        {
            "name": "Reference Data",
            "retrieval_affinities": ["reference"],
            # Passed through as-is so an invalid ``entity_ids`` (e.g. a
            # non-list) surfaces as INTERNAL_ERROR from the shared validator.
            "entity_ids": entity_ids if entity_ids is not None else [],
            "max_tokens": resolved_tokens // 3,
            "max_items": 10,
        },
    ]
    return _sectioned_context(
        registry,
        intent,
        section_specs=section_specs,
        resolved_tokens=resolved_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_task_context",
        retrieval_phase=retrieval_phase,
    )


# ---------------------------------------------------------------------------
# Macro Tool 11: get_sectioned_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_sectioned_context(
    intent: str,
    sections: list[dict[str, Any]],
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
    retrieval_phase: str | None = None,
) -> str:
    """Retrieve context organized into independently budgeted sections.

    Unlike get_objective_context and get_task_context (which use fixed
    section layouts), this tool lets you define your own sections with
    custom affinities, content types, scopes, entity IDs, and per-section
    token budgets.

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context(intent, sections=[...])`` — the
        canonical parameterized tool with the identical ``sections`` schema.

    Args:
        intent: Natural language description of the task or question.
        sections: List of section configs. Each section is a dict with:
            - name (str, required): Section heading.
            - retrieval_affinities (list[str]): e.g. ["domain_knowledge"]
            - content_types (list[str]): e.g. ["code", "documentation"]
            - scopes (list[str]): e.g. ["universal", "project"]
            - entity_ids (list[str]): Entity IDs to anchor retrieval.
            - max_tokens (int): Token budget for this section (default 2000).
            - max_items (int): Max items for this section (default 10).
        domain: Optional domain filter applied across all sections.
        max_tokens: Total token budget override. Pass ``0`` (default) to use
            the configured budget from ``retrieval.budgets`` in
            ``config.yaml``; pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.
        retrieval_phase: Optional — when in the task this retrieval
            happens: one of "pre_work", "mid_task", "post_hoc". Pass
            "post_hoc" when retrieving after the work is done, e.g. to
            check conclusions. Leave unset when you don't know.

    Example sections:
        [
          {"name": "Schema Context", "retrieval_affinities": ["reference"],
           "entity_ids": ["uc://catalog.schema.table"], "max_tokens": 3000},
          {"name": "Past Patterns", "retrieval_affinities": ["technical_pattern"],
           "content_types": ["code", "procedure"], "max_tokens": 2000}
        ]
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )
    if not sections:
        _raise_invalid_params(
            "sections must not be empty",
            data={"field": "sections"},
        )

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_sectioned_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    return _sectioned_context(
        registry,
        intent,
        section_specs=sections,
        resolved_tokens=budget.max_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_sectioned_context",
        retrieval_phase=retrieval_phase,
    )
