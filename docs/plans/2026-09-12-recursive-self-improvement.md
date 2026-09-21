# Capability plan: autonomous graph evolution and recursive self-improvement

**Date:** 2026-09-12 · **Status:** proposal, nothing executed
**Supersedes nothing.** Sits above `2026-09-09-overnight-corpus.md`, which is
maintenance; this is the capability track.

---

## 0. Why this document exists

The request was: stop doing infrastructure, plan to finish the issue backlog, then
analyse the codebase deeply and propose a capability — specifically, a system inside
Trellis that changes the *shape of its own graph* on its own initiative, past some
threshold, without an outside system telling it to. And beyond that, to ask what
recursive self-improvement would actually take.

I measured production before designing anything. Three findings changed the plan, and
one of them changes what the project even is.

One more thing happened before this document was handed over, and it belongs at the
top rather than buried in §7. The plan pre-registers what would refute it. I then ran
one of those checks — the load-bearing one under §5.1 — **and it fired**. Sections 5.1,
6-Phase-1 and 7 are the rewritten versions. The change is narrow and it matters: the
dense signal this plan depends on is real, but it is *half* a training pair, not a
ready-made one, and one of its two arms is currently a hardcoded constant. What that
costs is a Phase 1 that builds a column instead of consuming a stream.

---

## 1. What I measured

Read-only probes against the production Postgres (`trellis_knowledge`,
`trellis_operational`) on 2026-09-12, plus the SQLite ops-tier stores under
`~/.trellis/data/stores/`. Every number below is re-derivable; the queries are in
§8.

### 1.1 The graph

| Quantity | Value |
|---|---|
| Node rows / **current** nodes | 1,995 / **1,688** |
| Edge rows / **current** edges | 1,738 / **1,732** |
| **Edges per node** | **1.03** |
| Nodes at degree ≤ 1 | **1,347 of 1,688 (79.8%)** — 1,164 at exactly 1, 183 isolated |
| Nodes carrying a `document_ids` link | **143 of 1,688 (8.5%)** |
| Nodes with exactly one SCD-2 version | **1,622 of 1,688 (96.1%)** |
| `entity_aliases` rows | **0** |
| `extraction_status` set | 17 of 1,688 (1.0%) |

Current edge types, exhaustively:

```
used                762     wasGeneratedBy      423
wasAssociatedWith   246     appliesTo           164
wasAttributedTo     133     hasObservation        4
```

### 1.2 The event stream

10,117 events, 2026-07-06 → 2026-09-12 (68 days). Trailing 30 days:

```
mutation.executed  2,227    memory_op.judged   1,868    entity.created   1,127
link.created         936    memory.stored        842    trace.ingested     153
pack.assembled        76    feedback.recorded     72    write.rejected      71
```

### 1.3 The ops tier

| Store | Table | Rows |
|---|---|---|
| `outcomes.db` | `outcomes` | **0** |
| `tuner_state.db` | `proposals` | **0** |
| `tuner_state.db` | `tuner_cursors` | **0** |
| `parameters.db` | `parameter_snapshots` | **0** |

---

## 2. Three findings

### Finding 1 — the graph is a provenance ledger, not a knowledge graph

At 1.03 edges per node with 80% of nodes at degree ≤ 1, this is a forest of stars, not
a network. Every one of the six live edge types is PROV-O provenance (`used`,
`wasGeneratedBy`, `wasAssociatedWith`, `wasAttributedTo`) or the advisory link
(`appliesTo`). **There is not one semantic relation in production** — nothing says
*this depends on that*, *this contradicts that*, *this is part of that*. The graph
records who did what, when. It does not record what relates to what.

`Activity` (406 nodes) × `used` (762) × `wasGeneratedBy` (423) is the trace-extraction
signature: each recorded activity touches some files and produces some outputs. That
is a write-ahead log rendered in graph form.

Two consequences bear directly on the request:

- **8.5% document linkage** means a graph hit is a bare name. The 2026-07-17 review
  called the graph "name-only junk"; on linkage that is still true 57 days later.
- **96.1% of nodes have never been revised.** SCD-2 temporal versioning is built,
  correct, and contract-tested — and essentially nothing exercises it. `entity.updated`
  has fired 14 times, ever.

**So there is no shape to reshape yet.** A threshold-driven graph reshaper pointed at
this graph would be reorganising a provenance log. That does not kill the project; it
sequences it.

### Finding 2 — the self-improvement loop is already built, and it has never run

This is the finding that changes what the project is.

`src/trellis/learning/tuners/` contains a complete closed loop:

```
RuleTuner.apply_rules        → reads outcomes, proposes a parameter change
promote_proposal             → gates on effect size, emits PARAMS_PROMOTED
run_auto_promotion           → promotes what clears the stricter auto gate
monitor_post_promotion       → watches the metric after the change
run_post_promotion_sweep     → auto-rolls-back on degradation
```

