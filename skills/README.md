# Trellis Skill Templates

> **Status: preview.** These skills are in flux while parallel work lands. The `SKILL.md` content is intentionally slim so drift stays manageable, but expect minor revisions to prompts and tool-call shapes before the next minor release.

Drop-in skill files for Claude Code (and other agent frameworks that follow the `SKILL.md` convention). Each skill is a self-contained directory with a `SKILL.md` describing when and how the agent should use it.

## What's here

| Skill | Purpose |
|---|---|
| [retrieve-before-task](retrieve-before-task/) | Before starting non-trivial work, pull a token-budgeted context pack from Trellis and inject it into the task. |
| [record-after-task](record-after-task/) | After completing meaningful work, write a structured trace and mark its outcome. |
| [link-evidence](link-evidence/) | When the agent learns something durable, store it as a memory + entity in the knowledge graph. |

## Installing into Claude Code

Claude Code loads skills from `~/.claude/skills/` (user-scoped) or `<project>/.claude/skills/` (project-scoped).

**User-scoped (works across every project):**

```bash
mkdir -p ~/.claude/skills
cp -r skills/retrieve-before-task ~/.claude/skills/
cp -r skills/record-after-task ~/.claude/skills/
cp -r skills/link-evidence ~/.claude/skills/
```

**Project-scoped (only for one repo):**

```bash
mkdir -p .claude/skills
cp -r skills/* .claude/skills/
```

Restart Claude Code and the skills will be discoverable as `/retrieve-before-task`, `/record-after-task`, `/link-evidence`.

## Prerequisites

All three skills assume the Trellis MCP server is running and registered with your agent. See [docs/getting-started/mcp-claude-code.md](../docs/getting-started/mcp-claude-code.md) for setup. The one-liner:

```bash
trellis admin quickstart
```

## Customizing

Each `SKILL.md` is plain markdown with YAML frontmatter — edit freely. Common tweaks:

- **Domain hints**: pre-fill `domain="backend"` or similar in the example tool calls.
- **Token budgets**: lower `max_tokens` if you want leaner injections, or raise for deep-research workflows.
- **When to trigger**: rewrite the `description` field so your agent invokes the skill in the moments you care about.

## Using these patterns elsewhere

The skills are written as Claude Code SKILL.md files, but the *patterns* — retrieve before acting, record after success, link discovered evidence — apply to any agent framework. For LangGraph, see [examples/langgraph_agent.py](../examples/langgraph_agent.py). For OpenClaw, see [examples/integrations/openclaw/SKILL.md](../examples/integrations/openclaw/SKILL.md).
