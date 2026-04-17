# MCP Setup — Cursor

Cursor supports MCP servers via `~/.cursor/mcp.json` (global) or `<project>/.cursor/mcp.json` (project-scoped). Trellis works in either.

## Install

```bash
pip install -e ".[dev]"   # or: pip install trellis-ai
trellis admin init        # initializes ~/.config/trellis/ with SQLite stores
```

## Configure Cursor

Create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "trellis-ai": {
      "command": "trellis-mcp",
      "args": []
    }
  }
}
```

Restart Cursor. Open the chat panel and the 11 Trellis macro tools should appear in the tool list (8 core + 3 sectioned-context).

### Project-scoped install

Keep memory inside the project:

```bash
trellis admin init --scope project   # stores -> ./.trellis/
```

Then `<project>/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "trellis-ai": {
      "command": "trellis-mcp",
      "args": [],
      "env": { "TRELLIS_CONFIG_DIR": ".trellis" }
    }
  }
}
```

## Verify

```bash
trellis-mcp --help
trellis admin health
```

In Cursor, prompt: *"List your available MCP tools."* Confirm the 11 Trellis tools appear (`get_context`, `save_experience`, `save_knowledge`, `save_memory`, `get_lessons`, `get_graph`, `record_feedback`, `search`, plus `get_objective_context`, `get_task_context`, `get_sectioned_context`).

## Recommended `.cursorrules` snippet

Add this to `.cursorrules` (or your project rules) so Cursor uses Trellis automatically:

```markdown
## Institutional Memory (Trellis)

Before starting non-trivial work, call `get_context` with your task intent
to find prior art. After completing meaningful work, call `save_experience`
with a trace JSON. When you discover services, concepts, or patterns,
call `save_knowledge`. Mark task outcomes with `record_feedback`.
```

For drop-in skill templates, see [../../skills/](../../skills/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear | Confirm `~/.cursor/mcp.json` is valid JSON and restart Cursor. |
| `command not found` | Make sure the venv with `trellis-mcp` is on Cursor's `PATH`. The simplest fix is to use the absolute path: `"command": "/full/path/to/.venv/bin/trellis-mcp"`. |
| Empty results from `get_context` | Run `trellis demo load` or ingest your own traces first. |

## See also

- [docs/agent-guide/operations.md](../agent-guide/operations.md) — MCP tool reference.
- [examples/mcp_claude_code.md](../../examples/mcp_claude_code.md) — example prompts (apply equally to Cursor).