It is wired to a CLI (`trellis metrics tune`, `trellis metrics promote`,
`trellis worker tune`) and a REST admin route. `trellis worker tune` already runs the
whole arc — proposal, governed promotion, `PARAMS_AUTO_PROMOTED`, armed monitoring with
`auto_demote=True`, and `PARAMS_AUTO_ROLLED_BACK` on regression.

**Propose → gate on effect size → auto-promote → monitor → auto-rollback is recursive
self-improvement machinery. Trellis has it. It has zero rows in all four of its state
tables and no cron entry, 68 days in.**

The root cause is one keyword argument. The only producer of an `OutcomeEvent` in the
codebase is `_emit_outcome` in `src/trellis/feedback/recording.py:387`, and it only
fires when the caller passes `outcome_store=`. Both production callers omit it:

- `src/trellis/mcp/server.py:2521` — passes `event_log=`, not `outcome_store=`
- `src/trellis_api/routes/curate.py:198` — same

The parameter defaults to `None`. So:

```
record_feedback  →  [outcome_store never supplied]  →  OutcomeStore: 0 rows
                 →  RuleTuner aggregates nothing    →  0 proposals
                 →  auto-promote has nothing to promote
                 →  rollback has nothing to monitor
```

Every reference to `outcome_store` outside `ops/` and `stores/` is a *consumer* inside
`learning/tuners/`. The subsystem has consumers and no producer.

This is the failure class CLAUDE.md already names three times — #345's pgvector
contract that had never executed anywhere, #424's executor built outside the factory,
#255 shipping in July and writing nothing until August while reporting success. **The
self-improvement stack is the largest instance of that pattern in the repo, and it went
unnoticed because nothing reads its output.**

### Finding 3 — the signal is thin where the loop reads and dense where it doesn't

The tuner learns from `OutcomeEvent`s bridged from pack feedback. That population is
**72 events in 30 days**, against **76 packs assembled**. Roughly 2.5 graded
retrievals per day.

Meanwhile `memory_op.judged` fires **1,868 times in 30 days** — 62 per day, **25×
denser** — and nothing consumes it: no query in `src/` filters on that event type at
all. #264 ("log every judged memory operation as a training example") is open and
labelled `ready`.

What that costs, concretely. Two-proportion test, 80% power, α = 0.05, using
n ≈ 16·p̄(1−p̄)/δ² at p̄ ≈ 0.5:

| Signal | n per arm (1 month) | Smallest detectable shift |
|---|---|---|
| Pack feedback | 72 | **±24 points** |
| Judged memory ops | 1,868 | **±4.6 points** |

A tuner reading pack feedback can only see effects larger than 24 percentage points.
Nothing in retrieval tuning is that big. **On its current input the loop is not
under-powered, it is blind** — and it would report its blindness as "no proposal
cleared the gate," which is indistinguishable from "nothing needed changing."

**The dense stream is half a training pair, and I checked which half.** Before writing
§5.1 I probed the payloads rather than trusting the density. `memory_op.judged` does not
carry a `verdict` key at all — the field is `decision` — and what it holds is the
system's *own output*, not an outcome:

| `op_type` | n (30d) | `decision` values | What it is |
|---|---|---|---|
| `classification` | 999 | 12 labels (`reference` 317, `research` 269, `task-list` 114, …) | the proposed `content_type` |
| `distillation` | 869 | **`keep`, 869/869** | a literal at the call site |

