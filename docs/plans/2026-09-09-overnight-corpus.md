# Overnight corpus — 2026-09-09

**Base:** `origin/main` at `1ef5c9c` (`git rev-parse origin/main` before you trust that).
**Predecessor:** [`docs/issues/reviews/2026-09-04/`](../issues/reviews/2026-09-04/) — 40 adversarial
briefs, a machine manifest, seven implementation plans. This document is **not** a second copy of
that corpus. It is the sequencing layer over what that corpus left behind after one execution pass.

---

## 0. The thing you must read first: the corpus already ran once

Between `e4e7604` (the commit that added the review corpus) and `1ef5c9c` there are **17 commits**,
and **13 of the 40 briefs were executed**. Seven issues closed on merge:

| Closed | By |
|---|---|
| #342 | #531 `test(retrieval): prove promoted precedents are served` |
| #350 | #539 `fix(pgvector): provision extension before pool startup` |
| #351 | #543 `ci: run ArcadeDB graph contract` |
| #439 | #534 `fix(api): expose pack withholding to SDK` |
| #522 | #536 `fix(cli): preserve untrusted Rich output` |
| #523 | #532 `fix(core): preserve long path diagnostics` |
| #526 | #533 `ci: cover optional dependency tests` |

Six more shipped a PR and **deliberately stayed open**, each PR body naming its own remainder:
#256 (#537), #264 (#540), #356 (#545), #360 (#538 + #544), #369 (#530), #514 (#535).

**This is why the corpus needed rewriting rather than extending.** A large fraction of what a naive
overnight queue would dispatch tonight is already merged. Two live documents still describe the
pre-execution world and would send agents at merged work — that is Lane C, and it is the first thing
to fix, not the last.

### Autonomy classes used below

