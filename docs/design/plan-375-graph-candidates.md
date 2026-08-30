# Plan — #375: a query-relevant candidate path for the graph axis

**Status: design, for review. No code in this phase.**
All numbers below are from the reference deployment (Postgres graph + pgvector),
measured read-only on **2026-08-30** over **all 46** `PACK_ASSEMBLED` events on
record (2026-07-07 → 2026-08-27) and the 60 `FEEDBACK_RECORDED` events, which
cite 33 distinct item ids as helpful. Note the window: #371/#376 quote a rolling
30-day slice (n=37 packs, n=16 attributed), this plan uses the full history, so
figures shift slightly — graph `useful_token_fraction` is 0.171 here against
#376's 0.1744. Nothing below contradicts those issues; it extends them.
Reproduce with §8.

## 1. The problem

`GraphSearch.search()` takes a query string and, in production, never reads it:
nothing supplies `filters["seed_ids"]` and no seed extractor is injected, so
every pack takes the unseeded branch, which is `GraphStore.query` —
`ORDER BY created_at DESC LIMIT 80`. The graph axis returns the eighty most
recently created nodes, filters them structurally, ranks them by *position in
that recency list*, and serves the top twenty. It is a recency feed wearing a
search interface (#371), documented and made observable but deliberately left
unchanged by #376. The reachable set is a fixed row count, so its coverage of
the graph decays as 1/N — median 8.6% of servable nodes and falling. #375 asks
for a candidate path that is a function of the intent, and rules out the three
seed producers that are already empty.

## 2. What the measurements actually say

**The axis's value is concentrated in seven nodes and one type.** Splitting the
190 graph servings by `node_type`:

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

**That value is dated, and expires in about a day of active work.** Today those
three sit at rows **57, 63 and 67** of the 80-row window. **57 of the 63 `gotcha`
nodes in the graph are already unreachable at any rank, for any intent.** The
graph grew by 46 nodes on 2026-08-26 and 35 on 2026-08-27. So "do nothing" is not
a stable state — it is a dated failure, and a much sharper argument for acting
than the 1/N decay #375 leads with.

**Every query-relevant candidate producer we can measure loses most of that
value.** Recall of the 31 cited-helpful servings, replayed per pack against each
pack's own as-of graph:

| candidate selection | fires on | recall of 31 |
|---|---|---|
| A. recency-80 → top-20 (status quo) | 46/46 | 31 (100%, by construction) |
| B. name/alias exact match → `get_subgraph(depth=2)` (#375 option 2, #369 substrate) | 24/46 | **0** |
| C. IDF text rank over node name+description, top 20 | 44/46 | 5 (16%) |
| D. as C, restricted to citation-earning types | 44/46 | 6 (19%) |
| E. doc-link bridge from the same pack's own candidates | ~24/46 | 1 (3%) |
| F. **40 doc-linked slots + 40 recency slots** | 46/46 | **29 (94%)** |
| G. `SemanticSeedExtractor` (#375 option 1 substrate) | 0/37 (#376) | 0 |

B deserves a sentence because it is #375's own preferred option. It fires on
half the packs but the seeds it finds are the graph's least discriminating
nodes — `trellis`, `Trellis`, `trellis-ai`, `CLI`, `Todoist`, `backfill` — and
the graph has no structure to expand through: **977 edges are all PROV
(`used` / `wasGeneratedBy` / `wasAssociatedWith` / `appliesTo` /
`wasAttributedTo`), zero are topical, 125 semantic nodes have degree 0 and 399
have degree 1 — 87% are degree ≤ 2.** Depth-2 expansion from those seeds reaches
a **median of 1 servable node** (0.16% coverage). Shipping B would replace an
8.6% window with a 0.16% one on half of all packs.

**The thing that separates the earning nodes from the churn is already a
first-class store filter.** All 62 of the 63 `gotcha` nodes carry
`document_ids`; so do every other `save_knowledge` write (concept 8, system 7,
Device 4, Software 3, decision 1) — 85 nodes, and *only* those. Everything with
a zero citation rate is trace-minted provenance churn with no doc link.
`document_ids` is `DOC_LINK_FIELD` in the query DSL, whose docstring already
says it exists so "a caller scanning for doc-linked nodes [does not pay] for
every unlinked node in the graph and silently truncate". `exists` is its only
supported operator, it compiles on SQLite, Postgres and the Bolt backends, and
it is pinned by the shared contract suite
(`tests/unit/stores/contracts/graph_store_contract.py:1127-1166`) — so this is
verified store capability, not a hoped-for one.

**And the node form is the cheap form.** The same gotcha reaches packs two ways:

| form | axis | servings | cited | rate | tokens/item |
|---|---|---|---|---|---|
| graph node (`name`+`symptom`) | graph | 48 | 22 | 0.458 | **15** |
| evidence document (prose) | semantic | 58 | 13 | 0.224 | 117 |

Only one pack ever carried both, so this is not duplication today. The graph
axis is 6.7% of injected tokens and 31 of 91 cited-helpful servings.

## 3. Options

Each is stated with what it costs and what it needs that does not exist.

1. **Do nothing; document the axis as a recency feed.** Free, already shipped,
   measures best today (`useful_token_fraction` 0.171 vs semantic 0.154, keyword
   0.038, full history). *Cost:* the three nodes carrying 71% of the axis's citations are
   13–23 rows from eviction and nothing reports it when they go. The axis will
   keep returning `File` and `SoftwareApplication` rows at a 0.000 citation rate.
2. **Entity-summary documents on the ingest path** (#375 option 1). Makes
   `SemanticSeedExtractor` live. *Needs:* a new document producer on every
   memory-ingest path, an embed per entity, and an embedder in the graph axis —
   which `build_strategies` treats as strictly optional. *Cost:* highest of the
   five; unmeasurable until the corpus exists, and arm G is what it looks like
   until then.
3. **Name/alias index → seeds → traversal** (#375 option 2). *Needs:* #369's
   write-path mint plus a backfill. *Cost:* arm B — 0 of 31. Refuted by
   measurement, not by taste. #369 is still worth doing for *resolution*; it is
   not a retrieval fix.
4. **A text index over node names in the store layer** (tsvector / trigram +
   per-backend compiler + contract tests). Genuinely query-relevant, reaches old
   nodes (median 17 of its top-80 lie outside the recency window). *Needs:* a new
   indexed column and a `FilterOp`, on four backends, one of which (ArcadeDB) has
   no CI coverage at all. *Cost:* arms C/D — 16–19% recall. Node names are a
   strict subset of the document text the keyword axis already indexes, so this
   largely re-implements the worst-performing axis (keyword, 0.050/serving) over
   less text.
5. **Split the candidate window: reserve slots for doc-linked knowledge nodes,
   keep the recency window.** Arm F. Query-independent, but it buys back the
   *reach* #375 is really asking for: from 6 of 63 gotchas to the whole
   `save_knowledge` corpus. *Needs:* nothing new in the store — `document_ids
   exists` is already supported. *Cost:* a second store query per pack, and the
   ranking question in §5.

## 4. Recommendation

**Do 5 now. Do not build a query-relevant candidate path yet. Revisit it — as
ranking *within* the knowledge pool, not as selection over the whole graph — when
that pool outgrows its window, which is roughly two months out.**

The reasoning, in order:

- **#375's premise does not survive its own data.** The complaint is that the
  axis cannot answer a query. The measurement says the axis's demonstrated value
  has nothing to do with answering queries: it is three environment gotchas that
  every agent in the repo needs regardless of intent. Every mechanism that makes
  the axis query-relevant throws 77–100% of that away. Building one now would be
  a regression dressed as a fix.
- **But "do nothing" is not the alternative it looks like**, because the value is
  reachable only by accident of write order and is days from being unreachable.
  The defensible position is neither "it works, leave it" nor "make it a search"
  — it is *make the reachable set the right set*.
- **The right set is already named in the schema.** `document_ids` marks
  deliberately-authored knowledge with evidence, is the exact partition with a
  non-zero citation rate, and is a supported store-side filter. Nothing has to be
  invented.
- **Order matters.** A relevance ranking over the whole graph is a guess; a
  relevance ranking over ~85 authored knowledge nodes is a small, honest
  problem — and it is not #369's refused "raise the cap", because the pool is
  bounded by a store-side filter, truncation is visible, and a miss costs a rank
  rather than a wrong identity answer.

Sketch, deliberately thin because implementation is the next phase:

- **Phase 1 (retrieval seam, store capability already present).** `GraphSearch`
  issues two store queries: the existing recency window **unchanged**, plus a
  `NodeQuery(filters=[FilterClause("document_ids", "exists")], limit=K)`
  knowledge window. The union is a **strict superset** of today's candidate set;
  `K=0` restores today's behaviour exactly.
- **Phase 2 (write path, #375 constraint 3).** Nothing today guarantees the
  doc-link partition keeps meaning "authored knowledge": #299 stamps
  `document_ids=[source_doc]` on every extraction mint. Those are
  `extraction_status="unconfirmed"` and already excluded by `GraphSearch`, so the
  partition holds *today* by accident of a filter written for another reason.
  Make it hold on purpose — a `knowledge_kind` stamp written by `save_knowledge`,
  or an explicit contract that confirming an extraction mint promotes it into the
  knowledge pool. This is the durable half and it is where the write-path work is.
- **Phase 3 (deferred, gated on §5).** When the knowledge pool exceeds its
  window, rank within it by intent. Only then is a query-relevant candidate path
  worth what it costs.

## 5. How we would know it worked, and how we would know it regressed

**A statistical A/B is out of reach and should not be attempted.** The graph axis
produced 190 servings at p=0.163 over seven weeks. Detecting a drop to 0.10 at
80% power needs roughly 300 servings per arm — about six months per arm at the
current rate. Any plan that proposes "ship it and watch the citation rate" is
proposing to watch noise. Worse, `useful_token_fraction` cannot see this at all:
the axis is 6.7% of injected tokens, so wiping it out entirely moves the headline
by under a point.

**Counterfactual replay cannot be used, and the plan must say so loudly.**
`retrieve/pack_replay.py` re-walks `PACK_ASSEMBLED.budget_trace[]`, which records
the candidates the budget *saw*. This change alters which candidates exist.
Replay would answer a different question and report a confident number for it.

So the gates are properties and dated predictions, not statistics:

1. **Superset property, live two-arm replay** (the #376 precedent; n=46, no power
   problem). Replay every recorded intent against production's own stores,
   read-only, under both trees. The Phase-1 candidate set must be a strict
   superset of the status-quo set on **46/46**, and the served top-20 diff must
   be enumerated item-by-item *with node_type*. **Pass = 0 of the 31
   cited-helpful servings lost.** Measured today, the 40/40 split arm loses 2;
   an additive window must lose none. If it cannot, the composition is wrong,
   not the idea.
2. **A dated, falsifiable prediction.** The three gotchas at rows 57/63/67 will
   leave the 80-row recency window within about a day of the next swarm wave.
   Under Phase 1 they must still be served afterwards. Re-run the row-position
   query in §8 before and after; if they vanish from packs, Phase 1 did not work.
3. **Reachability, deterministic.** Doc-linked knowledge nodes inside the served
   candidate window: **6 of 63 gotchas today → ≥ K**. One query, no inference.
4. **Make the regression detectable at all.** `PACK_ASSEMBLED.injected_items[]`
   carries `strategy_source` but **not `node_type` or `node_role`** — both are on
   the `PackItem` metadata and dropped at the event boundary. Every per-type
   number in §2 required joining to `nodes WHERE valid_to IS NULL`, which
   silently drops superseded nodes. Forward both fields (the #285 precedent) and
   the per-type citation table becomes a standing weekly check instead of an
   archaeology exercise. **Ship this first, before any behaviour change.**

**Confound, stated so nobody re-derives it.** Everything served is under two days
old and every grader was a swarm agent working in those same days, so recency and
relevance are not separable in this data (helpful median age 13.1h vs unhelpful
11.5h, #376). The 0.458 gotcha rate is conditional on being served. This is why
the gates above are containment properties and dated predictions rather than
effect sizes.

## 6. What ships off by default

- **Phase 1 ships on**, with `K` as the off switch (`K=0` is byte-identical to
  today). Shipping it off by default is what left `SemanticSeedExtractor` dead
  and unmeasured for a year; the failure it prevents is live and dated.
- **Phase 3 ships off** and stays off until the pool actually outgrows the
  window, on the #376 pattern.
- **`build_strategies(graph_seed_extractor=...)` stays `None`.** Nothing in this
  plan wires it.

## 7. Non-goals

- Making the graph axis depend on an embedder. `build_strategies` treats
  `embedding_fn` as strictly optional and this plan keeps that true.
- Wiring `SemanticSeedExtractor`, or writing `entity_summary` documents.
  `tests/unit/retrieve/test_semantic_seeds.py::TestMemoryCorpusIsANoOp` should
  still pass when this lands — it is pinning a different fix than the one
  recommended here.
- A name/alias retrieval path. #369's write-path mint is still worth doing for
  *entity resolution*; it is not a retrieval fix and this plan does not claim it.
- A text index on the graph store, on any backend.
- Changing `GraphSearch`'s scoring function, `PackBuilder`'s strategy loop, RRF,
  or the budget walk. Phase 1 changes what is *asked for*, not how it is ranked
  or spent.
- Any use of `pack_replay` to evaluate this change.
- Raising `limit * _GRAPH_RECENCY_OVERFETCH`, or any client-side scan of the
  whole node table.

## 8. Reproducing the numbers

Read-only against production. `psql` is not on the host; use the repo venv's
`psycopg` with the DSN from the `trellis-skynet` wrapper.

```sql
-- §2 the three gotchas' position in the live 80-row window (the eviction clock)
SELECT properties->>'name',
       (SELECT count(*) FROM nodes n2
         WHERE n2.valid_to IS NULL AND n2.created_at > n.created_at) AS newer_rows
FROM nodes n WHERE n.valid_to IS NULL AND n.node_type='gotcha'
ORDER BY n.created_at DESC LIMIT 10;   -- in-window iff newer_rows < 80

-- §2 the doc-link partition
SELECT node_type, count(*) FROM nodes WHERE valid_to IS NULL
  AND jsonb_array_length(COALESCE(document_ids,'[]'::jsonb)) > 0
GROUP BY 1 ORDER BY 2 DESC;            -- gotcha 62, concept 8, system 7, …

-- §2 graph edge structure
SELECT edge_type, count(*) FROM edges WHERE valid_to IS NULL GROUP BY 1;
```

Per-axis and per-node_type citation tables join `PACK_ASSEMBLED.injected_items[]`
(operational DB) to `nodes` (knowledge DB) on `item_id = node_id`; the arm
recalls in §2 rebuild each pack's as-of node population with
`valid_from <= t AND (valid_to IS NULL OR valid_to > t)`. Scripts used for this
plan are in the session scratchpad (`measure_names.py`, `measure_expand.py`,
`measure_text.py`, `measure_bridge.py`, `measure_final.py`); they are throwaway
and deliberately not committed — gate 4 above is the durable replacement.
