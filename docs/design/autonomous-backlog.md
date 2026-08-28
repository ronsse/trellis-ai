# Autonomous backlog — subagent-executable work items

> **What this is.** A dependency-ordered queue of work items each sized for one
> focused subagent session and one PR. It exists so an orchestrating agent can make
> continuous progress on the Trellis roadmap without the operator adjudicating every
> fork. It is a *scheduling* view over work already tracked in GitHub issues and
> [`implementation-roadmap.md`](./implementation-roadmap.md) §3.H — **not a second
> source of truth.** Where they disagree, the roadmap wins.
>
> Created 2026-08-26. Item statuses live in the GitHub issues, not here; this file
> records decomposition, sequencing, and the autonomy class of each item.

## How an agent runs an item

1. `get_context(intent=...)` before starting. An empty pack is a real answer.
2. Branch from `main`, one item per branch.
3. Implement + tests. `source .venv/bin/activate` first — the Makefile calls bare
   `ruff` / `mypy` / `pytest` and a non-activated shell fails with a misleading
   exit 127. (Do **not** use `uv run` casually: it re-resolves and rewrites
   `uv.lock` as a side effect.)
4. Open a PR. **Merge only when all six workflows are green** — `lint`, `typecheck`,
   `tests` (3.11/3.12/3.13), `codeql`, `openapi`. `main` has no required status
   checks configured, so GitHub will *not* enforce this for you; the gate is the
   agent's responsibility. See [Repo-level gap](#repo-level-gap) below.
5. `save_experience` on the way out, including failed steps with their `error` set.

## Autonomy classes

Every item is tagged with who decides when a fork appears mid-item.

| Class | Meaning |
|---|---|
| **`panel`** | Reversible. The agent may settle open questions with the `decision-panel` skill (cross-lab model panel) and proceed. A **split panel escalates to the operator** — that is the case where operator input is worth most. |
| **`human`** | Not eligible for the panel regardless of confidence: publishing, deletion or redaction of production data, credential operations, spend, history rewrite, repo-settings changes. |
| **`blocked`** | Cannot start; the blocker is named. |

---

## Wave 0 — housekeeping (do first; hours, not days)

