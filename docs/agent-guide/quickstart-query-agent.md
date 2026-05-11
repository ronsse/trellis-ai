# Quickstart: a Query-Engine Agent on Trellis

> **Who this is for:** anyone trying Trellis for the first time, especially with an LLM-powered query-engine agent (Claude Code, LangGraph, a custom orchestration layer) in mind. Gets you from `git clone` to "the agent retrieves real routing metadata from a real graph" in about 5 minutes.

> **What this covers:** install, seed, explore via CLI, run a Python sample agent, point Claude Code or another MCP-aware client at the running instance.

> **Before you read this:** the conceptual guides — [modeling-guide.md](modeling-guide.md), [extractor-authoring.md](extractor-authoring.md), [source-modeling-cookbook.md](source-modeling-cookbook.md), [freshness-and-curation.md](freshness-and-curation.md) — answer *why* the system is shaped the way it is. This doc is a worked example of *what* you do.

---

## 1. Install

```bash
git clone https://github.com/anthropics/trellis-ai.git
cd trellis-ai
uv pip install -e ".[dev]"
```

`[dev]` pulls in the extras you'll want for the demo (the workers package, which ships the dbt + OpenLineage extractors).

## 2. Initialize the local stores

```bash
trellis admin init
```

Creates `~/.config/trellis/config.yaml` and a stores directory with SQLite backends for each plane (graph, document, vector, blob, trace, event log). This is the substrate. No external services required.

## 3. Seed the graph

```bash
trellis demo load
```

Output (abridged):

```
Loading demo data...
  + 26 entities
  + 22 relationships
  + 6 traces
  + 3 evidence items
  + 4 documents
  + 3 precedents
  + 16 cold-start entities (24 edges) via extractor path

Done! Loaded 100 items into the knowledge graph.
```

Two layers of demo data land:

1. **Legacy narrative content** — hand-coded services, people, runbooks, incidents, precedents. Gives the demo a "real production" feel.
2. **Cold-start fixture** — a small dbt manifest (jaffle_shop) + OpenLineage events, loaded through the *real* extractor + governed mutation path. Same code path a production deployment uses. This is what proves the cold-start story works.

The cold-start fixture lives in [`examples/cold-start-fixture/`](../../examples/cold-start-fixture/) and is editable — see [the README there](../../examples/cold-start-fixture/README.md) for drift testing.

## 4. Verify via CLI

```bash
trellis retrieve entity user-api          # legacy fixture
trellis retrieve entity model.jaffle_shop.fct_orders   # cold-start fixture
```

The second command returns a dataset-shaped entity with cross-database routing properties:

```json
{
  "entity_id": "model.jaffle_shop.fct_orders",
  "entity_type": "dbt_model",
  "properties": {
    "name": "fct_orders",
    "schema": "marts",
    "database": "analytics",
    "description": "Fact table for orders...",
    "source_system": "snowflake",
    "database_name": "analytics",
    "schema_name": "marts",
    "physical_uri": "snowflake://analytics/marts/fct_orders",
    "materialized": "table"
  }
}
```

