# Gap analysis and all-local inventory

**Measured 2026-09-16** against `origin/main` @ `1ef5c9c`, the production deployment
@ `2e62ffe`, and the live production Postgres (read-only). Corpus item **B4** of the
2026-09-13 work corpus (`docs/plans/2026-09-13-corpus.md`, on branch
`plans/corpus-2026-09-13` — not yet on `main`, so it is named rather than linked), which
parks the question asked on 2026-09-12: *where are the gaps, what is untested, what is undeployed, and what stands
between here and running every component locally?*

Every number below is a measurement with a date, not an estimate. The window rolls —
re-derive rather than trusting a figure in prose.

---

## 0. Method, and what it cannot see

**CI coverage is a join, not a path list.** A test runs on a leg only if that leg
*selects its path* **and** *sets every gating marker on it*. Checking one half is how a
doc comes to claim coverage that does not exist. So the universe was collected once,
each leg was collected under its own real environment, and the legs were subtracted:

```
pytest tests/ --collect-only -q -q -p no:cacheprovider          # -q -q because addopts carries -v
```

**One measurement limit, stated exactly.** The local CI venv has no `psycopg`, so
**7 test files collect zero locally**:

```
tests/unit/stores/contracts/test_pgvector_contract.py
tests/unit/stores/contracts/test_postgres_document_contract.py
tests/unit/stores/contracts/test_postgres_event_log_contract.py
tests/unit/stores/contracts/test_postgres_graph_contract.py
tests/unit/stores/contracts/test_postgres_trace_contract.py
tests/unit/stores/test_pgvector.py
tests/unit/stores/test_postgres_stores.py
```

390 test files are on disk; 383 collected. **All 7 lie inside `live-infra.yml`'s path
list**, so the *file-level* classification below is exact and the "runs nowhere" set is
sound. What is understated is only `live-infra`'s own test count (541 measured, true
count higher). No conclusion here rests on that number.

**What was not measured.** Container-internal behaviour (prod's containers were not
entered), the Neo4j and ArcadeDB prod stores (neither is in the production config), and
anything requiring a write. No `claude -p` runs, no prod mutation, no config flips.

**On the heavy model.** The corpus item reserved Kimi `deep` for a real fork, through
the decision panel only. No fork arose: B4 is measurement, and every question it asks
has an answer in the data rather than a judgement between options. The panel was
therefore not invoked, and no spend was incurred.

---

## 1. Component inventory

Implemented / tested on which CI leg / present in the deployed build / runs locally.

| Component | files | LOC | tests | CI leg | in prod `2e62ffe` | runs locally |
|---|---:|---:|---:|---|---|---|
| `trellis/stores` | 58 | 18066 | 1558 | tests.yml 1246 · live-infra 529 · **97 nowhere** | modified (20 files) · `base/registry.py` **absent** | yes — sqlite/local are defaults |
| `trellis/retrieve` | 36 | 14925 | 1116 | tests.yml 1116 | modified (7) · `builder_factory.py` **absent** | yes |
| `trellis/extract` | 23 | 6268 | 434 | tests.yml 434 | modified (6) | yes |
| `trellis/learning` | 12 | 6092 | 256 | tests.yml 256 | unchanged | yes |
| `trellis/mcp` | 4 | 4146 | 387 + 19 int | tests.yml 406 | modified (2) | yes |
| `trellis/mutate` | 10 | 4186 | 347 | tests.yml 347 | modified (7) · `evidence_ingest.py`, `name_aliases.py` **absent** | yes |
| `trellis/classify` | 18 | 3873 | 374 | tests.yml 374 | modified (1) | yes — local Ollama |
| `trellis/schemas` | 22 | 3613 | 479 | tests.yml 479 | modified (2) | yes |
| `trellis/ops` | 6 | 2132 | 231 | tests.yml 231 | modified (3) | yes |
| `trellis/core` | 14 | 2008 | 297 | tests.yml 297 | modified (5) · `memory_op_judged.py`, `path_presence.py` **absent** | yes |
| `trellis/ingest_corpus` | 8 | 1771 | 96 | tests.yml 96 | unchanged | yes |
| `trellis/meta` | 4 | 945 | 53 | tests.yml 53 | unchanged | yes |
| `trellis/feedback` | 5 | 939 | 80 | tests.yml 80 | unchanged | yes |
| `trellis/migrate` | 2 | 597 | 22 | tests.yml 22 | unchanged | yes |
| `trellis/plugins` | 3 | 579 | 36 | tests.yml 36 | unchanged | yes |
| `trellis/llm` | 7 | 554 | 112 | tests.yml 112 | modified (3) · `json_response.py` **absent** | yes — local Ollama |
| `trellis/analyze` | 2 | 216 | 9 | tests.yml 9 | unchanged | yes |
| `trellis/auth` | 2 | 190 | 36 | tests.yml 36 | unchanged | yes |
| `trellis/wire` | 2 | 166 | 33 | tests.yml 33 | unchanged | yes |
| `trellis/testing` | 2 | 172 | — | — | unchanged | yes |
| `trellis_cli` | 29 | 15913 | 655 + 16 int | tests.yml 670 · **1 nowhere** | modified (23) · `admin_backfill_name_aliases.py` **absent** | yes |
| `trellis_workers` | 32 | 6412 | 401 | tests.yml 401 | modified (6) | yes |
| `trellis_api` | 19 | 4442 | 341 + 22 int | tests.yml 341 · **22 nowhere** | modified (9) · **1 file diverged** | yes |
| `trellis_sdk` | 9 | 2875 | 125 + 15 int | tests.yml 125 · **15 nowhere** | modified (6) | yes |
| `trellis_wire` | 6 | 1173 | 33 | tests.yml 33 | modified (3) · `withholding.py` **absent** | yes |

