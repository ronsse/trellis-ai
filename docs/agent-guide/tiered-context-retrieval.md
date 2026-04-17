# Tiered Context Retrieval

## Problem

Multi-agent workflows decompose a user's business objective into narrow technical tasks. Each agent asks a task-scoped question ("generate SQL for this layer") but never asks the strategic question ("what does the user want, what already exists, and who owns this data?"). Result: agents miss domain knowledge, ownership, governance conventions, and prior execution history.

## Solution: Sectioned Pack Assembly

Instead of a flat relevance-sorted pack, the graph provides **sectioned packs** where each section targets a different kind of knowledge with its own budget and retrieval strategy.

### The Four Retrieval Tiers

| Tier | What It Provides | When to Assemble |
|------|-----------------|------------------|
| **Objective** | Business intent, domain conventions, ownership, governance, what already exists | Once per workflow, from user's original request |
| **Strategic** | Patterns, prior art, design decisions, materialization rules | Once during planning |
| **Tactical** | Column schemas, code examples, known pitfalls for this exact task | Per-step |
| **Reflective** | Quality constraints, compliance rules, comparison against original objective | After execution |

### Quick Start

```python
from trellis.retrieve import PackBuilder, TierMapper
from trellis.schemas.pack import SectionRequest

builder = PackBuilder(strategies=[keyword_search, graph_search])

# Assemble objective context once
objective_pack = builder.build_sectioned(
    intent="Build daily GGR reporting from sportsbook bet events",
    sections=[
        SectionRequest(
            name="Domain Knowledge",
            retrieval_affinities=["domain_knowledge"],
            max_tokens=2000,
            max_items=10,
        ),
        SectionRequest(
            name="Operational Context",
            retrieval_affinities=["operational"],
            max_tokens=1500,
            max_items=8,
        ),
    ],
    domain="sportsbook",
)

# Assemble task context per step
task_pack = builder.build_sectioned(
    intent="Generate SQL for session aggregation layer",
    sections=[
        SectionRequest(
            name="Technical Patterns",
            retrieval_affinities=["technical_pattern"],
            max_tokens=2000,
        ),
        SectionRequest(
            name="Entity Metadata",
            retrieval_affinities=["reference"],
            entity_ids=["uc://foundation.sportsbook.bets_v7"],
            max_tokens=2000,
        ),
    ],
    domain="sportsbook",
)
```

### Using MCP Tools

If your agents use the XPG MCP server:

```
get_objective_context(intent="Build daily GGR from sportsbook", domain="sportsbook")
get_task_context(intent="Generate SQL for session aggregation", entity_ids=["uc://table"])
```

### Using the SDK

```python
from trellis_sdk.skills import (
    get_objective_context_for_workflow,
    get_task_context_for_step,
)

# Once at workflow start
objective = get_objective_context_for_workflow(client, "Build daily GGR from sportsbook")

# Per step
task = get_task_context_for_step(
    client,
    "Generate SQL for session aggregation",
    entity_ids=["uc://foundation.sportsbook.bets_v7"],
)
```

## The Context Plan Pattern

For multi-agent orchestrators, define a **context plan** mapping phases to sections:

```python
CONTEXT_PLAN = {
    "objective": {
        "sections": ["domain_knowledge", "operational"],
        "assembled": "once_at_start",
        "shared_with": "all_phases",
    },
    "discover": {
        "sections": ["reference"],
        "assembled": "per_phase",
    },
    "plan": {
        "sections": ["technical_pattern", "reference"],
        "assembled": "per_phase",
    },
    "generate": {
        "sections": ["technical_pattern"],
        "assembled": "per_step",
    },
    "validate": {
        "sections": ["domain_knowledge"],  # reflective: back to constraints
        "assembled": "per_phase",
    },
}
```

The orchestrator:
1. Assembles objective context during intake using the **user's actual words**
2. Stores it on run state
3. Passes it to every downstream phase alongside phase-specific task context
4. Each agent sees both `objective_context` (shared) and `task_context` (step-specific)

## How Classification Works

Content is routed to sections via `retrieval_affinity` — a multi-label classification facet on `ContentTags`. Classification happens at three layers:

1. **Deterministic (ingest)** — Structural, keyword, and source-system classifiers populate affinity based on content shape (~70% coverage)
2. **Heuristic (retrieval)** — `TierMapper` applies default rules when content has no explicit affinity (e.g., `content_type=constraint + scope=org → domain_knowledge`)
3. **LLM-enriched (ingest, async)** — For ambiguous content, the LLM classifier assigns affinity alongside existing facets

Content can have **multiple affinities** — a "lookback windows" doc is both `domain_knowledge` and `technical_pattern`. It can appear in either tier depending on which section requests it.

## SectionRequest Reference

```python
class SectionRequest:
    name: str                          # Section heading in formatted output
    retrieval_affinities: list[str]    # Filter: domain_knowledge, technical_pattern, operational, reference
    content_types: list[str]           # Filter: pattern, code, constraint, etc.
    scopes: list[str]                  # Filter: universal, org, project, ephemeral
    entity_ids: list[str]             # Direct match: items with these IDs always included
    max_tokens: int = 2000             # Per-section token budget
    max_items: int = 10                # Per-section item cap
```

When a section has no filters, it acts as a wildcard — all items are eligible. When multiple filters are specified, they're applied conjunctively (AND).

## Cross-Section Deduplication

If the same item matches multiple sections, it's kept only in the section where it scores highest. This prevents wasting token budget on duplicates.
