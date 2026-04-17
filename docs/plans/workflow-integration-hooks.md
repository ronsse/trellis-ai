# Workflow Integration Hooks — Design Brief

## What This Is

A prompt/design brief for implementing generic workflow integration hooks in the
trellis-ai core library. These hooks enable any workflow engine to integrate
with the experience graph: context in before a step runs, traces out after, and
results fed back to the graph.

## Background

The `trellis-platform` consumer repo (`fd-data-architecture-poc/trellis-platform`) built
these patterns as one-off implementations to integrate the fd-poc pipeline's Claude
Code workers with the XPG experience graph. They work, but they're hardcoded to
fd-poc's session/dispatch model. The patterns are generic and should live in the
core library.

### What Exists Today (in fd-poc, NOT in this repo)

**`fd-data-architecture-poc/src/fd_poc/agents/graph_context.py`**
- `fetch_entity_context(entity_ids, intent, domain, max_tokens) -> str`
- Calls `POST /api/v1/packs` for rich context, falls back to per-entity
  `GET /api/v1/entities/{id}` and `GET /api/v1/search`
- Returns markdown string injected into worker prompt's `context_brief`
- Graceful degradation: WARNING log + empty string if API unreachable

**`fd-data-architecture-poc/src/fd_poc/agents/trace_recorder.py`**
- `record_worker_trace(skill_name, run_id, entity_ids, status, duration_ms, ...) -> trace_id | None`
- Calls `POST /api/v1/traces` with Trace schema (source=workflow, steps, outcome, context)
- Records success AND failure (failure traces enable learning from mistakes)
- Fire-and-forget: WARNING log + None if API unreachable

**`fd-data-architecture-poc/src/fd_poc/agents/result_feedback.py`**
- `record_generation_result(layer_name, target_table, success, sql_content, ...) -> None`
- On success: `POST /api/v1/entities` (DOCUMENT node) + `POST /api/v1/links` (DESCRIBED_BY edge)
- On failure: no-op (failure captured in trace)
- Fire-and-forget: WARNING log if API unreachable

### How They're Wired (in fd-poc)

```python
# workflow.py — pre-dispatch
from fd_poc.agents.graph_context import fetch_entity_context

context_brief = {
    "run_id": run_id,
    "skill": "plan",
    "graph_context": fetch_entity_context(entity_ids, intent=..., max_tokens=4000),
}
session = AgentSession(..., context_brief=context_brief)

# workflow.py — post-dispatch (inside _dispatch_skill)
from fd_poc.agents.trace_recorder import record_worker_trace

record_worker_trace(
    skill_name=step.name,
    run_id=run_id,
    entity_ids=entity_ids,
    status="success" if result.succeeded else "failure",
    duration_ms=duration_ms,
)
```

## What Should Be Built in This Repo

### 1. `trellis_sdk.hooks.ContextInjector`

```python
class ContextInjector:
    """Assembles graph context for a workflow step."""

    def __init__(self, client: TrellisClient, default_max_tokens: int = 4000):
        ...

    def for_entities(
        self,
        entity_ids: list[str],
        intent: str = "",
        domain: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Returns markdown context string. Empty string if unavailable."""

    def for_intent(self, intent: str, domain: str | None = None) -> str:
        """Context from intent alone (no known entity IDs)."""
```

Should use `client.assemble_pack()` internally, with fallback to per-entity
lookup. Format output as markdown sections. Token budget should be split
across pack items, not given entirely to the pack builder.

### 2. `trellis_sdk.hooks.TraceRecorder`

```python
class TraceRecorder:
    """Records workflow step executions as Traces."""

    def __init__(self, client: TrellisClient, workflow_id: str, agent_id: str = "workflow"):
        ...

    def record(
        self,
        step_name: str,
        status: str,  # success | failure | partial
        duration_ms: int,
        entity_ids: list[str] = [],
        summary: str = "",
        metrics: dict | None = None,
        error: str | None = None,
    ) -> str | None:
        """Returns trace_id or None. Never raises."""
```

Should build a well-formed Trace with proper step, outcome, and context
fields. The `workflow_id` ties all steps in a pipeline run together.

### 3. `trellis_sdk.hooks.ResultFeedback`

```python
class ResultFeedback:
    """Records evidence linking workflow results to graph entities."""

    def __init__(self, client: TrellisClient):
        ...

    def record_success(
        self,
        target_entity_id: str,
        result_name: str,
        summary: str,
        full_content: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Creates DOCUMENT entity + DESCRIBED_BY edge. Never raises."""

    def record_failure(
        self,
        target_entity_id: str,
        error_summary: str,
        trace_id: str | None = None,
    ) -> None:
        """Failure is captured in traces, not as documents. Log only."""
```

### 4. Documentation

- Add a "Workflow Integration" section to `docs/agent-guide/` explaining
  the three hooks and when to use each
- Add `.mcp.json` example for running trellis MCP server as a sidecar
- Add example showing a generic workflow engine using all three hooks

### Design Constraints

- All hooks MUST degrade gracefully (WARNING log, no exceptions)
- All hooks MUST work with both remote `TrellisClient(base_url=...)` and
  local `TrellisClient(registry=...)` modes
- No fd-poc specific types — use plain dicts, strings, and the SDK's
  existing Trace/Entity schemas
- Token budgets should be configurable and respected