The distillation constant is **structural, not empirical**: `capture.py:397` passes
`decision="keep"` as a string literal, inside a loop that only runs for candidates that
already landed in the document store. A `drop` cannot be emitted. That is the failure
class this repo keeps producing and CLAUDE.md keeps recording — a measurement path wired
to a constant (#345, #424, #255, the reference rate that could only read 1.00).
`confidence` is at least not constant (0.9 · 744, 1.0 · 653, 0.85 · 238, 0.95 · 198,
then a tail to 0.6), but it is self-reported by the same model that produced the
decision, so it grades nothing.

**This is by design, and the design says so.** `schemas/memory_op.py`'s own docstring
defines the shape as `(input context, decision, downstream outcome)` and states that
"the feedback-attribution join that supplies the downstream-outcome *label*, land
separately (#263). Core's deliverable ends at the payload shape plus the join being
possible." `subject_ref` exists to be that join key; `event_log.py:216` repeats it. So
1,868 rows of input-and-decision are accruing exactly as intended, with the outcome
column empty and **zero readers** — nothing in `src/` queries this event type.

The density argument survives intact. The "it is already a supervision signal, just
consume it" claim does not: what exists is a dense *candidate* stream that needs an
outcome joined to it. Section 5.1 is written against the corrected version.

---

## 3. Part 1 — finishing the backlog

37 open issues. The split matters more than the count:

| Bucket | Count | Who |
|---|---|---|
| `blocked:owner-decision` | 9 | **you** |
| `owner-only` | 7 | **you** |
| `blocked:signal` | 3 | blocked on data that does not exist yet |
| `blocked:dep` | 1 | **you** |
| `ready` | 5 | agent |
| everything else unblocked | ~17 | agent |

Deduplicated, **17 issues need a decision from you and cannot be worked around**;
about 20 are agent-executable today. Plus four PRs green and waiting on a merge
(#555, #552, #553, #554).

### 3.1 The decision sheet

These are the 17. Grouped so they can be cleared in one sitting rather than one at a
time over a month. Each needs a yes/no or a pick, not a design session.

**Spaces and boundaries** (4) — one coherent decision, they interlock:
#474 confirm-to-save for global semantic memory · #475 Assumptions header on packs ·
#476 enforce personal/work/household spaces at pack and mutation time ·
#477 contradiction invariant + `last_verified`

**Query-history curation** (3) — all three are downstream of one call on whether
analyst usage curation is in scope at all:
#200 separate operational from analyst query history · #202 guard against broad domain
keyword false positives · #203 graph-safe query-history scouting

**Operational catch-up** (4) — these are mine to do, yours to authorise:
#546 production runs a commit on no remote branch · #547 Stop hook rewards
non-retrieval · #548 retrieve-before-task covers 1 of 8 retrieval mechanisms ·
#405 environment and infrastructure debt

**Credential and publishing** (3) — never agent-eligible by the standing rule:
#250 AuraDB credential hygiene · #208 ArcadeDB secret + expired AWS SSO ·
#256 extract Bolt backends to a plugin (ends in a PyPI publish)

**Strategy** (3):
#194 enforce data classification on retrieval and mutation (keystone, security) ·
#478 evaluate the OpenAI data-agent context contract · #275 the board itself

`blocked:signal` (#261, #201, plus #208) unblocks as a side effect of Phase 1 below —
they are waiting on data volume, not on a decision.

### 3.2 Sequencing the ~20 agent-executable ones

Do not work them in issue order. Six of them are Phase 1 and Phase 2 of the capability
plan, and doing them in capability order means the backlog burns down *and* the
capability lands, instead of trading against each other:

- **Phase 1 pulls in** #264, #550, #365, #503, #502
- **Phase 2 pulls in** #375, #371, #549, #463
- **Genuinely independent maintenance**: #551, #360, #514, #515, #306, #556, #494, #356

That leaves eight issues of pure maintenance, which is the right amount of it.

---

## 4. Part 2 — the capability: autonomous graph evolution

### 4.1 What it concretely means

"The system decides the graph should look different" resolves to six actuators. The
table below orders them **by how reversible each one is**, which is what §4.2 consumes.
It is not a build order, and §4.1.1 says why not.

| # | Actuator | Trigger shape | Reversal |
|---|---|---|---|
| 1 | **Summarise a neighbourhood** — mint a summary node, re-point edges | degree > threshold, or N nodes sharing a parent document | Summary node is additive; SCD-2 keeps pre-repoint versions |
| 2 | **Merge duplicates** — bind an alias | high name/embedding similarity + compatible types | `entity_aliases` is a separate table; unbind restores |
| 3 | **Adjust edge confidence** | corroboration count crosses a threshold | Property update, SCD-2 versioned |
| 4 | **Retire** — lifecycle stamp, not deletion | no citation in N days + no inbound edges | `retention.restore` (exists, fired once) |
| 5 | **Re-type** — change `node_type`/`edge_type` to a canonical | what `schema_evolution.py` already proposes, surface-only today | SCD-2 versioned |
| 6 | **Mint semantic edges** — the class the graph has none of | co-citation in packs, shared document, extractor agreement | New edge; retire to reverse |

Actuator 6 is the one that changes the graph from a provenance ledger into a knowledge
graph. It is also the only one with no existing analogue in the codebase.

### 4.1.1 Build order is not that order — reversibility governs authority, not sequence

The table above was read as a queue when it was first written, and that is a category
error this plan should not ship with. Reversibility decides *what an agent may do
without asking* (§4.2). It says nothing about which actuator is worth building, and the
two orderings turn out to be close to opposites: merging duplicates is second-most
reversible and has almost nothing to merge, while the thing with the largest ceiling
needs a prerequisite that is not in the table at all.

§5.2's own rule settles it — **measure the ceiling before building the actuator** — so
the build order is derived from base rates, re-measured on production on **2026-09-20**
(read-only, 1,975 current nodes / 2,026 current edges / 0 alias rows; #594's reading a
week earlier was 1,896 / 1,945, and the shape is stable: mean degree 2.05 both times,
isolation 11.2% → 11.5%).

**0 · Referent identity and qualifier capture — a prerequisite, not an actuator.**
PR #599 (`docs/design/adr-referent-identity.md`, status Proposed, **not merged**, so
the path does not resolve from this branch) chose `EntityAlias` per
`(source_system, raw_id)` and leads with what that does *not* solve:
the mechanism supplies convergence, the producer must supply the qualifier, and
**0 of 991** outward-denoting nodes carries one today. Adopting the mechanism first is
worse than the status quo, because it converts an ambiguous name into an authoritative
binding. Nothing that mints an external edge can precede this.

**1 · Mint semantic edges, starting at the `save_knowledge` seam (actuator 6).** The
highest ceiling, and the seam is now identified rather than asserted. Of 196 nodes
minted under the lowercase `save_knowledge` types, **182 are isolated (93%)** —
`gotcha` at 81/83, `concept` at 70/76. Against a whole-graph isolation of 228/1,975
(11.5%), those 196 nodes are **10% of the graph and 80% of all its isolation**. The
tool built for durable knowledge deposits entities with no edges at all, so this is a
producer-seam repair with a known denominator, not a mining exercise over the whole
corpus.

**2 · Re-type (actuator 5), which is most of what looks like dedup.** Six of the
eight duplicate-name groups are cross-type (item 4), so three-quarters of the work
a dedup actuator appears to have is actually a type disagreement.

**3 · Summarise (1), adjust confidence (3), retire (4)** — untested ceilings, and each
needs a signal Phase 1 has not yet produced.

**4 · Merge duplicates (actuator 2) — provisionally refuse.** 8 duplicate-name groups
covering 14 extra nodes (0.71% of nodes), and splitting them by type leaves **2
same-type groups** against **6 cross-type**. Cross-type collisions are type
disagreements, not merges; they belong to actuator 5. So the merge ceiling is ~2 groups,
≈0.1% of the graph, which is the size §5.2 says to refuse at. This is also why K4 found
**zero** exact-name collisions among outward referents: for the `artifact:` namespace
the minted id *is* the verbatim name, so two producers typing the same string converge
by construction. The defect there is under-qualification, not divergence — and
under-qualification is item 0's problem, not a merge's.

**The two items M4 orders ahead of dedup are two ends of one missing seam.** Every
locator-shaped property in the graph — 137 key instances, `repo` 65, `machine` 41,
`host` 17, `file` 9, `endpoint` 4, `url` 1 — sits on a lowercase `save_knowledge` node.
**Zero** sit on any of the 991 nodes whose type denotes something outside Trellis
(`SoftwareApplication` 851, `File` 125, `Dataset` 7, `Device` 4, and one each of
`API` / `Command` / `SystemdUnit` / `Wrapper`). So the nodes that hold addresses have no
edges, and the nodes that *are* external referents have no addresses. Edge-minting and
qualifier capture are not two independent prerequisites; they are the same gap seen from
both ends, which is the argument for doing them adjacently and before anything else.

**Reconciling this with #594, because the numbers differ.** #594 reported "265
external-referent nodes carry one `url` and four `endpoint`s between them" and "only 4
edges connect two external referents". Under K4's definition — the eight node types that
denote something outside Trellis — the same prod database gives **991 nodes, 0 carrying
any locator key, and 0 edges with such a node at both ends** (`used` 901 and
`wasGeneratedBy` 138 touch one; nothing joins two). The sets are not comparable: #594
stated no definition for its 265. The reconciliation is that the graph's single `url`
and four `endpoint` keys sit on `system` and `gotcha` nodes — `save_knowledge` deposits,
not extractor-minted referents — so #594's set must have included the lowercase types,
and its conclusion ("there is no addressing scheme for a federation map to resolve
against") holds *more* strongly than it stated, not less. **State the type set with any
count of this population**; without one the number is not re-derivable.

### 4.2 The reversibility boundary — this is inside your existing rule

Your standing rule is *authority is bounded by reversibility, not importance*. Graph
mutation passes it, and not by argument — by construction:

- **SCD-2 means graph writes are non-destructive.** A change closes the old version
  (`valid_to`) and opens a new one. The prior shape is still readable via `as_of`.
- **The governed pipeline** validates, policy-checks, idempotency-checks and emits an
  event for every mutation, so each autonomous change is auditable and attributable.
- **The event log is immutable**, so the record of what the system did to itself cannot
  be edited by the system.

**The hard stop is deletion.** CLAUDE.md pins `GraphStore` deletion as *a physical
purge of all versions, edges and aliases* — `redaction.apply` depends on that. A purge
is not reversible and must stay outside the actuator set permanently. Actuator 4 is a
*lifecycle stamp* (`retention.prune`, which `retention.restore` undoes), never
`graph_store.delete`. That distinction is the whole safety argument and it should be
enforced in code, not in prose: the autonomous path gets a handler allowlist that does
not contain the delete command, and a test asserts the allowlist by AST the way
`test_policy_gate_rule.py` already does for policy gates.

### 4.3 Counterfactual replay cannot evaluate this — so build a shadow graph

This is the sharpest constraint, and the repo already recorded it. From CLAUDE.md on
#371:

> Counterfactual replay cannot evaluate this class of change: `pack_replay` re-walks
> `budget_trace[]`, which records the candidates the walk *saw*, and a seeding change
> alters which candidates exist — both arms have to actually run.

Every one of the six actuators alters which candidates exist. **So `analyze replay` —
the evaluation method this codebase built and trusts — is structurally unable to judge
graph reshaping.** Any plan that assumes otherwise is measuring nothing.

What does work is the pattern `classify/shadow.py` already established for tags: write
the verdict somewhere retrieval cannot see, compare, promote only what wins.

**Graph shadow overlay.** A proposed reshaping is written as a shadow layer — shadow
nodes/edges keyed so that `GraphSearch` cannot address them (the same structural
unaddressability that makes `content_tags_shadow` safe: filters address
`$.content_tags.<facet>`, so a sibling key is unreachable). Then:

1. Assemble each pack **twice** — once against the live graph, once against
   live+overlay. Serve the live one. Record both.
2. The citation the agent gives grades the served pack. The overlay arm's score is
   computed from the same citations against its own ranking.
3. Promote the overlay into the live graph only when the paired difference clears an
   effect-size gate — reusing `promote_proposal`'s existing machinery rather than a
   second one.
4. Arm `monitor_post_promotion` with `auto_demote=True`, so a reshaping that degrades
   outcomes rolls back on its own.

Shadow assembly costs a second pack build per retrieval. At 2.5 packs/day that is
free. It stops being free around 100/day, which is a problem worth having.

### 4.4 What the threshold is allowed to be

The user's framing — "after a certain threshold, the shape should look this way" —
needs one guardrail, drawn from #336's demotion gate:

**Act on evidence of harm, never on absence of evidence of benefit.**

#336 is the cautionary case and it is exact. The noise proposal rule used
`helpful_citations / appearances < 0.3`, which reads as "served often, rarely used" —
but P(cited helpful | served) was measured at **0.1029**, so an ordinary item goes
uncited twice with probability 0.805. The rule flagged **64 of 79 scored items**
including durable memories, *by construction*. Nobody had measured the base rate the
threshold implicitly assumed.

Every threshold in the graph evolver therefore has a precondition: **the base rate it
implies must be measured first, and the proposal rule must be shown to fire on less
than a stated fraction of the corpus before any actuator is wired to it.** A rule that
would fire on 80% of the graph is not a threshold, it is a constant.

---

## 5. Part 3 — what recursive self-improvement actually takes

Three things, in this order. Only the third is novel.

### 5.1 A signal dense enough to learn from

Established in Finding 3: at 72 graded retrievals a month, the smallest detectable
effect is ±24 points. Nothing worth tuning is that large. **This is the binding
constraint on the entire project and it is arithmetic, not judgement.**

The fix is not "collect more feedback" — retrieval happens 2.5 times a day because that
is how often work happens. Density has to come from a stream that fires on *writes*, and
one already does: `memory_op.judged`, 1,868 rows in 30 days, running now.

**But it is not a supervision signal yet, and Finding 3 says exactly why.** Each row is
`(input digest, decision, subject_ref)` — the first two thirds of the training pair its
own schema defines. The `decision` is the system's own output: a `content_type` label on
the classification half, and the literal string `keep` on all 869 rows of the
distillation half. Nothing in the row says whether the decision was *right*.

So Phase 1's work is not "consume #264." It is **join an outcome to those 1,868 rows**,
which is what #263 named and left unbuilt. The join key already exists and is already
populated — `subject_ref` is `(ref_type="doc", ref_id=<doc_id>)`, deliberately kept as a
join key rather than a label. The outcome to join is the one the retrieval side already
produces per item: *was this memory served, and when served, was it cited helpful?* That
turns a judged distillation into a real pair — "the judge kept this; downstream it was
served 4 times and cited twice" versus "the judge kept this; it has never been served in
68 days."

Two consequences worth stating before anyone builds it:

1. **The outcome is sparser than the decision, and that is fine.** Not every judged
   document gets served, so the joined population is smaller than 1,868. It is still the
   right denominator, because a judged-but-never-served memory is itself a label — the
   judge's precision is measurable against serving even when citation is absent. This is
   the one place the plan deliberately departs from #336's rule (act on evidence of harm,
   never on absence of evidence of benefit): #336 governs *demoting an item*, which is
   destructive; grading *the judge* off a never-served population is a statement about a
   classifier, not a sentence on a memory. Keep the two separate or the gate gets
   re-litigated.
2. **Fix the `keep` constant in the same change, or the distillation half stays
   unlearnable.** 869 identical labels carry zero bits. The rejected population already
   exists one function up: `_gate_candidates` (`capture.py:123`) throws candidates out for
   three recorded reasons — `blocked_scan`, `rejected_injection`, `rejected_worthiness` —
   counts them into the report, and emits nothing. `_emit_training_pairs` then runs over
   the survivors only, and re-filters again on "did the doc actually land." So every drop
   is already known, named, and discarded at the exact seam that would record it. Emitting
   a judged row for those with the gate's own reason as the `decision` is a small edit and
   is the difference between a stream with variance and a stream without. Do it before
   measuring anything: until then the distillation arm's accuracy is 100% by construction,
   and a model trained on it learns to say `keep`.

### 5.2 An actuator whose effect is measurable in that signal

This is where domain promotion died, and the lesson is worth keeping. It was built,
measured, and **refused**: the best possible query over all 1,160 domain tags reached
69 of 1,005 documents, median 1. The actuator worked; the ceiling made it pointless.

So: **measure the ceiling before building the actuator.** For each of the six, ask what
the best achievable outcome would be if it worked perfectly, and refuse it if the
answer is small. Actuator 6 (semantic edges) has the highest ceiling by a distance —
the graph axis currently contributes a recency feed; making it query-relevant is the
difference between one of three retrieval axes working and not.

That rule has now been applied to all six, and it changed the order (§4.1.1, measured
2026-09-20). Two results are worth carrying here rather than only there. Actuator 6's
ceiling has a **denominator**: 182 isolated nodes out of the 196 the `save_knowledge`
types have minted, which is 80% of the whole graph's isolation concentrated in 10% of
its nodes. And actuator 2 (merge duplicates) is the second case after domain promotion
where the rule says **refuse** — 2 same-type duplicate groups on a 1,975-node graph,
≈0.1%, with the other 6 groups being type disagreements that belong to actuator 5.
Refusing an actuator is the rule working, not the rule failing.

### 5.3 The immutable core — the part that makes it *safe* to be recursive

A system that tunes its own parameters will, given a throughput objective and enough
iterations, discover that the cheapest way to promote more proposals is to lower its own
promotion threshold. This is not speculative; it is the shape of every optimiser given
access to its own gate.

**The denylist must exist and must not itself be tunable.** Specifically, the
`ParameterRegistry` needs a set of keys the tuner may never write, and that set must be
a code constant with an AST-derived test — not a config value, not a policy row, not
anything the running system can reach:

- promotion and auto-promotion thresholds
- the effect-size gate and its power requirements
- the post-promotion monitoring window and rollback trigger
- the handler allowlist from §4.2 (no delete, ever)
- the denylist itself

Everything else is fair game. That boundary is the same one you already drew in
`~/.claude/CLAUDE.md` — reversibility, not importance — pushed down into the system so
it holds without an operator in the loop.

**And the recursion stops one level up.** The system may propose changes to its
proposal rules. It may not promote them. A change to how the system decides is a change
to the boundary, and that comes to you — exactly as a split panel does.

---

## 6. Phasing

Each phase has a **falsification gate**: a cheap check that kills the phase if it
fails, before the expensive part.

### Phase 0 — reconnect what exists · days

**This section said "a two-line change" and that was wrong.** Executing it — running
`record_feedback` against a real `SQLiteOutcomeStore` rather than reading the call
sites — found **four** defects, filed as [#557]. Two are fixed
([#558]); two are not, and the second of those means the tuner still cannot
match a rule after the wiring lands.

| | Defect | State |
|---|---|---|
| **D1** | `outcome_store=` supplied at neither agent-facing call site, so `_emit_outcome` — the only producer of the only input of `RuleTuner` — has never run. `outcomes.db`: 0 rows since 2026-07-06, against 72 graded feedback events in 30 days. | **fixed** (#558) |
| **D2** | The bridge stamps `component_id="retrieve.pack_builder.PackBuilder"`; all three `DEFAULT_RULES` target `retrieve.strategies.KeywordSearch` / `GraphSearch` / `retrieve.rerankers.RRFReranker`, matched by exact string equality. Empty intersection. A **granularity mismatch** — feedback is graded against the *pack*, rules actuate *strategies* — not a typo, so closing it needs per-strategy attribution off `PACK_ASSEMBLED.injected_items[].source_strategy` plus a mapping to the dotted class paths. | **open** |
| **D3** | `from_agent_signal` leaves `items_served=[]` deliberately (an agent cites what helped, it does not enumerate what it was shown); `_emit_outcome` passed `len([]) == 0` as an **int**, clearing `OutcomeEvent`'s `is not None` guard, so `reference_rate` returned the sentinel `0.0` and `graph_low_reference_rate_tighten_domain_boost` (`reference_rate lt 0.2`) would have fired **always**. D1 without D3 proposes on a constant at 30 samples — strictly worse than dormant. | **fixed** (#558) |
| **D4** | Nothing schedules a tuner pass: `tuner_cursors` is empty and the nightly cron runs `worker curate` only. An operator wiring gap, not a code defect. | **open** |

So the remaining Phase 0 work is:

1. **Close D2.** This is the phase's real content and the only part that is a design
   question rather than a wiring one.
2. **Answer D4 by hand first** — run one `trellis worker tune --dry-run` pass manually
   before adding anything to the nightly cron. A scheduled pass that produces nothing
   is indistinguishable from a scheduled pass that never ran, which is the shape this
   whole phase exists to stop reproducing.
3. Then add the dry-run pass to the nightly cron with `auto_promote` disabled, and
   watch for a week.

**Gate:** does the OutcomeStore accumulate rows, and does `RuleTuner` emit a single
proposal against real data? If the loop cannot move one scalar parameter on production
traffic, nothing above it is worth building. **Rows alone do not pass this gate** —
after #558 the store accumulates and the rule set still cannot match, which is exactly
the state a rows-only gate would have reported as success.

[#557]: https://github.com/ronsse/trellis-ai/issues/557
[#558]: https://github.com/ronsse/trellis-ai/pull/558

### Phase 1 — feed it a signal that can carry an effect · weeks

Three edits, in this order, because each makes the next measurable:

1. **Give the distillation stream variance.** Emit a judged row from `_gate_candidates`
   with its own rejection reason as the `decision`, instead of only from the survivors
   with the literal `keep`. Until this lands, 869 of 1,868 rows are one value and the
   arm cannot be scored. Smallest edit here, and the one everything else rests on.
2. **Build the outcome join (#263).** `subject_ref.ref_id` → `doc_id` → the serving and
   citation record the retrieval side already writes. This is the column the schema
   docstring says was deferred; it is the actual Phase 1 deliverable, not #264.
3. **Then #264** (the exporter over the joined pairs) · #550 (third verdict, so the
   unjudged bucket can shrink) · #365 (retrieval availability) · #503, #502 (advisory
   cap starves its own fitness loop).

**Gate:** usable *graded* events per month above 500 — where graded means joined to an
outcome, not merely emitted. 1,868 emitted rows do not satisfy this gate and must not be
counted toward it; that substitution is the whole error this section was rewritten to
avoid. Below 500, §5.1's arithmetic says stop.

**Measured 2026-09-13 on a prod copy (#584): not met.** Edit 1 is #566 and edit 2 is #584.
- The strict reading, rows cited after their judgment, is **289** per 30 days. Graded is 493 and served is 749.
- 119 of the 289 are the one-off classification shadow batch.
- Distillation, the recurring arm, gives ~254 per 30 days at its historical rate and **~90** at last week's. Capture sees every transcript; fewer new sessions is the cause.
- A cited row can only come from an attributed pack (57 in the window). So the join is as thin as pack grading, the constraint §5.1 set out to escape.

So edit 3's exporter is not built. Phase 3's paired comparison is measured in the same signal and inherits the same limit.

What would move it is more attributed grading or more captured sessions. Owner call: whether *served* counts as the outcome (point 1 above). It passes at the historical rate (669) and fails at last week's (163).

### Phase 2 — give the graph a shape worth changing · weeks

#375 (no seed producer exists on the write path) · #371 (graph axis ignores its query)
· #549 (`get_file_context` has no producer — 0 of 1,875 documents carry a code path) ·
#463. Target document linkage above 40% from 8.5%, and a non-zero count of semantic
edges.

**Gate:** does a seeded graph axis change the served pack on real intents? The
`SemanticSeedExtractor` replay is the precedent and the warning — it was wired,
replayed over 37 real intents, and produced **0 seeds on 37/37, 0/37 packs changed**.
Run that replay before building on top.

### Phase 3 — the graph evolver · the real build

Shadow overlay, the six actuators behind a handler allowlist, the governed-mutation
path, promotion via `promote_proposal`, rollback via `monitor_post_promotion`. Ship
actuators one at a time, each with its base rate measured first per §4.4, **in §4.1.1's
order and not §4.1's**: qualifier capture (#599's prerequisite, not an actuator) →
semantic edges at the `save_knowledge` seam → re-type → summarise / confidence / retire
→ merge duplicates last, and only if its ceiling has moved off ≈0.1%.

**Gate:** the paired shadow/live comparison must show a positive effect on at least one
actuator before the second is built.

### Phase 4 — recursion · only after 3 holds for a quarter

The system proposes changes to its own proposal rules, and surfaces them for approval.
Promotion stays manual at this level, permanently.

---

## 7. What would refute this plan

Stated up front so it is checkable rather than defended:

- **Phase 0 produces proposals and they are all garbage.** Then the tuner's rule
  language is the problem, not its wiring, and Phase 1 does not help.
- ~~**#264's judged stream turns out not to be gradeable.**~~ **This one fired — checked
  before delivery rather than in Phase 1, and it was half right.** The stream is
  self-referential exactly as feared: the field is `decision` (not `verdict`, as this
  document first had it), it carries the system's own output, and the distillation half
  is the literal `keep` on 869 of 869 rows. What survives is the density argument and the
  join key: `subject_ref` was built to carry an outcome and simply never had one attached.
  So §5.1 and Phase 1 were rewritten from "consume the signal" to "build the missing
  column, and give the decision variance first." The load-bearing assumption was wrong in
  its stated form and the plan changed shape rather than being defended — which is the
  point of writing this section before the work, not after.
- **The joined signal stays below 500.** **Fired 2026-09-13** (Phase 1's gate, #584). The emitted
  stream is dense, but its join to an outcome is capped by pack grading.
- **The graph axis is dispensable.** If Phase 2's seeding replay shows packs do not
  change, then the graph is not a retrieval surface and reshaping it optimises
  something nobody reads. That would redirect this work to the document and vector
  planes instead.
- **Shadow assembly is too expensive.** At 2.5 packs/day it is free. If retrieval
  volume grows 40×, the doubling stops being acceptable and the comparison needs
  sampling.

---

## 8. Re-deriving the numbers

```sql
-- graph shape
select count(*), count(*) filter (where valid_to is null) from nodes;
select edge_type, count(*) from edges where valid_to is null group by 1 order by 2 desc;
select count(*) filter (where jsonb_array_length(document_ids) > 0) from nodes
  where valid_to is null;
select c, count(*) from (select node_id, count(*) c from nodes group by 1) s
  group by 1 order by 1;

-- signal density
select event_type, count(*) from events
  where occurred_at > now() - interval '30 days' group by 1 order by 2 desc;

-- is the judged stream gradeable? (§5.1's load-bearing check)
select payload->>'op_type', payload->>'decision', count(*)
  from events where event_type='memory_op.judged' group by 1,2 order by 3 desc;
-- a single decision value under an op_type = that arm carries zero bits.
-- note the key is 'decision'; there is no 'verdict' key.
```

```bash
# ops tier — all four should be non-zero after Phase 0
for db in outcomes tuner_state parameters; do
  sqlite3 ~/.trellis/data/stores/$db.db \
    "select name from sqlite_master where type='table'"
done
```

The windows roll. Re-derive rather than trusting a figure in this document — that is
the same instruction CLAUDE.md gives about its own measurements, for the same reason.

---

## 9. Correction from the owner, and what it parks

Recorded 2026-09-12, after this document was first delivered. It changes Finding 1's
*meaning*, not its measurement.

> "The goal of this graph is to create a provenance map of memories, and to be able to
> connect to other things like a knowledge graph, like a local memory store, like a
> database, like a file store, and establish relationships and what matters. It is not
> a knowledge graph for a specific database, but it is a knowledge graph in that sense."

The numbers in §1.1 stand — 1.03 edges/node, 79.8% of nodes at degree ≤ 1, six live
edge types all PROV-O provenance plus `appliesTo`, zero semantic relations. **The
diagnosis drawn from them was wrong.** I read "no semantic relations" as "the graph
does not describe subject matter," and proposed Actuator 6 (mine semantic edges between
memories) as the highest-ceiling actuator on that reading.

The intent is federation. The graph is a provenance map whose referents are meant to
reach *outside* Trellis — a file store, a database, an external knowledge graph, a local
memory store — recording what relates to what and what matters across them. Under that
reading the gap is not missing subject-matter edges between documents. It is that
**every node in production refers to something inside Trellis.** A provenance map whose
only referents are its own documents is a map of one room.

Three consequences for the plan above:

1. **§4.1's actuator ranking is provisional.** "Mine semantic edges between memories"
   optimises the wrong axis if the point is external reach. The candidate that replaces
   it is an *external-referent* edge type plus one producer per federated system.
   **Resolved 2026-09-20 in §4.1.1**, which derives a build order from measured
   ceilings and puts referent identity ahead of every edge-minting actuator. The
   correction survived the measurement: the largest edge-minting ceiling turned out to
   be at the `save_knowledge` producer seam, whose nodes are 93% isolated *and* are the
   only nodes in the graph carrying locator properties — so "one producer per federated
   system" and "mine semantic edges" meet at the same seam rather than competing.
2. **The substrate already permits it.** CLAUDE.md's type-extensibility rule makes
   `EntityType` / `EdgeKind` any string at the storage and API layers; the enums are
   well-known defaults, not a closed set. So this needs **producers, not schema** — the
   same shape as #375 (a seeded graph axis with no seed producer on the write path) and
   #549 (`get_file_context` with no code-path producer). Note that both of those issues
   are *already* about a missing external referent: a file path is exactly the kind of
   outside-Trellis thing this graph is supposed to point at. They may be the first two
   federation producers rather than maintenance items.
3. **Do not re-litigate the term.** It is a knowledge graph, scoped to provenance and
   federation. The useful question about any proposed edge is what *external* thing it
   points at.

### Parked for a deep-reasoning session

Named by the owner in the same message, to be worked with a heavier thinking model and
deliberately not started here:

- **Gap analysis** — where the gaps are, what is unimplemented, what is untested. A
  reasoning pass over the whole system, not a maintenance sweep.
- **Everything local.** Trellis is not fully running locally today. Target is every
  component on skynet with no cloud dependency inside the loop.
- **Model tiering for the loop.** Classification already runs on a local model
  (`hermes3:8b`). Connect more Kimi models for the heavier thinking decisions in the
  recursive-self-improvement path — the proposal and evaluation calls the tuner cannot
  make deterministically — with the local model keeping the high-volume judgements. The
  `moonshot:` provider prefix already exists (added 2026-09-09 on
  `feat/panel-multi-provider`), so this is routing and tiering, not integration.
