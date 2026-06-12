# Trellis Skill Templates

> **Status: preview.** These skills are in flux while parallel work lands. The `SKILL.md` content is intentionally slim so drift stays manageable, but expect minor revisions to prompts and tool-call shapes before the next minor release.

Drop-in skill files for Claude Code (and other agent frameworks that follow the `SKILL.md` convention). Each skill is a self-contained directory with a `SKILL.md` describing when and how the agent should use it.

> **Where the files live.** The canonical copies ship inside the installed package at `src/trellis_cli/skills/` so they travel in the wheel (`pip install trellis-ai`), not just a repo checkout. This `skills/` directory is the public pointer to them. Install them with the CLI — you don't need to copy paths by hand.

## What's here

| Skill | Purpose |
|---|---|
| retrieve-before-task | Before starting non-trivial work, pull a token-budgeted context pack from Trellis and inject it into the task. |
| record-after-task | After completing meaningful work, write a structured trace and mark its outcome. |
| link-evidence | When the agent learns something durable, store it as a memory + entity in the knowledge graph. |

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

Doing a full setup at once? `trellis admin quickstart --with-skills user` initializes the stores, registers the MCP server, **and** installs the skills in a single command.

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

All three skills assume the Trellis MCP server is running and registered with your agent. See [docs/getting-started/mcp-claude-code.md](../docs/getting-started/mcp-claude-code.md) for setup. The one-liner that does stores + MCP + skills together:

```bash
trellis admin quickstart --with-skills user
```

## Customizing

Each `SKILL.md` is plain markdown with YAML frontmatter — edit freely **after installing** (the installed copy under `~/.claude/skills/` is yours to change; the packaged source is the template). Common tweaks:

- **Domain hints**: pre-fill `domain="backend"` or similar in the example tool calls.
- **Token budgets**: lower `max_tokens` if you want leaner injections, or raise for deep-research workflows.
- **When to trigger**: rewrite the `description` field so your agent invokes the skill in the moments you care about.

## Using these patterns elsewhere

The skills are written as Claude Code SKILL.md files, but the *patterns* — retrieve before acting, record after success, link discovered evidence — apply to any agent framework. For LangGraph, see [examples/langgraph_agent.py](../examples/langgraph_agent.py). For OpenClaw, see [examples/integrations/openclaw/SKILL.md](../examples/integrations/openclaw/SKILL.md).
