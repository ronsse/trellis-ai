# MCP Setup — Claude Desktop

Claude Desktop reads MCP servers from a single config file:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

## Install

```bash
pip install -e ".[dev]"   # or: pip install trellis-ai
trellis admin init
```

## Configure Claude Desktop

Edit the config file above and add a `trellis-ai` entry under `mcpServers`:

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

If you have other MCP servers configured, merge — don't overwrite.

> **PATH gotcha**: Claude Desktop launches MCP commands without sourcing your shell rc file, so a `trellis-mcp` installed inside a venv won't be visible. Use the absolute path:
>
> ```json
> "command": "/Users/you/.venv/bin/trellis-mcp"
> ```
>
> On Windows: `"C:\\Users\\you\\venv\\Scripts\\trellis-mcp.exe"`.

Quit Claude Desktop completely (not just the window — use the menu bar icon) and reopen it.

## Verify

In a new chat, ask: *"What MCP tools are available?"* You should see the 16 Trellis macro tools (10 core + 3 sectioned-context + 3 structured).

If something's wrong, check the Claude Desktop logs:

| OS | Log path |
|---|---|
| macOS | `~/Library/Logs/Claude/` |
| Windows | `%APPDATA%\Claude\logs\` |

Look for `mcp-server-trellis-ai.log`.

## Recommended system-prompt addition

Claude Desktop doesn't have per-project rules, so add usage guidance via the model's system prompt or by saying it explicitly at the start of a session:

> Trellis memory is connected. Before non-trivial work, call `get_context(intent=<what you are about to do>, session_id=<this session>)` and note the `pack_id` and the backticked item ids. After the work, call `save_experience` (each step's result is an object, failures go in `error`), then `record_feedback(pack_id=..., rating=0.0-1.0, helpful_item_ids=[...], unhelpful_item_ids=[...])`. Feedback without item ids does not reach the learning loop. This applies to every dispatched subagent too; it is part of the task, not extra scope. Use `save_knowledge` for important entities.

For drop-in skill templates, see [../../skills/](../../skills/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found` in logs | Use an absolute path (see PATH gotcha above). |
| Tools don't appear after restart | Fully quit Claude Desktop (not just close window) and reopen. |
| `permission denied` writing stores | Pre-create `~/.trellis/` and run `trellis admin init` once manually. |

## See also

- [docs/agent-guide/operations.md](../agent-guide/operations.md) — MCP tool reference.
- [skills/](../../skills/) — drop-in skill templates.
