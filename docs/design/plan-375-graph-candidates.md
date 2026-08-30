# Plan — #375: a query-relevant candidate path for the graph axis

**Status: design, for review. No code in this phase.**

Measured read-only against the reference deployment (Postgres graph + pgvector)
on **2026-08-30**, over **all 46** `PACK_ASSEMBLED` events on record
(2026-07-07 → 2026-08-27) and the 60 `FEEDBACK_RECORDED` events, which cite 33
distinct item ids as helpful. Every number is re-derivable with
[`plan-375-arms.py`](plan-375-arms.py) (§8).

Two provenance notes, because both bit an earlier draft of this plan:

- **Window.** `GraphSearch` is driven by `limit = max(20,
  PACK_ASSEMBLED.budget_max_items)` (`mcp/server.py:752`, `_FLAT_MAX_ITEMS = 50`
  at `:611`), `scan = limit * 4`, and a served slice of `nodes[:limit]` applied
  **after** the structural/unconfirmed filter. Production flat packs are
  therefore **200/50**, not the 80/20 an earlier draft assumed; both values
  landed in #269 on 2026-07-13. `budget_max_items` is 50 on 19 of 46 packs
  including **all 8 of the most recent**, and the arm replays below use each
  pack's own value (it varies 3/5/6/8/10/24/50).
- **Slice.** Because `nodes[:limit]` runs after the filters, the cut that decides
  servability is **post-filter rank**, not store-side row number.

#371/#376 quote a rolling 30-day slice (n=37 packs, n=16 attributed); this plan
uses the full history, so figures shift slightly — graph `useful_token_fraction`
is 0.171 here against #376's 0.1744. Nothing below contradicts those issues.

## 1. The problem

