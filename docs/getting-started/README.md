# Getting Started with Trellis

A 5-10 minute on-ramp. Pick the path that matches what you want to do.

| If you want to... | Start here |
|---|---|
| Plug Trellis into Claude Code | [mcp-claude-code.md](mcp-claude-code.md) |
| Plug Trellis into Cursor | [mcp-cursor.md](mcp-cursor.md) |
| Plug Trellis into Claude Desktop | [mcp-claude-desktop.md](mcp-claude-desktop.md) |
| Use the Python SDK in your own agent | [../../examples/sdk_local_demo.py](../../examples/sdk_local_demo.py) |
| Wrap Trellis tools in LangGraph | [../../examples/integrations/langgraph/README.md](../../examples/integrations/langgraph/README.md) |
| Run the REST API as a shared service | see "Remote Mode" below |
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
     --max-tokens 1500 \
     --format json
   ```

5. **Wire it into your agent.** Pick an integration above (MCP, SDK, LangGraph) and follow the linked guide.

## Local mode vs. Remote mode

| | Local | Remote |
|---|---|---|
| Use the SDK directly with no server | yes | — |
| MCP server in Claude Code (single user) | recommended | possible |
| Multiple agents share one substrate | — | recommended |
| Stores on Postgres / pgvector / S3 | — | required |
| Process isolation between agent and store | — | yes |

Default is local SQLite. Switch backends in `~/.config/trellis/config.yaml`:

```yaml
stores:
  trace: { backend: postgres, dsn: ${TRELLIS_PG_DSN} }
  vector: { backend: pgvector }
  blob: { backend: s3, bucket: ${TRELLIS_S3_BUCKET} }
```

Start the REST API with:

```bash
trellis admin serve --port 8420
```

## Next steps

- **Read the playbooks** in [../agent-guide/playbooks.md](../agent-guide/playbooks.md) — task-shaped recipes for the most common workflows.
- **Skim the schemas** in [../agent-guide/schemas.md](../agent-guide/schemas.md) so you know what shape data takes.
- **Run an example** from [../../examples/](../../examples/).
- **Drop the template skills** in [../../skills/](../../skills/) into your Claude Code or Cursor setup.
