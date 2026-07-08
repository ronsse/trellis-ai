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

**A memory system for AI agents — personal and shared.** Agents save memories, experiences, and knowledge from flexible sources. Trellis deduplicates and embeds them on ingest for semantic retrieval, builds a cross-agent graph, attributes outcomes back to the **exact context items** that produced them, and tunes retrieval under statistical governance. Multi-backend (SQLite, Postgres + pgvector, ArcadeDB, Neo4j, S3). Four interfaces (CLI, MCP, REST, Python SDK). One install.

```mermaid
%%{init: {"flowchart": {"curve": "basis"}}}%%
flowchart LR
    Agent(["🤖 Agent"]) -->|reads| Pack("📦 Context pack")
    Pack -->|grounds| Work("🛠️ Does work")
    Work -->|writes| Trace("📝 Trace + feedback")
    Trace --> Memory[("🧠 Graph +<br/>audit log")]
    Memory -.->|"better pack next time — noise demoted, weights tuned"| Pack

    classDef agent fill:#312e81,stroke:#818cf8,stroke-width:2px,color:#eef2ff;
    classDef read fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef work fill:#1e293b,stroke:#60a5fa,stroke-width:2px,color:#eff6ff;
    classDef write fill:#78350f,stroke:#fbbf24,stroke-width:2px,color:#fffbeb;
    classDef store fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    class Agent agent;
    class Pack read;
    class Work work;
    class Trace write;
    class Memory store;
    linkStyle 4 stroke:#34d399,stroke-width:2px;
```

Most "agent memory" attributes feedback to a session or a user. Trellis attributes it to the **specific items served** in the pack. That's the seam that lets noise suppression, advisory fitness, and parameter promotion all work from the same signal — and lets multiple agents share a substrate where institutional knowledge compounds instead of evaporating at the end of each session.

## Why Trellis vs rolling your own?

A useful cross-agent knowledge layer means assembling, at minimum:

- an **immutable trace store** with schema validation and an audit log
- a **graph store with temporal versioning** (SCD-2) so "what did we know at time T?" is answerable
- a **vector + keyword search** wired into the same query layer
- a **retrieval engine** that combines them, dedupes by item id, and enforces a token budget
- **feedback attribution to the specific items** served (not just the session or the user)
- a **statistical-gating mechanism** so noisy feedback doesn't destroy your weights
- a **governed mutation pipeline** that won't let agents corrupt the graph
- an **MCP server, REST API, Python SDK, and CLI** around all of it
- a **tiered extractor** that graduates from LLM-driven to deterministic as patterns stabilize
- a **plugin contract** so adopters can swap any backend without forking core

Trellis ships all of that as one install. Started from a vector DB plus glue code, the list above is months of yak-shaving — most of which doesn't surface until something breaks.

### What Trellis is NOT

- **Not a vector DB.** Vectors are one of six stores. Pluggable behind the same API (SQLite, pgvector, ArcadeDB, Neo4j HNSW).
- **Not a session blob.** Trellis *is* agent memory — personal-agent or cross-agent — but feedback attributes to the *specific items served*, not to a session or user, and multiple agents can share the same substrate.
- **Not a RAG framework.** RAG is one access pattern; Trellis adds attribution, statistical governance, and an audit log around it.
- **Not a managed service.** Self-hosted: local SQLite, or your Postgres / ArcadeDB / Neo4j. One `pip install` (or one Docker image).
- **Not a prompt library.** Trellis stores *what happened* (traces) and *what was learned* (precedents + advisories) — not what to say.

### When *not* to use Trellis

- You're building a single-session chatbot. The overhead is unjustified — prompt engineering is enough.
- You only need vector similarity. A bare vector DB is simpler.
- Your agent has no need to learn from past work. Pure tool-call agents don't benefit.
- You're at < 100 agent runs total. The statistical-governance machinery has nothing to chew on yet.

## Quickstart — 60 seconds