**Every component runs locally today.** There is no component whose only implementation
is a cloud service. What is not local is a matter of *configuration* (§4), not of
missing code.

---

## 2. CI coverage — the join, measured

| | tests |
|---|---:|
| universe (`tests/`, all markers) | **8085** |
| `tests.yml` (4-way matrix, SQLite only) | 7719 |
| `live-infra.yml` (3.13 + service containers) | 541 *(understated — see §0)* |
| **run on no CI leg** | **140** |

The 140, by marker:

```
  70  neo4j,skipif            25  arcadedb,skipif        21  live,neo4j,postgres,slow
   8  live                     7  asyncio,live,...        4  live,neo4j,skipif
   2  slow                     1  live,postgres           1  live,slow
   1  arcadedb,live,skipif
```

By file:

```
tests/unit/stores/test_neo4j_graph.py              39     tests/integration/sdk/test_live_client.py         8
tests/unit/stores/test_neo4j_vector.py             27     tests/unit/stores/test_arcadedb_graph.py          8
tests/unit/stores/test_arcadedb_vector.py          17     tests/integration/sdk/test_live_async_client.py   7
tests/integration/api/test_live_smoke.py           13     tests/unit/stores/test_neo4j_connectivity_live.py 4
tests/integration/api/test_smoke_parity.py          9     tests/integration/test_migrate_graph_live.py      3
tests/unit/stores/test_sqlite_graph_bulk_upsert.py  2     tests/integration/test_recommended_config.py      2
tests/integration/cli/test_subprocess_serve.py      1
```

### Two claims in `CLAUDE.md` are now false

Both are in the *Autonomous / swarm work* section's test-coverage caveat, and both were
true when written:

1. > "**Nowhere at all:** the ArcadeDB graph contract (`test_arcadedb_graph_contract.py`)"

   **False.** It collects **106** tests on `live-infra`, landed by `638b241` (#543). The
   ArcadeDB *unit* suites (`test_arcadedb_graph.py` 8, `test_arcadedb_vector.py` 17) are
   what still run nowhere — a narrower and different claim.

2. > "59 Postgres-marked tests there are simply unwired"

   **False.** Only 3 non-contract `postgres`-marked tests exist under
   `tests/unit/stores/` today, and all 3 are covered — `test_api_key_store.py`, wired by
   `179577a` (#545).

These are D1 material; they are not corrected here because this document is the
measurement, not the fix.

### One free win

`tests/unit/stores/test_sqlite_graph_bulk_upsert.py` is **2 pure-SQLite tests marked
`slow`** that no leg runs. They need no service container and no extra — a one-line
selection change covers them. Every other member of the 140 needs infrastructure.

---

## 3. Deployment — prod is a fork, not a lag

```
/api/version → package_version 0.9.0.dev264+g2e62ffe52 · api 1.1 · wire 0.1.0 · /readyz 200
```

`2e62ffe` is **2026-09-03**. Against `origin/main` @ `1ef5c9c`:

- **42 commits on `main` are not in prod**
- **2 commits in prod are not on `main`** — `4349dbb` and `2e62ffe`, both `api/ui`, on the
  local branch `ui/sidebar-and-table-interaction`
- merge-base `eab9d74` (2026-09-02)

**The fork is exactly one file: `src/trellis_api/static/index.html`.** Both ahead-commits
touch nothing else, and that file has diverged on both sides. So the deploy catch-up
(#546) is not a hard rebase — it is a fast-forward plus one three-way merge on the
Memory Explorer's HTML. That is a materially smaller job than "42 behind and 2 ahead"
suggests, and worth knowing before the work is scheduled.

### Nine modules do not exist in the running build

```
core/memory_op_judged.py      mutate/evidence_ingest.py    stores/base/registry.py
core/path_presence.py         mutate/name_aliases.py       trellis_cli/admin_backfill_name_aliases.py
llm/json_response.py          retrieve/builder_factory.py  trellis_wire/withholding.py
```

**`core/memory_op_judged.py` is the one that matters for the capability plan.** `ba1678d`
(#540) emits judged classification and extraction ops — the data source Phase 1's
`memory_op.judged` → outcome join reads. It is not in the deployed build, so **prod is
not producing that stream at all**. The 289-vs-500 gate measured for B1 was computed over
history the *emitters on main* would have written; the deployment is contributing nothing
new to it until #546 lands.

Other capability-relevant commits prod lacks: `681d046` #535 (centralized JSON parsing),
`545b652` #539 (pgvector extension provisioning), `1ef5c9c` #530 (atomic name-alias
lifecycle), `7cbe3a3` #544 (governed evidence ingestion), `8ec879c` #508 (write-provenance
staleness), `fa5bc9d` #499 (advisory cap), `96a812a` #486 (recency clock), `8090ea4` #534
(pack withholding over the SDK), `638b241` #543 + `179577a` #545 (the two CI-coverage
commits above), `e47e3ba` #533 (optional-dependency test coverage).

---

## 4. Cloud dependencies between today and fully local

### Already local — more than assumed

The production config at `~/.trellis/config.yaml`:

```yaml
knowledge:   graph {postgres} · vector {pgvector, 768} · document {postgres} · blob {local}
operational: trace {postgres} · event_log {postgres}
embeddings:  openai-compatible → http://localhost:11434/v1 · nomic-embed-text
llm:         openai-compatible → http://localhost:11434/v1 · hermes3:8b
```

- **All six stores are local.** `trellis-postgres` (`pgvector/pgvector:pg16`) on
  `127.0.0.1:5433` carries graph, vector, document, trace and event log; blobs are local
  disk. DSNs are injected from 1Password at launch, never written to the config.
- **The LLM and the embedder are already 100% local Ollama.** The `openai` *provider*
  name is the wire protocol, not the vendor — `base_url` is `localhost:11434`. Classification,
  enrichment, distillation and embedding have no network egress today.
- Ollama carries `hermes3:8b`, `hermes3:8b-64k`, `qwen2.5-coder:7b`, `llama3.2:3b`,
  `nomic-embed-text`.

### Not local — the actual remaining list

| Dependency | Where | Needed for | To go local |
|---|---|---|---|
| **GitHub Actions** | `tests.yml`, `live-infra.yml`, `lint`, `typecheck`, `codeql`, `openapi`, `publish` | all CI | self-hosted runner, or accept CI as the one remote |
| **Cloudflare Access + Tunnel** | `gateway-caddy`, `gateway-cloudflared` | `trellis-mcp.nathanronsse.com` remote MCP | tailnet-only (already reachable at `100.67.145.92:8421`); the public hostname is the only thing that needs Cloudflare |
| **litellm `deep` / `bulk` (Kimi)** | `litellm` container → Moonshot | decision panel, heavy reasoning | no local substitute at that capability; this is the deliberate exception |
| **NVIDIA build.nvidia.com** | decision panel's second lab | panel cross-lab diversity | same — a panel of one lab is not a panel |
| `boto3` / S3 blob store | `stores/s3/blob.py`, `[cloud]` extra | not configured in prod | already off; `blob: local` |
| `openai` / `anthropic` SDKs | `[llm-openai]`, `[llm-anthropic]` | not used in prod | already off; the OpenAI-compatible path needs neither |
| `psycopg`, `pgvector` | `[cloud]` extra | prod's stores | **local already** — the extra's name is misleading; it is what makes the *local* Postgres work |

**So the all-local gap is three items, not a platform migration**: CI, the public
hostname, and the heavy-reasoning tier. Everything in the data path is already on this
box. The `[cloud]` extra is a misnomer worth renaming — it is the Postgres extra, and it
is what the fully-local deployment depends on.

---

## 5. Base rates for Phase 3's first actuators

Measured read-only against `trellis_knowledge`, `statement_timeout=60s`,
`conn.read_only = True`. Population: **1896 current nodes, 1945 current edges**
(`valid_to is null`).

### 5.1 Duplicate-entity candidates — the base rate is near zero

| | |
|---|---:|
| clusters sharing `(node_type, normalised name)` | **6** |
| nodes in them | 14 |
| **merge candidates** (nodes − clusters) | **8** |
| cluster sizes | 4×1, 2×5 |
| names colliding *across* node types | **7** |
| `entity_aliases` rows | **0** |

```
4  Project              'hermes'            2  File  'skynet hub catalog services yaml'
2  SoftwareApplication  'hermes'            2  File  'skynet hub stacks homepage config services yaml'
2  SoftwareApplication  'git commit'        2  SoftwareApplication 'edit file'
```

Cross-type collisions are the more interesting half, because they are invisible to any
same-type dedup rule:

```
'hermes'            → Project, SoftwareApplication, System
'trellis ai'        → Concept, SoftwareApplication, system
'trellis'           → Concept, SoftwareApplication
'trellis retrieval' → Concept, System
'skynet hub'        → Concept, SoftwareApplication
'op run'            → Command, Tool
'hermesx'           → Tool, Wrapper
```

**The actuator implication is unwelcome and worth stating plainly: a deduplication
actuator has almost nothing to do.** Eight merges over 1896 nodes is 0.42%. A dedup pass
keyed on `(type, name)` sees none of the seven cross-type collisions — every one of them
is a genuine disagreement about *what kind of thing* the referent is (`hermes` is filed
as a Project, an application and a System at once), which no normalisation resolves and
which needs a type-reconciliation decision rather than a merge.

Separately, and not the same fact: **two type names exist in the graph in two casings** —
`Concept` 40 / `concept` 72, and `System` 4 / `system` 29. Nothing collides across them
today, so this is not currently causing a missed merge; it is a vocabulary split that
will start causing one. The lowercase set is the `save_knowledge` vocabulary (see §5.2),
so the two facts have one cause.

`entity_aliases` being empty means no merge has ever been performed, so this 0.42% is the
*all-time* accumulation, not a residue after cleanup.

### 5.2 Neighbourhood sizes — a star graph with 213 orphans

| | |
|---|---:|
| mean degree | **2.05** |
| median | **1** |
| p75 / p90 / p99 | 1 / 3 / 17 |
| max | **207** |
| **isolated (degree 0)** | **213 (11.2%)** |
| edges with an endpoint outside the current set | 0 |

```
degree:  0→213  1→1294  2→176  3→26  4→18  5→18  6→23  7→19  8→11  9→11  10→16 … 20+→11
```

Mean degree by type:

```
SoftwareApplication  n=833  mean 1.06  isolated  13
Activity             n=447  mean 4.34  isolated   0
CreativeWork         n=182  mean 1.01  isolated   0
File                 n=120  mean 1.11  isolated   3
Agent                n= 46  mean 9.04  isolated   0
Concept              n= 40  mean 4.75  isolated   0
gotcha               n= 72  mean 0.00  isolated  72   <<<
concept              n= 72  mean 0.10  isolated  66   <<<
system               n= 29  mean 0.14  isolated  25   <<<
```

**The headline is the last three rows.** The lowercase-typed nodes — `gotcha`, `concept`,
`system` — are **163 of the 213 isolated nodes**, and `gotcha` is isolated at
**72 of 72**. These are the `save_knowledge` writes: the tool most explicitly built for
durable cross-task knowledge deposits every one of its entities with **no edges at all**.
They are in the graph and unreachable from it; only the vector and keyword axes can find
them, and the graph axis — which is a recency feed unless seeded — will surface them only
while they are new.

That is a concrete, fixable capability gap, and it is upstream of anything Phase 3 would
do: an actuator that reshapes neighbourhoods cannot help a node that has none. It also
explains part of why the seeded `GraphSearch` branch has no production producers — there
is little neighbourhood to expand into for exactly the content most worth expanding.

The `max 207` node is **not** a defect, and it is worth saying so: it is the Agent
`trellis_meta_cli_worker`, carrying 207 `wasAssociatedWith` edges — one per activity it
ran, which is 10.6% of all edges and exactly the correct PROV-O shape. The next four hubs
are the same shape (`claude-code` 98, `trellis_meta_cli_analyze` 50, then two report
nodes). The degree distribution is a star graph around a handful of agents, by design;
the finding is the 213 orphans, not the hubs.

### 5.3 External-referent edges — 8.6% touch, 0.2% connect

The federation-map question: how much of the graph points *out* of Trellis?

| | |
|---|---:|
| nodes naming an out-of-Trellis referent | **265 / 1896 (14.0%)** |
| — by locator property | 242 |
| — by outward node type (`File`, `Dataset`, `Device`, `Command`, `Wrapper`, `API`, `SystemdUnit`) | 135 |
| **edges with ≥1 external endpoint** | **168 / 1945 (8.6%)** |
| edges with **both** endpoints external | **4** |
| **semantic (non-PROV) edges, whole graph** | **8 / 1945 (0.41%)** |

Locator keys actually in use:

```
<name is a path> 137 · repo 60 · machine 41 · host 15 · file 9 · endpoint 4 · url 1
```

Edge types on the external-touching set:

```
wasGeneratedBy 148 · appliesTo 11 · used 5 · entity_related_to 4
```

**The shape is exactly what the provenance-ledger reading predicts, and it is worse than
"missing semantic edges".** 148 of the 168 external-touching edges are `wasGeneratedBy` —
i.e. *this activity produced that file*. That is a provenance statement about a Trellis
event, not a federation link. The graph knows a file was written; it does not know the
file **is** anything, relates to anything, or lives anywhere addressable. There are
**four** edges in the entire graph that connect two external referents to each other.

Whole-graph edge distribution, for context:

```
used 882 · wasGeneratedBy 444 · wasAssociatedWith 261 · appliesTo 190 ·
wasAttributedTo 159 · entity_related_to 4 · hasObservation 4 · wasInformedBy 1
```

1747 of 1945 edges (**89.8%**) are the five PROV-O provenance shapes.

**And there is no addressing scheme.** Of 265 external-referent nodes, one carries a
`url` and four an `endpoint`. Everything else is a bare path, a repo name, or a hostname
in a free-form property bag — with no convention saying which key is authoritative, no
scheme prefix, and no resolver. A federation map needs a *referent identity* (something
like `file://skynet/home/nronsse/...`, `pg://trellis_knowledge/nodes/<id>`) before edges
between referents mean anything. Today the same file mentioned by two activities is two
string spellings that happen to match, or happen not to.

### 5.4 Document linkage

| | |
|---|---:|
| nodes with ≥1 `document_ids` | **180 / 1896 (9.5%)** |
| distribution | 0 → 1716 · 1 → 180 |

No node links to more than one document. The graph↔memory join — the thing that makes
this a *provenance map of memories* rather than a graph beside a memory store — exists
for one node in ten.

---

## 6. What this implies

Three things the measurements change about the capability plan:

1. **The RSI actuator list is mis-ordered.** Deduplication (§5.1) has 8 candidates and
   would be the first actuator by the plan's ordering; it is the *smallest* available
   win. The two larger ones are upstream of it: reconcile the **type** vocabulary (seven
   names filed under conflicting types, two type names carrying two casings — none of
   which a same-type merge rule can see), and give `save_knowledge` writes edges
   (§5.2 — 163 of 213 orphans, `gotcha` at 72/72).

2. **The federation map needs referent identity before it needs edges** (§5.3). Four
   external-to-external edges is not a sparse map; it is no map. The missing primitive is
   an addressing convention for out-of-Trellis referents, not more extraction.

3. **Phase 1's data source is not running in production** (§3). `core/memory_op_judged.py`
   does not exist in the deployed build. Every day before #546 lands is a day the judged
   stream does not grow, and the 289-vs-500 gate cannot close on volume that is not being
   written.

### Carried to the owner

- **#546 deploy catch-up** — and it is one file's worth of merge conflict, not 42
  commits' worth (§3).
- **#555 first** in the A1 merge queue; it unblocks `live-infra` for the whole open PR
  set.
- The **`[cloud]` extra rename** — it is the extra the all-local deployment requires.
- Whether the **2 UI commits** on `ui/sidebar-and-table-interaction` should become a PR
  or be re-applied onto `main`'s `index.html`.
