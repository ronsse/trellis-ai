# Plan: Real-corpus end-to-end evaluation

**Status:** Proposed 2026-05-06
**Owner:** rotating
**Self-contained:** yes — read this top to bottom; you do not need any prior conversation context.

## 1. Premise — what this plan is for

Trellis ships a synthetic agent-loop convergence scenario
([`eval/scenarios/agent_loop_convergence/`](../../eval/scenarios/agent_loop_convergence/),
plan §5.4 of [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md))
that runs 30 rounds of pack-build → grade → feedback → dual-loop
against a hand-templated 3-domain corpus and produces a positive
`convergence.useful_delta` (+0.571 on SQLite). The scenario passes.
It is also the strongest evidence we have that the dual-loop feedback
system works — which is to say, not strong enough.

Three gaps make the synthetic-only chart easy to dismiss:

1. **The LLM is mocked.** No scenario has called a live OpenAI or
   Anthropic provider end-to-end. Every cost/quality claim about
   enrichment is theoretical.
2. **The corpus was authored to make the loop work.** Three hand-tuned
   domains, six hand-written distractors, ground truth by construction.
   A reviewer can plausibly say "of course it converges — you wrote
   the test that way."
3. **The graph is a node bag.** The scenario upserts isolated
   `node_type="entity"` nodes and zero edges. `GraphSearch` has nothing
   to traverse, the ProvO/schema.org canonical vocabulary
   ([`adr-graph-ontology.md`](./adr-graph-ontology.md)) is not exercised,
   and `ContentTags` only stamps 1 of 5 facets.

This plan closes those gaps in three phases, each producing a citable
chart on increasingly real-world corpora. The point is **evidence,
not metrics** — we want a single artifact that a design partner can
look at and conclude "the system improves with use."

## 2. State of the corpus today — alignment audit

### 2.1 What the synthetic scenario produces

| Surface | Current state | Canonical per ADR |
|---|---|---|
| `node_type` for entities | `"entity"` (open string, not aliased) | `SoftwareApplication`, `Dataset`, `CreativeWork`, `Concept` per domain |
| Edges between entities | none — only isolated nodes | `dependsOn`, `partOf`, `relatedTo`, ProvO verbs |
| `ContentTags` facets stamped | `signal_quality` only | + `domain`, `content_type`, `scope`, `retrieval_affinity` |
| `DataClassification` | not stamped | `sensitivity`, `regulatory_tags`, `jurisdiction` |
| `Lifecycle` | not stamped | `state`, `valid_from`, `valid_until` |

Per the ADR, none of this is broken — storage accepts any string, and
[`schemas/well_known.py`](../../src/trellis/schemas/well_known.py)'s
`canonicalize_*` helpers are pass-through for unknown values.
What's missing is **coverage**: scenario 5.4 cannot produce evidence
for retrieval that depends on graph edges, ContentTags facets beyond
signal_quality, or any policy-relevant classification.

### 2.2 Why the dbt corpus closes most of these gaps for free

The shipped [`DbtManifestExtractor`](../../src/trellis_workers/extract/dbt_manifest.py)
produces real entities and real edges:

| dbt resource | Current `entity_type` | Canonical mapping (ADR §3.1) |
|---|---|---|
| `model` | `dbt_model` | `Dataset` |
| `source` | `dbt_source` | `Dataset` |
| `seed` | `dbt_seed` | `Dataset` |
| `snapshot` | `dbt_snapshot` | `Dataset` |
| `test` | `dbt_test` | `CreativeWork` or `Concept` |
| (manifest itself) | — | `Project` or `Activity` |

| dbt edge | Current `edge_kind` | Canonical mapping (ADR §3.2) |
|---|---|---|
| `depends_on` (model→model, model→source) | `"depends_on"` (snake_case) | `dependsOn` (camelCase) — **canonical** |

Two cleanup items the Phase B-1 work will touch (§5.2): (a) canonicalize
`"depends_on"` → `"dependsOn"` at extraction (one-line change, or we
do it at ingestion via `canonicalize_edge_kind()`), (b) optionally map
the dbt-specific entity types onto `Dataset` / `CreativeWork` via the
`schema_alignment` metadata field rather than renaming the entity type
strings (preserves the dbt-specific vocabulary alongside the canonical
alignment).