```bash
pip install trellis-ai
trellis admin init          # write ~/.trellis/config.yaml + init SQLite stores
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

> **Wiring an external agent system into Trellis?** Start at the one-page
> decision tree:
> [docs/getting-started/integrate-your-agent.md](https://github.com/ronsse/trellis-ai/blob/main/docs/getting-started/integrate-your-agent.md).
> MCP-speaking agents get the whole setup — stores, MCP server, and the
> drop-in skills — in one command: `trellis admin quickstart --with-skills user`.

> **Setting up for a team, a data platform, or production?** Local single-user
> needs no decisions. Beyond that, a few choices are yours to make and easy to
> miss — domains & ontology, domain ownership, and API security. They're laid
> out as a checklist in
> [docs/getting-started/setup-decisions.md](docs/getting-started/setup-decisions.md).

## Architecture at a glance

```mermaid
flowchart TB
    Users(["🤖 Agents + 🧑 Humans — Claude, LangGraph, scripts, CI"])

    subgraph Interfaces["🔌 Interfaces"]
        direction LR
        CLI("CLI<br/>trellis") ~~~ MCP("MCP Server<br/>14 tools") ~~~ REST("REST API<br/>FastAPI") ~~~ SDK("Python SDK<br/>local + remote") ~~~ UI("Web UI<br/>Memory Explorer")
    end

    subgraph Core["⚙️ Core engine"]
        direction LR
        PB("📦 Pack Builder<br/>keyword + semantic + graph<br/>dedupe · rerank · token budget") ~~~ GP{{"🛡️ Governed Mutations<br/>validate → policy → idempotency<br/>→ execute → emit event"}} ~~~ CL("🏷️ Classification<br/>4 deterministic classifiers<br/>LLM fallback (async)") ~~~ WK("🔄 Workers<br/>enrichment · pattern mining<br/>maintenance · curation")
    end

    subgraph Data["🗄️ Data layer"]
        direction LR
        TR[("Traces<br/>immutable")] ~~~ GR[("Graph<br/>SCD Type 2")] ~~~ DO[("Documents<br/>full-text")] ~~~ VE[("Vectors<br/>semantic")] ~~~ EL[("Event Log<br/>audit")] ~~~ BL[("Blobs<br/>files")]
    end

    subgraph Backends["🧩 Pluggable backends"]
        direction LR
        SQ("SQLite<br/>default / local") ~~~ AR("ArcadeDB<br/>blessed: graph + vector") ~~~ PG("Postgres + pgvector<br/>cloud alternative") ~~~ S3("S3<br/>blob storage")
    end

    Users ==> Interfaces
    Interfaces ==> Core
    Core ==> Data
    Data ==> Backends

    classDef iface fill:#1e293b,stroke:#818cf8,stroke-width:2px,color:#eef2ff;
    classDef agent fill:#312e81,stroke:#a5b4fc,stroke-width:2px,color:#eef2ff;
    classDef core fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef data fill:#78350f,stroke:#fbbf24,stroke-width:2px,color:#fffbeb;
    classDef back fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    class CLI,MCP,REST,SDK,UI iface;
    class Users agent;
    class PB,GP,CL,WK core;
    class TR,GR,DO,VE,EL,BL data;
    class SQ,PG,S3,AR back;
    style Interfaces fill:#0b1120,stroke:#6366f1,stroke-width:1px,stroke-dasharray:6 4,color:#a5b4fc;
    style Core fill:#0b1120,stroke:#10b981,stroke-width:1px,stroke-dasharray:6 4,color:#6ee7b7;
    style Data fill:#0b1120,stroke:#f59e0b,stroke-width:1px,stroke-dasharray:6 4,color:#fcd34d;
    style Backends fill:#0b1120,stroke:#ec4899,stroke-width:1px,stroke-dasharray:6 4,color:#f9a8d4;
```

## What's in the substrate

```mermaid
%%{init: {"flowchart": {"curve": "basis"}}}%%
flowchart TB
    subgraph Graph["🕸️ Entity graph — temporally versioned"]
        direction LR
        A("🧩 service<br/>auth-api") -- depends_on --> B("🧩 service<br/>user-db")
        A -- part_of --> C("👥 team<br/>platform")
    end

    T("📝 trace — 'Added rate limiting to auth-api'<br/>• researched existing patterns<br/>• tool_call edit_file gateway.py<br/>• tool_call run_tests ✅ 42 passed<br/>• outcome: success")
    E("📄 evidence — 'RFC: API guidelines'<br/>uri: s3://…")
    P("⭐ precedent — 'Rate limiting pattern<br/>for API gateways'<br/>confidence: 0.85<br/>applies_to: [auth, payments]")

    A -. touched_entity .-> T
    T -- used_evidence --> E
    T == promoted_to_precedent ==> P

    classDef entity fill:#1e293b,stroke:#818cf8,stroke-width:2px,color:#eef2ff;
    classDef trace fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef evidence fill:#78350f,stroke:#fbbf24,stroke-width:2px,color:#fffbeb;
    classDef precedent fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    class A,B,C entity;
    class T trace;
    class E evidence;
    class P precedent;
    style Graph fill:#0b1120,stroke:#6366f1,stroke-width:1px,stroke-dasharray:6 4,color:#a5b4fc;
