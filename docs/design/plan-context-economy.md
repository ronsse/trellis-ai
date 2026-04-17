# Implementation Plan: Context Economy (MCP + CLI + Skills Layering)

Reference: [context-economy-strategy.md](./context-economy-strategy.md)

---

## Current State

- CLI (`trellis`) exists with ingest, curate, retrieve, admin command groups
- MCP server exists with 19 tools across 6 categories (memory, knowledge, experience, trace, context, skill)
- MCP server returns raw JSON dicts, no summarization or token budgeting
- No REST API (planned in architecture plan)
- No Python SDK for orchestrator integration
- No "Macro Tool" pattern; MCP tools are 1:1 with store operations
- PackBuilder has basic token budgeting (max_items, max_tokens)

## Phase 1: CLI Hardening (The Execution Layer)

**Goal:** Make the CLI the authoritative, scriptable interface that agents and humans both rely on.

### Tasks

1. **Ensure all operations are CLI-accessible**
   - Audit: compare MCP tools against CLI commands, fill gaps
   - Missing CLI commands to add:
     - `trellis retrieve lessons` - list promoted precedents (exists in MCP as `experience_lessons`)
     - `trellis retrieve cases` - list recent traces (exists in MCP as `experience_cases`)
     - `trellis curate feedback` - record feedback against a pack/trace
     - `trellis admin stats` - store counts and health metrics
   - All commands must support `--format json` (most already do)

2. **Add output filtering flags**
   - `--fields` flag on list/search commands to select specific fields
     - Example: `trellis retrieve search "dbt" --format json --fields summary,trace_id`
   - `--limit` flag (already exists on some commands, standardize across all)
   - `--truncate N` flag to cap text fields at N characters

3. **Add pipe-friendly output modes**
   - `--format jsonl` for line-delimited JSON (streamable)
   - `--format tsv` for tab-separated (for `awk`/`cut` pipelines)
   - `--quiet` flag that suppresses Rich formatting and status messages

4. **Stdin support for batch operations**
   - `trellis ingest trace -` reads from stdin (already works)
   - `trellis ingest trace --batch` reads newline-delimited trace JSON
   - `cat traces/*.json | trellis ingest trace --batch`

### Acceptance Criteria
- Every MCP tool has a CLI equivalent
- `--format json` works on all commands, output is valid JSON
- `--fields` filters output to requested fields only
- Agents can chain `trellis ... | jq ...` for precise context extraction

---

## Phase 2: MCP Macro Tools (The UX Layer)

**Goal:** Replace granular MCP tools with high-level "Macro Tools" that return summarized markdown, not raw JSON.

### Tasks

1. **Design Macro Tool set**
   - Reduce from 19 tools to ~6-8 intent-driven tools:

   | Macro Tool | Replaces | Returns |
   |-----------|----------|---------|
   | `get_context(intent, domain?, max_tokens?)` | `context_assemble`, `memory_search`, `knowledge_query` | Summarized markdown pack, max 2000 tokens |
   | `save_experience(trace_json)` | `trace_ingest` | Confirmation with trace_id |
   | `save_knowledge(name, type, properties?, relates_to?)` | `knowledge_add`, `knowledge_relate` | Confirmation with entity_id |
   | `save_memory(content, metadata?)` | `memory_store` | Confirmation with doc_id |
   | `get_lessons(domain?, limit?)` | `experience_lessons`, `experience_cases` | Markdown list of top precedents |
   | `get_graph(entity_id, depth?)` | `context_graph` | Markdown-formatted subgraph |
   | `record_feedback(trace_id, success, notes?)` | (new) | Confirmation |
   | `search(query, limit?)` | `memory_search`, `knowledge_query` | Markdown results, truncated |

2. **Implement summarization layer**
   - `src/trellis/mcp/formatters.py`:
     - `format_pack_as_markdown(pack: Pack, max_tokens: int) -> str`
     - `format_entities_as_markdown(entities: list[Entity]) -> str`
     - `format_traces_as_markdown(traces: list[Trace], max_per: int) -> str`
   - Rules:
     - Strip internal IDs from output unless explicitly requested
     - Truncate long content with `[... truncated]`
     - Use hierarchical markdown headers for structure
     - Target: each tool response fits in ~500-2000 tokens

3. **Refactor MCP server**
   - Create `src/trellis/mcp/` package (move from single `mcp_server.py`)
     - `server.py` - FastMCP app and tool registration
     - `tools.py` - Macro tool implementations
     - `formatters.py` - Response formatting
   - Deprecate old granular tools (keep for one release cycle with deprecation warnings)

4. **Add token budget to all MCP responses**
   - Every Macro Tool accepts optional `max_tokens` parameter (default: 2000)
   - Formatter respects budget: packs items until budget is reached, appends `[N more items omitted]`
   - Log token usage in `ContextRetrievalEvent` for observability