`GraphSearch.search()` takes a query string and, in production, never reads it:
nothing supplies `filters["seed_ids"]` and no seed extractor is injected, so
every pack takes the unseeded branch, which is `GraphStore.query` —
`ORDER BY created_at DESC LIMIT 200`. The graph axis returns the two hundred most
recently created nodes, filters them structurally, ranks them by *position in
that recency list*, and serves the top fifty. It is a recency feed wearing a
search interface (#371), documented and made observable but deliberately left
unchanged by #376. The reachable set is a fixed row count, so its coverage of
the graph decays as 1/N. #375 asks for a candidate path that is a function of the
intent, and rules out the three seed producers that are already empty.

## 2. What the measurements say

### 2.1 The axis's value is concentrated in seven nodes and one type

Splitting the 190 graph servings by `node_type`:

| node_type | servings | cited helpful | rate |
|---|---|---|---|
| `gotcha` | 48 | 22 | **0.458** |
| `Activity` | 54 | 9 | 0.167 |
| File / SoftwareApplication / system / concept / decision / CreativeWork / Agent / Concept | **88** | **0** | **0.000** |

The 31 cited-helpful servings are **7 distinct nodes**, and 22 of the 31 are
three `gotcha` entities — `uv run rewrites uv.lock…`, `make lint/format/test
need the venv on PATH`, `contract suites: only SQLite runs on PRs`. They are
cited by everyone because they describe the environment the repo is worked in,
not because they matched an intent.

The same knowledge reaches packs two ways, and the node form is the cheap one:

| form | axis | servings | cited | rate | tokens/item |
|---|---|---|---|---|---|
| graph node (`name`+`symptom`) | graph | 48 | 22 | 0.458 | **15** |
| evidence document (prose) | semantic | 58 | 13 | 0.224 | 117 |

Only one pack ever carried both. The graph axis is 6.7% of injected tokens and
31 of 91 cited-helpful servings.

### 2.2 Two of those three gotchas are already evicted — not approaching eviction

At the live 200/50 window their **post-filter ranks are 54, 58 and 48**. The cut
is 50. So `contract suites…` is still served and the other two are not, today,
with nothing reporting it. This corrects an earlier draft that put them "13–23
rows from eviction" under an 80-row model; the direction of the finding is
unchanged but the state is worse than approaching — it has already happened.

### 2.3 The system's own telemetry is crowding out its memory

**200 of 987 current nodes (20%) are Trellis cron output** —
`cli.worker.curate.learning@<timestamp>`, `cli.analyze.value@<timestamp>` and 11
other job names, one node minted per invocation, three dailies at 52 rows each.
All are `node_type='Activity'` and `node_role='semantic'`, so nothing filters
them. They have appeared in **0 of 190 graded servings**.

They now occupy **26 of the 50 served slots**, and the trend is what matters:
median **6.5** slots at pack time across the 46 recorded packs, **11–14** across
the most recent 8, **26** today. Suppressing them (the 200-row scan holds 97
non-cron rows, so the slots refill immediately, reaching back to 2026-08-25
instead of 2026-08-26):

| | now | `cli.*` suppressed |
|---|---|---|
| gotchas served | 3 | **6** |
| doc-linked nodes served | 5 | **11** |
| the three cited gotchas, post-filter rank | 54 / 58 / 48 | **28 / 31 / 23** |

### 2.4 Every query-relevant candidate producer measured loses most of the value

Recall of the 31 cited-helpful servings, replayed per pack against that pack's
own as-of graph and its own budget:

| candidate selection | fires on | recall of 31 |
|---|---|---|
| A. status quo | 46/46 | 31 (100%, by construction) |
| B. name/alias exact match → `get_subgraph(depth=2)` (#375 option 2, #369 substrate) | 24/46 | **0** |
| C. IDF text rank over node name+description | 44/46 | 5 (16%) |
| D. as C, restricted to citation-earning types | 44/46 | 6 (19%) |
| E. doc-link bridge from the same pack's own candidates | ~24/46 | 1 (3%) |
| F. `SemanticSeedExtractor` (#375 option 1 substrate) | 0/37 (#376) | 0 |

B deserves a sentence because it is #375's own preferred option. It fires on half
the packs but the seeds it finds are the graph's least discriminating nodes —
`trellis`, `Trellis`, `trellis-ai`, `CLI`, `Todoist`, `backfill` — and the graph
has no structure to expand through: **977 edges, of which 973 are PROV
(`used` 396, `wasGeneratedBy` 247, `wasAssociatedWith` 200, `appliesTo` 80,
`wasAttributedTo` 50) and 4 are `hasObservation`; none are topical. 125 semantic
nodes have degree 0 and 399 have degree 1 — 87% are degree ≤ 2.** Depth-2
expansion from those seeds reaches a **median of 1 servable node**. Decisively:
**the three gotchas carrying 22 of the 31 citations have degree 0** — no edges at
all — so no seed-and-traverse mechanism reaches them at any depth from any seed.
The 0-of-31 is structural, not a tuning artifact.

### 2.5 What separates the earning nodes — structurally, not statistically

| partition | servings | cited | rate |
|---|---|---|---|
| doc-linked | 85 | 22 | 0.259 |
| doc-linked, **non-gotcha** (`concept` / `system` / `decision`) | 37 | **0** | **0.000** |
| **not** doc-linked (all `Activity`) | 105 | 9 | 0.086 |
| `node_type == 'gotcha'` | 48 | 22 | **0.458** |

Stated plainly: **as a classifier over today's data, `document_ids exists` is
strictly dominated by `node_type == 'gotcha'`** — 0.259 against 0.458 at
identical recall, because every one of the 22 doc-linked citations *is* a gotcha.
An earlier draft claimed "everything with a zero citation rate is trace-minted
churn with no doc link"; that is false in both directions and is withdrawn.

The argument for `document_ids` is **structural, and has to be made as such**:
it is the write-time signature of `save_knowledge` — deliberately authored
knowledge with evidence attached — so it generalises to knowledge kinds not yet
invented, whereas `node_type == 'gotcha'` overfits to the one kind that happens
to dominate a 31-citation sample. It is also already a store-side filter
(`DOC_LINK_FIELD`, `exists`, compiled on SQLite / Postgres / Bolt and pinned by
`tests/unit/stores/contracts/graph_store_contract.py:1127-1166`). The honest
summary is that **the mechanism serves gotchas today**, and that is fine — it is
what the evidence supports.

`node_role == "curated"` is the schema's existing name for "pre-digested
synthesis, highest information density per token", and `GraphSearch` already
boosts it (`curated_boost`, 1.3). Production has **zero** curated nodes. It is
not a drop-in: `Entity` enforces *generation_spec iff CURATED*, and a
hand-authored gotcha has no generator. Whether the pinned-knowledge shelf should
be `curated`, a new role, or a `document_ids` filter is an open question for
implementation — flagged here so the option is not missed again.

## 3. Options

1. **Do nothing; document the axis as a recency feed.** Free, already shipped.
   *Cost:* two of the three nodes carrying 71% of the axis's citations are
   already unserved (§2.2), 26 of 50 slots go to rows that have never been cited
   (§2.3), and nothing reports either.
2. **Stop minting a Knowledge-Plane node per cron invocation** (§2.3). *Cost:*
   the smallest of the five. No second store query, no new gate, no partition
   contract. *Needs:* a write-path decision about what these rows are. The
   retrieval semantics of `node_role="structural"` fit exactly ("excluded from
   retrieval by default"); its *definition* ("regenerated from source") fits
   loosely. The stronger reading is a plane violation — a cron invocation is an
   Operational-Plane fact that the trace store and event log already hold, so
   the Knowledge Plane should not carry one node per run at all. *Caveat:*
   `node_role` is immutable across SCD-2 versions
   (`stores/base/graph.py:157-175`), so the 200 existing rows cannot be
   re-stamped by `entity.update`; they need `retention.prune` — or simply time,
   since only ~5/day are minted and they fall past rank 50 within days once
   minting stops.
3. **Reserve candidate slots for doc-linked knowledge nodes** — a second store
   query, `FilterClause("document_ids", "exists")`, unioned with the recency
   window. *Needs:* nothing new in the store. *Cost:* **`NodeQuery` exposes only
   `filters` / `limit` / `as_of`, and every backend compiler appends a
   hard-coded `ORDER BY created_at DESC LIMIT`** — so this window is *a second
   recency feed over a smaller partition*, with the same 1/N decay, unless K
   covers the whole partition. At K=40 against today's 85-node partition it
   reaches 40/85 and only 17 of 62 gotchas; at K=85 it reaches all of both. 62
   of the 85 were written in one backfill batch on 2026-08-07, so `created_at`
   ordering *within* them is arbitrary. K must be sized against the partition,
   and the partition grows (~1/day, faster during a wave).
4. **A text index over node names in the store layer** (tsvector / trigram +
   per-backend compiler + contract tests). *Cost:* arms C/D — 16–19% recall.
   Node names are a strict subset of the document text the keyword axis already
   indexes, so this largely re-implements the worst-performing axis (0.050 per
   serving) over less text, on four backends, one of which (ArcadeDB) has no CI.
5. **Entity-summary documents on the ingest path** (#375 option 1) or a
   **name/alias index** (#375 option 2, #369 substrate). *Cost:* arms F and B —
   0 of 31 each. Option 2 is refuted structurally (§2.4), not by tuning. #369 is
   still worth doing for *entity resolution*; it is not a retrieval fix.

## 4. Recommendation

**Ship gate 4 (observability), then option 2 (stop the cron churn), then
re-decide in a month. Do not build a query-relevant candidate path now.**

- **#375's premise does not survive its own data.** Every mechanism that makes
  the axis query-relevant throws 77–100% of its demonstrated value away, and for
  the issue's preferred option the loss is structural: the nodes that carry the
  value have no edges.
- **The binding constraint is not relevance, it is that a fifth of the graph is
  the system's own cron log, occupying half the served slots at a citation rate
  of zero.** Option 2 is a write-path change, needs no new gate, cannot lose a
  cited-helpful serving (none is a `cli.*` row), and moves the three cited
  gotchas from ranks 54/58/48 to 28/31/23 — from two-of-three unserved to
  three-of-three served.
- **Option 3 stays on the table but is not first.** Its store filter is real and
  contract-tested, but §2.5 withdraws the statistical case for it and §3 shows it
  is a second `created_at DESC` feed. It is worth doing as a *durable* shelf for
  authored knowledge once option 2 has removed the noise that currently swamps
  the window — and worth measuring against `node_type` / `node_role` alternatives
  rather than assumed.
- **Order matters.** A relevance ranking over the whole graph is a guess; a
  ranking over ~85 authored knowledge nodes is a small, honest problem. Reach it
  by removing noise first, so the ranking question is asked of a population worth
  ranking.

## 5. How we would know it worked, and how we would know it regressed

**A statistical A/B is out of reach and should not be attempted.** The graph axis
produced 190 servings at p=0.163 over seven weeks; detecting a drop to 0.10 at
80% power needs **223–451 servings per arm** depending on method — six months or
more per arm. `useful_token_fraction` cannot see it either: the axis is 6.7% of
injected tokens, so wiping it out entirely moves the headline under a point.

**Counterfactual replay cannot be used.** `retrieve/pack_replay.py` re-walks
`PACK_ASSEMBLED.budget_trace[]`, which records the candidates the budget *saw*.
These changes alter which candidates exist. Replay would answer a different
question and report a confident number for it.

**The historical replay can show absence of harm, but cannot show benefit.**
Under each pack's own budget, arms A (status quo), B (cron-suppressed), C
(knowledge window K=40) and D (K=all) each recover **31/31**. That is the
safety result, and it supersedes an earlier draft's 29/31, which was an artifact
of the wrong window model. It is *not* evidence any of them helps: the
crowding-out in §2.3 post-dates the last recorded pack (6.5 → 14 → 26 slots), so
the failure being fixed is not present in the graded history.

So the gates are properties and dated predictions:

1. **Safety, historical (n=46, no power problem).** Replay every recorded intent
   against production's own stores, read-only, under both trees, each pack at its
   own `budget_max_items`. **Pass = no cited-helpful serving lost.** Both
   candidate arms already satisfy this at 31/31.
2. **Efficacy, forward-looking and dated.** The three cited gotchas sit at
   post-filter ranks 54/58/48 today and 28/31/23 under suppression. Prediction:
   after option 2 ships, all three appear in live `PACK_ASSEMBLED` payloads
   within a week. Falsifiable, and checkable from the event log alone.
3. **Reachability, deterministic.** `cli.*` rows in the served window: 26 → 0.
   Doc-linked nodes served: 5 → 11. Gotchas served: 3 → 6. One query each.
4. **Make regression detectable at all — ship this first, before any behaviour
   change.** `PACK_ASSEMBLED.injected_items[]` carries `strategy_source` but
   **not `node_type` or `node_role`**; both are on the `PackItem` metadata and
   dropped at the event boundary. Every per-type number above required joining to
   `nodes WHERE valid_to IS NULL`. That join was checked both ways for this plan
   — all 78 served ids resolve to a current row, so it did not distort these
   figures — but it is not a join a standing check should depend on. Forward both
   fields (the #285 precedent) and §2.1 becomes a weekly query.

**On composition (this is the mechanism, and it decides everything).** The served
count is bounded twice and independently: `nodes[:limit]` inside
`GraphSearch.search` — before scoring, and `i` also feeds `position_decay_step` —
and `PackBudget.max_items`. **Adding candidates to a capped-output ranker is
either neutral or displacing; there is no third case.** So a design must say how
the union is ordered before the slice, and the three obvious answers are not
equivalent: concatenating recency-first is byte-identical to today (a literal
no-op); re-sorting by `created_at DESC` puts the knowledge nodes — which are old
— below the cut (also a no-op); **reserving slots is the only composition that
does anything, and it necessarily displaces.** Option 2 sidesteps this entirely:
it removes candidates rather than adding them, so it frees slots instead of
competing for them. If option 3 is later built, it must name its reservation size
and accept that a non-empty served diff is the *evidence it worked*, not evidence
the composition is wrong.

**Confound, restated so nobody re-derives it.** Everything served is under two
days old and every grader was a swarm agent working those same days, so recency
and relevance are not separable here (helpful median age 13.1h vs unhelpful
11.5h, #376). The 0.458 gotcha rate is conditional on being served.

## 6. What ships off by default

- **Gate 4 (forwarding `node_type` / `node_role`) ships on.** It is additive
  telemetry with no retrieval effect.
- **Option 2 ships on.** It is a write-path stamp affecting rows the axis has
  never once been graded on, and it is reversible per the swarm autonomy contract.
- **Option 3, if built, ships with its reservation size as the off switch**
  (K=0 restores prior behaviour exactly). It should not ship in the same change
  as option 2 — two simultaneous changes to the same 50 slots cannot be
  attributed.
- **`build_strategies(graph_seed_extractor=...)` stays `None`.** Nothing here
  wires it.

## 7. Non-goals

- Making the graph axis depend on an embedder. `build_strategies` treats
  `embedding_fn` as strictly optional and this plan keeps that true.
- Wiring `SemanticSeedExtractor`, or writing `entity_summary` documents.
  `tests/unit/retrieve/test_semantic_seeds.py::TestMemoryCorpusIsANoOp` should
  still pass when this lands.
- A name/alias retrieval path, or a text index on any graph backend.
- Changing `GraphSearch`'s scoring function, `PackBuilder`'s strategy loop, RRF,
  or the budget walk. Option 2 changes which rows exist to be ranked, not how
  ranking or spending works.
- Any use of `pack_replay` to evaluate this.
- Raising `limit * _GRAPH_RECENCY_OVERFETCH`, or any client-side scan of the
  whole node table.
- Filtering `cli.*` rows by name pattern inside `retrieve/`. The suppression
  must be a property of the row, decided where the row is written.

## 8. Reproducing the numbers

[`plan-375-arms.py`](plan-375-arms.py) re-derives §2.1–§2.5, the arm recalls, and
the live window composition. Read-only; `psql` is not on the host, so it uses
`psycopg` from the repo venv:

```bash
# trellis-skynet already exports both DSNs
export TRELLIS_KNOWLEDGE_PG_DSN=... TRELLIS_OPERATIONAL_PG_DSN=...
~/projects/trellis-ai/.venv/bin/python docs/design/plan-375-arms.py
```

Arms B–F of §2.4 (name/alias seeding, text rank, doc-link bridge) were measured
with throwaway variants of the same window model and are not in the committed
harness; the harness covers every number the recommendation is sized on.