| id | item | class | notes |
|---|---|---|---|
| H1 | Merge [#328](https://github.com/ronsse/trellis-ai/issues/328) — dependabot ruff 0.15.22→0.16.4, mypy bump | `panel` | CI-green and mergeable today. Expect new ruff findings on the bump; fixing them is in scope for this item. |
| H2 | Land the 2026-08-26 doc reconciliation (`TODO.md` + roadmap) | `panel` | Seven stale checkboxes ticked, one rescoped, two false claims corrected. |
| H3 | Dispose of [#304](https://github.com/ronsse/trellis-ai/issues/304) — honest DoD-3 loop metric + reframe | `panel` | Open 8 days, CI-green. A metric *reframe* is reversible, so the panel may decide it. |
| H4 | Dispose of [#208](https://github.com/ronsse/trellis-ai/issues/208) — re-home to consumer-kg or close | `human` | Closing an issue can hide an open gate (the #312 failure). Operator call. |

## Wave 1 — make measurement trustworthy

Nothing downstream can be believed until these land. Three of the four are cases of a
measurement wired to a constant or to a stale snapshot — the failure shape this repo
keeps rediscovering.

**A1 — Trace and observation content is unreachable by semantic search.** `class: panel`
*(decision already taken — see below)*
`save_knowledge` reaches the semantic axis via the evidence document [#260](https://github.com/ronsse/trellis-ai/issues/260) auto-creates. `save_experience` and
`record_observation` have no embed call on any path, so the highest-volume item type in
the corpus (traces outnumber the next surface ~18:1 over 30 days) is reachable only by
keyword and a weak substring graph axis.
**Decided by panel 2026-08-26, unanimous:** a **batch backfill worker**, not a
write-path change — it covers the existing backlog as well as new writes, adds zero
latency to the auto-capture path that has already had a fragility outage, and respects
trace immutability. Both panelists independently raised risks that are now acceptance
criteria:
- Vector writes must go through the governed `MutationExecutor`, not direct store writes.
- Traces are immutable, so embedded-state cannot be stamped on the trace. The worker
  needs an explicit external watermark or side table; **a tracking gap here silently
  skips rows**, which is the failure mode to test for first.
- Embed `outcome.summary` + `intent`, not the whole step log (mostly tool noise).
Acceptance: a trace written today is retrievable by semantic search after one worker
pass; a deliberately-interrupted pass resumes without skipping or double-embedding.

**A2 — [#338](https://github.com/ronsse/trellis-ai/issues/338) vector row metadata is a stale embed-time snapshot.** ✅ **LANDED** 2026-08-26 via [#343](https://github.com/ronsse/trellis-ai/pull/343)
The write-through alone would have fixed nothing observable. Two further defects sat behind
it, both verified on `main` first: `SemanticSearch` strips `content_tags` from the filters it
forwards, and — the larger one — `_build_filters` **returns early when `tag_filters is None`**,
so the `{"not_in": ["noise"]}` default was never constructed on the path MCP `get_context`
uses without a `domain`. Noise exclusion held on **neither** axis. Writing the acceptance test
is what exposed this; option 1 alone could not have passed it.

⚠️ **This changed what production packs return for every `get_context` call**, not just the 45
divergent rows. Worth watching the next packs served.

⚠️ Coverage caveat: the two new `VectorStoreContractTests` run against **SQLite only**. The
pgvector contract suite has never executed anywhere and its fixture is broken — see
[#345](https://github.com/ronsse/trellis-ai/issues/345). pgvector is the production backend.

**A3 — [#336](https://github.com/ronsse/trellis-ai/issues/336) effectiveness-based noise demotion is unsound without item attribution.** `class: panel` — **unblocked 2026-08-26**
Either gate the demotion behind sufficient attribution, or fix the attribution that feeds
it. **Read `pack_attribution_rate` (0.933), not the headline `attribution_rate` (0.359)** —
correcting an error in this file's first draft. The headline is dragged down by feedback on
work where no pack was served, which says nothing about whether a *served* pack's items were
cited. Demotion soundness depends on the latter, and by that measure the signal is much
healthier than the headline implied. The real weakness is **sample size** — 15 pack-targeted
events over 30 days — not citation rate.

**A4 — Raise feedback attribution from 32%.** `class: panel`
*(measured 2026-08-26; the framing below was wrong and the correction is the finding)*
Live attribution rate is 0.32 (12 of 37 feedback events over 30 days). Per-item rows
are what `learning/pack_observations.py` joins on; unattributed feedback contributes
nothing. This was written up as an *ergonomics* problem at the MCP surface —
`pack_id` and item ids being too easy to omit. **The event log says otherwise.**
Decomposing the 37:

| bucket | n | attributable in principle? |
|---|---|---|
| names a `pack_id`, cites item ids | 12 | already is |
| names a `pack_id`, cites nothing | **1** | yes — the only pure ergonomic loss |
| names only a `trace_id`, **no pack assembled anywhere in the preceding 6h** | **19** | no — no pack existed |
| names only a `trace_id`, some pack within 6h | 4 | maybe |

So the citation rate *given a pack was named* is **12/13 = 0.92**, and 107 of 109 cited
ids resolve to their pack's `injected_item_ids`. The surface works for the population it
can serve. What the headline number is mostly measuring is that **retrieval did not
happen before the graded work** — a retrieve-adoption problem, not a citation one, and
no change to the feedback surface can reach it. Sampled trace-level events say so in
their own notes: *"No context pack informed this work (no `get_context` call this
session)."*

Landed instead:
- **`attribution_rate` decomposed** (`ops/write_health.py`, `trellis analyze health`) into
  `pack_targeted_feedback` / `pack_targeted_attributed` / `pack_attribution_rate` /
  `untargeted_feedback`. The headline keeps its original denominator — narrowing it
  quietly would be the "metric improves because it was redefined" failure this repo
  keeps rediscovering.
- **Latent defect fixed:** `FeedbackRecordHandler` accepted a caller-supplied `pack_id`
  through `POST /feedback` and dropped it before emitting, so that whole family of
  feedback was unjoinable regardless of what the caller sent. It now forwards `pack_id`
  and derives `success` from `rating` (without which `_join_one` reads every governed
  grade as a failure). `trellis curate feedback` gained `--pack-id`.
- **`TRELLIS_REQUIRE_PACK_ATTRIBUTION`**, default **off**: enforcement at the MCP
  surface, shipped in the off position. See the escalation below.

**Escalated to the operator — panel split (exit 3), 2026-08-26.** On whether an uncited
pack-targeted `record_feedback` call should be **refused**: `openai/gpt-5.5` said yes
(0.78) — refusal is the only route to a joinable call that does not emit a second event
for the same pack, given the recording layer is idempotent per-call rather than
per-pack. `moonshotai/kimi-k3` said no (0.62) — the enforcement ceiling is ~1 event on
today's traffic and refusing risks losing the single highest-information event in the
corpus. Both independently said the *primary* investment is the metric decomposition,
and both flagged that "warn and ask for a follow-up call" double-counts unless
superseding is also built. The knob makes the operator's decision an environment
variable rather than another PR; it defaults to today's behaviour because changing
production posture is not the agent's call.

Acceptance, restated honestly: `pack_attribution_rate` is the number an ergonomic change
can move, and the mechanism does not fabricate attribution when the caller has none —
enforcement fails open whenever the pack resolves to nothing citable, and never touches
trace-level feedback. Depends-on for A3 should read `pack_attribution_rate`, not the
headline.

## Wave 1b — token economics: prove memory returns more than it costs

Added 2026-08-26 at operator request. The thesis of this system is that injected
memory is worth more than the tokens it consumes. **Nothing currently computes that
ratio**, so the thesis is an assumption. This wave makes it a number.

State of the instrumentation, measured 2026-08-26 — most of it already exists:

| Half of the ratio | Where it lives | Joinable? |
|---|---|---|
| Cost per *item* | `PACK_ASSEMBLED.injected_items[].estimated_tokens` | **yes** |
| Benefit per *item* | `FEEDBACK_RECORDED.helpful_item_ids` / `unhelpful_item_ids` | **yes** |
| Cost per *call* | `TOKEN_TRACKED.response_tokens` (+ `budget_tokens`, `trimmed`) | **no — see F1** |
| Cost in dollars | `trellis_cost.summarize_trellis_cost` → `overhead_dollars`, `by_operation` | n/a |

`trellis_cost.py`'s own docstring names the missing piece as deliberately out of
scope: *"what this deliberately does not claim: the agent's total spend ... or a ratio
against it."* This wave closes exactly that.

**F1 — Give `TOKEN_TRACKED` a `pack_id`, then compute value-per-token.** `class: panel`
The item-level join is arithmetically available today and nothing performs it. The
call-level join is blocked by **one missing field**: `track_token_usage`
([`retrieve/token_tracker.py:39`](../../src/trellis/retrieve/token_tracker.py)) emits
`layer` / `operation` / `response_tokens` / `budget_tokens` / `trimmed` / `agent_id`
and **no `pack_id`**, so response cost cannot be attributed to the pack that caused it.
Add it (free-form payload, additive, no schema break), then build the analyzer.

Primary metric — **useful-token fraction**: of the tokens injected into a pack, what
share went to items later marked helpful? This is directly actionable: if most injected
tokens land on items never cited, the pack is too wide and trimming is justified by
measurement rather than by taste. Report per strategy, per item type, and per intent
family, because the answer almost certainly differs by axis and a single global number
would hide which axis to trim.

Acceptance:
- `trellis analyze value` (or equivalent) reports useful-token fraction and
  dollars-per-cited-item over a window, with `--format json`.
- It reports **coverage** alongside every ratio, and refuses to state a ratio computed
  from too few attributed packs. See the dependency note below — a number derived from
  32% attribution that does not say so is exactly the measurement-wired-to-a-constant
  failure this repo keeps finding.
- Sectioned packs are handled or explicitly excluded with a stated reason
  (`build_sectioned` emits no `injected_items[]`, so it contributes zero rows).

**A4 landed 2026-08-26, and it changes this dependency.** The number F1's coverage refusal
must read is **`pack_attribution_rate` (0.933)**, not the headline `attribution_rate` (0.359)
— an error in this file's first draft. Of packs actually served and graded, 93% carry item
citations, so the join is far better fed than the headline suggested. F1's real constraint is
**sample size**: 15 pack-targeted feedback events across 31 packs in 30 days. State `n`
alongside every ratio and refuse to report one below a stated minimum.

**F2 — Act on the measurement: trimming and disclosure policy.** `class: panel`
Once F1 produces a number, the levers become tunable instead of guessed:
- **Excerpt width** — `truncate_excerpt` and the embed-time cut in `build_vector_row`.
- **Progressive disclosure** — #305 shipped index-mode packs and `get_items` batch
  fetch. The open question F1 answers is whether index-mode should become the
  **default** for exploratory intents, with full excerpts fetched only for items the
  agent actually opens. That trade is currently made by taste; it should be made by the
  useful-token fraction of each mode.
- **Skill size** — the retrieve/record skills are themselves injected context on every
  session. They are a fixed per-session cost and belong in the same budget.
Acceptance: each lever change is justified by a before/after useful-token fraction on
the same window, not by a plausibility argument.

**F3 — Counterfactual benefit (deferred, named so it is not forgotten).** `class: human`
F1 measures *precision of what was served* — the honest name for it is a value-density
proxy, not benefit. True benefit is counterfactual: does an agent with memory
outperform the same agent without it? That needs a withhold arm — deliberately serving
empty packs for a sampled fraction of tasks and comparing outcomes. That is a decision
about degrading live retrieval to learn something, which is an operator call, not a
panel one. Do not let F1's number get described as "benefit" in prose; it is not.

## Wave 2 — retrieval quality

**B1 — [#298](https://github.com/ronsse/trellis-ai/issues/298) same-day trace/artifact stubs outrank topical content.** `class: panel`
Open 18 days; the oldest untouched retrieval defect. Partially mitigated by the content
floor and by #311's skip-discipline prompts — **re-measure before implementing**, the
remaining gap may be smaller than the issue describes.

**B2 — PackBuilder chunk rollup (roadmap §G.4).** `class: panel`
`PackBuilder` dedups by `item_id`, so two chunks of one document can both enter a pack
and spend the budget twice. Group by `parent_doc_id` at assembly. Also default-filter
`chunk_index` rows out of the documents list view.

**B3 — Index the alias resolver (roadmap §G.4).** ~~`class: panel`~~ — **DONE 2026-08-02, verified 2026-08-27.**
The premise is stale. [#289](https://github.com/ronsse/trellis-ai/issues/289) (`a889c85`)
replaced both duplicated scans with the shared
[`entity_resolution.build_name_alias_resolver`](../../src/trellis/extract/entity_resolution.py):
an indexed `resolve_alias(source_system="name", key)` row read first, a bounded scan only
to bootstrap, and the unambiguous result minted back into `entity_aliases` so the next
lookup is indexed. Both call sites delegate to it
(`memory_ingest_hook._graph_alias_resolver`, `mcp/server.py:_build_alias_resolver`), and
all four backends index the lookup — SQLite/Postgres with a partial *unique* index on
`(source_system, raw_id) WHERE valid_to IS NULL`, the Bolt pair (Neo4j, ArcadeDB) with a
non-unique composite index plus close-then-insert. ~20 tests in
`tests/unit/extract/test_entity_resolution.py`. **Do not rebuild this.**

Three findings from the verification, none of which the original item anticipated:

- **The resolver has never executed in production.** `TRELLIS_ENABLE_MEMORY_EXTRACTION`
  is unset on both the `trellis-api` and `trellis-mcp` containers, so
  `_build_memory_extractor` short-circuits to `None` on every surface except the
  `trellis-skynet` CLI wrapper with an explicit `--extract`. Measured evidence: **zero
  rows in `entity_aliases` (all namespaces) and zero `mentions` edges**, against 964
  current nodes and 1239 documents. So an empty alias table is *not* evidence the
  write-back is broken — the path is dark. Anyone measuring this loop must check the env
  flag first.
- **Minting only extinguishes scans for names that resolve.** 118 of 119 `@mention`
  occurrences in the corpus match nothing (`@gmail` ×21 from email addresses,
  `@modelcontextprotocol` ×6 from npm scopes), and a zero-match scan has nothing to bind,
  so it rescans forever. Partly addressed: the extractor now resolves each *distinct*
  token once per document, which removed 40% of resolver calls on this corpus.
- **B3′ — the real residual gap: past `DEFAULT_NAME_SCAN_LIMIT` the resolver stops
  learning and starts being wrong.** `class: panel` — this is the item worth queueing.
  `query` is `ORDER BY created_at DESC LIMIT n`, so above 2000 current nodes an older
  entity reports a clean "no match" (the duplicate-`hermes` failure #289 ended, returning
  by another door) and `mintable` is permanently `False`. 964 current nodes growing
  ~30/day (~17/day excluding a one-off backfill) puts the cliff weeks out. Raising the
  cap is not the fix — it delays the cliff and leaves the silent wrong answer. The fix is
  to stop needing the scan: mint a `name` alias when the entity is written, plus a one-off
  backfill. That spans the mutation write path and a CLI admin command, **not**
  `extract/`, so it needs its own item and owner sign-off on touching a store-adjacent
  write path.

## Wave 3 — security floor (Productionization §3.H.1)

**C1 — Wire the policy gate.** `class: panel` — **do this first; it is an undeclared prerequisite**
`MutationExecutor` skips Stage 2 entirely when `policy_gate is None`
([`executor.py:135`](../../src/trellis/mutate/executor.py)), and `build_curate_executor`
— the single factory every surface uses — passes only `event_log` and `handlers`
([`mutate/__init__.py:43`](../../src/trellis/mutate/__init__.py)). `DefaultPolicyGate`
is exported but never constructed outside tests. The documented five-stage governed
pipeline is a four-stage pipeline in production. Not a defect in itself (gates are
injected by design) but #194 cannot be satisfied until something wires one.

**C2 — [#256](https://github.com/ronsse/trellis-ai/issues/256) extract Bolt backends to a `trellis-stores-bolt` plugin.** `class: panel`
Keystone, labelled `ready`. Halves #194's enforcement surface, so it precedes C3.

**C3 — [#194](https://github.com/ronsse/trellis-ai/issues/194) classification enforcement, minimal slice.** `class: blocked` → `panel` once C1+C2 land
Populate `DataClassification` on write paths; PackBuilder/search filter and the policy
gate deny by caller scope. Carries an owner-approved exception to pull one slice of
tag-vocab Phase 4 forward. Acceptance is already written in roadmap §3.H.1.

**C4 — [#264](https://github.com/ronsse/trellis-ai/issues/264) log every judged memory operation as a training example.** `class: panel`
Labelled `ready` / `mechanical`. The classify-layer instance already exists
(`JudgedOpType.CLASSIFICATION` from #321); this generalizes it.

## Wave 4 — query-history curation primitives (§3.H.2)

Fixture-testable now; the consumer-kg pilot restart is the *validation* gate, not the
implementation gate. Spec: [`adr-query-history-promotion.md`](./adr-query-history-promotion.md) §2–§5.

| id | item | class |
|---|---|---|
| D1 | [#200](https://github.com/ronsse/trellis-ai/issues/200) usage families — pipeline-operational vs analyst, distinct promotion rules | `panel` |
| D2 | [#202](https://github.com/ronsse/trellis-ai/issues/202) matching guardrails — `user` must not match `vendor_user_id` | `panel` |
| D3 | [#203](https://github.com/ronsse/trellis-ai/issues/203) aggregate-only readiness scout, no raw SQL in output | `panel` |
| D4 | [#201](https://github.com/ronsse/trellis-ai/issues/201) BI/dashboard metadata source — largest, may slip to pilot restart | `blocked:signal` |

## Wave 5 — capture density

**E1 — [#306](https://github.com/ronsse/trellis-ai/issues/306) observer-agent capture via local model.** `class: panel`
Extends #255 session auto-capture from session-level to tool-level density using
hermes3:8b as observer (free, private — `DETERMINISTIC > LOCAL > FRONTIER`). Drafts
route through the governed pipeline and the memory-path draft policy.
**Precondition:** #255's own defect history is instructive — it shipped in July and did
not actually run until August because of blocked turn ordering and a context-window
coupling where Ollama ignores `num_ctx` and hermes fabricates. Verify the observer
produces non-fabricated output on a held-out transcript *before* wiring it to writes.

**E2 — Capture-coverage measurement.** `class: panel` — **DONE, PR #372 (unmerged).**
[#332](https://github.com/ronsse/trellis-ai/issues/332) fixed the sidechain rule that
discarded 61% of transcripts. Nothing measured what fraction of sessions produce a
memory, so the next silent coverage regression was invisible. Built before E1 adds
capture surface, as the item asked.

Landed as `sessions_with_memory / sessions_triggered` in `trellis analyze health`
([`ops/capture_coverage.py`](../../src/trellis/ops/capture_coverage.py)). Three things
worth carrying into E1:

- **The denominator is `should_distill`, the pipeline's own deployed gate** — not a new
  eligibility rule. `sessions_seen` is dominated by watermark skips (production
  2026-08-27: 291 seen, 21 adjudicated), and sampled-out sessions are excluded so the
  rate cannot track a cost knob.
- **Absence is a state, not a zero.** `unobserved` / `stale` / `degraded` / `measured`,
  with `capture_rate = None` below `MIN_ELIGIBLE_SESSIONS`. Pointed at production it
  correctly reads `unobserved` while noting 59 sessions *did* store a memory — a naive
  metric would have reported 0% on a working pipeline. It is **not** keyed on
  `write_provenance`: the sweep is a host-run worker, and that stamp is wrong rather
  than stale ([#348](https://github.com/ronsse/trellis-ai/issues/348)).
- **`CORPUS_SYNCED` is not a sweep-liveness signal** and cannot be made into one — it
  fires from the write seam, so a sweep that kept nothing emits nothing. 8 events in 30
  days against a nightly cron. Hence `CAPTURE_SWEEP_COMPLETED`, emitted unconditionally.

Also split `sessions_skipped_empty` out of `sessions_sampled_out`: under the old
counter, #332's 61% presented as a sampling decision.

**#365 — retrieval availability.** Option three shipped in the same PR: `analyze health`
states that `untargeted_feedback` assumes non-retrieval and that retrieval availability
is unmeasured. The other two shapes remain open and are follow-ups; the issue's own
ordering holds, since recording an attempt *on arrival* cannot see a call that never
arrives.

---

## Repo-level gap

`main` has **no required status checks** and `required_approving_review_count: 0`;
`allow_auto_merge` is `false`. Any push or merge can land on `main` without CI having
passed. Every workflow exists and runs on PRs — nothing enforces the result. Agents
working this backlog gate on CI themselves, but that is a convention, not a control.

`class: human` — this is a repo-settings change and stays with the operator.

## Deliberately not in this backlog

- **[#250](https://github.com/ronsse/trellis-ai/issues/250) Aura credential purge** — 3 of 4 tasks verified done 2026-08-26; the remaining one needs a browser session at the Neo4j console. Irreducibly operator-only.
- **[#257](https://github.com/ronsse/trellis-ai/issues/257) ingest-normalization ADR** — labelled `owner-only`.
- **[#261](https://github.com/ronsse/trellis-ai/issues/261) promote-to-standing advisory** — `blocked:signal`; needs loop throughput this deployment does not yet produce at single-user scale.
- **Tag-vocabulary phases, B.4 RDF export, E.4 AWS dry-run** — gated on a design partner asking or on infra access, per roadmap §4.
