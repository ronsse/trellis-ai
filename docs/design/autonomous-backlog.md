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

**A2 — [#338](https://github.com/ronsse/trellis-ai/issues/338) vector row metadata is a stale embed-time snapshot.** `class: panel`
Noise exclusion never held on the semantic path because the vector row's metadata is
frozen at embed time. Interacts with A1: whatever A1 writes must not inherit this bug.
Sequence A1 and A2 together or A2 first.

**A3 — [#336](https://github.com/ronsse/trellis-ai/issues/336) effectiveness-based noise demotion is unsound without item attribution.** `class: panel`
Either gate the demotion behind sufficient attribution, or fix the attribution that
feeds it. Depends on A4 for the second option.

**A4 — Raise feedback attribution from 32%.** `class: panel`
Live attribution rate is 0.32 (12 of 37 feedback events over 30 days). Per-item rows
are what `learning/pack_observations.py` joins on; unattributed feedback contributes
nothing. This is the single biggest constraint on the learning loop and it is an
*ergonomics* problem at the MCP surface, not a missing feature — `pack_id` and item ids
must be hard to omit. Acceptance: attribution rate measurably rises over a 7-day window
after the change; the mechanism does not fabricate attribution when the caller has none.

## Wave 2 — retrieval quality

**B1 — [#298](https://github.com/ronsse/trellis-ai/issues/298) same-day trace/artifact stubs outrank topical content.** `class: panel`
Open 18 days; the oldest untouched retrieval defect. Partially mitigated by the content
floor and by #311's skip-discipline prompts — **re-measure before implementing**, the
remaining gap may be smaller than the issue describes.

**B2 — PackBuilder chunk rollup (roadmap §G.4).** `class: panel`
`PackBuilder` dedups by `item_id`, so two chunks of one document can both enter a pack
and spend the budget twice. Group by `parent_doc_id` at assembly. Also default-filter
`chunk_index` rows out of the documents list view.

**B3 — Index the alias resolver (roadmap §G.4).** `class: panel`
`memory_ingest_hook._graph_alias_resolver` and its `save_memory` twin are an O(n)
full-graph scan capped at 2000 nodes. Fine at current scale, a bottleneck on a real
vault. Replace with an indexed name→entity lookup.

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

**E2 — Capture-coverage measurement.** `class: panel`
[#332](https://github.com/ronsse/trellis-ai/issues/332) fixed the sidechain rule that
discarded 61% of transcripts. Nothing currently measures what fraction of sessions
produce a memory, so the next silent coverage regression is invisible. Build the metric
before adding capture surface in E1.

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
