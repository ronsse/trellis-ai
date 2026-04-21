<p align="center">
  <img src="assets/trellis-logo.svg" alt="Trellis" width="420">
</p>

# Trellis

[![Tests](https://github.com/ronsse/trellis-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/ronsse/trellis-ai/actions/workflows/tests.yml)
[![Lint](https://github.com/ronsse/trellis-ai/actions/workflows/lint.yml/badge.svg)](https://github.com/ronsse/trellis-ai/actions/workflows/lint.yml)
[![Type Check](https://github.com/ronsse/trellis-ai/actions/workflows/typecheck.yml/badge.svg)](https://github.com/ronsse/trellis-ai/actions/workflows/typecheck.yml)
[![PyPI](https://img.shields.io/pypi/v/trellis-ai.svg)](https://pypi.org/project/trellis-ai/)
[![Python](https://img.shields.io/pypi/pyversions/trellis-ai.svg)](https://pypi.org/project/trellis-ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ronsse/trellis-ai/blob/main/LICENSE)

**Shared context substrate for AI agents. Retrieval that learns what's useful. Runs local or cloud.**

Trellis is the layer that sits between your agents and the context they need to do work. Agents write **immutable traces** of what they did, read **token-budgeted context packs** before starting new tasks, and the system **tunes retrieval from feedback** — low-signal items get suppressed, high-signal patterns get promoted to reusable precedents. Multiple agents share the same substrate, so institutional knowledge compounds instead of evaporating at the end of each session.

Not a vector DB, not per-conversation "memory" — it's the **cross-agent knowledge layer** that gives a team of agents a shared past.

## Quickstart — 60 seconds

```bash
pip install trellis-ai
trellis admin init          # write ~/.config/trellis/config.yaml + init SQLite stores
trellis demo load           # populate 66 realistic items: entities, traces, precedents
trellis admin serve         # open http://localhost:8420
```

You'll land on the dashboard. Try:

```bash
trellis retrieve search 'user-api'           # keyword + semantic search
trellis retrieve entity user-api             # entity with neighborhood
trellis retrieve traces --domain backend     # recent agent work in a domain
trellis retrieve pack --intent "deploy staging for user-api"   # assembled context pack
```

Every CLI command supports `--format json` for machine output.

## Architecture at a glance

```mermaid
flowchart TB
    subgraph Interfaces["Interfaces"]
        direction LR
        CLI["CLI<br/>trellis"]
        MCP["MCP Server<br/>11 tools"]
        REST["REST API<br/>FastAPI"]
        SDK["Python SDK<br/>local + remote"]
        UI["Web UI<br/>Cytoscape.js"]
        Users["Agents + Humans<br/>Claude, LangGraph, …"]
    end

    subgraph Core["Core engine"]
        direction LR
        PB["Pack Builder<br/>keyword + semantic + graph<br/>dedupe · rank · token budget"]
        GP["Governed Mutation Pipeline<br/>validate → policy → idempotency<br/>→ execute → emit event"]
        CL["Classification<br/>4 deterministic classifiers<br/>LLM fallback (async)"]
        WK["Workers<br/>enrichment · pattern mining<br/>maintenance · ingestion"]
    end

    subgraph Data["Data layer"]
        direction LR
        TR["Traces<br/>immutable"]
        GR["Graph<br/>SCD Type 2"]
        DO["Documents<br/>full-text"]
        VE["Vectors<br/>semantic"]
        EL["Event Log<br/>audit"]
        BL["Blobs<br/>files"]
    end

    subgraph Backends["Pluggable backends"]
        direction LR
        SQ["SQLite<br/>default / local"]
        PG["Postgres<br/>+ pgvector"]
        S3["S3<br/>blob storage"]
        LA["LanceDB<br/>vector ANN"]
    end

    Users --> CLI & MCP & REST & SDK & UI
    Interfaces --> Core
    Core --> Data
    Data --> Backends

    classDef iface fill:#1f2937,stroke:#60a5fa,color:#e5e7eb;
    classDef core fill:#0b3d2e,stroke:#34d399,color:#e5e7eb;
    classDef data fill:#3b2f1c,stroke:#fbbf24,color:#e5e7eb;
    classDef back fill:#3b1c36,stroke:#f472b6,color:#e5e7eb;
    class CLI,MCP,REST,SDK,UI,Users iface;
    class PB,GP,CL,WK core;
    class TR,GR,DO,VE,EL,BL data;
    class SQ,PG,S3,LA back;
```

## What's in the substrate

```mermaid
flowchart TB
    subgraph Graph["Entity graph (temporally versioned)"]
        direction LR
        A["service: auth-api"] -- depends_on --> B["service: user-db"]
        A -- part_of --> C["team: platform"]
    end

    T["trace: 'Added rate limiting to auth-api'<br/>• researched existing patterns<br/>• tool_call edit_file gateway.py<br/>• tool_call run_tests (42 passed)<br/>• outcome: success"]
    E["evidence: 'RFC — API guidelines'<br/>uri: s3://…"]
    P["precedent: 'Rate limiting pattern<br/>for API gateways'<br/>confidence: 0.85<br/>applies_to: [auth, payments]"]

    A -- touched_entity --> T
    T -- used_evidence --> E
    T -- promoted_to_precedent --> P

    classDef entity fill:#1f2937,stroke:#60a5fa,stroke-width:1px,color:#e5e7eb;
    classDef trace fill:#0b3d2e,stroke:#34d399,stroke-width:1px,color:#e5e7eb;
    classDef evidence fill:#3b2f1c,stroke:#fbbf24,stroke-width:1px,color:#e5e7eb;
    classDef precedent fill:#3b1c36,stroke:#f472b6,stroke-width:1px,color:#e5e7eb;
    class A,B,C entity;
    class T trace;
    class E evidence;
    class P precedent;
```

> Every node carries `valid_from` / `valid_to` — query any past state with `as_of`.

- **Traces** — what agents did: steps, tool calls, reasoning, outcomes. Immutable.
- **Entities + edges** — the graph of services, teams, tools, datasets, and how they relate. Temporally versioned.
- **Evidence** — documents and snippets agents read, with URIs to local files or S3.
- **Precedents** — distilled patterns promoted from successful (and failed) traces.
- **Events** — a full audit log of every mutation, for observability and effectiveness analysis.

## How the feedback loop works

```mermaid
flowchart TB
    subgraph Agents["Agents — read & write"]
        direction LR
        IF["CLI • MCP • REST • Python SDK"]
    end

    Pack["Context Pack Builder<br/>keyword + semantic + graph<br/>dedupe → rerank → token-budget"]
    Work["Agent does work<br/>emits trace + feedback"]
    Mut["Governed Write Pipeline<br/>validate → policy → idempotency<br/>→ classify → execute → emit event"]
    Store["Pluggable Storage<br/>SQLite · Postgres · S3<br/>pgvector · LanceDB"]

    subgraph Workers["Background workers — analyze & curate"]
        direction TB
        W["Effectiveness analysis<br/>• noise tagging<br/>• advisory fitness<br/>• precedent promotion<br/>• extraction-tier graduation"]
    end

    IF --> Pack
    Pack -- "markdown context" --> Work
    Work --> Mut
    Mut --> Store
    Store --> W
    W -. "tags, advisories, precedents" .-> Pack

    classDef iface fill:#1f2937,stroke:#60a5fa,color:#e5e7eb;
    classDef core fill:#0b3d2e,stroke:#34d399,color:#e5e7eb;
    classDef store fill:#3b2f1c,stroke:#fbbf24,color:#e5e7eb;
    classDef worker fill:#3b1c36,stroke:#f472b6,color:#e5e7eb;
    class IF iface;
    class Pack,Work,Mut core;
    class Store store;
    class W worker;
```

### How a trace flows through Trellis

```mermaid
flowchart LR
    A["Agent<br/>does work"] --> I["Ingest<br/>validate schema"]
    I --> PG["Policy Gate<br/>check rules"]
    PG --> CL["Classify<br/>tag 4 facets"]
    CL --> EX["Execute<br/>write to stores"]
    EX --> EV["Emit event<br/>append to log"]

    EV -. feedback .-> W["Workers<br/>effectiveness · noise<br/>precedent promotion"]
    W -. tags, advisories .-> PBx["Pack Builder<br/>assembles next pack"]
    PBx --> A2["Agent<br/>next task"]

    classDef agent fill:#1f2937,stroke:#60a5fa,color:#e5e7eb;
    classDef write fill:#3b2f1c,stroke:#fbbf24,color:#e5e7eb;
    classDef read fill:#0b3d2e,stroke:#34d399,color:#e5e7eb;
    classDef worker fill:#3b1c36,stroke:#f472b6,color:#e5e7eb;
    class A,A2 agent;
    class I,PG,CL,EX,EV write;
    class PBx read;
    class W worker;
```

Packs carry `pack_id` and per-item refs; when the agent reports success or failure, feedback is attributed back to the exact items that were in the pack. Background workers aggregate that feedback into **noise tags** (so low-signal items drop out of future packs) and **advisory confidence adjustments** (so learned rules get sharper). Successful traces can be promoted to precedents, which then seed future packs for similar tasks.

## Install

Requires Python 3.11+.

```bash
pip install trellis-ai                    # core (SQLite everywhere)
pip install "trellis-ai[cloud]"           # + Postgres, pgvector, S3
pip install "trellis-ai[vectors]"         # + LanceDB (local ANN)
pip install "trellis-ai[llm-openai]"      # + OpenAI for enrichment & extraction
pip install "trellis-ai[llm-anthropic]"   # + Anthropic
pip install "trellis-ai[all]"             # everything
```

## Interfaces

**CLI** — `trellis` for humans and scripts. Every command has `--format json`.

```bash
trellis ingest trace trace.json
trellis retrieve pack --intent "..." --domain backend --max-tokens 2000
trellis curate promote TRACE_ID --title "..." --description "..."
trellis analyze context-effectiveness
trellis admin check-extractors       # readiness diagnostic for tiered extraction
```

**REST API** — `trellis admin serve` or `trellis-api`. OpenAPI at `/docs`, UI at `/`.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/v1/traces` | Ingest a trace |
| POST | `/api/v1/packs` | Assemble a context pack |
| GET | `/api/v1/entities/{id}` | Entity + neighborhood |
| POST | `/api/v1/feedback` | Record pack outcome |
| GET | `/api/v1/effectiveness` | Pack effectiveness report |

**MCP server** — `trellis-mcp`. Eleven macro tools (8 core + 3 sectioned-context) return token-budgeted **markdown**, not raw JSON, so context lands clean in the agent's window.

| Tool | Purpose |
|------|---------|
| `get_context` | Combined search → markdown pack |
| `save_experience` | Ingest a trace |
| `save_knowledge` | Create entity + optional relationship |
| `save_memory` | Store a document (runs through tiered extraction) |
| `get_lessons` | Precedents as markdown |
| `get_graph` | Entity + neighborhood as markdown |
| `record_feedback` | Record task success/failure |
| `search` | Combined doc + graph search as markdown |

All tools accept `max_tokens` (default 2000).

**Python SDK** — dual-mode (`import trellis_sdk`). Same API, flip `base_url` to go from in-process to HTTP.

```python
from trellis_sdk import TrellisClient

client = TrellisClient()                                  # local
client = TrellisClient(base_url="http://localhost:8420")  # remote

pack = client.assemble_pack("deploy checklist for staging", max_tokens=2000)
trace_id = client.ingest_trace(trace_dict)
client.record_feedback(pack.pack_id, task_succeeded=True)
```

Skill helpers return pre-summarized markdown strings for direct LLM injection:

```python
from trellis_sdk.skills import get_context_for_task

context = get_context_for_task(client, "implement retry logic", domain="backend")
```

## Planes & substrates

Trellis separates **agent-facing** stores from **Trellis-internal** stores. Each plane has a blessed default backend ("substrate"); other backends are opt-in.

```mermaid
flowchart LR
    subgraph Knowledge["Knowledge Plane — agent-facing"]
        direction TB
        G["Graph<br/>entities + edges"]
        D["Documents<br/>full-text"]
        V["Vectors<br/>semantic similarity"]
        B["Blobs<br/>files & artifacts"]
    end

    subgraph Operational["Operational Plane — Trellis-internal"]
        direction TB
        TR["Trace store<br/>immutable work records"]
        EL["Event log<br/>mutation audit trail"]
    end

    subgraph Substrates["Substrates (blessed defaults)"]
        direction TB
        SQ["SQLite — local default"]
        PG["Postgres / pgvector — cloud"]
        LA["LanceDB — local ANN"]
        S3["S3 — blobs in cloud"]
    end

    G --- SQ
    D --- SQ
    V --- SQ
    TR --- SQ
    EL --- SQ
    G -.-> PG
    D -.-> PG
    V -.-> PG
    V -.-> LA
    B --- SQ
    B -.-> S3

    classDef knowledge fill:#0b3d2e,stroke:#34d399,color:#e5e7eb;
    classDef ops fill:#3b1c36,stroke:#f472b6,color:#e5e7eb;
    classDef sub fill:#1f2937,stroke:#60a5fa,color:#e5e7eb;
    class G,D,V,B knowledge;
    class TR,EL ops;
    class SQ,PG,LA,S3 sub;
```

Solid lines are defaults (SQLite everywhere); dotted lines are cloud/alternate substrates wired via `~/.config/trellis/config.yaml`.

## Storage — local or cloud

Backends are wired from `~/.config/trellis/config.yaml`. SQLite is the default everywhere.

| Store | Default | Cloud options |
|-------|---------|---------------|
| Trace / Document / Graph / Event Log | `sqlite` | `postgres` |
| Vector | `sqlite` | `pgvector`, `lancedb` |
| Blob | `local` | `s3` |

```yaml
stores:
  graph:
    backend: postgres
    dsn: postgresql://user:pass@host/db
  vector:
    backend: lancedb
  blob:
    backend: s3
    bucket: trellis-artifacts
    region: us-east-1
```

Graph stores support SCD Type 2 temporal versioning — every node carries `valid_from` / `valid_to`, and `get_node_history()` returns the full audit trail. Pass `as_of` to any query to time-travel.

## Classification & tiered extraction

Every item is classified at ingestion on four orthogonal facets: `domain`, `content_type`, `scope`, `signal_quality`. Deterministic classifiers run inline (microseconds); LLM-backed classifiers only fire when deterministic confidence is below threshold.

Raw sources (agent messages, dbt manifests, OpenLineage events, …) flow through a **tiered extraction pipeline**: deterministic rule-based extractors run first, then hybrid JSON extractors, then LLM extraction as an opt-in fallback. As patterns stabilize, extraction graduates from expensive-but-universal LLM calls to cheap-and-deterministic rules — so the cost curve drops the more the domain crystallizes.

## Integrations

The Claude Code / Cursor / Claude Desktop rows are first-class — `trellis-mcp` ships with the package. The bottom three are reference templates under [`examples/integrations/`](https://github.com/ronsse/trellis-ai/tree/main/examples/integrations) — copy the file into your own project rather than depending on it as a library.

| | |
|-|-|
| [**Claude Code**](https://github.com/ronsse/trellis-ai/blob/main/docs/getting-started/mcp-claude-code.md) | One-command MCP install (`trellis admin quickstart`) |
| [**Cursor**](https://github.com/ronsse/trellis-ai/blob/main/docs/getting-started/mcp-cursor.md) | Add Trellis MCP via `~/.cursor/mcp.json` |
| [**Claude Desktop**](https://github.com/ronsse/trellis-ai/blob/main/docs/getting-started/mcp-claude-desktop.md) | Add Trellis MCP via `claude_desktop_config.json` |
| [**OpenClaw template**](https://github.com/ronsse/trellis-ai/tree/main/examples/integrations/openclaw) | MCP skill + `openclaw.json` snippet for OpenClaw agents |
| [**LangGraph template**](https://github.com/ronsse/trellis-ai/tree/main/examples/integrations/langgraph) | Reference `tools.py` wrapping the SDK as LangChain tools |
| [**Obsidian template**](https://github.com/ronsse/trellis-ai/tree/main/examples/integrations/obsidian) | Reference `vault.py` + `indexer.py` for indexing notes as evidence |

## Examples & skill templates

- [**examples/**](https://github.com/ronsse/trellis-ai/tree/main/examples) — runnable scripts: SDK local + remote, retrieve→act→record loop, custom extractor, custom classifier, LangGraph agent, batch ingest.
- [**skills/**](https://github.com/ronsse/trellis-ai/tree/main/skills) — drop-in Claude Code skills: `retrieve-before-task`, `record-after-task`, `link-evidence`.
- [**docs/getting-started/**](https://github.com/ronsse/trellis-ai/tree/main/docs/getting-started) — IDE-specific MCP setup walkthroughs.

## Development

```bash
git clone https://github.com/ronsse/trellis-ai.git
cd trellis-ai
uv pip install -e ".[dev]"

pytest tests/unit/                # unit tests (~1300)
pytest -m postgres                # postgres integration tests
ruff check src/ tests/            # lint
mypy src/                         # type check
```

## Docs

- [**Getting started**](https://github.com/ronsse/trellis-ai/tree/main/docs/getting-started) — 5-10 min on-ramp + IDE-specific MCP setup
- [**Agent guide**](https://github.com/ronsse/trellis-ai/tree/main/docs/agent-guide) — trace format, schemas, operations reference, playbooks
- [**Design docs**](https://github.com/ronsse/trellis-ai/tree/main/docs/design) — architecture, ADRs, classification, dual-loop evolution
- [**CLAUDE.md**](https://github.com/ronsse/trellis-ai/blob/main/CLAUDE.md) — quick orientation for AI coding assistants working in this repo

Before writing an ingestion runner for a new source, read [**docs/agent-guide/modeling-guide.md**](https://github.com/ronsse/trellis-ai/blob/main/docs/agent-guide/modeling-guide.md) — it covers the four-question test for deciding what becomes a node vs a property vs a document, and the anti-patterns to avoid.

## License

MIT — see [LICENSE](https://github.com/ronsse/trellis-ai/blob/main/LICENSE).
