# Swarm handoff — running autonomous work on trellis-ai

> **Read this first if you are picking up autonomous implementation work.** It is the
> operating manual: the autonomy contract, the merge gate, the traps that have already
> cost time, and the dependency-ordered queue. Companion files:
> [`autonomous-backlog.md`](./autonomous-backlog.md) (what the work *is*),
> [`decision-ledger.md`](./decision-ledger.md) (decisions taken and pending),
> [`implementation-roadmap.md`](./implementation-roadmap.md) (**authoritative** — when it
> and the backlog disagree, the roadmap wins).
>
> Last updated 2026-08-27 at `c356ed6`.

## 1. State

`main` = `c356ed6`. PR queue clear. Prod containers on skynet run current `main`.

Landed 2026-08-26: doc reconciliation + backlog (#340), token-economics wave (#341),
DoD-3 reframe (#304), **noise exclusion actually holding** (#343), dependabot ruff
0.16.4/mypy (#328), **attribution decomposed + join key restored** (#344), Wave 1
bookkeeping (#346).

Live measurements to build against — re-measure rather than trusting these:

| metric | value | note |
|---|---|---|
| `pack_attribution_rate` | **0.933** | 14 of 15 pack-targeted events cite items |
| `attribution_rate` (headline) | 0.359 | dragged down by 24 untargeted events |
| `untargeted_feedback` | 24 of 39 | work where no pack was ever served |
| `injected_coverage` | 1.0 | pack side is healthy |
| packs served (30d) | 31 | **sample size is the real constraint, not citation rate** |

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
| Concurrent subagents in one tree | uncommitted work collides | give each a `git worktree`; the shared `.venv` `.pth` points at the *main* tree, so set `PYTHONPATH=<worktree>/src` or tests silently run against main's code |
| `git checkout -b` then commit | HEAD reverted mid-session once; a commit landed on `main` | `git branch --show-current` immediately before committing |
| GitHub Actions outage | runs sit `queued`, then `CANCELLED` | watch `githubstatus.com` components, not `gh pr checks` (which errors); re-trigger cancelled runs with `update-branch` |

## 6. The queue

Dependency-ordered. Items are sized for one subagent and one PR. Full specs in
[`autonomous-backlog.md`](./autonomous-backlog.md).

**Next up — do these first:**

1. **#345 — pgvector contract fixture.** Cheap, and it is a coverage hole over the
   *production* backend. Fix `s._conn.cursor()` → `with s._conn() as conn, conn.cursor()`,
   then decide whether to wire a pgvector service container into CI, and reconcile
   `CLAUDE.md`'s claim that contract suites "run in CI against SQLite, Postgres, and a
   containerized Neo4j" — for the vector contract that is SQLite only.
2. **A1 — trace/observation semantic embedding.** Panel-decided (unanimous): a **batch
   backfill worker**, not a write-path change. Traces are immutable, so embedded-state
   cannot be stamped on the record — it needs an external watermark or side table, and
   **a tracking gap silently skips rows**, which is the first thing to test. Embed
   `outcome.summary` + `intent`, not the step log. Vector writes go through
   `MutationExecutor`.
3. **A3 — #336 noise-demotion soundness.** Unblocked. Read `pack_attribution_rate`
   (0.933), **not** the headline. The weakness is sample size (15 events), not citation rate.
4. **F1 — token economics.** Add `pack_id` to `TOKEN_TRACKED`
   ([`token_tracker.py:39`](../../src/trellis/retrieve/token_tracker.py) emits `layer` /
   `operation` / `response_tokens` / `budget_tokens` / `trimmed` / `agent_id` and no join
   key), then compute **useful-token fraction** — of tokens injected, what share went to
   items later cited helpful, per strategy and per intent family. Report `n` with every
   ratio and refuse one below a stated minimum. This is the operator's headline ask: prove
   memory returns more than it costs.

**Then:** F2 (trimming + progressive-disclosure defaults, depends F1) → B1 (#298,
**re-measure first**, may be smaller than written) → B2 (chunk rollup) → B3 (alias
resolver indexing) → C1 (**wire the policy gate** — `build_curate_executor` passes none, so
the documented five-stage pipeline is four stages in production; prerequisite for #194) →
C2 (#256) → C3 (#194) → C4 (#264) → D1–D4 (query-history) → E1 (#306) / E2.

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
