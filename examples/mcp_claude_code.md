# Using Trellis from Claude Code (MCP)

> **Status: preview.** This walkthrough is in flux while parallel work lands. Tool names, signatures, and setup commands may change before the next minor release.

End-to-end walkthrough of installing the Trellis MCP server in Claude Code and using its eleven macro tools in real prompts.

## Setup (one command)

```bash
trellis admin quickstart
```

This initializes the local SQLite stores and registers `trellis-mcp` in your Claude Code `settings.json` under `mcpServers`. Restart Claude Code afterwards so it picks up the new server.

For per-project config (stores live in `./.trellis/`):

```bash
trellis admin quickstart --scope project
```

## What you get

Eleven macro tools, all returning **token-budgeted markdown** (not raw JSON).

**Core tools** — the daily-driver eight:

| Tool | When to use |
|------|-------------|
| `get_context` | Before any non-trivial task — pulls relevant docs / entities / traces. |
| `search` | Targeted lookup when you know the keyword or entity name. |
| `save_experience` | After completing meaningful work — records a trace. |
| `save_knowledge` | When you discover a service, concept, or pattern worth tracking. |
| `save_memory` | Quick "remember this" for snippets and findings. |
| `get_lessons` | Pull proven patterns / precedents for a domain. |
| `get_graph` | Explore the neighborhood of an entity by id. |
| `record_feedback` | Mark a trace as success/failure to feed the dual-loop. |

**Sectioned-context tools** — for richer multi-step retrieval:

| Tool | When to use |
|------|-------------|
| `get_objective_context` | Once at the start of a workflow — assembles domain knowledge + operational context with a fixed two-section layout. |
| `get_task_context` | Per step inside a workflow — scopes retrieval to specific entity ids. |
| `get_sectioned_context` | When you want full control — define your own sections with per-section affinities and budgets. |

## Example prompts

### Retrieve before acting

> Before we start, use `get_context` to check what's been done with the auth service in the backend domain. Then summarize the patterns you'd reuse.

### Save what you learned

> We just finished hardening the orders API with rate limiting. Use `save_experience` to record the trace, then `save_knowledge` to register `rate-limiter-v2` as a pattern entity related to `orders-api`.

### Pull lessons for a domain

> Use `get_lessons(domain="data-platform", max_tokens=1500)` and walk me through the top three precedents.

### Explore the graph

> Look up `orders-api` with `search`, then call `get_graph` on its id at depth 2 to see what it depends on.

## Recommended CLAUDE.md snippet

Adding this to your project's `CLAUDE.md` makes the agent reach for Trellis automatically:

```markdown
## Institutional Memory (Trellis)

Before starting non-trivial work, call `get_context` with your task intent.
After completing meaningful work, call `save_experience` with a trace.
When you discover services, concepts, or patterns worth tracking, call
`save_knowledge`. Mark task outcomes with `record_feedback` so the
quality loop learns.
```

## Troubleshooting

- **Server didn't start**: run `trellis-mcp --help` directly. If `command not found`, the install didn't put it on `$PATH` — `pip install -e ".[dev]"` from the repo root will fix it.
- **Tools don't appear in Claude Code**: confirm the entry exists in `~/.claude/settings.json` under `mcpServers.trellis-ai`, then restart Claude Code.
- **`get_context` returns nothing**: load demo data with `trellis demo load` so retrieval has something to chew on, or ingest some real traces first.

See [docs/getting-started/mcp-claude-code.md](../docs/getting-started/mcp-claude-code.md) for a deeper setup reference.