### Acceptance Criteria
- MCP tool count reduced from 19 to ~8
- All MCP responses are markdown, not raw JSON
- Each response fits within its token budget
- IDE integration (Cursor, Cline) provides useful context without bloating the window

---

## Phase 3: Python SDK (The Routing Layer)

**Goal:** Provide a lightweight SDK for orchestrators (LangGraph, CrewAI) with pre-summarized responses.

### Tasks

1. **Create `src/trellis_sdk/` package**
   - `client.py` - `TrellisClient` class
     - Constructor: `TrellisClient(base_url=None, config_path=None)`
     - If `base_url` provided: uses HTTP (httpx) to call REST API
     - If no `base_url`: uses local stores directly via StoreRegistry
   - Add to pyproject.toml as separate package

2. **Define skill functions (high-level wrappers)**
   - `src/trellis_sdk/skills.py`:
     ```python
     def get_context_for_task(client: TrellisClient, intent: str, domain: str = None, max_tokens: int = 1500) -> str:
         """Returns a markdown summary of relevant context. Designed for LLM consumption."""

     def get_latest_successful_trace(client: TrellisClient, task_type: str) -> str:
         """Returns markdown summary of the most recent successful trace matching task_type."""

     def save_trace_and_extract_lessons(client: TrellisClient, trace: dict) -> str:
         """Ingests trace and runs precedent extraction. Returns summary."""

     def get_applicable_policies(client: TrellisClient, domain: str) -> str:
         """Returns markdown list of policies relevant to domain."""
     ```
   - Every skill returns **a string**, not a data object -- ready for LLM context injection

3. **LangGraph tool adapter**
   - `src/trellis_sdk/integrations/langgraph.py`:
     ```python
     def create_xpg_tools(client: TrellisClient) -> list[BaseTool]:
         """Returns LangGraph-compatible tools wrapping XPG skills."""
     ```
   - Each tool has a concise description (<100 chars) to minimize schema overhead

4. **CrewAI tool adapter**
   - `src/trellis_sdk/integrations/crewai.py`:
     - Same pattern, CrewAI `Tool` wrappers

5. **Claude Code skill**
   - Create an XPG skill for Claude Code that wraps CLI commands
   - Provides `trellis-context` skill that agents can invoke to get pre-summarized context
   - Uses CLI under the hood with `--format json | jq` for filtering

### Acceptance Criteria
- `TrellisClient` works in both local and remote modes
- Skill functions return concise markdown strings (<2000 tokens)
- LangGraph integration works as a standard tool node
- SDK adds minimal dependencies (just `httpx` for remote mode)

---

## Phase 4: Token Observability Dashboard

**Goal:** Measure and optimize token economy across all integration layers.

### Tasks

1. **Add token tracking to all response paths**
   - CLI: log response size in chars/estimated tokens to structlog
   - MCP: log per-tool response token count
   - SDK: log per-skill response token count
   - All logged as structured events to EventLog

2. **Token usage report**
   - `trellis analyze token-usage --days 7`
   - Breaks down by: layer (CLI/MCP/SDK), tool/command, agent_id
   - Shows: avg tokens per response, total tokens, largest responses
   - Highlights tools that consistently exceed budget

3. **Automatic response trimming**
   - If a response exceeds the token budget, auto-trim with strategy:
     1. Remove lowest-relevance items first
     2. Truncate remaining item excerpts
     3. Append count of omitted items
   - Log trimming events for observability

### Acceptance Criteria
- Token usage is tracked across all layers
- Report identifies token-heavy tools/queries
- Auto-trimming keeps responses within budget without losing critical info

---

## Dependency Order

```
Phase 1 (CLI Hardening)
  |
  +---> Phase 2 (MCP Macro Tools)
  |       |
  |       +---> Phase 4 (Token Observability)
  |
  +---> Phase 3 (Python SDK)  -- also depends on REST API from architecture plan
```

Phase 1 (CLI) is the foundation. Phase 2 (MCP) and Phase 3 (SDK) can proceed in parallel after Phase 1, though the SDK benefits from having the REST API from the architecture plan. Phase 4 spans all layers and should come last.

---

## Cross-Cutting Concerns

### Shared Formatters
Both the MCP Macro Tools (Phase 2) and the SDK skills (Phase 3) need the same summarization logic. The `formatters.py` module created in Phase 2 should live in core `trellis` (not in the MCP package) so the SDK can reuse it.

**Recommended location:** `src/trellis/retrieve/formatters.py`

### Integration with Architecture Plan
- Phase 3 (SDK) depends on the REST API from architecture plan Phase 2
- Phase 2 (MCP Macro Tools) should use the service layer extracted in architecture plan Phase 2
- Token observability (Phase 4) should use the EventLog and telemetry from architecture plan Phase 3
