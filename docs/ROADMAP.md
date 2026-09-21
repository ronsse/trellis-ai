# Roadmap — trellis-ai

```yaml
last-review: 2026-09-16
```

> **This file is a router, not a restatement.** Five documents already say where this
> project is going, and they disagree about scope *on purpose* — product thesis, code
> state, autonomy contract, capability program, decisions. Nothing here is the source of
> truth for any of them. What this file owns is the one question none of them answer:
> *which door do I open, and what has to become true before the next thing can start.*
>
> It carries no item list and no Done section. Both would be a second copy of something
> already maintained elsewhere, and the repo has paid for that twice — see
> [`implementation-roadmap.md`](design/implementation-roadmap.md) §5's "deliberately a
> pointer, not a restatement", and the two stale coverage claims that survived for months
> in `CLAUDE.md` because each was a transcription of a measurement rather than the
> measurement.

## 1. Which document is authoritative for what

| The question you actually have | Read | It is authoritative for |
|---|---|---|
| What is this for, who adopts it, what are we deliberately **not** building | [`PRD.md`](PRD.md) | the thesis, the adopter profiles, the anti-scope list (§6 "Not doing") |
| What is the state of the code, and in what order do the open phases execute | [`design/implementation-roadmap.md`](design/implementation-roadmap.md) | the live single-page hand-off: §1 state, §2 recently completed, §3 open phases A–H, §4 execution order |
| I am an autonomous agent picking up work | [`design/swarm-handoff.md`](design/swarm-handoff.md) | the autonomy contract, the merge gate (§4), the traps that already cost time |
| What did we decide, and what is still open | [`design/decision-ledger.md`](design/decision-ledger.md) | decisions taken and pending |
| Where is the recursive-self-improvement work | [`design/plan-self-improvement-program.md`](design/plan-self-improvement-program.md) | the capability program: eight sub-plans, their dependencies, the POC hard rules |
| What is actually in flight *right now* | **the tracker and the open PRs** | — nothing in this repo can answer it, see §2 |

Picking up implementation work starts at `implementation-roadmap.md`, exactly as
`CLAUDE.md` says. This page is for deciding *whether* that is the right door.

## 2. The queue is not in this repository

`git log` cannot see unmerged work, and a file that enumerates the queue is wrong within
a day. Measured 2026-09-16: **36 open pull requests, 44 open issues.** Re-derive rather
than trusting either number:

```bash
gh pr list --state open --limit 100 --json number,title
gh issue list --state open --label ready --limit 30
```

So the Now / Next / Later below name **gates, not items**. Each one states what has to
become true and how to check it, because the check outlives the number.

## 3. Now

**N1 · Unblock the merge gate.** `swarm-handoff.md` §4 sets the gate at *"green against
current `main`"* — and it is currently unsatisfiable for reasons unrelated to any PR's own
change. Measured 2026-09-16 over the last 40 `live-infra` runs: of the 37 triggered by
`pull_request`, **31 failed, 5 passed, 1 cancelled** — and 4 of the 5 passes were on the
two branches that *fix the ArcadeDB alias-claim race*. Sampling four failing branches
spanning docs, feature and CI work, all four fail the **same single test**,
`test_arcadedb_graph_contract.py::…::test_bind_alias_if_absent_is_atomic_for_concurrent_contenders`.
One race is red-lighting the whole open queue, which makes it a *precondition*, not an
item competing with 35 others.

- Acceptance: the tally below stops being dominated by `failure`.
  ```bash
  gh run list --workflow live-infra.yml --limit 40 --json conclusion,event \
    --jq '[.[] | select(.event=="pull_request") | .conclusion] | group_by(.) | map({(.[0]): length})'
  ```
- **Owner-gated.** Merging is never the agent's, at any confidence.

