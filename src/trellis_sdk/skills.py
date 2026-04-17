"""Pre-built skill functions for orchestrators.

Each function returns a concise markdown string suitable for injecting
directly into an LLM's context window.
"""

from __future__ import annotations

from typing import Any

from trellis.retrieve.formatters import (
    format_pack_as_markdown,
    format_traces_as_markdown,
)
from trellis_sdk.client import TrellisClient

_MAX_PREVIEW_STEPS = 5


def get_context_for_task(
    client: TrellisClient,
    intent: str,
    *,
    domain: str | None = None,
    max_tokens: int = 1500,
) -> str:
    """Get relevant context for a task as a markdown summary.

    Args:
        client: TrellisClient instance.
        intent: What you're trying to do.
        domain: Optional domain scope.
        max_tokens: Token budget for the response.

    Returns:
        Markdown string summarizing relevant context.
    """
    pack = client.assemble_pack(intent, domain=domain, max_items=20)
    items = pack.get("items", [])
    if not items:
        return f"No relevant context found for: {intent}"
    return format_pack_as_markdown(items, intent, max_tokens=max_tokens)


def get_latest_successful_trace(
    client: TrellisClient,
    task_type: str,
    *,
    domain: str | None = None,
) -> str:
    """Get the most recent successful trace matching a task type.

    Args:
        client: TrellisClient instance.
        task_type: Keyword to search for in trace intents.
        domain: Optional domain filter.

    Returns:
        Markdown summary of the trace, or a "not found" message.
    """
    traces = client.list_traces(domain=domain, limit=20)

    # Filter for matching and successful traces
    matching = [
        t
        for t in traces
        if task_type.lower() in t.get("intent", "").lower()
        and t.get("outcome") == "success"
    ]

    if not matching:
        return f"No successful traces found for: {task_type}"

    trace = matching[0]
    full = client.get_trace(trace["trace_id"])
    if full is None:
        return f"Trace {trace['trace_id']} not found"

    intent = full.get("intent", "")
    outcome = full.get("outcome", {})
    summary = outcome.get("summary", "") if isinstance(outcome, dict) else ""
    steps = full.get("steps", [])

    lines = [
        f"# Successful Trace: {intent[:100]}",
        f"**ID:** {trace['trace_id']}",
        f"**Domain:** {trace.get('domain', 'general')}",
        f"**Created:** {trace.get('created_at', '')[:10]}",
    ]
    if summary:
        lines.append(f"**Summary:** {summary[:300]}")
    if steps:
        lines.append(f"**Steps:** {len(steps)}")
        for step in steps[:_MAX_PREVIEW_STEPS]:
            name = step.get("name", "unnamed")
            lines.append(f"  - {name}")
        if len(steps) > _MAX_PREVIEW_STEPS:
            lines.append(f"  - ... and {len(steps) - _MAX_PREVIEW_STEPS} more")

    return "\n".join(lines)


def save_trace_and_extract_lessons(
    client: TrellisClient,
    trace: dict[str, Any],
) -> str:
    """Ingest a trace and return a summary.

    Args:
        client: TrellisClient instance.
        trace: Trace dict to ingest.

    Returns:
        Markdown summary confirming ingestion.
    """
    trace_id = client.ingest_trace(trace)
    intent = trace.get("intent", "unknown")
    outcome = trace.get("outcome", {})
    status = (
        outcome.get("status", "unknown") if isinstance(outcome, dict) else "unknown"
    )

    return f"Trace ingested: **{trace_id}**\n- Intent: {intent}\n- Outcome: {status}\n"


def get_recent_activity(
    client: TrellisClient,
    *,
    domain: str | None = None,
    limit: int = 10,
    max_tokens: int = 1500,
) -> str:
    """Get a summary of recent activity.

    Args:
        client: TrellisClient instance.
        domain: Optional domain filter.
        limit: Max traces to include.
        max_tokens: Token budget.

    Returns:
        Markdown summary of recent traces.
    """
    traces = client.list_traces(domain=domain, limit=limit)
    if not traces:
        return "No recent activity found."
    return format_traces_as_markdown(traces, max_tokens=max_tokens)


def get_objective_context_for_workflow(
    client: TrellisClient,
    intent: str,
    *,
    domain: str | None = None,
    max_tokens: int = 4000,
) -> str:
    """Assemble objective-level context for an entire workflow.

    Call this once at the start of a multi-agent workflow. The returned
    context covers domain knowledge, ownership, governance conventions,
    and relevant prior execution traces. Pass the result to all
    downstream agent phases as shared context.
    """
    return client.get_objective_context(intent, domain=domain, max_tokens=max_tokens)


def get_task_context_for_step(
    client: TrellisClient,
    intent: str,
    *,
    entity_ids: list[str] | None = None,
    domain: str | None = None,
    max_tokens: int = 4000,
) -> str:
    """Assemble task-level context for a specific workflow step.

    Call this per-step to get technical patterns and reference data
    relevant to the current task. Complements the objective context
    with step-specific details like entity schemas and code examples.
    """
    return client.get_task_context(
        intent, entity_ids=entity_ids, domain=domain, max_tokens=max_tokens
    )
