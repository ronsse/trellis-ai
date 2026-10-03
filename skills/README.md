# Trellis Skill Templates

> **Status: preview.** These skills are in flux while parallel work lands. The `SKILL.md` content is intentionally slim so drift stays manageable, but expect minor revisions to prompts and tool-call shapes before the next minor release.

Drop-in skill files for Claude Code (and other agent frameworks that follow the `SKILL.md` convention). Each skill is a self-contained directory with a `SKILL.md` describing when and how the agent should use it.

> **Where the files live.** The canonical copies ship inside the installed package at `src/trellis_cli/skills/` so they travel in the wheel (`pip install trellis-ai`), not just a repo checkout. This `skills/` directory is the public pointer to them. Install them with the CLI — you don't need to copy paths by hand.

## What's here

| Skill | Purpose |
|---|---|
| retrieve-before-task | Before non-trivial work, pull a token-budgeted context pack from Trellis — and again after a pivot, after compaction, and in every dispatched subagent. |
| record-after-task | After meaningful work, write a schema-valid trace, grade the packs that were served by item id, and save any durable environment fact as knowledge. |
| link-evidence | When the agent learns something durable, store it with `save_knowledge` anchored to the graph entity it is about, and check the reply to confirm the link. |

## Installing into Claude Code

The fastest path is one command — it copies all three skills out of the installed package, so it works from a `pip install` as well as a repo checkout.

**User-scoped (works across every project):**

```bash
trellis admin install-skills user
```

**Project-scoped (only for one repo):**

```bash
trellis admin install-skills project
```

`user` writes to `~/.claude/skills/`; `project` writes to `<cwd>/.claude/skills/`. Both are idempotent — a skill that already exists is skipped and reported. Pass `--force` to overwrite, and `--format json` for machine-readable output.

Doing a full setup at once? `trellis admin quickstart --with-skills user` initializes the stores **and** installs the skills, then prints the `claude mcp add` command that registers the MCP server. Run that command once.

Restart Claude Code afterward and the skills will be discoverable as `/retrieve-before-task`, `/record-after-task`, `/link-evidence`.

### Manual fallback

If you'd rather copy the files yourself (e.g. into a non-Claude-Code agent), they live under `src/trellis_cli/skills/` in the repo:

```bash
mkdir -p ~/.claude/skills
cp -r src/trellis_cli/skills/retrieve-before-task ~/.claude/skills/
cp -r src/trellis_cli/skills/record-after-task ~/.claude/skills/
cp -r src/trellis_cli/skills/link-evidence ~/.claude/skills/
```

## Prerequisites

All three skills assume the Trellis MCP server is running and registered with your agent. See [docs/getting-started/mcp-claude-code.md](../docs/getting-started/mcp-claude-code.md) for setup. This sets up the stores and skills together, then prints the command that registers the MCP server:

```bash
trellis admin quickstart --with-skills user
```

## Customizing

Each `SKILL.md` is plain markdown with YAML frontmatter — edit freely **after installing** (the installed copy under `~/.claude/skills/` is yours to change; the packaged source is the template). Common tweaks:

- **Domain hints**: pre-fill `domain="backend"` or similar in the example tool calls.
- **Token budgets**: add `max_tokens` to the `get_context` call (default 2000) — lower for leaner injections, higher for deep-research workflows.
- **When to trigger**: rewrite the `description` field so your agent invokes the skill in the moments you care about.

## Using these patterns elsewhere

The skills are written as Claude Code SKILL.md files, but the *patterns* — retrieve before acting, record after success, link discovered evidence — apply to any agent framework. For LangGraph, see [examples/langgraph_agent.py](../examples/langgraph_agent.py). For OpenClaw, see [examples/integrations/openclaw/SKILL.md](../examples/integrations/openclaw/SKILL.md).
