# Swarm plan — the wave after 2026-08-29

> Companion to [`swarm-handoff.md`](./swarm-handoff.md) (the operating manual) and
> [`decision-ledger.md`](./decision-ledger.md). This file is the **plan**; the handoff doc is
> the **rules**. Where they disagree, the handoff doc wins.
>
> Written at `main` = `281224b`, 33 issues open.

## Why the backlog grew

The 2026-08-28/29 wave merged **21 PRs and filed ~15 issues**. The open count went *up*. That
is not a failure — it is what happens when you point measurement at a system where five
mechanisms were reporting success while doing nothing. **The backlog is now a more accurate
map than it was, and it is a bigger map.** Plan against the map, not against the count.

## Sequencing principle

Order by **whether a defect is currently producing wrong behaviour in production**, not by
how interesting it is. Three of the items below are live; most are latent. A latent defect
with an elegant fix loses to a live one with a dull fix.

---

## Wave A — live defects (do first)

Small, active, independently verifiable. One agent can hold all three.

| Item | Why it is live |
|---|---|
| [#397](https://github.com/ronsse/trellis-ai/issues/397) | `reconcile.py:331` writes the supersession stamp without `preserve_updated_at=True`, and `strategies.py:452` decays keyword relevance off exactly that column. **Declaring a document stale makes it maximally fresh** — ranked above the document that replaced it. Two callers inherit it. |
| [#396](https://github.com/ronsse/trellis-ai/issues/396) | `GET /api/v1/search` still returns chunk rows (prod is 56% chunks), and the docstring #391 added enumerates only the `list_documents` callers — it omits the entire `search` caller class **on day one**. |
| [#393](https://github.com/ronsse/trellis-ai/issues/393) | `AdvisoryStore._load` degrades to empty on a corrupt file and the next write overwrites it. Since #394's stable ids, this additionally **un-suppresses everything** silently. |

**Acceptance for the lane:** #397 fixed *and* every metadata-only `document_store.put` audited
for the same omission — this is the fourth instance of "one write path passes a parameter and
its sibling does not" (`PolicyStore`, `AdvisoryStore`, `vector_store` in #381, now this).
Find the class, not the instance.

---

## Wave B — the retrieval core

**[#375](https://github.com/ronsse/trellis-ai/issues/375) is the largest open defect in the
system:** one of three retrieval axes does not search. `GraphSearch.search()` carries
`query: str,  # noqa: ARG002`, nothing supplies `seed_ids`, and the unseeded branch falls
through to `GraphStore.query` = `ORDER BY created_at DESC`. Measured: a **median 8.6% of
servable nodes over a median 58-hour window**, coverage **falling monotonically 0.150 → 0.072**
as the graph grows, because the reach is a fixed row count and decays as 1/N.

Three traps, all already paid for once:

1. **The obvious fix is a measured no-op.** `SemanticSeedExtractor` produced **0 seeds on
   37/37 packs** — it filters on `document_form == "entity_summary"`, a stamp only the eval
   loaders write, and **0 of 1,166 production vector rows carry it**. Do not re-propose it.
2. **The broken axis is the best-performing one** — per-serving citation rate graph **0.180**
   vs semantic 0.096 vs keyword 0.027. A naive fix can make retrieval worse, and the axis is
   only 7% of tokens so the headline will not show it.
3. **Counterfactual replay cannot evaluate this.** `pack_replay.py` re-runs the budget walk
   over recorded candidates; this change alters *which candidates exist*. Replay will silently
   answer a different question.

The fix lives on the **write path / store layer** — a query-relevant candidate producer —
which is why #376 correctly scoped it out of `retrieve/`.

Then the token-economics gaps, which protect every number the project reports:
[#362](https://github.com/ronsse/trellis-ai/issues/362) (`get_items` fetch cost off-book),
[#363](https://github.com/ronsse/trellis-ai/issues/363) (`TOKEN_TRACKED.pack_id` — **blocked on
the container rebuild, not on code**),
[#364](https://github.com/ronsse/trellis-ai/issues/364) (42% of injected tokens unjudged).

---

## Wave C — the security floor

Dependency-ordered and the order is load-bearing:

1. **[#256](https://github.com/ronsse/trellis-ai/issues/256)** — extract the Bolt backends to a
   `trellis-stores-bolt` plugin. Labelled `ready` / `keystone`. **Halves #194's enforcement
   surface**, so it genuinely precedes it.
2. **[#360](https://github.com/ronsse/trellis-ai/issues/360)** — govern the document and vector
   planes. Panel-decided (unanimous, option B), recorded as ledger **T-3**, never started.
   **Newly worth more:** T-3's caveat was "stage 2 is a no-op everywhere until a policy gate is
   wired (C1)" — C1 landed as #370, so governing now buys enforcement rather than only audit.
3. **[#194](https://github.com/ronsse/trellis-ai/issues/194)** — classification enforcement.
   Blocked on both above.

---

## Wave D — CI and environment integrity

The operator is getting a failed-run alert on **every merge**, which is a standing tax.

- **The `live-infra` red** (in flight) — #380's evidence gate made two loop fixtures fail. The
  fixtures encode the old unsound rule. **The deeper defect is that `tests.yml` deselects the
  `live` marker, so the only workflow running those tests fires *after* merge.**
- [#377](https://github.com/ronsse/trellis-ai/issues/377) — `configure_stderr_logging` bakes
  `sys.stderr` at call time, so one bare `CliRunner` poisons structlog process-wide (109
  failures across 23 directories). A dormant instance still sits in
  `tests/unit/workers/trace_embed/conftest.py`, harmless only because `workers` sorts last.
- [#398](https://github.com/ronsse/trellis-ai/issues/398) — **local typecheck reproduces CI in
  no venv on this box.** Lint, typecheck and tests each need a *different* venv and nothing in
  the repo says so.
- Coverage holes: [#350](https://github.com/ronsse/trellis-ai/issues/350),
  [#351](https://github.com/ronsse/trellis-ai/issues/351) (**the blessed substrate's contract
  runs in no workflow**), [#356](https://github.com/ronsse/trellis-ai/issues/356).

---

## Wave E — capture and learning

- [#369](https://github.com/ronsse/trellis-ai/issues/369) — name-alias resolution **stops
  learning** past `DEFAULT_NAME_SCAN_LIMIT` and starts returning clean "no match" for older
  entities. ~34–60 days out at measured growth; the cliff arrives silently.
- [#264](https://github.com/ronsse/trellis-ai/issues/264) — judged-memory logging (`ready` /
  `mechanical`; measured as 2 of 5 stages missing emitters).
- [#306](https://github.com/ronsse/trellis-ai/issues/306) — observer-agent capture. **Precondition:**
  #255's history — it shipped in July and did not run until August. Verify the observer produces
  non-fabricated output on a held-out transcript *before* wiring it to writes.

---

## Tidying (cheap, do opportunistically)

- **Close [#298](https://github.com/ronsse/trellis-ai/issues/298)** — all three proposed
  directions refuted by measurement; the live remainder is `artifact:`-only at 0.7% of tokens.
- **Close [#371](https://github.com/ronsse/trellis-ai/issues/371)** in favour of #375 — it is
  now the *documentation* of a defect whose fix is tracked elsewhere.

---

## Operator-only — not swarm-eligible at any confidence

| | |
|---|---|
| Ledger **A-3** | container rebuild + `TRELLIS_ENABLE_MEMORY_EXTRACTION` (unset on both containers, so prod holds **zero `entity_aliases` rows and zero `mentions` edges**) |
| Ledger **A-4** | 22 memories demoted for absence of praise — 14 need only a tag cleared, 8 need `retention.restore` |
| Ledger **D-4** | advisory rendering — **panel split 4–1**; recommendation is B *sequenced* after three post-repair nightly runs |
| Prod repair | 13 chunk vector rows diverge; `resync-vector-metadata` fixes them, and must run **after** the A-4 restores |
| Repo settings | `main` has no required status checks (ledger D-3) |

---

## Direction forks: adversarial review, not a thin poll

Operator instruction, 2026-08-29. When an agent hits a fork it has no defensible answer to,
the panel brief must carry **intent**, not just mechanics — and the panel must be asked to
attack the recommendation, not to rank options in a vacuum.

**A brief that settles a fork contains all five:**

1. **What the system is for**, in a paragraph, written for a reader who has never seen it.
   The panel cannot weigh a trade-off in a system whose purpose it has to infer.
2. **Measured numbers with `n`**, and the command that reproduces them. A thin brief returns
   confident answers to a question you did not ask, and the failure is silent.
3. **The options, with what each one costs** — including the option of doing nothing.
4. **The author's own prior, stated**, so the panel argues against a position rather than into
   a vacuum.
5. **What would change the answer.** Ask every panelist for this explicitly; it is often worth
   more than the vote.

**Composition beats size, and this is now measured.** On the D-4 advisory decision the two
Nous panelists were unanimous at 0.70–0.74; adding a **Nemotron** panelist via free NVIDIA
inference produced a dissent at 0.70 on a ground neither had raised — and **converted a
unanimous verdict into an escalation.** A two-lab panel would have reported consensus and the
agent would have acted on it.

```bash
python3 ~/.claude/skills/decision-panel/panel.py --brief brief.md --reversible \
  --models "openai/gpt-5.5,moonshotai/kimi-k3,nvidia:nvidia/nemotron-3-super-120b-a12b"
```

⚠️ NVIDIA's endpoint is unreliable at panel timeouts — five model ids tried, three failed with
504/504/404 **despite being listed by `/v1/models`**. A listed id is not a servable id. The
`nemotron-3-*` family answered reliably. Budget a spare panelist.

**Split → stop and report. Never tie-break.** Ship the safe default (the capability exists but
is off) so the operator's answer costs a config change, not a PR.

## Every PR goes through the review gate

Non-negotiable, and it has earned its cost — see `swarm-handoff.md` §4.1 for the run order.
Across five runs it found 13 problems in a *docs-only* PR, one blocking defect that would have
made the fitness loop permanently unreachable, one blocking regression that would have made a
live defect worse, and two HIGH defects agents found in their own diffs. **No false blocking
findings.**

The single most valuable lens for this codebase is **`silent-failure-hunter`**, because the
repo's defining defect is a mechanism reporting success while doing nothing.