```

> Every node carries `valid_from` / `valid_to` — query any past state with `as_of`.

- **Traces** — what agents did: steps, tool calls, reasoning, outcomes. Immutable.
- **Entities + edges** — the graph of services, teams, tools, datasets, and how they relate. Temporally versioned.
- **Evidence** — documents and snippets agents read, with URIs to local files or S3.
- **Precedents** — distilled patterns promoted from successful (and failed) traces.
- **Events** — a full audit log of every mutation, for observability and effectiveness analysis.

## How Trellis improves

Most "agent memory" systems attribute feedback to a session or a user. Trellis attributes it to the **exact items that were served**. Every assembled pack carries a `pack_id` plus per-item refs; when the agent reports success or failure, a `FEEDBACK_RECORDED` event joins cleanly back to the specific items, advisories, and strategies that produced the pack.

Three mechanisms build on that attribution:

- **Noise suppression.** Items whose post-hoc success rate drops below a threshold get tagged `signal_quality="noise"` and are excluded from future packs by default. This happens via the EventLog-authoritative loop (`run_effectiveness_feedback`) — no manual review required.
- **Governed parameter promotion.** Retrieval scoring weights (recency half-life, domain boosts, position decay) are tunable per `(component, domain)` cell. Observed outcomes propose parameter changes; `promote_proposal` only applies them when sample size and effect size clear a statistical gate (defaults: 5 samples, 15% effect). This is the stair-step — each cycle can sharpen retrieval, but only on evidence strong enough to survive the gate.
- **LLM bootstraps, deterministic inherits.** Classification runs four deterministic classifiers inline; the LLM only fires when confidence is below threshold. Extraction routes `DETERMINISTIC > HYBRID > LLM`, with LLM as an opt-in fallback (`allow_llm_fallback=False` by default). As rules and tags stabilize, the cost curve drops — the LLM did the bootstrapping, and deterministic paths inherit the signal.

Packs are assembled **fresh on every call** today — nothing is pregenerated or cached — so every improvement (new noise tags, new parameter snapshots, new precedents) applies immediately to the next retrieval. Session-aware dedup prevents the same items from being re-served to the same agent within a 60-minute window.

## How the feedback loop works

```mermaid
%%{init: {"flowchart": {"curve": "basis"}}}%%
flowchart TB
    IF(["🤖 Agents — CLI • MCP • REST • Python SDK"])

    Pack("📦 Context Pack Builder<br/>keyword + semantic + graph<br/>dedupe → rerank → token-budget")
    Work("🛠️ Agent does work<br/>emits trace + feedback")
    Mut{{"🛡️ Governed Write Pipeline<br/>validate → policy → idempotency<br/>→ classify → execute → emit event"}}
    Store[("🗄️ Pluggable Storage<br/>SQLite · Postgres + pgvector<br/>ArcadeDB · Neo4j · S3")]
    W("🔄 Background workers<br/>• noise tagging<br/>• advisory fitness<br/>• precedent promotion<br/>• extraction-tier graduation")

    IF ==> Pack
    Pack == "markdown context" ==> Work
    Work ==> Mut
    Mut ==> Store
    Store --> W
    W -. "tags · advisories · precedents" .-> Pack

    classDef iface fill:#312e81,stroke:#a5b4fc,stroke-width:2px,color:#eef2ff;
    classDef core fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef gate fill:#1e293b,stroke:#60a5fa,stroke-width:2px,color:#eff6ff;
    classDef store fill:#78350f,stroke:#fbbf24,stroke-width:2px,color:#fffbeb;
    classDef worker fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    class IF iface;
    class Pack,Work core;
    class Mut gate;
    class Store store;
    class W worker;
    linkStyle 5 stroke:#f472b6,stroke-width:2px;