The `source_system` / `database_name` / `schema_name` / `physical_uri` quartet is the agent's contract for "how do I query this?" — populated automatically by the dbt extractor from the manifest's `metadata.adapter_type`. See [modeling-guide.md — cross-database routing properties](modeling-guide.md#cross-database-routing-properties-for-queryable-datasets).

## 5. Run the sample query agent

```bash
make -C examples/docker-demo demo
```

What it does:

1. Spins up an in-process Trellis (no separate server needed) via `trellis.testing.in_memory_client`.
2. Loads the cold-start fixture via the extractor pipeline.
3. Receives a hardcoded task intent: *"I need to query customer order history for analytics."*
4. Looks up the relevant `Dataset` entities by ID.
5. Prints the routing properties an agent would use to dispatch a real query.
6. Sketches the closing of the feedback loop.

Expected output (abridged):

```
================================================================
 1. Seed the graph (cold-start fixture)
================================================================
  dbt-manifest: 16/16 mutations applied
  openlineage: 24/24 mutations applied

================================================================
 3. Routing properties on the dataset entities
================================================================
  model.jaffle_shop.dim_customers:
    source_system: snowflake
    database_name: analytics
    schema_name: marts
    physical_uri: snowflake://analytics/marts/dim_customers
    description: Customer dimension with lifetime-value aggregations...
  model.jaffle_shop.fct_orders:
    source_system: snowflake
    database_name: analytics
    schema_name: marts
    physical_uri: snowflake://analytics/marts/fct_orders
    description: Fact table for orders...
```

The script source is in [`examples/docker-demo/sample_query_agent.py`](../../examples/docker-demo/sample_query_agent.py) — short, annotated, useful as a starting template for your own agent.

## 6. Point Claude Code at the local Trellis

The Trellis MCP server exposes a small surface of tools an agent calls during a task:

- `get_objective_context(intent, domain?)` — sectioned pack at the objective level (broad strategic context).
- `get_task_context(intent, ...)` — sectioned pack at the task level (focused tactical context).
- `record_feedback(pack_id, rating, label, comment)` — closes the variation-selection loop.
- `execute_mutation(...)` — submits a governed mutation (used sparingly by agents; most writes go through extractors).

Start the server:

```bash
trellis admin serve --port 8000
```

Then in your Claude Code or other MCP-aware tool, configure the MCP server URL as `http://localhost:8000/mcp`. The MCP tools become available to the agent.

For the full MCP setup walkthrough (config files, credential injection, IDE-specific instructions), see [`docs/agent-guide/operations.md`](operations.md).

## 7. Test schema drift detection

Edit `examples/cold-start-fixture/manifest.json` — change a description, add a model, or rename a schema. Then:

```bash
trellis extract refresh --source jaffle-dbt \
  --sources-file examples/cold-start-fixture/sources.yaml \
  --format json
```

Output includes the structured diff:

```json
{
  "status": "refreshed",
  "source": "jaffle-dbt",
  "entities_scanned": 8,
  "new_entities": 0,
  "changed_entities": 1,
  "diffs": [
    {
      "entity_id": "model.jaffle_shop.fct_orders",
      "diff": {
        "changed": {"description": ["V1 desc", "V2 desc"]}
      }
    }
  ]
}
```

Each changed entity also produces a `TAGS_REFRESHED` event in the EventLog, source=`extract.refresh:jaffle-dbt`. Agents subscribing to the EventLog stream invalidate cached pack content on these events.

See [`freshness-and-curation.md`](freshness-and-curation.md) for the broader refresh model — periodic re-run vs pushed events, wiring into cron / GHA / Airflow / K8s CronJob.

## 8. Where to go next

The four cornerstone docs answer the conceptual questions:

| Question | Read |
|---|---|
| What goes in the graph, vs document, vs blob? | [modeling-guide.md](modeling-guide.md) |
| How do I ingest from my own source (Jira, Confluence, Unity Catalog)? | [source-modeling-cookbook.md](source-modeling-cookbook.md) + [extractor-authoring.md](extractor-authoring.md) |
| How do I keep the graph fresh as production changes? | [freshness-and-curation.md](freshness-and-curation.md) |
| Where are the API / CLI / MCP references? | [operations.md](operations.md), [schemas.md](schemas.md) |

## Deployment shape (for a real install)

The in-memory demo above runs everything in-process. A real deployment splits into:

- **Trellis server**: `trellis admin serve` behind a reverse proxy. SQLite for dev; Postgres + pgvector or Neo4j for production substrates per [CLAUDE.md](../../CLAUDE.md).
- **Extractors**: per-source scripts run by your scheduler (cron / GHA / Airflow / K8s CronJob) calling `trellis extract refresh --source <name>`.
- **Agents**: connect via MCP (recommended for Claude Code, Claude Desktop, IDE plugins) or REST (`POST /api/v1/...`) or SDK (`trellis_sdk.TrellisClient`).
- **Optional event-stream sources**: push events directly at `POST /api/v1/extract/drafts`. No polling.

A docker-compose stack for this deployment shape is a documented v2 follow-up; the in-memory `make demo` proves the loop closes today.

---

## Troubleshooting

- **`make demo` fails with `trellis is not installed`.** Run `make -C examples/docker-demo install`, or `pip install -e .` from the repo root.
- **`trellis demo load` says "demo data already loaded".** Use `trellis demo load --force` to reload.
- **`trellis extract refresh --source X` errors with "source not declared".** Check `--sources-file` points at the right `sources.yaml`. The default is `./sources.yaml` relative to the cwd.
- **Sample agent shows `Doc-store search hits: 0`.** Expected — the agent script seeds entities directly without populating the document store. In a real deployment, run `trellis ingest dbt-manifest <path>` (which indexes descriptions as a side channel) or run a `get_objective_context` call which combines graph + document + vector search.
- **EventLog noise.** Set `TRELLIS_LOG_LEVEL=WARNING` (the sample agent does this automatically). Set to `DEBUG` to see the full mutation audit trail.