| Class | Meaning |
|---|---|
| `selector-ready` | Open + `mechanical` + `ready` + no `keystone`/`owner-only`/`blocked:*` + a valid ```` ```files_allowed ```` block. The `issue_selector` contract would take it — **note that nothing executes that selector today**, so this means "safe to hand an agent", not "will be picked up". |
| `agent` | Reversible in-repo change; an agent may open a PR. Merge gate §4/§4.1 applies. |
| `decision` | Needs an owner semantics call before any code. |
| `owner-only` | Console, credential, deploy, publish, or spend. Never an agent's. |
| `do-not-do` | Actively refuted or deferred by a recorded decision. Dispatching it re-does refuted work. |

### Gate commands (do not substitute a different venv)

```bash
VENV=/mnt/ssd/trellis-worktrees/.ci-venv311   # 3.11.15, ruff 0.16.5, mypy 2.3.1 — NOT .ci-venv
$VENV/bin/ruff check src/ tests/ && $VENV/bin/ruff format --check src/ tests/
$VENV/bin/mypy src/
$VENV/bin/pytest tests/ -q
```

Merge requires green against **current** `main`: `lint`, `typecheck`, `test 3.11/3.12/3.13`,
`openapi-check`, `CodeQL`, `Analyze (python)` all `SUCCESS` and `mergeState: CLEAN`. §4.1 of
[`swarm-handoff.md`](../design/swarm-handoff.md) adds a mandatory simplify pass and review pass per PR.

---

## 1. Lane A — Closure adjudication (do this first; it is the highest-value hour)

Six issues carry merged work and are still open. Each is either closable now or has a stated
remainder. **Before closing any of them, grep the issue's own acceptance criteria and the linked
ADR's Status/Decision line** — the autonomy contract's guard, and the failure mode
[`closed-issues-can-hide-open-gates`](../design/swarm-handoff.md) records in both directions.

### A1 — #369: the stated closure gate is now satisfied `agent`

PR #530 said, verbatim: *"This PR does not close the issue while the ArcadeDB contract remains
unverified in live CI."*

That gate is met. #543 added the `arcadedb: arcadedata/arcadedb:26.8.1` service
(`live-infra.yml:95`) **and** `TRELLIS_TEST_ARCADEDB: "1"` (`:175`) — the marker toggle, without
which the service would be a decoration and the suite still deselected. The alias atomicity
contract lives in `tests/unit/stores/contracts/graph_store_contract.py:663-770`
(`test_bind_alias_if_absent_preserves_sequential_first_wins`,
`..._is_atomic_for_concurrent_contenders`, `test_bind_alias_serializes_concurrent_stale_owner_replacement`),
which `GraphStoreContractTests` runs against ArcadeDB in that job. **`Live infrastructure tests`
is `success` on `1ef5c9c`** — the #530 merge commit itself.

**Action:** re-verify the run is green on live `main`, then close #369 citing the run. If it is red,
that is a finding worth more than the closure.

### A2 — #525: duplicate whose principal is now closed `agent`

Manifest verdict `duplicate` of #526; #526 closed via #533. Nothing blocks the close any more.
**Action:** close #525 as duplicate of #526, one comment naming the merge.

### A3 — #364: stale-fixed by PR #389 `agent`

Manifest verdict `stale-fixed`, `status: closure-ready`, `pr: 389`. Last touched 2026-08-27.
**Action:** verify the mechanism is on main, then close citing #389.

### A4 — #356: state the remainder, do not close `agent`

PR #545 landed **the Postgres half only** — `test_postgres_stores.py` and `test_api_key_store.py`
now run in the live job (`live-infra.yml:217-218`), pinned by
`tests/unit/test_postgres_live_infra_rule.py`. The remainder is exactly one thing, and
`live-infra.yml:208-211` already carries the comment explaining it: `test_neo4j_vector.py::TestQuery`
issues AuraDB-grade `SEARCH ... IN (VECTOR INDEX ...)` that self-hosted `neo4j:2025.12` cannot
parse, and unlike the e2e suite it has **no capability probe**.

**Action:** comment on #356 narrowing it to the capability probe, and re-label. Then B2 below is
the implementation.

### A5 — #360: two PRs in, roster at 18, issue correctly open `agent`

#538 shipped PR1 (the derived AST ratchet, 24 sites inventoried,
`tests/unit/test_governed_write_rule.py`). #544 shipped PR2 (core `EvidenceIngestHandler`; roster
**25 → 18**; document 19→13, vector 6→5). Remaining per #544's own body: metadata-only paths,
batched ingest, and ratcheting 18 → 0.

**Action:** comment the current roster count on #360 so the ratchet's progress is legible on the
issue rather than only in a test file. Do not close.

### A6 — #256 and #264: staged by design `agent` / `agent`

- **#256** — #537 landed the `RegistryContext` / `RegistryPreparable.prepare_registry_params` seam
  plus `tests/unit/test_registry_plugin_boundary_rule.py`. Package extraction and publishing follow.
  **Publishing is never an agent's** (autonomy contract). The extraction PR is a keystone question
  for the owner. Comment the seam's landing; do not close; do not dispatch extraction.
- **#264** — #540 landed the shared `emit_memory_op_judged` seam
  (`src/trellis/core/memory_op_judged.py`) plus the two missing judged ops. PR-B (derived roster)
  was excluded *pending #514*. **#514's parser work landed** — see B1. Comment that PR-B is
  unblocked; do not close.

---

## 2. Lane B — Follow-ons the merges just unblocked

### B1 — #264 PR-B: the derived judge-site roster `selector-ready` (label it)

**Blocked on #514's `generate_call_sites`. That now exists** at `tests/ast_rules.py:405`, exported
at `:214`, already consumed by `tests/unit/llm/test_json_response_rule.py` (including a
`live_population=` vacuity floor at `:80` — reuse that guard, do not invent a second one).

The defect PR-B closes: nothing in the repo can say which LLM-judged sites emit `MEMORY_OP_JUDGED`.
Today five call sites route through `emit_memory_op_judged`
(`classify/shadow.py:777`, `mcp/reconcile.py:418`, `extract/memory_ingest_hook.py:251`,
`workers/session_capture/distill.py:380`) — a roster that will rot exactly like #443's did (3
declared control keys against 6 `pop` sites).

**Build it as a derived rule, not a list.** The repo's own answer to rotting prose, thirteen times
over. Guard against vacuity the three ways `test_policy_gate_rule.py` does: a floor on sites found,
a synthetic tree of known evasions run through the *shipped* predicate, and a signature check.

**Trap:** `EXTRACTION` judged-event counts are **zero in production** while
`TRELLIS_ENABLE_MEMORY_EXTRACTION` is off (#540's own operational note, ledger A-3). A rule that
divides by observed emissions measures the flag, not the roster. Derive from the source tree.

### B2 — #356: the Neo4j vector capability probe `agent`

The e2e suite has a probe; `test_neo4j_vector.py` does not. Give it one and sweep the file in.
Scope is one file plus one workflow line. **Do not widen the pytest invocation to
`tests/unit/stores/` wholesale** — `live-infra.yml:205-207` says why, and the structural rule from
#545 will fail you if you try.

### B3 — #514 remainder: measure before proposing `decision` (do not build tonight)

Structured outputs and a provider capability flag were explicitly cut from the slice by corpus
adjudication and by #535 ("remain deferred"). #535 added a malformed-distill counter *specifically*
so the retry question becomes answerable from data. **The overnight-safe work is to read that
counter, not to act on it.** If it is zero, the whole area is unmeasured and stays deferred.

---

## 3. Lane C — Record integrity (verified against `origin/main`, not the working tree)

> Read this before dispatching anything. My first pass at this lane produced five findings and
> **all five were wrong**, because I surveyed `~/projects/trellis-ai`, which sits deliberately on
> a feature branch 42 commits behind `origin/main` (the editable install means its branch *is*
> production). `git fetch` does not fix that — the tree is intentionally not on main. Read from
> `origin/main` or from a worktree. The three findings below were re-derived that way.

### C1 — `CLAUDE.md:283-292` says the ArcadeDB contract runs "Nowhere at all" `selector-ready`

It runs. `live-infra.yml:95` declares the service, `:175` sets the marker toggle, `:213-218` names
the contracts directory, and `GraphStoreContractTests` covers ArcadeDB there. The same paragraph
also still says "The 59 Postgres-marked tests under `tests/unit/stores/` outside `contracts/` are
still deselected" — #545 wired two of those files in. Both halves are stale as of #543/#545.

This is the **one** finding from my invalid first pass that survived re-derivation, and it got
worse in the meantime rather than better.

### C2 — `swarm-handoff.md` §6 lists merged work as the first executable batches `agent`

§6's "Batch 1 — early parallel (Wave D)" names **#360 PR1 → #256 staged seam**, plus **#369, #439,
#342, #514**. Every one is merged. "Batch 2 — CI / stores: hard dependency #351 → #356 … #526 and
#350" — #351, #526, #350 merged; #356 half-merged. The valid-now table lists #522 and #523, both
closed. The snapshot line says "40 open"; it is 33 of the original 40 plus #546–#549.

§6 is the **first thing an overnight agent reads** and it currently dispatches finished work.
Update the batches, the snapshot count, and the reviewed-at SHA. Leave §2 (autonomy), §3 (panel),
§4/§4.1 (gates), §5 (traps) and §7 (dispatch template) alone — those are current.

### C3 — the review corpus README's disposition is a dated snapshot presenting as live `agent`

`docs/issues/reviews/2026-09-04/README.md` opens with "**Current disposition:** 40 remain open on
GitHub". `manifest.json` carries `status: not-started` on 40 rows, seven of which are now closed.

**Do not rewrite the briefs** — they are dated review evidence and their value is that they are
fixed at `f9ff32c`. Add an execution-status header pointing here, and update `manifest.json`'s
`status`/`pr` fields, which exist precisely to carry this. The verdicts stay.

---

## 4. Lane D — Valid-now / valid-slice never dispatched

### D1 — #494: document `retrieve pack --quiet` id semantics `selector-ready` (label it)

`valid-now`, `W2-docs`, no dependencies, territories `src/trellis_cli/retrieve.py` +
`docs/agent-guide/operations.md`. Labelled `question` on GitHub, which is why no selector would
take it — that label is the blocker, not the work. Smallest genuinely-ready item in the corpus.

### D2 — #515: cache token fields in `TokenUsage` only `agent`

`valid-slice`, `W3-llm`, territory `src/trellis/llm/providers/anthropic.py`. **The slice is the
measurement, not the caching.** Add the cache-read/cache-write token fields so the batch pass's
cache behaviour becomes observable; enabling prompt caching is a separate decision that needs the
numbers this slice produces. One of the two items the 2026-09-04 sweep never planned.

**Trap:** this is the exact shape of #359's `useful_token_fraction` lesson — do not report a saving
computed from a constant. If the fields read zero on every call, that is the finding.

### D3 — #257: ingest-normalization ADR `owner-only`

Corpus says `valid-now`; the live label says `owner-only`, and §6 lists it under owner
prerequisites. **The live label wins** — requirements hierarchy puts the GitHub issue above the
review corpus. Not dispatchable regardless of the verdict. Listed here so nobody re-derives it.

---

## 5. Lane E — New work the execution pass revealed

### E1 — File: writes commit before Stage 5 audit emission `agent` (file the issue; do not fix)

PR #544's own "Known limitation": `MutationExecutor` commits handler writes **before** Stage 5
audit emission, so an EventLog failure can leave a document with no `save_memory` tail. #544
rejected a local retry shim because it can falsely attribute foreign documents, and named the real
fix as a **pipeline-level outbox/atomicity design**.

That is a design question touching the governed pipeline every surface depends on — file it with
#544's reasoning quoted, link it from #360, and let a panel or the owner size it. **Filing is
reversible; redesigning the executor overnight is not the kind of thing to hand an unattended agent.**

### E2 — The harness gap, restated because it keeps not being the thing anyone fixes `decision`

`select_candidate` and `evaluate_issue` in `src/trellis_workers/code_authoring/issue_selector.py`
are called by **nothing outside their own module and their own tests** — verified by grep. skynet-hub
#10/#11 shipped Cohort-2 as guardrails only; gate (a) is open. `roadmap-nightly.sh` (cron `0 5 * * *`)
is `REPORT_ONLY`.

So "selector-ready" in this document means *an agent could safely be handed this*, and nothing more.
The 17 commits above were dispatched by a human-initiated swarm, not by the selector. Whether to
close gate (a) is an owner decision — it is the difference between a queue and a robot.

---

## 6. Lane F — Owner-only (queued, not dispatchable)

| Item | What it needs |
|---|---|
| **#546** Steps 0–4 | Deployment catch-up — production runs a commit on no remote. Owner-run. |
| **#547** | Stop-hook nudge rewards never retrieving. Owner-run. |
| **#548** | retrieve-before-task documents 1 of 8 retrieval mechanisms. Waits on #549. |
| **#549 Part B** | `trellis-skynet worker embed-traces` once against prod, add to nightly cron, re-probe `get_file_context`. Part A is committed on `feat/549-file-context-files-touched`, **unpushed**. |
| **#250** | AuraDB credential hygiene — 1Password + Neo4j console. Credential ops are never an agent's. |
| **#256** extraction/publish | PyPI publish is never an agent's, at any confidence. |
| **#478**, **#475** | `blocked:owner-decision`. |
| Board #275 membership for #546–#549 | Blocked on `gh auth refresh -s project` (interactive). |
| Push + PR for `feat/549-file-context-files-touched` | Outward-facing — needs an explicit go-ahead. |

---

## 7. Blocked — do not dispatch, and why

Dispatching any of these re-does refuted work. Each has a recorded reason.

| Issue | Reason |
|---|---|
| **#375** | Mechanisms **refuted by measurement**. `SemanticSeedExtractor` replayed over all 37 real production intents: **0 seeds on 37/37, 0/37 packs changed.** Do not dispatch the obvious seeding fix. |
| **#371** | Behaviourally open — the graph axis is a recency feed until a *producer* of entity-anchored documents exists on the memory-ingest path. That is #375's problem, and #375 is refuted. |
| **#463** | `blocked:signal`. Panels agreed no behaviour change now; they **split on clock semantics**. A split panel escalates to the owner. Outcome recorded by #529. |
| **#502** | Owner chose **defer**. Ledger F-5 records the trigger and the safe default. Outcome recorded by #529. |
| **#503** | `blocked:signal`. |
| **#194** | `blocked:owner-decision` + `keystone`. Practically depends on **#360**, not #256. |
| **#200–#203** | Query-history cluster; three of four are `blocked:owner-decision`. |
| **#261**, **#306** | `blocked:signal`, both downstream of #255. #306 is the sweep's other unplanned item — it stays unplanned because the signal is what is missing, not the plan. |
| **#365** | `blocked:signal` — and its own recommended option **already shipped**: `retrieval_availability_note` at `ops/write_health.py:672`, populated `:874`, rendered `analyze.py:681/687`. |
| **#474**, **#476**, **#477** | `blocked:owner-decision`. |
| **#208** | External — wrong repo. Re-home it. |
| **#405**, **#275**, **#478** | Meta-boards, not a code PR. |

---

## 8. Suggested execution order

**Serial, first, alone — Lane C.** Every later dispatch reads those documents. C1 → C2 → C3 in one
PR; they are the same claim in three places.

**Then Lane A in parallel** (A1–A6 are GitHub-state only, no file collisions).

**Then, parallel, disjoint territories:**

| Slot | Item | Territory |
|---|---|---|
| 1 | **B1** #264 PR-B | `tests/`, `src/trellis/core/memory_op_judged.py` |
| 2 | **B2** #356 probe | `tests/unit/stores/test_neo4j_vector.py`, `live-infra.yml` |
| 3 | **D1** #494 | `src/trellis_cli/retrieve.py`, `docs/agent-guide/` |
| 4 | **D2** #515 | `src/trellis/llm/providers/anthropic.py` |

B2 and any other CI-workflow edit **must serialize** — they share `live-infra.yml`, and #545's
structural rule pins its selection.

**Then E1** (file only). **Then stop.** Lane F waits for the owner; Lane 7 is not work.

---

## 9. What would refute this corpus

- **`Live infrastructure tests` is not green on live `main`.** A1 collapses and becomes a bug report.
- **A merged PR did not actually ship what its body claims.** Every Lane A action is derived from PR
  prose. The prose is evidence, not proof — check the tree. This repo's own recurring failure is
  *measurement paths wired to constants*.
- **`generate_call_sites` does not generalize past LLM `.generate` sites.** B1 assumes reuse; if the
  helper is shaped only for that one predicate, PR-B needs its own scanner and the estimate is wrong.
- **The selector is wired after all.** If anything does call `select_candidate`, the `selector-ready`
  labels stop being advisory and start dispatching, and D1's `question` label becomes load-bearing.
- **#525 is not a duplicate.** The verdict is one reviewer's, dated 2026-09-04, and #526's fix
  (#533) may not cover the `importorskip` surface #525 names. Check before closing.