```

### How a trace flows through Trellis

```mermaid
%%{init: {"flowchart": {"curve": "basis"}}}%%
flowchart LR
    A(["🤖 Agent<br/>does work"]) ==> I("1️⃣ Ingest<br/>validate schema")
    I ==> PG{{"2️⃣ Policy Gate<br/>check rules"}}
    PG ==> CL("3️⃣ Classify<br/>tag 4 facets")
    CL ==> EX[("4️⃣ Execute<br/>write to stores")]
    EX ==> EV("5️⃣ Emit event<br/>append to log")

    EV -. feedback .-> W("🔄 Workers<br/>effectiveness · noise<br/>precedent promotion")
    W -. "tags · advisories" .-> PBx("📦 Pack Builder<br/>assembles next pack")
    PBx ==> A2(["🤖 Agent<br/>next task"])

    classDef agent fill:#312e81,stroke:#a5b4fc,stroke-width:2px,color:#eef2ff;
    classDef write fill:#78350f,stroke:#fbbf24,stroke-width:2px,color:#fffbeb;
    classDef gate fill:#1e293b,stroke:#60a5fa,stroke-width:2px,color:#eff6ff;
    classDef read fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef worker fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    class A,A2 agent;
    class I,CL,EX,EV write;
    class PG gate;
    class PBx read;
    class W worker;
    linkStyle 5,6 stroke:#f472b6,stroke-width:2px;
```

Packs carry `pack_id` and per-item refs; when the agent reports success or failure, feedback is attributed back to the exact items that were in the pack. Background workers aggregate that feedback into **noise tags** (so low-signal items drop out of future packs) and **advisory confidence adjustments** (so learned rules get sharper). Successful traces can be promoted to precedents, which then seed future packs for similar tasks.

## Install

Requires Python 3.11+.

```bash
pip install trellis-ai                    # core (SQLite everywhere — local default)
pip install "trellis-ai[arcadedb]"        # + ArcadeDB (blessed graph + vector via Bolt + HTTP)
pip install "trellis-ai[cloud]"           # + Postgres, pgvector, S3
pip install "trellis-ai[neo4j]"           # + Neo4j driver (graph + vector via Bolt / AuraDB)
pip install "trellis-ai[llm-openai]"      # + OpenAI for enrichment & extraction
pip install "trellis-ai[llm-anthropic]"   # + Anthropic
pip install "trellis-ai[all]"             # everything
```

### Git-pinned installs (consumers vendoring Trellis from a SHA)

Published-package extras (`trellis-ai[arcadedb]`) work as above. If instead you pin Trellis
**directly from Git**, the intuitive PEP 508 extras-on-a-URL form is awkward and some build
tooling (pip resolver edge cases, older Poetry, certain lockfile generators) rejects it:

```text
# Often rejected / awkward depending on tooling:
trellis-ai[arcadedb] @ git+https://github.com/ronsse/trellis-ai.git@<sha>
```

The portable workaround is to pin Trellis by direct URL **without** the extra, and declare the
underlying driver dependency yourself. The extras are thin — each just pulls one driver:

| Extra | Add this dependency directly |
|---|---|
| `[arcadedb]` | `neo4j>=5.20` (ArcadeDB speaks the Neo4j Bolt wire protocol) |
| `[neo4j]` | `neo4j>=5.20` |
| `[cloud]` | `psycopg[binary]>=3.1`, `psycopg-pool>=3.1`, `pgvector>=0.3`, `boto3>=1.34` |

```toml
# pyproject.toml — Git-pinned consumer
dependencies = [
  "trellis-ai @ git+https://github.com/ronsse/trellis-ai.git@<sha>",
  "neo4j>=5.20",   # what [arcadedb]/[neo4j] would have pulled
]
```

See [#198](https://github.com/ronsse/trellis-ai/issues/198) for context.

## Interfaces

**CLI** — `trellis` for humans and scripts. Every command has `--format json`.

```bash
trellis ingest trace trace.json
trellis retrieve pack --intent "..." --domain backend --max-tokens 2000
trellis curate promote TRACE_ID --title "..." --description "..."
trellis analyze context-effectiveness
trellis admin check-extractors       # readiness diagnostic for tiered extraction
trellis admin migrate-graph \
  --from-config sqlite.yaml \
  --to-config aura.yaml      # backend-agnostic graph migration (SQLite↔Postgres↔Neo4j)
