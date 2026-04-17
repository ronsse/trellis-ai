---
name: trellis-ai
description: Structured institutional memory for AI agents — traces, precedents, knowledge graph, temporal versioning, and governed mutations. Complements built-in memory with structured knowledge.
version: 1.0.0
homepage: https://github.com/ronsse/trellis-ai
metadata:
  openclaw:
    requires:
      anyBins: ["trellis-mcp"]
---

# Trellis

Structured institutional memory for AI agents. Record traces of your work, build a shared knowledge graph, and retrieve context packs before starting new tasks.

## When to Use XPG vs Built-in Memory

| Use Case | Built-in Memory | Trellis |
|----------|----------------|------------------|
| Daily notes, session context | Yes | |
| Quick personal reminders | Yes | |
| Structured traces of work (steps, tool calls, outcomes) | | Yes |
| Reusable patterns and precedents | | Yes |
| Knowledge graph with typed relationships | | Yes |
| Temporal versioning (time-travel queries) | | Yes |
| Cross-agent institutional knowledge | | Yes |
| Evidence linking (docs, snippets, files) | | Yes |

**Rule of thumb:** If it's a note for yourself, use built-in memory. If it's structured knowledge that other agents or future sessions should learn from, use XPG.

## Available Tools

11 macro tools, all returning token-budgeted markdown (not raw JSON). Eight cover the most common write-and-read workflow; three more provide sectioned context for richer multi-step retrieval.

**Core tools**

| Tool | What It Does | Example |
|------|-------------|---------|
| `get_context` | Search docs + graph + traces for task context | `get_context(intent="implement retry logic", domain="backend")` |
| `save_experience` | Record a trace of completed work | `save_experience(trace_json="{...}")` |
| `save_knowledge` | Create an entity in the knowledge graph | `save_knowledge(name="auth-service", entity_type="service")` |
| `save_memory` | Store a document for later retrieval | `save_memory(content="Rate limiting uses token bucket algorithm")` |
| `get_lessons` | List precedents (proven patterns) | `get_lessons(domain="backend", max_tokens=1500)` |
| `get_graph` | Explore entity neighborhood | `get_graph(entity_id="01JRK5N7QF", depth=2)` |
| `record_feedback` | Record whether a task succeeded | `record_feedback(trace_id="01JRK5N7QF", success=true)` |
| `search` | Search documents and entities | `search(query="database migration", limit=5)` |

**Sectioned-context tools** (use for richer multi-step or workflow-spanning retrieval)

| Tool | What It Does | Example |
|------|-------------|---------|
| `get_objective_context` | One pack covering domain knowledge + operational context for a whole workflow | `get_objective_context(intent="ship auth migration", domain="backend")` |
| `get_task_context` | Pack scoped to specific entities for one step inside a workflow | `get_task_context(intent="rotate JWT keys", entity_ids=["auth-service"])` |
| `get_sectioned_context` | Pack with caller-defined sections, per-section budgets and affinities | `get_sectioned_context(intent="...", sections=[{...}, {...}])` |

## Patterns

### Retrieve Before Acting

Before starting non-trivial work, check for prior art:

```
get_context(intent="what you're about to do", domain="relevant-domain")
```

This returns relevant traces, precedents, and evidence so you avoid repeating past mistakes and reuse proven patterns.

### Record After Success

After completing meaningful work, save the experience:

```
save_experience(trace_json='{"source": "agent", "intent": "what you did", "steps": [...], "outcome": {"status": "success", "summary": "what happened"}, "context": {"domain": "backend"}}')
```

Then record feedback:

```
record_feedback(trace_id="<returned_id>", success=true, notes="Clean implementation")
```

### Link Evidence

When you discover useful documentation or patterns, store them and connect to entities:

```
save_memory(content="API rate limits: 1000 req/min per client", metadata={"source": "api-docs", "domain": "platform"})
save_knowledge(name="rate-limiter", entity_type="concept", relates_to="api-gateway-id", edge_kind="entity_part_of")
```

## Context Window Tips

- Use `max_tokens` to control response size — default is 2000 tokens
- For quick lookups, set `max_tokens=500`
- For deep research, allow up to `max_tokens=4000`
- All responses are pre-formatted markdown, ready for your context window
- Prefer `get_context` for broad task context; use `search` for targeted queries
