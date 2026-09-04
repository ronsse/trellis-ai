# Autonomous backlog — subagent-executable work items

> **What this is.** A dependency-ordered queue of work items each sized for one
> focused subagent session and one PR. It exists so an orchestrating agent can make
> continuous progress on the Trellis roadmap without the operator adjudicating every
> fork. It is a *scheduling* view derived from **GitHub issues** (live requirements and
> status) and [`implementation-roadmap.md`](./implementation-roadmap.md) where it governs
> a program item — **not a second source of truth** for any individual issue.
>
> Created 2026-08-26; the live queue was reconciled 2026-09-04 using the dated issue
> corpus ([#527](https://github.com/ronsse/trellis-ai/pull/527)) as *evidence*, not as
> authority. **Requirements hierarchy:** **GitHub issue body/comments** (live issue
> requirements and status) → [`implementation-roadmap.md`](./implementation-roadmap.md)
> (where it governs a program item) → this file (scheduling derived from the first two).
> The corpus is adversarial review evidence and optional implementation aids only — not
> requirements authority, and **not every open issue appears in roadmap §3.H.** **Read
> [Current queue](#current-queue--2026-09-04) first** — the Wave sections below are the
> accumulated record, not the schedule.

## How an agent runs an item

1. `get_context(intent=...)` before starting. An empty pack is a real answer.
2. Branch from `main`, one item per branch.
3. Implement + tests. `source .venv/bin/activate` first — the Makefile calls bare
   `ruff` / `mypy` / `pytest` and a non-activated shell fails with a misleading
   exit 127. (Do **not** use `uv run` casually: it re-resolves and rewrites
   `uv.lock` as a side effect.)
   **Run the gates bare, exactly as CI does: `mypy src/`, `ruff check src/ tests/`.**
   `typecheck.yml` and `lint.yml` both run **Python 3.11** and pass no
   `--python-version` override, so neither should you — an override makes the local
   run check a target CI never checks, and hides genuine 3.11-vs-3.12 divergence.
   An earlier revision of this file told agents to run
   `mypy --python-version 3.12 src/`; that was a workaround for a venv provisioned on
   the wrong Python, and it is withdrawn.
   **If your environment cannot reproduce bare `mypy src/`, the environment is wrong,
   not the command** — on 3.12, pip resolves a numpy whose stub carries an unguarded
   PEP 695 `type` statement that mypy rejects under the pinned
   `python_version = "3.11"`, aborting with **zero files in `src/` checked**.
   `make lint`, `make typecheck` and `make format` now run
   `scripts/check_tool_pins.py --check-env` first and refuse to proceed when the
   interpreter or a pinned tool is not CI's
   ([#398](https://github.com/ronsse/trellis-ai/issues/398), `format` since
   [#498](https://github.com/ronsse/trellis-ai/issues/498) — it is the invocation that
   *writes*); `make env-check` runs the report on its own. Do not read that list as a
   roster: `tests/unit/test_makefile_gate_rule.py` derives it, so any target whose
   recipe invokes ruff or mypy must reach `env-check` through its prerequisites.
   `TRELLIS_ALLOW_ENV_DRIFT=1` downgrades it to a warning for deliberate work in a
   known-drifted environment — but a gate green under that flag does not predict CI,
   and only `1`/`true`/`yes`/`on` turn it on: `=0` leaves the gate enforcing (#498).
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

## Current queue — 2026-09-04

> **Derived scheduling view** — read each item's **GitHub issue body/comments** for live
> requirements and status; consult [`implementation-roadmap.md`](./implementation-roadmap.md)
> only where it governs that program item (not all open issues are in §3.H).
> **Evidence and aids** (not authority):
> [`docs/issues/reviews/2026-09-04/README.md`](../issues/reviews/2026-09-04/README.md),
> [`manifest.json`](../issues/reviews/2026-09-04/manifest.json), per-issue `#NNN.md`
> briefs, and optional [`plans/`](../issues/reviews/2026-09-04/plans/) for seven issues
> (#256, #264, #342, #360, #369, #439, #514). **Do not copy the 40 briefs here;** read
> the corpus for measured evidence and close criteria, then confirm status on GitHub and
> the roadmap before dispatching.
>
> Reviewed against `main` at `e4e7604` before this doc commit. **Determine live `main` via
> `git rev-parse origin/main`** — do not treat a SHA embedded here as current.
>
> Waves 0–5 below are the *historical record* of what each item decided, measured, or
> refuted. For live state and requirements, **the GitHub issue wins**; the corpus supplies
> measured evidence only.

**Snapshot:** **40 open** on GitHub as of the 2026-09-04 review. The count stays 40 until
close/duplicate mutations actually land — the configured automation token currently lacks
GitHub **Issues write** permission, so corpus disposition actions (e.g. closing
[#525](https://github.com/ronsse/trellis-ai/issues/525) / [#364](https://github.com/ronsse/trellis-ai/issues/364))
cannot be applied from agents until that is restored.

| Issue | Corpus verdict | Agent note |
|---|---|---|
| [#525](https://github.com/ronsse/trellis-ai/issues/525) | `duplicate` | **Closure-ready** — duplicate of #526; do not dispatch |
| [#364](https://github.com/ronsse/trellis-ai/issues/364) | `stale-fixed` | **Closure-ready** — PR #389; do not dispatch |

PR [#527](https://github.com/ronsse/trellis-ai/pull/527) **selectively transcribed** the
seven implementation plans and unique review evidence into `docs/issues/reviews/2026-09-04/`.
The orchestration briefs and reports on remote branch `handoff/issue-sweep-2026-09-04` were
**intentionally superseded** and were **not** merged as an ancestral commit — do not retain
their 37-open count or unsafe closure recommendations. That branch is **eligible for
owner-approved deletion** after confirming no archival retention is desired.

### First executable batches *(not exhaustive)*

**Batch 1 — early parallel** (disjoint territories; recheck before dispatch):

**#360 PR1**, **#256 seam**, **#369**, **#439**, **#342**, **#514**.

- **#264 PR-A** after or parallel with Batch 1 if no file collision; **#264 PR-B**
  (derived roster) **after #514** when `generate_call_sites` exists (implementation
  adjacency, not a manifest dependency).
- **Serialize #360 PR2 with #264 PR-A** — extraction/MCP overlap on `save_memory`.

**Batch 2 — CI / stores** (hard dependency **#351 → #356** per manifest; no issue
dependency between #526 and #350):

| Issue | Verdict | Note |
|---|---|---|
| [#526](https://github.com/ronsse/trellis-ai/issues/526) | `valid-now` | May parallelize with #350 if territories/workflows disjoint after recheck |
| [#351](https://github.com/ronsse/trellis-ai/issues/351) | `valid-now` | **Before #356** (manifest dependency) |
| [#356](https://github.com/ronsse/trellis-ai/issues/356) | `valid-now` | After #351 |
| [#350](https://github.com/ronsse/trellis-ai/issues/350) | `valid-now` | May parallelize with #526 if disjoint |

**Workflow-collision scheduling (optional):** #526, #351, and #356 share CI workflow
territory — coordinate merges when touching the same files; this is **not** a serial
issue dependency.

**Other valid-now / valid-slice items** (dispatch when Batch 1–2 are full or territories
allow; full roster in manifest + roadmap):

| Issue | Verdict | Note |
|---|---|---|
| [#522](https://github.com/ronsse/trellis-ai/issues/522) | `valid-now` | Rich operator-output renders outside #492 handle rule |
| [#523](https://github.com/ronsse/trellis-ai/issues/523) | `valid-now` | `sanitize_error_message` suppresses boundary test messages on long `--basetemp` |
| [#494](https://github.com/ronsse/trellis-ai/issues/494) | `valid-now` | Document `retrieve pack --quiet` id population |
| [#515](https://github.com/ronsse/trellis-ai/issues/515) | `valid-slice` | **Measurement slice only** — map Anthropic cache token fields into `TokenUsage`; defer `cache_control` until benefit measured |

All other open items: verdict and wave in
[`manifest.json`](../issues/reviews/2026-09-04/manifest.json); live requirements and
status on each **GitHub issue**; roadmap sections only where they govern that item.

### Owner prerequisites *(not dispatchable)*

| Issue | Gate | Note |
|---|---|---|
| [#257](https://github.com/ronsse/trellis-ai/issues/257) | `owner-only` | Ingest-normalization ADR — roadmap and issue label; not swarm-eligible regardless of corpus `valid-now` evidence |
| [#475](https://github.com/ronsse/trellis-ai/issues/475) | `blocked:owner-decision` | Assumptions header — assumptions line set **not agreed**; live issue blocks dispatch |

### Persistent blockers (do not misread)

| Issue | Status |
|---|---|
| [#371](https://github.com/ronsse/trellis-ai/issues/371) | **Behaviorally open, blocked-signal** — graph axis is a recency feed until a real seeding path exists; do not dispatch alone |
| [#375](https://github.com/ronsse/trellis-ai/issues/375) | **Mechanisms refuted** — `SemanticSeedExtractor` changed 0/37 production packs; premise remains but proposed fixes are dead |
| [#503](https://github.com/ronsse/trellis-ai/issues/503) | **blocked-signal** — wait for the first item-scoped advisory to reach a pack |
| [#208](https://github.com/ronsse/trellis-ai/issues/208) | **external** — re-home to consumer-kg or close (`human`) |
| [#250](https://github.com/ronsse/trellis-ai/issues/250) | **blocked-operator** — Aura console credential purge |
| [#201](https://github.com/ronsse/trellis-ai/issues/201), [#261](https://github.com/ronsse/trellis-ai/issues/261), [#306](https://github.com/ronsse/trellis-ai/issues/306) | **blocked-signal** — pilot/loop throughput not yet present |
| [#194](https://github.com/ronsse/trellis-ai/issues/194) | **blocked-decision** — depends practically on **#360** and owner semantics, **not** on #256 (backend extraction is not on the enforcement critical path) |

---

## Historical scheduling snapshot — 2026-09-03

> **HISTORICAL — do not dispatch from this section.** Retained for measurements, refusals,
> and gate findings only. Lane A file-store guards ([#471](https://github.com/ronsse/trellis-ai/issues/471),
> [#448](https://github.com/ronsse/trellis-ai/issues/448),
> [#459](https://github.com/ronsse/trellis-ai/issues/459)) and most Lane G review-gate
> items landed before the 2026-09-04 corpus — verify issue state on GitHub before acting.
> For current work, use [Current queue — 2026-09-04](#current-queue--2026-09-04) above.

Re-derived from the **37 open issues** on 2026-09-03 (superseded; the corpus now tracks
**40**). Roughly a third were swarm-ready, a third needed a design call, and a third were
operator-gated. **Lane G** was added 2026-09-04 for the cluster filed since — most of it
by adversarial gate reviews of the PRs the other lanes produced.

### Lane A — the file-store guard family *(historical — landed)*

`DegradableJsonStore` (#426) unified `PolicyStore` and `AdvisoryStore`; these are the
three residues. Different files, so they parallelize.

| # | item | class |
|---|---|---|
| [#471](https://github.com/ronsse/trellis-ai/issues/471) | `_fingerprint` swallows `OSError` → `None == None` → the stale guard **passes** | `panel` |
| [#448](https://github.com/ronsse/trellis-ai/issues/448) | the nightly advisory writer exits `0` on a refused write; only the ad-hoc paths escalate | `panel` |
| [#459](https://github.com/ronsse/trellis-ai/issues/459) | a damaged policy file is illegible — CLI exit 1 with empty stdout, REST 500 `internal_error` | `panel` |

#471 is the load-bearing one: `refuse_if_stale` is what stands between a stale view and
#413's fail-open on access control, and a guard that disarms when it cannot `stat()` is
that defect one layer down. #448 is the signature shape of this repo — a mechanism
reporting success while doing nothing — on the one surface that is unattended. #459 is
the legibility half of #425, whose visibility half shipped in #458.

### Lane B — retrieval correctness *(historical — verify GitHub before dispatch)*

| # | item | class |
|---|---|---|
| [#410](https://github.com/ronsse/trellis-ai/issues/410) | `trellis retrieve pack` bypasses `PackBuilder` entirely — the #262 invariant is false for the CLI | `panel` |
| [#439](https://github.com/ronsse/trellis-ai/issues/439) | SDK/REST cannot report withholding; `hooks.for_intent` reproduces #404 verbatim | `panel` |
| [#465](https://github.com/ronsse/trellis-ai/issues/465) | the recency resolver picks source/row/nothing and records the choice nowhere | `panel` |
| [#463](https://github.com/ronsse/trellis-ai/issues/463) | chunk rows decay off the import clock | `panel` — **settle the question in the issue first** |
| [#298](https://github.com/ronsse/trellis-ai/issues/298) | same-day trace/artifact stubs outrank topical content | `panel` |
| [#369](https://github.com/ronsse/trellis-ai/issues/369) | alias resolution stops learning past `DEFAULT_NAME_SCAN_LIMIT`, and starts answering wrongly | `panel` |
| [#392](https://github.com/ronsse/trellis-ai/issues/392) | advisories are never rendered — the flat pack path does not call the formatter | design call |

#439 changes a wire contract (`PackResponse` has no `metadata`), so it trips
`openapi-check` and is larger than it reads. #465 is mechanical: the house pattern for
stamping which branch ran is already established three times over (`graph_selection`,
`content_floor_penalty`, `PACK_ASSEMBLED.payload["withholding"]`).

**[#371](https://github.com/ronsse/trellis-ai/issues/371) is blocked by
[#375](https://github.com/ronsse/trellis-ai/issues/375)** and must not be dispatched
alone. The graph axis cannot stop being a recency feed until something produces
entity-anchored documents on the *memory-ingest* path; wiring the existing
`SemanticSeedExtractor` was replayed over 37 real production intents and changed **0/37**
packs.

### Lane C — measurement *(historical — verify GitHub before dispatch)*

Every item here is an instance of *the number that would justify X is the one nobody
records.*

| # | item | note |
|---|---|---|
| [#363](https://github.com/ronsse/trellis-ai/issues/363) | `TOKEN_TRACKED.pack_id` coverage is 0/33 | same seam as #362 — one agent, sequenced |
| [#362](https://github.com/ronsse/trellis-ai/issues/362) | `get_items` fetch cost is off-book, so index mode cannot be evaluated | ← |
| [#364](https://github.com/ronsse/trellis-ai/issues/364) | 42% of injected tokens get no verdict, so `useful_token_fraction` describes 58% of itself | **closure-ready** — PR #389 shipped judged coverage; issue still open on GitHub |
| [#348](https://github.com/ronsse/trellis-ai/issues/348) | nothing surfaces the editable-install staleness `write_provenance` was designed to catch | **done** — `resolve_stamp_staleness`; stamp gains `stamp_stale` / `source_tree_commit` only when stale, state reported in full by `trellis admin write-config` |

### Lane D — CI reaches the backends it claims to support *(historical — see corpus W1-ci)*

| # | item | note |
|---|---|---|
| [#351](https://github.com/ronsse/trellis-ai/issues/351) | **the ArcadeDB graph contract runs in no workflow** — the *blessed* substrate is untested anywhere | highest value in the lane |
| [#350](https://github.com/ronsse/trellis-ai/issues/350) | `PgVectorStore` cannot create its own extension: `register_vector` is the pool's `on_connect`, so `_init_schema` is never reached | |
| [#356](https://github.com/ronsse/trellis-ai/issues/356) | `tests/unit/stores/` runs in no workflow, and cannot simply be swept in | blocked on a capability probe |
| [#398](https://github.com/ronsse/trellis-ai/issues/398) | nothing notices when the local environment stops being the one CI gates on | unblocks every other agent |

#398 was filed as "local typecheck cannot reproduce CI", and that half was
environmental: the shared venv was provisioned on Python 3.12, which resolves a numpy
whose stub mypy rejects under the pinned 3.11 target, so `mypy src/` aborted having
**checked zero files in `src/`** — and the workaround that circulated in response,
`--python-version 3.12`, made the local run check a target CI never checks. Rebuilding
the venv on 3.11 fixes that instance. The durable half is that *nothing noticed*: the
same venv also sat one patch behind the ruff pin, and #378's finding was that the older
ruff stays green on code the newer one rejects. `scripts/check_tool_pins.py
--check-env` now compares the running interpreter and the tools on PATH against the
pins, and `make lint` / `make typecheck` / `make format` refuse to run a gate that is
not CI's.

### Lane E — keystone, design before code *(historical — see corpus dispatch order)*

[#360](https://github.com/ronsse/trellis-ai/issues/360) (the governed-pipeline rule does
not hold for the document and vector planes),
[#256](https://github.com/ronsse/trellis-ai/issues/256) (extract the Bolt backends to a
plugin — `ready`, `keystone`), and
[#264](https://github.com/ronsse/trellis-ai/issues/264) (log every judged memory
operation — labelled `ready` / `mechanical`, so it is a better swarm candidate than its
position here suggests).

### Lane F — the OpenAI data-agent evaluation (filed 2026-09-03) *(historical — blocked:owner-decision)*

[#478](https://github.com/ronsse/trellis-ai/issues/478) is the umbrella;
[#474](https://github.com/ronsse/trellis-ai/issues/474) confirm-to-save,
[#475](https://github.com/ronsse/trellis-ai/issues/475) assumptions header,
[#476](https://github.com/ronsse/trellis-ai/issues/476) spaces, and
[#477](https://github.com/ronsse/trellis-ai/issues/477) contradiction + `last_verified`
are its children. **All five are `blocked:owner-decision` — evaluation, not commitments**,
and none is swarm-eligible until the owner rules.

Two of the six original proposals were **not** filed, and the reason is reusable: visible
redaction was byte-identical to the closed #404, and the recurrence premise that ordered
the whole plan is refuted (there is no `promote_candidates` symbol, and 139 of 306 served
items recur). A prepared prompt is a snapshot; re-verify every "already exists" and "does
not exist" claim before acting on one.

### Lane G — the review-and-gate cluster (filed 2026-09-03/04) *(historical — most items landed)*

Filed *after* the re-derivation above, mostly by adversarial gate reviews of the PRs in
the other lanes. They are small, mechanical, and mostly disjoint — which is why they
parallelize well and why they are listed separately rather than folded into a lane whose
premise they do not share.

| # | item | status |
|---|---|---|
| [#495](https://github.com/ronsse/trellis-ai/issues/495) | 21 CLI tests fail under `FORCE_COLOR=1`, four of them written to prove Rich does not mangle output | **done** — `aff76a4` |
| [#498](https://github.com/ronsse/trellis-ai/issues/498) | `TRELLIS_ALLOW_ENV_DRIFT=0` *enabled* the override; `make format` ungated | **done** — `8ce5614` |
| [#506](https://github.com/ronsse/trellis-ai/issues/506) | `POST /vectors/reset` answered 200 on an unconfigured store, a failed reset, **and** 500 on success | **done** — `5de062c` |
| [#489](https://github.com/ronsse/trellis-ai/issues/489) | advisory refusals exit 2 where the canonical ADR says 5 | **done** — `3abe8af` |
| [#491](https://github.com/ronsse/trellis-ai/issues/491) | the format/exit parity rule is per-module, so a shared helper disarms it | **done** — `7844b9b` |
| [#501](https://github.com/ronsse/trellis-ai/issues/501) | the AST evasion roster has uncovered placements | **done** — `8a21d3f` |
| [#500](https://github.com/ronsse/trellis-ai/issues/500) | `temperature` is a 400 on current Claude models; the SDK floor makes it reachable | **done** — `854313a` |
| [#492](https://github.com/ronsse/trellis-ai/issues/492) | `retrieve search` mangles ids through Rich (`dataset:snowflake://` → `dataset❄//`) | **done** — `b4391b6` |
| [#493](https://github.com/ronsse/trellis-ai/issues/493) | `PackAssemblyError` is a `RuntimeError`, so it tracebacks past the CLI boundary | **done** — `b4391b6` |
| [#494](https://github.com/ronsse/trellis-ai/issues/494) | `retrieve pack --quiet` id population is undocumented | owner call, then a doc line |
| [#511](https://github.com/ronsse/trellis-ai/issues/511) | `POST /vectors/reset` has never worked on the blessed substrate | **done** — `8071fd4` |
| [#512](https://github.com/ronsse/trellis-ai/issues/512) | a backend's embedding width is a private attribute the route guesses at | **done** — the width *and* the reset capability are now declared on the `VectorStore` ABC |
| [#502](https://github.com/ronsse/trellis-ai/issues/502) | the advisory cap caps the fitness loop's input | **owner decision** — three candidate *semantics*, not three implementations |
| [#503](https://github.com/ronsse/trellis-ai/issues/503) | the advisory cap ranks category-blind | **do not do yet** — recorded hazard; wait for the first item-scoped advisory to reach a pack |

**Two premises in these issues were wrong, and both would have shipped a green suite.**
#489's closure list omits `generate-advisories`, which reaches exit 2 through two *inline*
`raise typer.Exit(code=2)` that route through neither named helper — implement the list
verbatim and the inconsistency survives. #493 claims a CLI-local fix leaves REST and MCP
exposed; both already handle it (`mcp/server.py`, `trellis_api/app.py`), so the CLI is the
only affected surface. #503's load-bearing sentence is wrong too, though it changes no
work: `ANTI_PATTERN` advisories carry `entity_id` as well as `ENTITY` ones. Each
correction is a comment on its issue.

**The gates are where the value was.** Every PR in this cluster was reviewed by an
independent agent trying to break it, and **not one came back clean**. The recurring
finding is that the defect sits *inside the PR's own load-bearing claim*, not around it —
a fix for roster rot that left four stale rosters, a colour guard with no test of its own,
an "advisory, never raises" probe with two escaping exception types, a sanitized error
body whose sanitizer was unpinned. A PR is most confident about exactly the property it
was written to establish, and that is where the coverage is thinnest.

### Not swarm-eligible *(historical 2026-09-03 roster — use corpus verdicts instead)*

`#194`, `#200`–`#203`, `#208`, `#250`, `#257`, `#261`, `#275`, `#306`, `#342`, `#405` —
owner-decision, blocked-on-signal, or umbrella tracking. Eleven of the **37** open issues
at that snapshot (superseded by the 40-issue corpus).

---

## Wave 0 — housekeeping *(historical record)*

> **HISTORICAL — do not dispatch Wave items unless the corpus or GitHub issue says they
> are still open.** These waves record what landed, what was refused, and the measurements
> behind each decision.

| id | item | class | status | notes |
|---|---|---|---|---|
| H1 | Merge [#328](https://github.com/ronsse/trellis-ai/issues/328) — dependabot ruff 0.15.22→0.16.4, mypy bump | `panel` | **done** — merged as `2f8db2c` | CI-green and mergeable today. Expect new ruff findings on the bump; fixing them is in scope for this item. |
| H2 | Land the 2026-08-26 doc reconciliation (`TODO.md` + roadmap) | `panel` | **done** — [#340](https://github.com/ronsse/trellis-ai/pull/340), `31058fc` | Seven stale checkboxes ticked, one rescoped, two false claims corrected. That pass is itself now stale; this file is its successor. |
| H3 | Dispose of [#304](https://github.com/ronsse/trellis-ai/issues/304) — honest DoD-3 loop metric + reframe | `panel` | **done** — merged | Open 8 days, CI-green. A metric *reframe* is reversible, so the panel may decide it. |
| H4 | Dispose of [#208](https://github.com/ronsse/trellis-ai/issues/208) — re-home to consumer-kg or close | `human` | **open** — the only Wave 0 item left (verified 2026-08-31) | Closing an issue can hide an open gate (the #312 failure). Operator call. |

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

✅ **The trace half LANDED** 2026-08-28 via [#357](https://github.com/ronsse/trellis-ai/pull/357)
(`e2529fb`), in the decided shape and against every acceptance criterion:
[`src/trellis_workers/trace_embed/`](../../src/trellis_workers/trace_embed/) — `worker.py`
(collect → render → embed → advance), `watermark.py` (the explicit external cursor), and
`handler.py`, which registers the long-declared-but-never-handled
`Operation.EVIDENCE_INGEST` **on the worker's own executor** rather than in
`create_curate_handlers`, so `evidence.ingest` does not silently acquire semantics on every
mutation surface as a side effect. Driven by `trellis worker embed-traces`. Two details
worth carrying forward: the cursor is an *optimisation*, not a record of work —
`trace_is_embedded` asks the vector store, so a cursor that ran ahead can only cause a
re-check, never a skip — and the embed is deliberately **not** fail-soft here (unlike
`run_embed_on_ingest`), because the vector row is the entire point and a fail-soft embed
would reproduce the green-looking no-op the item exists to fix.

⚠️ **The observation half did not land, and the item's title over-promises.**
`record_observation` ([`mcp/server.py`](../../src/trellis/mcp/server.py)) still has no embed
call on any path and the worker walks the **trace store only** — verified 2026-08-31. If
observations matter, that is a separate item; do not read `trace_embed/` as covering them.

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

**A3 — [#336](https://github.com/ronsse/trellis-ai/issues/336) effectiveness-based noise demotion is unsound without item attribution.** ~~`class: panel`~~ — ✅ **CLOSED** 2026-08-28 via [#380](https://github.com/ronsse/trellis-ai/pull/380) (`69986ac`)
**The spec this entry used to carry was wrong in both halves, and is deleted rather than
amended.** It said "read `pack_attribution_rate` (0.933), not the headline
`attribution_rate`" and "the real weakness is sample size". Neither survived measurement:

- **Wrong denominator.** `pack_attribution_rate` counts `helpful ∪ unhelpful`. The noise
  proposal reads **`helpful_item_ids` only**, and on *that* denominator coverage was
  **0.778** (14/18), not 0.933. Two metrics that sound like the same thing were not.
- **Wrong diagnosis.** The weakness was neither sample size nor threshold calibration.
  `usage_rate = helpful_citations / appearances` is **degenerate** at this base rate:
  `P(cited helpful | served) = 0.1029`, so an ordinary item served twice goes uncited with
  probability 0.805 and the rule flags good items *by construction*. On the live corpus
  every threshold in (0, 0.333] flagged the **same 64 of 79** items. Raising
  `min_appearances` cannot rescue it. #336's own premise ("no item attribution at all") was
  also stale — 93.2% of servings sit in a pack whose feedback named at least one item.

**What shipped** is [`classify/demotion_gate.py`](../../src/trellis/classify/demotion_gate.py):
demotion requires *evidence of unhelpfulness*, never absence of evidence of helpfulness. The
negative signal is four times denser (`P(cited unhelpful | served) = 0.4118`) and the old
rule never read `unhelpful_item_ids` at all. `EffectivenessReport` reports proposal
(`noise_candidates`) and verdict (`demotion_screen.admitted`) **separately**, because a
proposal that shrinks 62% at the gate is a fact about the proposal rule. Full reasoning:
`CLAUDE.md` § "The demotion evidence gate". **Do not re-derive the numbers from this file** —
the window rolls; re-run `trellis analyze value`.

Residual, tracked elsewhere: ledger **A-4** (restore the 22 memories the unsound gate
demoted) is production data and operator-only.

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

**B2 — PackBuilder chunk rollup (roadmap §G.4).** ~~`class: panel`~~ — ⛔ **REFUSED ON MEASUREMENT** 2026-08-28 via [#384](https://github.com/ronsse/trellis-ai/pull/384) (`d821094`). **Do not re-propose without new evidence.**
The old spec — "two chunks of one document both enter a pack and spend the budget twice;
group by `parent_doc_id` at assembly" — is deleted rather than amended, because an agent
following it would build something already rejected. The *phenomenon* is real (16 of 37
packs, 51 extra servings, 11.7% of injected tokens over the 30 days to 2026-08-28); the
*fix* is not. Four reasons, each measured, none of which the spec anticipated: the extras
are **top-ranked, not tail** (every cited-helpful extra sat at rank 3–5, so `K=1` demotes
5 of 5); chunk excerpts are **not duplicate text** (200-char overlap on a 3000-char target,
each excerpt cut from its own chunk's head); on-topic chunks are **jointly** useful, so
"keep the best chunk" is not a lossless summary; and the saving would not materialise
because `max_tokens` is a quota — 20 of 37 packs hit `max_items` and a freed slot largely
refills with another chunk of the same parent. #359 already banks the tail half.

What shipped instead is the **instrument, not a fix**:
[`retrieve/concentration.py`](../../src/trellis/retrieve/concentration.py) records
`PACK_ASSEMBLED.payload["parent_concentration"]` so the question is re-askable at larger `n`
instead of re-derived by string-matching `item_id`. The cited-helpful evidence rests on two
attributed groups, which is thin — **that** is the reopen condition, not taste. Full
reasoning: `CLAUDE.md` § "Repeat-source concentration — measured, and the rollup refused".

The item's *second* half was a separate, live defect and shipped: the documents list view
now default-filters chunk rows — [#385](https://github.com/ronsse/trellis-ai/issues/385) via
`bf113be`, extended to the other whole-document surfaces by
[#396](https://github.com/ronsse/trellis-ai/issues/396) (`0e5ed75`).

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

**C1 — Wire the policy gate.** ~~`class: panel`~~ — ✅ **LANDED** 2026-08-28 via [#370](https://github.com/ronsse/trellis-ai/pull/370) (`1e6c66e`)
`build_curate_executor` now passes `policy_gate=build_policy_gate(registry)`
**unconditionally** ([`mutate/__init__.py`](../../src/trellis/mutate/__init__.py); the
reasoning is in the function's own docstring). Stage 2 runs on every surface, so "is the
gate wired?" has an observable answer instead of depending on file state. The default
posture is an *empty* gate that is behaviourally and byte-for-byte indistinguishable from
the old no-gate world — pinned by
`tests/unit/mutate/test_policy_wiring.py::TestDefaultPostureIsTransparent`.

The same PR made the policy file **one** file
([`policy_source.py`](../../src/trellis/mutate/policy_source.py),
`resolve_policy_path`) — CLI and API had been writing two different paths — and fixed two
live defects the wiring exposed: gate `warnings` were dropped on the allow path, and
`action="warn"` was dead code. Resolution is **deny-wins**, not most-specific-wins.
Hardened again by [#413](https://github.com/ronsse/trellis-ai/issues/413) (`fb2e168`): a
policy file that loaded degraded now refuses to be written over, and the strict reader is
actually strict.

**Consequence for the rest of this wave:** C3/#194's acceptance criterion (b) is now
reachable, and [#360](https://github.com/ronsse/trellis-ai/issues/360) — which "gains most
of its point after C1" — is unblocked.

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

**E2 — Capture-coverage measurement.** ~~`class: panel`~~ — ✅ **DONE and MERGED** as `627536f` ([PR #372](https://github.com/ronsse/trellis-ai/pull/372), 2026-08-28).
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
