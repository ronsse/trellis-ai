# Using Trellis from Claude Code (MCP)

> **Status: preview.** This walkthrough is in flux while parallel work lands. Tool names, signatures, and setup commands may change before the next minor release.

End-to-end walkthrough of installing the Trellis MCP server in Claude Code and using its macro tools in real prompts.

## Setup

```bash
trellis admin quickstart
```

This initializes the local SQLite stores and prints the `claude mcp add` command that registers `trellis-mcp` with Claude Code. Run that command once, then restart Claude Code so it picks up the new server.

For per-project config (stores live in `./.trellis/`):

```bash
trellis admin quickstart --scope project
```

## What you get

Sixteen macro tools. The eleven in these tables return **markdown** (not raw JSON); [operations.md](../docs/agent-guide/operations.md#mcp-macro-tools) lists all sixteen, including the three that return JSON.

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
| `record_feedback` | Grade a served pack: `pack_id`, `rating`, and the `helpful_item_ids` / `unhelpful_item_ids` you used. |

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

Trellis memory is connected. Before non-trivial work, call
`get_context(intent=<what you are about to do>, session_id=<this session>)`
and note the `pack_id` and the backticked item ids. After the work, call
`save_experience` (each step's result is an object, failures go in `error`),
then `record_feedback(pack_id=..., rating=0.0-1.0, helpful_item_ids=[...],
unhelpful_item_ids=[...])`. Feedback without item ids does not reach the
learning loop. This applies to every dispatched subagent too; it is part of
the task, not extra scope. When you discover services, concepts, or patterns
worth tracking, call `save_knowledge`.
```

## Troubleshooting

- **Server didn't start**: run `trellis-mcp --help` directly. If `command not found`, the install didn't put it on `$PATH` — `pip install -e ".[dev]"` from the repo root will fix it.
- **Tools don't appear in Claude Code**: run `claude mcp list`. If `trellis` is absent, run the command `quickstart` printed, then restart Claude Code.
- **`get_context` returns nothing**: load demo data with `trellis demo load` so retrieval has something to chew on, or ingest some real traces first.

See [docs/getting-started/mcp-claude-code.md](../docs/getting-started/mcp-claude-code.md) for a deeper setup reference.
