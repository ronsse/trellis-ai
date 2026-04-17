# MCP Setup — Claude Code

Trellis ships an MCP server (`trellis-mcp`) that exposes 11 macro tools to Claude Code:

- **Core (8):** `get_context`, `save_experience`, `save_knowledge`, `save_memory`, `get_lessons`, `get_graph`, `record_feedback`, `search`.
- **Sectioned context (3):** `get_objective_context`, `get_task_context`, `get_sectioned_context`.

All return token-budgeted markdown sized for the agent's context window.

## One-command install

```bash
pip install -e ".[dev]"   # or: pip install trellis-ai
trellis admin quickstart
```

`quickstart` does three things:

1. Initializes SQLite stores under `~/.config/trellis/`.
2. Locates your Claude Code `settings.json` (`~/.claude/settings.json` on macOS/Linux, `%USERPROFILE%\.claude\settings.json` on Windows).
3. Adds an `mcpServers.trellis-ai` entry pointing at `trellis-mcp`.

Restart Claude Code after `quickstart` so it picks up the new server.

## Per-project install

If you'd rather keep stores beside your code (so each project has its own memory):

```bash
trellis admin quickstart --scope project
```

Stores land in `./.trellis/`, and the entry written to `.claude/settings.json` includes `TRELLIS_CONFIG_DIR` so the MCP server reads from the project directory.

## Manual configuration

If you'd rather edit settings yourself, add this to `~/.claude/settings.json`:

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

For project scope, also set the env var:

```json
{
  "mcpServers": {
    "trellis-ai": {
      "command": "trellis-mcp",
      "args": [],
      "env": { "TRELLIS_CONFIG_DIR": "${workspaceFolder}/.trellis" }
    }
  }
}
```

## Verify the install

```bash
trellis-mcp --help        # confirms the binary is on PATH
trellis admin health      # confirms stores are healthy
```

In Claude Code, ask: *"List your available tools."* You should see `get_context`, `save_experience`, etc. in the response.

## Recommended CLAUDE.md addition

Drop this into your project's `CLAUDE.md` so the agent reaches for Trellis without being told each session:

```markdown
## Institutional Memory (Trellis)

Before starting non-trivial work, call `get_context` with your task intent.
After completing meaningful work, call `save_experience` with a trace and
follow up with `record_feedback`. When you discover services, concepts, or
patterns worth tracking, call `save_knowledge`.
```

For drop-in template skills (a self-contained version of the above plus structured prompts), see [../../skills/](../../skills/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `trellis-mcp: command not found` | Reinstall with `pip install -e ".[dev]"` from the repo root, or check that the active venv is on PATH. |
| Tools don't appear in Claude Code | Confirm the entry exists in `settings.json` and restart Claude Code. |
| `get_context` returns "No relevant context" | Load demo data (`trellis demo load`) or ingest some real traces. |
| Permission errors writing to `~/.config/trellis/` | Pass `--scope project` to keep stores in the current directory. |

## See also

- [examples/mcp_claude_code.md](../../examples/mcp_claude_code.md) — example prompts.
- [docs/agent-guide/operations.md](../agent-guide/operations.md) — MCP tool reference.
- [skills/](../../skills/) — drop-in skill templates.