```

**REST API** — `trellis admin serve` or `trellis-api`. OpenAPI at `/docs`, UI at `/`.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/v1/traces` | Ingest a trace |
| POST | `/api/v1/documents` | Store a document (embeds on ingest when enabled) |
| POST | `/api/v1/packs` | Assemble a context pack |
| POST | `/api/v1/packs/sectioned` | Assemble a sectioned pack (per-section budgets) |
| GET | `/api/v1/entities/{id}` | Entity + neighborhood |
| GET | `/api/v1/documents` | Browse/search stored documents |
| GET | `/api/v1/events` | Tail the audit event log |
| GET | `/api/v1/packs` | Inspect assembled-pack telemetry |
| GET | `/api/v1/graph/history` | SCD-2 version history for a node |
| POST | `/api/v1/feedback` | Record pack outcome |
| GET | `/api/v1/effectiveness` | Pack effectiveness report |

**MCP server** — `trellis-mcp`. Fourteen macro tools return token-budgeted **markdown**, not raw JSON, so context lands clean in the agent's window.

| Tool | Purpose |
|------|---------|
| `get_context` | Combined keyword + semantic + graph search → markdown pack |
| `get_objective_context` | Objective-tier sectioned pack (domain + operational) |
| `get_task_context` | Task-tier sectioned pack scoped to entities |
| `get_sectioned_context` | Custom sectioned pack with per-section budgets |
| `save_experience` | Ingest a trace |
| `save_knowledge` | Create entity + optional relationship |
| `save_memory` | Store a document: content-hash + MinHash dedup, embed-on-ingest; tiered extraction when `TRELLIS_ENABLE_MEMORY_EXTRACTION=1` |
| `record_observation` | Record an empirical observation about an entity |
| `query_observations` | Query recorded observations |
| `get_lessons` | Precedents as markdown |
| `get_graph` | Entity + neighborhood as markdown |
| `record_feedback` | Record task success/failure |
| `search` | Combined doc (keyword + semantic) + graph search as markdown |
| `execute_mutation` | Governed mutation escape hatch (validate → policy → execute → audit) |

Retrieval tools accept `max_tokens` (default 2000). With `TRELLIS_ENABLE_EMBED_ON_INGEST=1` and an embedder configured, documents saved via `save_memory`, `POST /documents`, or `POST /evidence` become semantically retrievable immediately; backfill existing documents with `trellis admin reindex-vectors`.

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

Trellis separates **agent-facing** stores from **Trellis-internal** stores. Each plane has a blessed default backend ("substrate"); other backends are opt-in. **Backends are pluggable** — SQLite is the local default for everything, **ArcadeDB is the blessed graph + vector substrate** for self-hosted AWS deployments (Apache 2.0, Bolt + openCypher 25, native HNSW), and Postgres + pgvector remains a supported alternative for cloud workloads.

```mermaid
flowchart LR
    subgraph Knowledge["🧠 Knowledge Plane — agent-facing"]
        direction TB
        G[("Graph<br/>entities + edges")] ~~~ D[("Documents<br/>full-text")] ~~~ V[("Vectors<br/>semantic similarity")] ~~~ B[("Blobs<br/>files & artifacts")]
    end

    subgraph Operational["⚙️ Operational Plane — Trellis-internal"]
        direction TB
        TR[("Trace store<br/>immutable work records")] ~~~ EL[("Event log<br/>mutation audit trail")]
    end

    subgraph Substrates["🧩 Substrates"]
        direction TB
        SQ("SQLite<br/>local default — every store") ~~~ PG("Postgres + pgvector<br/>cloud — every store but blobs") ~~~ AR("ArcadeDB — blessed<br/>graph + vector (Apache 2.0)") ~~~ N4("Neo4j + AuraDB<br/>graph + vector alternative") ~~~ S3("S3<br/>blobs in cloud")
    end

    Knowledge == "each store binds to a substrate<br/>via config.yaml" ==> Substrates
    Operational == "same registry,<br/>own plane" ==> Substrates

    classDef knowledge fill:#064e3b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef ops fill:#4a044e,stroke:#f472b6,stroke-width:2px,color:#fdf2f8;
    classDef sub fill:#1e293b,stroke:#818cf8,stroke-width:2px,color:#eef2ff;
    classDef blessed fill:#1e293b,stroke:#34d399,stroke-width:2px,color:#ecfdf5;
    classDef alt fill:#1e293b,stroke:#94a3b8,stroke-width:2px,color:#cbd5e1,stroke-dasharray:5 5;
    class G,D,V,B knowledge;
    class TR,EL ops;
    class SQ,PG,S3 sub;
    class AR blessed;
    class N4 alt;
    style Knowledge fill:#0b1120,stroke:#10b981,stroke-width:1px,stroke-dasharray:6 4,color:#6ee7b7;
    style Operational fill:#0b1120,stroke:#ec4899,stroke-width:1px,stroke-dasharray:6 4,color:#f9a8d4;
    style Substrates fill:#0b1120,stroke:#6366f1,stroke-width:1px,stroke-dasharray:6 4,color:#a5b4fc;
```

