# Swarm handoff — running autonomous work on trellis-ai

> **Read this first if you are picking up autonomous implementation work.** It is the
> operating manual: the autonomy contract, the merge gate, the traps that have already
> cost time, and the dependency-ordered queue. Companion files:
> [`autonomous-backlog.md`](./autonomous-backlog.md) (what the work *is*),
> [`decision-ledger.md`](./decision-ledger.md) (decisions taken and pending),
> [`implementation-roadmap.md`](./implementation-roadmap.md) (**authoritative** — when it
> and the backlog disagree, the roadmap wins).
>
> Last updated 2026-08-27 at `738cb74`.

## 1. State

`main` = `738cb74`. **The prod containers on skynet do _not_ run current `main`** — §1.2.
That sentence used to read the other way round in this file; it stopped being true and
nothing noticed, which is the failure shape §8 is about.

Landed 2026-08-26: doc reconciliation + backlog (#340), token-economics wave (#341),
DoD-3 reframe (#304), **noise exclusion actually holding** (#343), dependabot ruff
0.16.4/mypy (#328), **attribution decomposed + join key restored** (#344), Wave 1
bookkeeping (#346).

Landed 2026-08-27 — ten PRs, closing Waves 1 and 1b entirely:

| PR | What |
|---|---|
| [#347](https://github.com/ronsse/trellis-ai/pull/347) | this handoff + the decision ledger |
| [#352](https://github.com/ronsse/trellis-ai/pull/352) | `save_experience` schema docs — **the first outside contribution to this repo** |
| [#353](https://github.com/ronsse/trellis-ai/pull/353) | **F1** — value density measured; response cost made attributable to packs |
| [#354](https://github.com/ronsse/trellis-ai/pull/354) | swarm state + the traps that wave taught |
| [#355](https://github.com/ronsse/trellis-ai/pull/355) | one unknown key discards the whole trace — said plainly, pinned by test |
| [#357](https://github.com/ronsse/trellis-ai/pull/357) | **A1** — traces reachable by semantic search at all |
| [#358](https://github.com/ronsse/trellis-ai/pull/358) | **#345** — the pgvector contract suite actually executes, and is wired into CI |
| [#359](https://github.com/ronsse/trellis-ai/pull/359) | **F2** — graduated disclosure, justified by counterfactual replay |
| [#361](https://github.com/ronsse/trellis-ai/pull/361) | T-3 governance decision; the roadmap stops restating CI coverage |

### 1.1 In flight

Three agents dispatched 2026-08-27 from `738cb74`, on non-colliding lanes:

| branch | item | lane |
|---|---|---|
| `swarm/b1-stub-noise` | B1 / [#298](https://github.com/ronsse/trellis-ai/issues/298) — **re-measure before implementing** | `retrieve/` |
| `swarm/c1-policy-gate` | C1 — wire the policy gate | `mutate/`, `core/` |
| `swarm/b3-alias-index` | B3 — probably already done; verify before building | `extract/` |

The previous wave's three interrupted branches are all resolved and their worktrees removed:
`swarm/a1` → #357, `swarm/345` → #358, `swarm/349` → #352 + #355.

### 1.2 The deployment lag — read this before trusting any production measurement

Measured 2026-08-27. **Three different builds of Trellis are live against one database.**

| Where | Reports | Actually runs | Gap |
|---|---|---|---|
| `trellis-api`, `trellis-mcp` containers | `5f5a1d779` | `5f5a1d779` | **16 commits behind `main`** |
| `trellis-skynet` (host CLI, editable install) | `35a9978fa`, `dirty: false` | the working tree — current `main` | **stamp is 43 commits stale and _wrong_** |

Two consequences that will mislead you if you do not hold them in mind:

- **The containers do not have #343.** Every pack served through MCP for the last two days
  was assembled by code where noise exclusion does *not* hold on the semantic axis. Measure
  recent packs, conclude "the noise problem is still real," and you have measured the
  absence of a deployment rather than the absence of a fix. Same for #338, #344, #353,
  #357, #359.
- **The CLI's provenance stamp is false, not merely stale.** `hatch-vcs` stamps at *install*
  time; an editable install's code is the working tree. So CLI writes carry a specific,
  confident, wrong commit — and `version_source: dist-metadata` claims to be authoritative
  while `dirty: false` removes the one hint that might prompt a second look. This inverts
  the design intent recorded in `CLAUDE.md` ("honestly unidentifiable rather than falsely
  identified"). Written up on [#348](https://github.com/ronsse/trellis-ai/issues/348).

The useful asymmetry: **analysis is current, the data is not.** `trellis-skynet` executes
the working tree, so `trellis analyze *` runs current `main` code — over rows written by
stale container code.

Rebuilding is a **production mutation** and therefore operator-only at any confidence —
ledger **A-3** carries the recommendation. Do not use `make docker-build`; production runs
the skynet-hub compose stack.

### 1.3 Live measurements — re-derive, never transcribe

**The window rolls.** These are 30 days to **2026-08-27**, `n=17` attributed packs. During
the last wave the headline moved 0.1148 → 0.1019 → 0.0884 **with no code change at all**,
purely because new feedback joined packs already inside the window. Any figure here without
an as-of date is a bug.

| metric | value | note |
|---|---|---|
| `useful_token_fraction` | **0.0884** | 8.8% of injected tokens land on items later cited helpful |
| `unhelpful_token_fraction` | 0.4915 | explicitly cited unhelpful |
| `unjudged_token_fraction` | 0.4201 | [#364](https://github.com/ronsse/trellis-ai/issues/364) — the headline is a *lower bound* over 58% of what it describes |
| `pack_attribution_rate` | **17/18 = 0.944** | of packs actually served and graded |
| `attribution_rate` (headline) | 17/43 = 0.40 | dragged down by 25 untargeted events; denominator kept on purpose |
| `response_pack_id_coverage` | **0.0** (0/33) | [#363](https://github.com/ronsse/trellis-ai/issues/363) — pinned at zero by §1.2, not by a wiring defect |
| packs served (30d) | 37 | **sample size is still the binding constraint** |

Per-axis, and this is the surprise worth carrying forward:

| axis | `useful_token_fraction` | n | injected tokens |
|---|---|---|---|
| graph (entity) | **0.1744** | 16 | 2,013 |
| semantic (vector) | 0.1069 | 17 | 18,533 |
| keyword (document) | **0.0241** | 9 | 8,019 |

**Keyword is the weakest axis by 7× while spending 4× the graph axis's tokens.** The graph
axis is the densest thing in a pack. Any proposal to trim the graph/entity axis — #298 among
them — has to reckon with that, and separate the *stubs* it means from entities generally.

## 2. The autonomy contract

Granted by the operator 2026-08-26. **Do not widen it on your own initiative.**

- **Reversible in git → yours.** Approach, naming, module boundaries, scope splits, test
  strategy, fix-now-vs-file-an-issue.
- **GitHub state → yours.** Open/close/label issues, comment on PRs, reorganize milestones.
  One guard: **before closing an issue, grep the linked ADR for its own Status/Decision
  line.** Closing an issue with an unresolved owner gate is how #312 hid one for three
  days — gate labels are only read on *open* issues.
- **Never yours, at any confidence:** publishing (PyPI, releases), deleting or redacting
  production data, credential operations, spend, force-push or history rewrite, repo
  settings.

**The swarm never blocks on a decision.** See §3.

## 3. Decisions: panel, then ledger

When you hit a fork you have no defensible answer to and it is reversible:

```bash
python3 ~/.claude/skills/decision-panel/panel.py --brief brief.md --reversible
```

Exit `0` unanimous → act. Exit `3` split → **do not tie-break**; record and continue.
Exit `4` unusable → retry or record.

Two rules that carry the whole mechanism:

- **Verify before acting.** Extract every `path:line`, API, flag and measurement the panel
  asserted and check it. This has already paid: a panel's warning about immutable records
  needing an external watermark was a real catch the caller had missed, and testing another
  panel's falsifiable condition *narrowed an item's scope* rather than flipping it.
- **Write a brief with measured numbers in it.** Panelists are instructed to choose even
  when underspecified and to name the ambiguity in `top_risk`. A thin brief returns
  something useless, confidently, and the failure is silent.

Then record it in [`decision-ledger.md`](./decision-ledger.md) — **with a recommendation**.
"This needs operator input" is not a ledger entry. Name the options, say which you would
pick and why, and state what being wrong costs. Ship the safe default (usually: the
capability exists but is off) so the operator's answer costs a config change, not a PR.

## 4. The merge gate

**Green against *current* `main`.** Not "green".

```bash
gh pr view <n> --json statusCheckRollup,mergeStateStatus \
  --jq '(.statusCheckRollup|map(.name+"="+((.conclusion//.status)//"?"))|join("  ")),("mergeState: "+.mergeStateStatus)'
```

All eight must be `SUCCESS` and `mergeState: CLEAN`: `lint`, `typecheck`,
`test (3.11/3.12/3.13)`, `openapi-check`, `CodeQL`, `Analyze (python)`.

**If `main` moved since the run, `gh pr update-branch` and wait for a fresh run.** This is
not pedantry — a dependabot bump of ruff/mypy landed mid-session and could legitimately
have failed the next PR's lint. `main` has **no required status checks**, so nothing but
this discipline stands between an agent and an unverified merge (ledger D-3).

**One merge authority.** Subagents open PRs; the orchestrator merges. Never let a subagent
merge its own work.

## 5. Traps that have already cost time

| Trap | What happens | Do this |
|---|---|---|
| `make lint/test/typecheck` | exit 127, `ruff: No such file or directory` | `source .venv/bin/activate` first |
| `uv run <anything>` | silently rewrites `uv.lock` (573 deletions from a read-only command) | never use it here; `git checkout -- uv.lock` if it appears |
| Local `make test` | deselects 635 tests (`postgres`, `pgvector`, `neo`, `arcadedb`, `live`, `slow`) | local green ≠ CI green |
| pgvector contract suite | **has never run anywhere**; fixture broken (#345) | production runs pgvector — vector changes are unverified on it |
| Scratch pgvector without `CREATE EXTENSION vector` | **hangs** on `futex_wait_queue`, ~30 idle conns, no error | create the extension; never point `TRELLIS_TEST_PG_DSN` at prod (`:5433`) — the fixture `TRUNCATE`s |
| Concurrent subagents in one tree | uncommitted work collides | give each a `git worktree` under `/mnt/ssd/trellis-worktrees/` |
| A worktree using the main tree's `.venv` | `import trellis` resolves to **main's** code, so the worktree's tests pass green without executing the changes under test — silently | prefix every `python`/`pytest` with `PYTHONPATH=<worktree>/src`. Verified 2026-08-27 by marker probe. Works *only because* `_editable_impl_trellis_ai.pth` is a plain-path `.pth`; a setuptools `__editable___*_finder` meta-path hook would beat `PYTHONPATH` and the same mitigation would fail silently. Re-check after any reinstall. |
| `git checkout -b` then commit | HEAD reverted mid-session once; a commit landed on `main` | `git branch --show-current` immediately before committing |
| Agent killed mid-task by a shared session limit | four agents died at once with hours of uncommitted work in their worktrees; nothing is lost *yet*, but nothing is durable either | **tell agents to commit early and often on their own branch.** A WIP commit costs nothing and survives; an uncommitted worktree is one `git checkout` from gone. The orchestrator should sweep worktrees for uncommitted work the moment an agent reports failure |
| A measurement taken against production | the containers may not run the code you just merged (§1.2) — you measure the absence of a *deployment*, not the absence of a fix | check `write_provenance.commit` on both the CLI (`trellis-skynet admin write-config`) and the API (`GET /api/version`) before believing a production number. They disagree with each other and with `main`. |
| Trusting a rolling-window figure copied from a doc | the 30-day window moves; the headline shifted 0.1148 → 0.0884 with **no code change** | re-derive every time; treat any figure without an as-of date as unusable |
| GitHub Actions outage | runs sit `queued`, then `CANCELLED` | watch `githubstatus.com` components, not `gh pr checks` (which errors); re-trigger cancelled runs with `update-branch` |

## 6. The queue

Dependency-ordered. Items are sized for one subagent and one PR. Full specs in
[`autonomous-backlog.md`](./autonomous-backlog.md).

**Waves 1 and 1b are closed.** #345, A1, F1 and F2 all landed 2026-08-27; B1, B3 and C1
are in flight (§1.1). What remains, roughly in dependency order:

1. **A3 — [#336](https://github.com/ronsse/trellis-ai/issues/336) noise-demotion soundness.**
   Read `pack_attribution_rate` (**0.944**, §1.3), *not* the headline. The weakness is
   sample size — 18 pack-targeted events — not citation rate.
2. **B2 — PackBuilder chunk rollup.** `PackBuilder` dedups by `item_id`, so two chunks of
   one document can both enter a pack and spend the budget twice. Group by `parent_doc_id`
   at assembly. Wait for B1 to land — same file territory.
3. **[#360](https://github.com/ronsse/trellis-ai/issues/360) — govern the document and
   vector planes.** Panel-decided (unanimous, option B) and recorded as ledger **T-3**;
   implementation not started. #357's worker-local handler is the natural seed. Gains most
   of its point *after* C1, since stage 2 is a no-op until a gate is wired.
4. **C2 → C3 → C4** — [#256](https://github.com/ronsse/trellis-ai/issues/256) Bolt plugin
   extraction (halves #194's enforcement surface, so it precedes it),
   [#194](https://github.com/ronsse/trellis-ai/issues/194) classification enforcement,
   [#264](https://github.com/ronsse/trellis-ai/issues/264) judged-memory logging (measured
   as 2 of 5 stages missing emitters).
5. **D1–D4** query-history curation, then **E1** ([#306](https://github.com/ronsse/trellis-ai/issues/306)) / **E2**.

**Measurement-integrity issues filed 2026-08-27, all unstarted.** These are cheap and they
protect every number above, so they are worth interleaving rather than queueing behind
feature work:

| issue | what is wrong |
|---|---|
| [#362](https://github.com/ronsse/trellis-ai/issues/362) | `get_items` fetch cost is off-book, so the number that would justify index mode cannot be measured |
| [#363](https://github.com/ronsse/trellis-ai/issues/363) | `TOKEN_TRACKED.pack_id` coverage 0/33 — **blocked on a container rebuild, not on code** (§1.2) |
| [#364](https://github.com/ronsse/trellis-ai/issues/364) | 42% of injected tokens get no verdict, so `useful_token_fraction` is a lower bound over 58% of what it describes |
| [#365](https://github.com/ronsse/trellis-ai/issues/365) | a retrieval that fails in transport is invisible — `write.rejected` exists for writes, nothing equivalent for a read that never arrives |

**CI coverage holes**, all filed, none started:
[#350](https://github.com/ronsse/trellis-ai/issues/350) (pgvector cannot create its own
extension), [#351](https://github.com/ronsse/trellis-ai/issues/351) (the *blessed* ArcadeDB
graph contract runs in no workflow), [#356](https://github.com/ronsse/trellis-ai/issues/356)
(`tests/unit/stores/` runs nowhere and cannot simply be swept in).

**File territories** — dispatch these in parallel, they do not collide:

| Lane | Owns |
|---|---|
| retrieval | `retrieve/`, `stores/*/vector*`, contract suites |
| feedback/learning | `feedback/`, `learning/`, `ops/write_health.py` |
| mutation/policy | `mutate/`, `core/` |
| extraction/workers | `extract/`, `trellis_workers/` |

## 7. Dispatch template

Every subagent prompt should carry: the item and its acceptance criteria; `CLAUDE.md` is
authoritative; the venv and `uv run` traps; its file territory and who else is running;
the Trellis loop (`get_context` before, `save_experience` after with failures in the step's
`error`, `record_feedback` with item ids); the panel escalation rule (**split → stop and
report, do not tie-break**); and **do not merge — the orchestrator owns the gate**.

Ask every agent to report *what it found that contradicts the brief*. That instruction has
produced the three most valuable findings so far.

## 8. What this repo keeps getting wrong

Every substantive finding on 2026-08-26 was the same shape: **a guarantee that quietly
stopped holding while everything reported green.**

- The noise filter that excluded nothing, because the default was never constructed.
- The attribution metric that measured retrieve-adoption while being read as ergonomics.
- Contract suites presented as "the authoritative spec" that never ran against the
  production backend.
- Earlier instances: filters hiding stored memory (#282), silent capture outages (#297),
  feedback that never joined (pre-#287), a reference rate that could only read 1.00.

**The panel never caught any of these. Measurement did, every time.** Before implementing
against a number, verify the number can move — query it, and check it can return more than
one answer. Before trusting a test, confirm it runs. Treat "it reports success" as the
weakest possible evidence.
