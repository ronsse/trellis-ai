# Trellis — OpenClaw Integration

Add structured institutional memory to your OpenClaw agents. Trellis provides traces, precedents, a knowledge graph, temporal versioning, and governed mutations — complementing OpenClaw's built-in memory with structured knowledge.

## Prerequisites

- Python 3.11+
- OpenClaw with MCP support

## Quick Start

1. Install Trellis:

```bash
pip install trellis-ai
# or from source
pip install -e ".[dev]"
```

2. Initialize the store:

```bash
trellis admin init
```

3. Add to your `openclaw.json`:

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

4. Restart OpenClaw. Your agent now has 8 macro tools for structured memory.

## Alternative: ClawHub Install

```bash
clawhub install trellis-ai
```

This installs the skill and configures the MCP server automatically.

## What Your Agent Gets

8 high-level tools returning token-budgeted markdown:

| Tool | Purpose |
|------|---------|
| `get_context` | Search docs + graph + traces, return summarized context pack |
| `save_experience` | Record a trace of completed work |
| `save_knowledge` | Create entity + optional relationship in the knowledge graph |
| `save_memory` | Store a document for later retrieval |
| `get_lessons` | List precedents (proven patterns from past work) |
| `get_graph` | Explore entity neighborhood in the knowledge graph |
| `record_feedback` | Record task success/failure for quality tracking |
| `search` | Combined document + entity search |

All tools accept `max_tokens` (default 2000) for context window budgeting. Responses are markdown, not raw JSON.

## Configuration

By default, XPG uses SQLite for all stores (zero configuration). For advanced setups, create `~/.config/trellis/config.yaml`:

```yaml
stores:
  vector:
    backend: lancedb    # serverless ANN, recommended for local use
  # graph:
  #   backend: postgres
  #   dsn: postgresql://user:pass@host/db
```

See the [main repository](https://github.com/ronsse/trellis-ai) for full configuration options.

## Verifying the Setup

```bash
# Check that the MCP server starts
trellis-mcp --help

# Check store health
trellis admin health

# Check store stats
trellis admin stats
```

## Further Reading

- [Agent Guide — Operations](../../docs/agent-guide/operations.md) — Full CLI, REST, MCP, and SDK reference
- [Agent Guide — Playbooks](../../docs/agent-guide/playbooks.md) — Step-by-step procedures including OpenClaw setup
- [Agent Guide — Schemas](../../docs/agent-guide/schemas.md) — All data models
- [Agent Guide — Trace Format](../../docs/agent-guide/trace-format.md) — How to construct valid traces
