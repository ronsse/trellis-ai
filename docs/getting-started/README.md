# Getting Started with Trellis

A 5-10 minute on-ramp. Pick the path that matches what you want to do.

> **Wiring an external agent system into Trellis?** Start at
> [integrate-your-agent.md](integrate-your-agent.md) — the one-page decision
> tree (MCP / Python SDK / REST), each path with concrete steps and a
> "how you know it worked" check.

| If you want to... | Start here |
|---|---|
| **Integrate any agent (decision tree)** | **[integrate-your-agent.md](integrate-your-agent.md)** |
| Plug Trellis into Claude Code | [mcp-claude-code.md](mcp-claude-code.md) |
| Plug Trellis into Cursor | [mcp-cursor.md](mcp-cursor.md) |
| Plug Trellis into Claude Desktop | [mcp-claude-desktop.md](mcp-claude-desktop.md) |
| Use the Python SDK in your own agent | [../../examples/sdk_local_demo.py](../../examples/sdk_local_demo.py) |
| Wrap Trellis tools in LangGraph | [../../examples/integrations/langgraph/README.md](../../examples/integrations/langgraph/README.md) |
| Run the REST API as a shared service | see "Running a shared server" below |
| Run Trellis server-side (API/UI + curation workers) | [running-trellis.md](running-trellis.md) — the operating runbook |
| Set up for a team / data platform / production | [setup-decisions.md](setup-decisions.md) — the human decisions to make |
| Browse all macro tools and CLI commands | [../agent-guide/operations.md](../agent-guide/operations.md) |

## What is Trellis?

A **structured experience store for AI agents**. Three jobs:

1. **Record traces** of what agents did (steps, tool calls, outcomes).
2. **Build a knowledge graph** of typed entities and relationships.
3. **Retrieve token-budgeted context packs** before the agent starts new work.

Agents use it via an MCP server (11 macro tools), a REST API, a Python SDK, or a CLI. Same data underneath, four interfaces on top.

## 60-second quickstart

```bash
pip install -e ".[dev]"        # or: pip install trellis-ai
trellis admin init             # creates ~/.config/trellis/config.yaml + SQLite stores
trellis demo load              # seeds ~66 realistic items (optional but recommended)
trellis admin stats            # confirm everything wired up
```

You now have a working substrate. Add `--scope project` to `init` if you'd rather have stores in `./.trellis/` next to your code.

> Going past a local sandbox? Before a team / data-platform / production setup, walk [setup-decisions.md](setup-decisions.md) — the choices (domains & ontology, domain ownership, API security) that the default install never prompts for.

## Hands-on: 5-minute walkthrough

1. **Confirm the substrate is alive.**

   ```bash
   trellis admin health --format json
   ```

2. **Search the demo data.**

   ```bash
   trellis retrieve search "rate limit" --format json
   ```

3. **Ingest a trace.**

   ```bash
   cat <<'EOF' > /tmp/trace.json
   {
     "source": "manual.demo",
     "intent": "Try ingesting a trace",
     "steps": [{"step_type": "note", "name": "first_step",
                "result": {"ok": true}}],
     "outcome": {"status": "success", "summary": "It worked."},
     "context": {"domain": "general"}
   }
   EOF
   trellis ingest trace --file /tmp/trace.json --format json
   ```

4. **Assemble a context pack.**

   ```bash
   trellis retrieve pack \
     --intent "improve transient failure handling" \
     --domain backend \
     --max-items 20 \
     --format json
   ```

5. **Wire it into your agent.** Pick an integration above (MCP, SDK, LangGraph) and follow the linked guide.

## In-process MCP vs. running a shared server

| | In-process MCP | Shared server |
|---|---|---|
| MCP server in Claude Code (single user) | recommended | possible |
| SDK / REST clients (scripts, CI, Python agents) | — | required |
| Multiple agents share one substrate | — | recommended |
| Stores on Postgres / pgvector / S3 | possible | recommended |
| Process isolation between agent and store | — | yes |

The MCP server (`trellis-mcp`) opens the stores **in-process** — no separate
server needed for a single Claude Code user. The **SDK is HTTP-only**: it always
talks to a running `trellis admin serve` (there is no in-process SDK mode), so
scripts, CI, and Python agent frameworks need the shared server.

Default stores are local SQLite. Switch backends in
`~/.config/trellis/config.yaml`:

```yaml
knowledge:
  vector: { backend: pgvector }
  blob: { backend: s3, bucket: ${TRELLIS_S3_BUCKET} }
operational:
  trace: { backend: postgres, dsn: ${TRELLIS_OPERATIONAL_PG_DSN} }
```

Start the REST API (and web UI) with:

```bash
trellis admin serve --port 8420
```

## Next steps

- **Run it server-side** with [running-trellis.md](running-trellis.md) — the operating runbook for the API/UI and the curation/learning workers, their autonomy tiers, the human-in-the-loop steps, and a minimal single-host setup with a verification checklist. Scheduler recipes live in [../deployment/scheduled-curation.md](../deployment/scheduled-curation.md).
- **Make the setup decisions** in [setup-decisions.md](setup-decisions.md) before any team / data-platform / production rollout — domains & ontology, domain ownership, API security.
- **Read the playbooks** in [../agent-guide/playbooks.md](../agent-guide/playbooks.md) — task-shaped recipes for the most common workflows.
- **Skim the schemas** in [../agent-guide/schemas.md](../agent-guide/schemas.md) so you know what shape data takes.
- **Run an example** from [../../examples/](../../examples/).
- **Install the template skills** with `trellis admin install-skills user` (or
  `trellis admin quickstart --with-skills user`) — see [../../skills/](../../skills/).