Each store binds to a substrate through `~/.config/trellis/config.yaml`; the table below has the exact store-by-store mapping. In the diagram, green-bordered substrates are blessed defaults and dashed ones are alternates. Choosing pgvector collocates keyword, semantic, and graph retrieval in a single Postgres transaction — one DSN, one consistency story. **Neo4j (and AuraDB)** is supported as a graph-native alternative for graph + vector when you want Cypher-native traversal or are already on a managed Neo4j instance.

## Storage — local or cloud

Backends are wired from `~/.config/trellis/config.yaml`. SQLite is the local default; **ArcadeDB is the blessed graph + vector substrate** (Apache 2.0, Bolt + openCypher 25, native HNSW via jVector) — see [`docs/design/adr-arcadedb-blessed-substrate.md`](docs/design/adr-arcadedb-blessed-substrate.md) and [`docs/deployment/recommended-config.yaml`](docs/deployment/recommended-config.yaml) for the recommended shape. **Postgres + pgvector** remains a supported alternative for shops standardized on Postgres. **Neo4j / AuraDB** is the migration target for existing Neo4j deployments — see [`docs/deployment/neo4j-local.md`](docs/deployment/neo4j-local.md) and [`docs/deployment/neo4j-auradb.md`](docs/deployment/neo4j-auradb.md).

| Store | Local default | Cloud blessed | Alternatives |
|-------|---------------|---------------|--------------|
| Trace / Document / Event Log | `sqlite` | `postgres` | — |
| Graph | `sqlite` | **`arcadedb`** | `neo4j` (Bolt / AuraDB), `postgres` |
| Vector | `sqlite` | **`arcadedb`** (native HNSW) | `pgvector`, `neo4j` (HNSW on `:Node`) |
| Blob | `local` | `s3` | — |

For copy-paste config, see [`docs/deployment/recommended-config.yaml`](docs/deployment/recommended-config.yaml) — three blessed shapes (local Neo4j+SQLite, cloud AuraDB+Postgres, Postgres-only). Set `TRELLIS_VALIDATE_CONNECTIVITY=1` in production to fail-fast at startup if Neo4j is unreachable.

```yaml
stores:
  graph:
    backend: postgres
    dsn: postgresql://user:pass@host/db
  vector:
    backend: pgvector
    dsn: postgresql://user:pass@host/db
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

New here? The [**integrate-your-agent**](https://github.com/ronsse/trellis-ai/blob/main/docs/getting-started/integrate-your-agent.md) decision tree picks the right path (MCP / Python SDK / REST) and gives each a verification step.

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
- [**skills/**](https://github.com/ronsse/trellis-ai/tree/main/skills) — drop-in Claude Code skills: `retrieve-before-task`, `record-after-task`, `link-evidence`. Install with `trellis admin install-skills user` (or `trellis admin quickstart --with-skills user`).
- [**docs/getting-started/**](https://github.com/ronsse/trellis-ai/tree/main/docs/getting-started) — IDE-specific MCP setup walkthroughs.

## Development

```bash
git clone https://github.com/ronsse/trellis-ai.git
cd trellis-ai
uv pip install -e ".[dev]"

pytest tests/unit/                # unit tests (~2300)
pytest -m postgres                # postgres integration tests
pytest -m neo4j                   # neo4j integration tests (set TRELLIS_TEST_NEO4J_URI)
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
