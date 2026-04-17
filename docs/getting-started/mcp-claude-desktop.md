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

In a new chat, ask: *"What MCP tools are available?"* You should see the 11 Trellis macro tools (8 core + 3 sectioned-context).

If something's wrong, check the Claude Desktop logs:

| OS | Log path |
|---|---|
| macOS | `~/Library/Logs/Claude/` |
| Windows | `%APPDATA%\Claude\logs\` |

Look for `mcp-server-trellis-ai.log`.

## Recommended system-prompt addition

Claude Desktop doesn't have per-project rules, so add usage guidance via the model's system prompt or by saying it explicitly at the start of a session:

> Before starting non-trivial work, call `get_context`. After completing work, call `save_experience` and `record_feedback`. Use `save_knowledge` for important entities.

For drop-in skill templates, see [../../skills/](../../skills/).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found` in logs | Use an absolute path (see PATH gotcha above). |
| Tools don't appear after restart | Fully quit Claude Desktop (not just close window) and reopen. |
| `permission denied` writing stores | Pre-create `~/.config/trellis/` and run `trellis admin init` once manually. |

## See also

- [docs/agent-guide/operations.md](../agent-guide/operations.md) — MCP tool reference.
- [skills/](../../skills/) — drop-in skill templates.
