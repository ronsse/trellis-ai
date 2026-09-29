# MCP Setup — Claude Code

Trellis ships an MCP server (`trellis-mcp`) that exposes 16 macro tools to Claude Code:

- **Core (10):** `get_context`, `save_experience`, `save_knowledge`, `save_memory`, `get_lessons`, `get_graph`, `get_items`, `get_file_context`, `record_feedback`, `search`.
- **Sectioned context (3):** `get_objective_context`, `get_task_context`, `get_sectioned_context`.
- **Structured (3):** `record_observation`, `query_observations`, `execute_mutation` — these return JSON rather than markdown.

All return token-budgeted markdown sized for the agent's context window.

## Install

```bash
pip install -e ".[dev]"   # or: pip install trellis-ai
trellis admin quickstart
```

`quickstart` does two things:

1. Initializes SQLite stores under `~/.trellis/` (or `$TRELLIS_CONFIG_DIR` / `$TRELLIS_DATA_DIR`, if set).
2. Prints the command that registers `trellis-mcp` with Claude Code. It does not run it; run it once yourself:

   ```bash
   claude mcp add --scope user trellis -- trellis-mcp
   ```

Restart Claude Code after registering so it picks up the new server.

Earlier versions of `quickstart` wrote an `mcpServers` entry into `~/.claude/settings.json` (or `.claude/settings.local.json` with `--scope project`) and reported the server as registered. Claude Code does not read MCP servers from either file, so that entry did nothing and you can delete it.

To install the drop-in agent skills at the same time, add `--with-skills user`
(global, `~/.claude/skills/`) or `--with-skills project` (`./.claude/skills/`):

```bash
trellis admin quickstart --with-skills user
```

See [skills/](../../skills/) for what each skill does and
`trellis admin install-skills --help` to install or update them on their own.

## Per-project install

If you'd rather keep stores beside your code (so each project has its own memory):

```bash
trellis admin quickstart --scope project
```

Stores land in `./.trellis/`. The command `quickstart` prints registers a local-scope server (private to you, in this directory) whose env points `TRELLIS_CONFIG_DIR` at the project's absolute `.trellis` path, so the MCP server reads from the project. Inside the project a local-scope `trellis` takes precedence over a user-scope one, so the two installs can coexist.

The CLI targets the project stores only when `TRELLIS_CONFIG_DIR` points there as well, for example `TRELLIS_CONFIG_DIR=$PWD/.trellis trellis admin health`.

## Manual configuration

`claude mcp add` is how Claude Code registers MCP servers; it records them in `~/.claude.json`. These are the commands `quickstart` prints, except that for a project it spells out the absolute path where this uses `$PWD`:

```bash
# Global: available in all your projects
claude mcp add --scope user trellis -- trellis-mcp

# One project, stores in ./.trellis: run from the project root
claude mcp add --scope local trellis -e TRELLIS_CONFIG_DIR="$PWD/.trellis" -- trellis-mcp
```

Keep the option order: `-e` accepts several values, so it goes after the server name and before `--`.

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
| Tools don't appear in Claude Code | Run `claude mcp list`. If `trellis` is absent, run the command `quickstart` printed, then restart Claude Code. If `claude mcp add` says the server already exists, remove it first with `claude mcp remove trellis --scope user` (`--scope local` for a project install). |
| `get_context` returns "No relevant context" | Load demo data (`trellis demo load`) or ingest some real traces. |
| Permission errors writing to `~/.trellis/` | Pass `--scope project` to keep stores in the current directory. |

## See also

- [examples/mcp_claude_code.md](../../examples/mcp_claude_code.md) — example prompts.
- [docs/agent-guide/operations.md](../agent-guide/operations.md) — MCP tool reference.
- [skills/](../../skills/) — drop-in skill templates.