**N2 · Deployment catch-up (#546, `owner-only`).** Production runs a commit that exists on
no remote branch, so the drift detector cannot see it. Re-measured 2026-09-16 (method and
figures in PR #594): the fork is **exactly one file**, `src/trellis_api/static/index.html`
— a fast-forward plus one three-way merge, not the 42-commit rebase the issue's framing
implies. Nine modules are absent from the running build, and one of them,
`core/memory_op_judged.py`, is the data source N3 depends on: **prod is not producing that
stream at all.**

- Acceptance: `trellis admin write-config --format json` and `GET /api/version` on the
  running API report a commit that `git branch --contains` can resolve on `main`.

**N3 · The security floor** — PRD §6 "Now": #250 credential hygiene (`owner-only`, needs
1Password + the Neo4j console) and #194 classification enforcement (`keystone`,
`blocked:owner-decision`). #194's pull-forward is an open question in PRD §8, not a
scheduled item: it needs a decision before it needs an implementer.

**N4 · Query-history curation primitives** #200–#203 — all three carry
`blocked:owner-decision`. They are fixture-testable today; what they are waiting on is the
disposition in PRD §8, not code.

## 4. Next

**X1 · Close the judged → outcome loop.** `memory_op.judged` is roughly 25× denser than
pack feedback, and PR #584 joins it to what followed. The Phase 1 gate is 500 strictly
graded rows; the join currently yields **289 over 30 days**, because citations are capped
by attributed packs — so the gate is not met and the exporter is not built. Two things
unblock it, in order: N2 (prod does not emit the stream), then attribution volume.

- Acceptance: the strict-graded row count over a 30-day window clears 500.

**X2 · Capability program, phases 1–3** —
[`plan-self-improvement-program.md`](design/plan-self-improvement-program.md) §4 carries
the execution order, §3 the eight sub-plans and their dependencies. Read §2 before
proposing anything: the POC directive (no silent fallbacks, no compat shims, no
half-finished features) supersedes `CLAUDE.md`'s softer defaults on that one dimension.

**X3 · The remaining open phases** — `implementation-roadmap.md` §3 B/C/D/E/G/H and the
execution order in §4. Nothing in this file reorders them.

## 5. Later

**L1 · The graph is a provenance map, and it is not yet connected to anything.** The
stated direction is a federation map — a provenance ledger of memories that links *out* to
file stores, databases and other knowledge graphs — rather than a subject-matter knowledge
graph of its own. Base rates measured 2026-09-16 (PR #594, over 1,896 current nodes and
1,945 current edges) say what that would take:

- **265 nodes (14.0%) already carry an external referent** — path, repo, machine, host,
  file, endpoint, url — but only **4 edges in the entire graph connect two of them**.
  The referents exist; the federation does not.
- **Non-PROV semantic edges are 8 of 1,945 (0.41%).** The graph records who generated
  what, and almost nothing about how things relate.
- **213 nodes (11.2%) are isolated**, and 163 of them are the `gotcha` / `concept` /
  `system` types written by `save_knowledge` — `gotcha` is isolated **72 of 72**. The tool
  built for durable cross-task knowledge deposits every entity with no edges, so the graph
  axis cannot reach any of it once it stops being recent.

L1 is *Later* because it is a design question, not a backlog item: nothing above it should
wait on it, and it should not be started as an implementation.

**L2 · All-local operation.** Nearer than it reads. All six stores are local, and the LLM
and embedder are already 100% local Ollama — `provider: openai` names the *wire protocol*,
not the vendor. The remaining non-local dependencies are three: GitHub Actions, the
Cloudflare public hostname, and the litellm Kimi tier used for panel-style reasoning.
(The `[cloud]` extra is a misnomer and is tracked for rename — psycopg and pgvector are
what make the *local* Postgres work.)

**L3 · Icebox** — PRD §6 "Not doing" is the anti-scope list, and it is load-bearing: this
project has scope-creep gravity and several of those entries are things that were built
and deleted. Read it before proposing an addition.

## 6. Keeping this file honest

Every figure above is a timestamped measurement with the command that reproduces it. When
one goes stale the fix is to **re-derive it, not to edit the sentence** — both of
`CLAUDE.md`'s long-lived coverage errors were transcriptions that nobody re-ran, and both
were wrong in the pessimistic direction, which is the direction that wastes work.

An item that completes moves to `implementation-roadmap.md` §2, not to a Done section
here.