## 3. Goal — the artifact this plan produces

A single chart, on a corpus a reviewer recognizes, showing
`convergence.useful_delta` (or equivalent) climbing across N rounds of
agent activity, with a real LLM provider in the loop, accompanied by:

- **Cost numbers** — total spend in dollars, tokens consumed, per-round
  cost trajectory.
- **Latency numbers** — wall-clock per round and per dual-loop pass.
- **Suppression evidence** — non-zero `loops.advisories_suppressed_total`
  on a corpus that wasn't written to make it happen.
- **Reproducibility** — committed seed, committed corpus snapshot or
  fetch script, one command to rerun.

## 4. Sequencing — three phases

### Phase A — Real LLM + real embeddings, synthetic corpus (~1.5-2 days, $5-25)

Run the existing scenario 5.4 with a live **Moonshot/Kimi** chat
provider and a live embedder in place of all mocks. Adds
`SemanticSearch` to the scenario's strategy list alongside the
existing `KeywordSearch` so the vector path is actually exercised.
Lowest-effort path to a citable chart that includes real retrieval
across both surfaces. See §5.1.

### Phase B-1 — dbt Jaffle Shop (~3-5 days)

Replace the synthetic generator with a real dbt project's
`manifest.json`. Use the shipped `DbtManifestExtractor` to populate
entities and `dependsOn` edges. Author 10-20 ground-truth queries
("what models does `orders` depend on?", "what tests apply to
`customers`?"). Run the same N-round agent loop. See §5.2.

### Phase B-2 — GitHub issues + PRs + commits (~5-7 days)

Pick a real OSS repo with a meaningful issue history. Build an
extractor (issue → `Concept`/`CreativeWork`, PR → `CreativeWork`,
commit → `Activity`, file → `File`, edges via `wasGeneratedBy`,
`used`, `wasInformedBy`). Author queries that mirror the agentic
shape ("what issue and PR resolved bug X?"). See §5.3.

### Phase C — Richer agent (deferred)

Synthetic / real corpus + a real-LLM agent that decides what to
query, reads the pack, decides whether it has enough. Hard part:
defining task success without ground-truth labels. Defer until
Phase B has shown which retrieval shapes the agent actually uses.

## 5. Phase plans

### 5.1 Phase A — Real LLM, synthetic corpus

**Secret injection: 1Password CLI + `op run`.** API keys are stored
in the **Agent Secrets** 1Password vault and injected at process start
via secret references in `.env`:

```
MOONSHOT_API_KEY=op://Agent Secrets/Moonshot-API-Key/credential
OPENAI_API_KEY=op://Agent Secrets/OpenAI-API-Key1/credential
```

Run scenarios with `op run --env-file=.env -- python eval/run.py …`.
Resolved values only ever live in the child process's environment —
never on disk, never in shell history, never in the chat transcript.
Requires 1Password CLI (already installed at `op 2.34.0+`) and the
desktop-app integration toggled on (`1Password → Settings → Developer
→ "Integrate with 1Password CLI"`). See `.env.example` for the
template. The Makefile target `make eval-phase-a` wraps the `op run`
invocation so contributors don't need to remember the syntax.

**Chat provider: Moonshot AI / Kimi.** Moonshot exposes an
OpenAI-compatible API surface, so the existing
[`OpenAIClient`](../../src/trellis/llm/providers/openai.py) is reused
unchanged — we override `base_url` to Moonshot's endpoint
(`https://api.moonshot.ai/v1` international / `https://api.moonshot.cn/v1`
domestic) and supply a `MOONSHOT_API_KEY` env var. **No new provider
class required.**

The provider already accepts `base_url` as a constructor kwarg (see
`_build_async_client` in `openai.py:28-45`). The `[llm-openai]`
optional extra installs the client library that Moonshot's API speaks.

**Embedding provider — decided 2026-05-06 via [`eval/_smoke/moonshot_probe.py`](../../eval/_smoke/moonshot_probe.py): OpenAI `text-embedding-3-small`.**

Probe result on the `.ai` endpoint with `MOONSHOT_API_KEY` set:

| Probe | Result |
|---|---|
| Moonshot chat (`kimi-k2-0905-preview`) | PASS — 26 in / 3 out tokens, 6.35s latency |
| Moonshot embeddings (3 candidate model names) | **FAIL** — `403 PermissionDeniedError: "The API you are accessing is not open"` for `moonshot-v1-embedding`, `moonshot-embedding-v1`, `kimi-embedding` |
| OpenAI `text-embedding-3-small` | PASS — 1536-dim, 2.52s latency |

**Verdict: split provider.** Phase A uses Moonshot for chat and OpenAI
for embeddings. Both keys flow through `op run --env-file=.env`. The
"single provider, single key" simplification was attractive but the
international Moonshot endpoint doesn't expose the embeddings surface
(or the user's account isn't entitled to it). The OpenAI fallback was
already on the ladder; promoting it from fallback to primary adds one
more API key and one more `base_url` (default), no other complication.

For posterity — fallback ladder considered (in order tried):

1. ~~Moonshot embeddings via `OpenAIEmbedder` + `base_url` override.~~
   Failed with 403 on all three candidate model names. Documented
   above for future re-probing if Moonshot opens the embeddings API
   on the international endpoint.
2. **OpenAI `text-embedding-3-small`** (1536-dim, $0.02 per M tokens).
   PASSED probe — Phase A's chosen embedder. Battle-tested, no
   integration risk.
3. Local sentence-transformers (e.g., `all-MiniLM-L6-v2`, 384-dim).
   Offline / CI fallback. Not built — needed only if option 2 also
   fails.

**Inputs:** existing `eval/scenarios/agent_loop_convergence/scenario.py`
gets two changes: (a) `SemanticSearch` added to the strategies list
alongside `KeywordSearch`, (b) the entity-summary docs and distractor
docs get embedded at population time (one `embed_batch()` call per
domain group). New: a `MoonshotRegistry` factory (or
`--provider moonshot` runner flag) that builds the chat client + the
embedder pointed at Moonshot, and wires both into the registry's
enrichment + vector paths.

**Knobs to set:**
- **Chat model: `kimi-k2-0905-preview`** (or whatever the current K2
  flagship is at run time) — agentic-tuned, strong on tool use,
  ~$0.6 / $2.5 per M tokens (input / output). Fallback for cost-only
  sanity runs: `moonshot-v1-32k` (~$0.17 / $1.0 per M).
- **Embedding model: OpenAI `text-embedding-3-small`** (1536-dim,
  $0.02 per M tokens). Decided 2026-05-06 — see probe verdict above.
  Registry's `embedding_dim` config must be set to 1536.
- **Vector store: SQLite** (default) for dev; corpus is small (~50
  embeddings = entities + distractors). Phase B-1's larger corpus
  may motivate switching to LanceDB or pgvector.
- `rounds`: 100 (plan §5.4 cites this; current default is 30).
- `feedback_batch_size`: 10 (so 10 dual-loop passes).
- Budget cap: **$25 hard stop** via per-round cost tracking; abort if
  exceeded with a partial chart. (Likely actual cost: $1-5 — the
  scenario's deterministic classifiers handle most items, so the
  chat LLM fires only on low-confidence fallback paths. Embeddings
  are computed once at corpus-load time, totaling well under
  $0.01 for ~50 short docs.)

**What we measure:**
- `convergence.useful_delta` and `convergence.weighted_delta`.
- `loops.advisories_suppressed_total` (currently 0 on synthetic;
  should stay 0 unless regime-shift kwargs are passed).
- New metrics: `cost.total_usd`, `cost.per_round_usd_mean`,
  `latency.round_wall_seconds_p50/p95`, `tokens.input_total`,
  `tokens.output_total`, `llm.calls_total`, `embedder.calls_total`,
  `embedder.tokens_total`.
- **Per-strategy retrieval contribution** — `KeywordSearch` hits vs.
  `SemanticSearch` hits vs. dedup overlap per round. Without this,
  we can't tell whether the embedding wiring is doing real work.
- Diff vs. the mocked-LLM, keyword-only baseline on the same seed —
  does the loop still converge once the vector path is live?

**Decision this phase unblocks:**
- "Real-LLM + real embeddings against Moonshot is feasible" — we
  have a runner that calls Moonshot/Kimi for chat *and* embeddings
  without breaking; the OpenAI-compat base_url path holds up for
  both surfaces.
- Baseline cost-per-round and embedder-cost-per-corpus on Kimi we
  can extrapolate to Phases B-1/B-2.
- Whether `SemanticSearch` measurably contributes to convergence on
  the synthetic corpus, or whether `KeywordSearch` alone dominates.
  Result informs Phase B-1's strategy mix.

**Out of scope:**
- Multi-provider comparison (Moonshot only — no GPT/Claude side-by-side).
- Quality-vs-cost sweeps across Kimi tiers (k2 vs. moonshot-v1-32k).
- Embedding-model quality sweeps (run with whatever the primary
  embedder produces; A/B against alternatives is its own follow-up).
- Local embedder (sentence-transformers) — only written if both
  Moonshot and OpenAI embedding paths fail.
- Evaluation profile changes.

### 5.2 Phase B-1 — dbt Jaffle Shop

**Corpus pick: [dbt-labs/jaffle-shop](https://github.com/dbt-labs/jaffle-shop)** (the canonical example dbt project) for first run, with [GitLab Data Team's `analytics` repo](https://gitlab.com/gitlab-data/analytics) as a stretch comparison if Jaffle Shop's small size (~10 models) doesn't produce meaningful distractor density.

**Setup:**
1. Clone the dbt project, run `dbt parse` to generate `target/manifest.json`.
2. New script `eval/corpora/dbt_loader.py` that:
   - Loads the manifest.
   - Calls `DbtManifestExtractor` to produce entity + edge drafts.
   - Routes drafts through `MutationExecutor` (per CLAUDE.md
     "All mutations go through the governed pipeline") — does **not**
     bypass to direct store writes.
   - Optionally canonicalizes `"depends_on"` → `"dependsOn"` at the
     ingestion site via `canonicalize_edge_kind()`.
   - Populates `entity_summary` documents per model from the
     `description` property + compiled SQL excerpt.
3. Author ~15 ground-truth queries per [`EvalQuery`](../../eval/generators/trace_generator.py)
   shape: `domain="dbt"`, `intent="..."`, `required_coverage=[<model_ids>]`.
   Examples:
   - "Which models does `customers` depend on?" → `required_coverage=["model.jaffle_shop.stg_customers", "model.jaffle_shop.stg_orders", "model.jaffle_shop.stg_payments"]`
   - "What does the `orders` model produce?" → entity + downstream consumers
   - "What tests apply to `customers`?" → tests with `depends_on.nodes` containing the model

**New scenario:** `eval/scenarios/dbt_corpus_convergence/scenario.py`,
forked from `agent_loop_convergence` with the corpus-loading function
swapped for the dbt loader and the queries list swapped for the
authored ones. The agent-loop machinery, dual-loop wiring, convergence
math, and reporting stay identical.

**Distractor strategy:** dbt projects include unrelated models with
overlapping vocabulary (e.g., `stg_customers` vs. `customers` vs.
`dim_customers`). The `KeywordSearch` strategy will pick all three
on a "customers" query; only one is the ground-truth answer. **No
hand-written distractors needed** — the corpus has organic noise.

**What we measure:** same as Phase A, plus:
- `recall_at_k` for k ∈ {3, 5, 8} per query — fraction of
  `required_coverage` retrieved in the top-k.
- `graph_edge_traversal_count` — does `GraphSearch` actually surface
  via `dependsOn` traversal? (The synthetic scenario can't measure
  this.)
- Per-`entity_type` retrieval distribution — `Dataset` vs.
  `CreativeWork` vs. `dbt_model` (legacy) hits.

**Decision this phase unblocks:**
- "Real-corpus convergence is real" — useful_delta climbs on a
  corpus we didn't author.
- Whether `GraphSearch` adds signal over `KeywordSearch` alone (set
  up A/B by enabling/disabling `GraphSearch` in the strategies list).
- Whether the canonical entity types (`Dataset` etc.) need to be
  emitted directly by the extractor (Phase 1 of the ontology ADR)
  or whether `schema_alignment` metadata is sufficient.

**Out of scope:**
- LLM-driven query rewriting (stays Phase C).
- Cross-project comparisons (single corpus per scenario run).
- Tag-vocabulary enrichment (the dbt extractor doesn't stamp
  `ContentTags` today; that's a follow-up).

### 5.3 Phase B-2 — GitHub issues + PRs + commits

**Corpus pick: TBD** — candidates ordered by likely usefulness:

1. A medium-size OSS Python project (e.g., `pydantic/pydantic`, ~5K
   issues, well-labeled, MIT). Pros: agentic retrieval shape; rich
   cross-references. Cons: needs new extractor.
2. A tighter project (e.g., `tiangolo/fastapi`, ~3K issues). Same
   pros/cons, smaller scale.
3. **Trellis itself** — meta-evaluation. Pros: self-contained,
   self-documenting, no licensing question. Cons: small issue count.

Pick after Phase B-1 lands and we have measured signal on what
"real-corpus convergence" looks like.

**Setup:**
1. New extractor `trellis_workers.extract.GitHubIssuesExtractor`
   (deterministic tier; fetches via GitHub REST API or operates on a
   committed JSON snapshot).
2. Entity mapping:
   - Issue → `CreativeWork` (open-string `github_issue` for legacy)
   - PR → `CreativeWork`
   - Commit → `Activity`
   - File → `File`
   - User → `Person` or `Agent` (depending on bot/human)
3. Edge mapping:
   - PR `wasGeneratedBy` issue (when "Closes #N" in PR body)
   - Commit `wasGeneratedBy` PR (commits in the PR's branch)
   - Commit `used` File (modified files)
   - Issue/PR `wasAttributedTo` User
4. Author queries that match the agentic shape:
   - "What issue and PR fixed bug X?" → required_coverage = [issue_id, pr_id, commit_id]
   - "Who owns the auth module?" → required_coverage = [user_ids of contributors to auth/]

**What we measure:** same as Phase B-1, plus:
- Cross-shape retrieval: does a query for an issue surface the
  related PR and commits via `wasGeneratedBy` traversal?
- `wasAttributedTo` traversal — does asking about an entity surface
  the responsible person/agent?

**Decision this phase unblocks:**
- "Trellis works on a graph that wasn't authored to demonstrate
  it" — the strongest evidence the system generalizes.
- Whether the typed-pack-shape gap (TODO.md row 72 — `item_type`
  semantics, summary generation) is real on real corpora.

**Out of scope (this plan):**
- Closing the typed-pack-shape gap. That's its own ADR-shaped lift
  noted in TODO row 72; this plan produces the evidence that
  motivates it.

### 5.4 Phase C — Richer agent (deferred, listed for context)

A real-LLM agent that:
1. Receives a task description (not a hand-authored query).
2. Calls `get_context` / `PackBuilder.build` itself.
3. Reads the pack content and decides whether it has enough to act.
4. If not, formulates a follow-up query and asks again.
5. Eventually emits a deliverable graded against ground truth.

Two questions to resolve before designing this:
- What's the task? (Code change? Question answering? Decision support?)
- How is success graded without hand-authored `required_coverage`?

Answer those by **looking at what Phase B-1 and B-2 packs actually
contain** — which item shapes does the agent reference, which does
it ignore? That signal points at what the richer agent should do.

## 6. Schema/ontology preconditions

Before Phase B-1 lands, two small alignment touches:

1. **Edge kind canonicalization at ingestion.** The dbt loader
   should call `canonicalize_edge_kind("depends_on")` → `"dependsOn"`
   when constructing `EdgeDraft`s, OR the `DbtManifestExtractor`
   should emit canonical names directly (Phase 1 of the ontology ADR).
   Either is acceptable; the loader-level fix is lower-risk.
2. **`schema_alignment` metadata on canonical entity types.** When
   the dbt loader maps `dbt_model` → `Dataset` (if it does — see §5.2
   open question), it should populate
   `metadata["schema_alignment"] = "schema.org/Dataset"` per the
   ADR's optional field convention.

Neither is a blocker for the chart; both improve the chart's
"corpus is canonically aligned" story.

## 7. Open questions / deferred

- **Claude-traces as a corpus.** Capturing real Claude sessions
  (user msg + tool calls + outputs) as Trellis traces is mechanically
  feasible but the design question — *what's worth saving when Claude
  already has project memory?* — has no obvious answer yet. Defer to
  its own scenario after Phase B has revealed what kinds of context
  the agent actually uses.
- **Multi-corpus convergence evals.** Does the loop converge faster
  on a familiar domain after training on another? Out of scope here;
  blocked on Phase B-1 + B-2 producing per-corpus baselines.
- **Generalist vs. specialist memory.** When does a precedent learned
  on the dbt corpus help on the GitHub corpus? Same blocker as above.
- **Filling the schema-coverage gap.** ContentTags facets beyond
  `signal_quality`, plus `DataClassification` and `Lifecycle`, are
  not stamped by either the synthetic scenario or the dbt extractor
  today. Folding them in is its own follow-up; this plan does not
  block on it because the convergence signal lives in
  `signal_quality` (the noise loop) and `relevance_score`.
- **Baseline / regression discipline.** TODO.md row 66 — defer until
  Phase A and Phase B-1 have run cleanly 3+ times so we have variance
  bounds to set thresholds against.

## 8. Success criteria — what "done" looks like per phase

| Phase | Done = |
|---|---|
| **A** | Live Moonshot/Kimi (chat + embeddings) scenario run committed to `eval/runs/`. Chart shows convergence.useful_delta ≥ +0.4 at N=100. Cost report under $25 total. Per-strategy retrieval contribution recorded (KeywordSearch vs. SemanticSearch hit counts, dedup overlap). Embedder smoke path (Moonshot primary, OpenAI fallback) documented in the run README. |
| **B-1** | dbt Jaffle Shop scenario passes `convergence.useful_delta ≥ 0` with `loops.advisories_suppressed_total ≥ 1` (organic suppression on real distractors). Per-query `recall@5 ≥ 0.7`. GraphSearch A/B shows non-zero contribution from edge traversal. |
| **B-2** | Real OSS repo scenario passes the same gates as B-1, plus shows non-trivial `wasGeneratedBy` traversal (PR↔issue↔commit links surface in packs). |
| **C** | (deferred — pending Phase B-1/B-2 output) |

## 9. Why this plan exists — the long-haul intent

To replace the "synthetic 5.4 + mocked LLM passes" story with a
**single, citable, reproducible chart on a real corpus with a real
LLM**. This is the artifact that turns "the system has a feedback
loop" into "the system measurably improves with use" — the
difference between a claim and evidence.

Anchored on three deliberate constraints:

- **Reuse existing scaffolding.** Scenario 5.4 is built. Generators
  and grading math are built. `DbtManifestExtractor` is built.
  Phases A and B-1 are 90% wiring, 10% new code.
- **Real corpora over synthetic ones.** A reviewer can dismiss a
  hand-authored corpus; they can't dismiss `jaffle-shop` or `pydantic`.
- **Every phase produces a chart.** Not "we built infrastructure for
  evaluation" — actual numerical output that demonstrates the loop
  works on input the loop's authors didn't shape to fit.

## 10. References

- [`adr-graph-ontology.md`](./adr-graph-ontology.md) — schema.org +
  PROV-O canonical names. Phase 0 landed; Phase 1 (extractor
  canonicalization) gated on this plan or its successor producing
  motivating evidence.
- [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.4
  — the synthetic baseline this plan extends. §7.1 — the regression
  / baseline discipline this plan eventually feeds.
- [`adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) —
  ContentTags / DataClassification / Lifecycle split. The "schema
  coverage gap" deferred in §7 is the unfilled side of this ADR.
- [`eval/scenarios/agent_loop_convergence/`](../../eval/scenarios/agent_loop_convergence/)
  — the existing synthetic scenario. Phase A reuses it as-is.
- [`src/trellis_workers/extract/dbt_manifest.py`](../../src/trellis_workers/extract/dbt_manifest.py)
  — the dbt extractor Phase B-1 builds on.
- [`TODO.md`](../../TODO.md) rows 58, 65, 66, 68, 72 — open eval
  items this plan supersedes or defers explicitly.
