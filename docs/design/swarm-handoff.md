# Swarm handoff — running autonomous work on trellis-ai

> **Read this first if you are picking up autonomous implementation work.** It is the
> operating manual: the autonomy contract, the merge gate, the traps that have already
> cost time, and the dependency-ordered queue. Companion files:
> [`autonomous-backlog.md`](./autonomous-backlog.md) (what the work *is*),
> [`decision-ledger.md`](./decision-ledger.md) (decisions taken and pending),
> [`implementation-roadmap.md`](./implementation-roadmap.md) (**authoritative** — when it
> and the backlog disagree, the roadmap wins).
>
> Last updated 2026-08-29 at `9ed98f7`.

## 1. State

`main` = `9ed98f7`. **The prod containers on skynet do _not_ run current `main`** — §1.2.

Landed 2026-08-26: #340, #341, #304, **noise exclusion actually holding** (#343), #328,
**attribution decomposed + join key restored** (#344), #346.

Landed 2026-08-27 — nine PRs closing Waves 1 and 1b: #347, **#352 (first outside
contribution)**, **#353 (F1)**, #354, #355, **#357 (A1)**, **#358 (#345)**, **#359 (F2)**, #361.

Landed 2026-08-28 (all thirteen merged between 00:00Z and 03:21Z UTC). **Five of eleven
agent PRs came back having refuted the item they were sent to implement** — #367, #368,
#376, #380, #384. A sixth, #389, corrected its issue's premise but shipped the fix anyway;
count it or not, but say which. This is the wave's most reusable outcome:

| PR | Item | Outcome |
|---|---|---|
| [#367](https://github.com/ronsse/trellis-ai/pull/367) | B3 alias indexing | **already done in #289** — fixed a real cost it found instead (40.3% of resolver calls were exact **within-document** repeats — the cache is per-document, which is what makes the number actionable) |
| [#368](https://github.com/ronsse/trellis-ai/pull/368) | B1 / #298 | **all three proposed directions refuted**; shipped `by_item_namespace`, the axis that could show it |
| [#370](https://github.com/ronsse/trellis-ai/pull/370) | C1 policy gate | **stage 2 now runs**; a full policy CRUD surface already existed wired to nothing |
| [#372](https://github.com/ronsse/trellis-ai/pull/372) | E2 capture coverage | denominator taken from the *deployed* gate, not a new eligibility rule |
| [#376](https://github.com/ronsse/trellis-ai/pull/376) | #371 graph axis | **the obvious fix produced 0 seeds on 37/37 packs** (30 distinct intents) and changed no served item — but it is not free: `test_the_embed_is_still_paid_for` pins one embed call per pack |
| [#380](https://github.com/ronsse/trellis-ai/pull/380) | A3 / #336 | **no threshold could have worked** — `usage_rate` is degenerate (`{0.0: 64, 0.333: 4, 0.5: 8, 0.8: 1, 1.0: 2}`), so every value in (0, 0.333] flags the same 64/79. #380 left the threshold alone and added a downstream evidence predicate; 64 → 24, all 8 named memories spared |
| [#382](https://github.com/ronsse/trellis-ai/pull/382) | #373 advisories | one resolver; **and the advisories are degenerate** (#383) — 37 when measured, **51 today**, growing ~2/night because nothing is ever replaced |
| [#384](https://github.com/ronsse/trellis-ai/pull/384) | B2 chunk rollup | **refused** — every cap loses cited-helpful bodies faster than it saves tokens |
| [#386](https://github.com/ronsse/trellis-ai/pull/386) | #381 | nightly curate now syncs vector metadata, **proven on scratch stores** not asserted |
| [#387](https://github.com/ronsse/trellis-ai/pull/387) | #378 | one ruff version, **enforced by a check instead of a comment** |
| [#389](https://github.com/ronsse/trellis-ai/pull/389) | #374 / #364 | `scan_events`; `useful_token_fraction` keeps its denominator and gains a bound |
| #366, #379 | orchestrator docs | the deployment lag, and a trap propagated to six agents |

**At least six of the orchestrator's own claims were wrong and agents caught every one** —
#374's urgency (~3.2x overstated), where #374's fix actually lives, #374's banner-suppression
mechanism, #371's "one production seed producer" (it is zero), #336's premise, and the
`PYTHONPATH` trap. The first draft of this sentence said *four*: do not trust a count in this
file that flatters its author.

### 1.1 In flight

| branch | item |
|---|---|
| `swarm/383-advisory-generator` | [#383](https://github.com/ronsse/trellis-ai/issues/383) + [#385](https://github.com/ronsse/trellis-ai/issues/385) — carries a **WIP commit that does not import**; resume, do not restart |
| `swarm/388-chunk-sync-and-order` | [#388](https://github.com/ronsse/trellis-ai/issues/388) (third #338 site) + the six remaining ascending-default event reads |

### 1.0 What the 2026-08-28 wave actually found

Three defects where **a mechanism reported success while doing nothing**. Two were live;
**#374 is latent** — it has never actually truncated, and the urgency in the issue as filed
was ~3.2x overstated (see the correction on it):

- **[#373](https://github.com/ronsse/trellis-ai/issues/373) — every advisory is invisible to every pack** (fixed by [#382](https://github.com/ronsse/trellis-ai/pull/382)). Writers use `<data_dir>/advisories.json`; readers use `<data_dir>/stores/advisories.json`, which does not exist, and `if adv_path.exists()` binds `None` silently.
  **Read the whole causal chain, not the headline** — the first correction of this was itself wrong. "0 advisories served" has **at least four** sufficient causes, and the path split is not the binding one:
  1. item attribution (the original diagnosis — partly fixed by #287 / #344),
  2. the path split (#373, fixed),
  3. **the flat path does not render advisories at all** — `format_advisories_as_markdown` is called only from `_sectioned_context`, and production has served **37 flat packs and 0 sectioned**, so fixing the path serves *zero additional tokens today* and buys telemetry,
  4. **the generator emits unusable output** ([#383](https://github.com/ronsse/trellis-ai/issues/383)) — 36 of 37 rows have `success_rate_without = 0.0`, so `effect_size` is the pack success rate wearing a causal claim.

  The instructive part is the failure *of the correction*: a single-cause explanation was replaced with a different single-cause explanation, which is the same error in a new position. When a metric reads zero, enumerate every path that could produce the zero before naming one.
- **[#371](https://github.com/ronsse/trellis-ai/issues/371) / [#375](https://github.com/ronsse/trellis-ai/issues/375) — the graph axis is a recency feed, not a search.** `GraphSearch.search()` carries `query: str,  # noqa: ARG002` and nothing supplies `seed_ids`, so it falls through to `GraphStore.query` = `ORDER BY created_at DESC`. Measured: a **median 8.6% of servable nodes over a median 58-hour window**, and coverage is **falling monotonically** (0.150 → 0.072) as the graph grows, because the reach is a fixed row count and decays as 1/N. One pack's window spanned **0.0 hours**.
- **[#374](https://github.com/ronsse/trellis-ai/issues/374) — the health analyzers will drop the NEWEST events.** `get_events` defaults to ascending; three analyzers cap at 5,000; production sits at **4,705**. For a health metric that is the worst truncation direction — a new outage falls outside the window, so the capture-health banner stays silent through exactly the incident it exists to catch.

Plus two CI-integrity defects: **[#377](https://github.com/ronsse/trellis-ai/issues/377)** (one bare `CliRunner` poisons structlog process-wide — 109 failures across 23 directories) and **[#378](https://github.com/ronsse/trellis-ai/issues/378)** (**two ruff versions run in CI at once**, 0.15.22 in `lint.yml` and 0.16.4 in `[dev]`, *and both carry a comment claiming they match*).

**The pattern to internalise:** four of six agents this wave were sent to implement something and came back having refuted it. That only happened because every brief demanded a measurement *before* a patch, and ended with "report anything you found that contradicts this brief." Keep both.

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

### 4.1 The review gate — every PR, before merge

Operator instruction, 2026-08-29: **every PR goes through a simplify pass and a review pass.**

The relevant agent definitions ship with the official plugin marketplace and are on disk at:

```
~/.claude/plugins/marketplaces/claude-plugins-official/plugins/pr-review-toolkit/agents/
  code-simplifier.md        · clarity/consistency, functionality preserved exactly
  code-reviewer.md          · CLAUDE.md compliance + bugs; reports only confidence >= 80
  silent-failure-hunter.md  · error handling, fallbacks, swallowed exceptions
  comment-analyzer.md       · comments that lie or restate the code
  pr-test-analyzer.md       · does the test actually exercise the change
  type-design-analyzer.md
```

**`pr-review-toolkit` is not installed — but check it the right way.** `ListPlugins` returns
empty and there is no `enabledPlugins` key in `~/.claude.json` or `~/.claude/settings.json`,
and **neither fact establishes that.** `ListPlugins` reads the *claude.ai* plugin namespace;
Claude Code marketplace plugins are recorded under `~/.claude.json` → **`pluginUsage`**, where
three (`anthropic-skills`, `desktop-commander`, `productivity`) are registered `@inline` and
demonstrably active. A probe that returns a clean empty result **while measuring a different
system** is precisely the defect §4.1 exists to hunt — and it was committed in the paragraph
that introduced §4.1.

So: check `pluginUsage`. Installing needs `/plugin`, which is interactive. Until then, **read
the definitions and dispatch them as ordinary subagents against the PR diff** — they are plain
markdown with a system prompt in the body, which is all the plugin adds. Check first whether
the session offers a built-in `/code-review`; it is user-triggered and billed, so an agent
cannot launch it, but the operator can.

Suggested run order — **untested**. Nothing had run these agents when this was written, so
this is a prediction, not experience:

1. **`silent-failure-hunter` first.** This repo's defining defect is *a mechanism that
   reports success while doing nothing*, and the week's record holds many instances
   (#338/#343, #344, #345/#358, #363, #370, #381/#386, #383, #385, #388). Two that do
   **not** belong in that set, because the boundary matters: #377 is a false *red* (a bare
   `CliRunner` caused 109 CI failures — the inverse), and #374 is **latent**, having never
   actually truncated.
2. **`code-simplifier`** — but hold it to its own rule: *preserve functionality exactly*.
   A simplification that changes behaviour is a bug wearing a tidy diff.
3. **`code-reviewer`** — reports only confidence >= 80, which is the right filter for a
   swarm that already produces long reports.

Two rules for using them:

- **Subagents self-review before opening the PR; the orchestrator reviews again before
  merging.** The second pass is the one that matters — an agent reviewing its own diff
  shares the blind spot that produced it.
- **A review finding is a claim, not a verdict.** Verify it the same way you verify a
  panel's assertions (§3) or an agent's report. Several agent claims this week were
  materially wrong in *both* directions, including two of the orchestrator's own filed
  issues.

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
| A worktree using the main tree's `.venv` | `import trellis` resolves to **main's** code for everything except the `pytest` **driver process**. **Nothing pytest spawns inherits the driver's exemption.** | `pyproject.toml` sets `pythonpath = ["src", "."]`, and that setting reaches the driver and nothing else. Verified empirically 2026-08-28 by marker probe in a scratch worktree: pytest → worktree `src` either way; bare `python -c "import trellis"` → **main's** `src`. So prefix bare `python`/scripts with `PYTHONPATH=<worktree>/src`; for the pytest *driver* it is harmless but not load-bearing. **A child process is not the driver** — the eight launch sites under `tests/` spawn `sys.executable` three times and a console script (`trellis`, `trellis-mcp`) five times, and *both* kinds resolve `trellis` through the venv's editable install, i.e. another checkout. #431: `tests/integration/cli/test_subprocess_smoke.py` parses a real process's stdout and from a worktree was reporting green about `main` (`tests/integration/mcp/test_stdio_stream_hygiene.py` parses one too, and has carried the checkout assertion since #428). #428 pinned the three fixtures that existed; #430/#431 shipped the enforcer — `tests/unit/test_subprocess_pythonpath_rule.py` (every launch site pinned or boundary-guarded; two scans of the population plus a written-down floor, because the two scans share a traversal and cannot catch each other narrowing it) and a `test_the_subprocess_under_test_is_this_checkout` in both subprocess directories. **This entry claimed pytest was affected, was propagated to six agents before being measured, then over-swung to "pytest is fine" — true only of the driver — and then to "every subprocess is a bare `python`", which is false for five of the eight.** The durable form of the fact is about *inheritance*, not about which binary is on the far end. |
| `git checkout -b` then commit | HEAD reverted mid-session once; a commit landed on `main` | `git branch --show-current` immediately before committing |
| Agent killed mid-task by a shared session limit | four agents died at once with hours of uncommitted work in their worktrees; nothing is lost *yet*, but nothing is durable either | **tell agents to commit early and often on their own branch.** A WIP commit costs nothing and survives; an uncommitted worktree is one `git checkout` from gone. The orchestrator should sweep worktrees for uncommitted work the moment an agent reports failure |
| A measurement taken against production | the containers may not run the code you just merged (§1.2) — you measure the absence of a *deployment*, not the absence of a fix | check `write_provenance.commit` on both the CLI (`trellis-skynet admin write-config`) and the API (`GET /api/version`) before believing a production number. They disagree with each other and with `main`. |
| Trusting a rolling-window figure copied from a doc | the 30-day window moves; the headline shifted 0.1148 → 0.0884 with **no code change** | re-derive every time; treat any figure without an as-of date as unusable |
| `gh pr merge --squash` on a branch whose commits are marked `[NOT REVIEWED]` / `WIP` | **the squash body is the concatenated commit messages, not the PR body.** Editing the PR body does not change what lands. On 2026-08-31 PR #428's gate explicitly flagged "12 of 13 commits carry `[NOT REVIEWED]`, squash-merge avoids landing them" — it does not, and `75b892e` now asserts NOT REVIEWED about code a gate had passed | pass `--body-file` (or `--body`) to `gh pr merge --squash` so the squash message is written deliberately. Fixing it afterwards means rewriting `main`, which is a force-push and **always the operator's call, never the swarm's** — so the only cheap moment is before the merge. Better still: have the authoring agent drop the markers in its final commit once they are false. |
| A vacuity guard whose denominator is the population it audits | **it cannot detect that population shrinking.** #457 shipped an AST rule with three guards — a floor of >100 branches found, a >90% resolvability ratio, and a non-empty helper set. Re-introducing the scanner's own historical bug (descending only `ast.stmt`, which skips every `ast.ExceptHandler`) dropped the real tree **148 → 123** and **all three guards stayed green**: 123 clears the floor, and the ratio was 123/**123**, taken over the very population the bug had already truncated | pin the scan against an **independent** count of the same thing — a naive module-wide `ast.walk` — and ship that cross-check, don't run it by hand once. #457's author found their bug exactly that way and then did not ship the check; its gate re-derived 148 with a from-scratch scanner and shipped it. **A guard that measures its subject using its subject is not a guard.** |
| Telling a gate to run `mypy src/` | **it is vacuous in `.ci-venv`.** `pyproject.toml` pins `python_version = "3.11"`, a numpy stub uses a 3.12 `type` statement, and mypy aborts with *"errors prevented further checking"* having checked **nothing** — then an agent reports the typecheck as run. Reproduces on `main`; three separate gate agents hit it on 2026-08-31 | `mypy --python-version 3.12 src/`. CI's `typecheck` job passes, so this is a review-harness defect, not a code one — tracked as [#398](https://github.com/ronsse/trellis-ai/issues/398). A tool that exits having silently checked nothing is the same defect class the gate exists to hunt, so treat a suspiciously fast clean mypy as a red flag. |
| Retargeting a stacked PR with `gh pr edit --base main` and waiting for CI | **it never runs.** GitHub's default `pull_request` activity types are `opened` / `synchronize` / `reopened` — **not `edited`** — so a retargeted PR sits at "no checks reported" forever and reads as *pending* rather than *never triggered*. Observed on #436, where the orchestrator retargeted and then read an empty `statusCheckRollup` as CI still working | push something (even an empty commit) after retargeting, or close/reopen. Distinguish the two states before waiting: an empty rollup means nothing was triggered; a rollup with blank conclusions means jobs are genuinely in flight. |
| Telling an agent to run the suite `-p no:randomly` | **`pytest-randomly` is not installed and is not declared anywhere in this repo**, so the flag is silently accepted and both runs are byte-identical. Verified 2026-08-31 (`find_spec('pytest_randomly')` is `None`; no mention in `pyproject.toml`) | drop it. Test *ordering* dependence is real in this repo — it is what the bare `CliRunner` incident was — but it has to be provoked some other way (`-p no:cacheprovider`, running a single file in isolation, reordering by hand). **This instruction was invented by the orchestrator and propagated to five briefs before anyone checked**, which is the repo's own signature defect — a mechanism that reports success while doing nothing — committed in the act of hunting it. |
| Trusting a green local `pytest` run | **the shared `.venv` is not CI's dependency set.** click 8.3.2 / structlog 25.5.0 locally vs **8.5.0 / 26.1.0** in CI. This produced **109 CI failures across 23 directories** on a PR whose author had a fully green local run of CI's exact command | check against **`/mnt/ssd/trellis-worktrees/.ci-venv`** (fresh 3.12, CI-resolved versions) before opening a PR. It reproduced that failure from a single test file in ~1s. **Do not modify the shared `.venv`** — it is production's editable install. Caveat: `.ci-venv` is authoritative for the click/structlog problem it was built for, **not** for lint or typecheck — see [#378](https://github.com/ronsse/trellis-ai/issues/378) |
| A bare `CliRunner()` in a test — **root cause FIXED `75b892e`, mechanism kept** | `configure_stderr_logging()` baked `sys.stderr` into structlog's **global** factory at call time; `CliRunner.invoke()` pins it to a buffer Click then closes, so **every later log call in the process** died on `I/O operation on closed file` — surfacing at whatever logs next, arbitrarily far from the cause. That is the shape to recognise: a process-global bound eagerly to a stream someone else owns. | [#377](https://github.com/ronsse/trellis-ai/issues/377) merged as `75b892e` — **the stream is now resolved lazily**, so a bare runner no longer poisons the process. The dormant instance this row used to name in `tests/unit/workers/trace_embed/conftest.py` is **gone** (verified 2026-08-31: that fixture takes the root `cli_runner` and says why in its docstring). Still prefer the `cli_runner` fixture in the root `tests/conftest.py`, which exports `IsolatedCliRunner` — it isolates deliberately rather than relying on the lazy resolve |
| Comparing two code paths by `relevance_score` | reports 100% divergence at 12 decimal places | `_apply_recency_decay` reads `datetime.now()`, so a 2-second gap between runs moves every score ~1e-7. **Diff on `item_id` order and excerpt, never on float scores** |
| GitHub Actions outage | runs sit `queued`, then `CANCELLED` | watch `githubstatus.com` components, not `gh pr checks` (which errors); re-trigger cancelled runs with `update-branch` |

## 6. The queue

Dependency-ordered. Items are sized for one subagent and one PR. Full specs in
[`autonomous-backlog.md`](./autonomous-backlog.md).

**Waves 1, 1b and 2 are closed**, along with A3 and C1. What remains, roughly in
dependency order:

> **Two items were removed from this list because they were *answered*, not done, and a
> stale queue is how an agent gets dispatched to build something already rejected.**
>
> - **A3 / #336** — closed by [#380](https://github.com/ronsse/trellis-ai/pull/380). The
>   old entry said "read `pack_attribution_rate` (0.944); the weakness is sample size."
>   **Both halves were wrong.** The gate reads `helpful_item_ids` *only* (0.778 on that
>   denominator), and the weakness was neither sample size nor calibration: `usage_rate` is
>   degenerate, so every threshold in (0, 0.333] flags the same 64 of 79 items.
> - **B2 chunk rollup** — the old entry said "group by `parent_doc_id` at assembly."
>   [#384](https://github.com/ronsse/trellis-ai/pull/384) **measured that and refused it**:
>   the extra servings are top-ranked (cited-helpful at ranks 3, 3, 4, 5, 5), a per-parent
>   cap of K=1 demotes 5 of 5 cited-helpful bodies, chunk overlap is only ~6.7% so they are
>   not duplicate text, and the freed budget would be re-spent anyway (20 of 37 packs hit
>   `max_items` with 436 candidates unserved). **Do not re-propose it without new evidence.**
>   The live remainder is #385, the documents *list view*, which is a display defect.

1. **[#360](https://github.com/ronsse/trellis-ai/issues/360) — govern the document and
   vector planes. ← the top unblocked feature item.** Panel-decided (unanimous, option B)
   and recorded as ledger **T-3**; implementation not started. #357's worker-local handler
   is the natural seed. ~~Gains most of its point *after* C1, since stage 2 is a no-op until
   a gate is wired.~~ **That caveat is satisfied:** C1 merged `1e6c66e`, so Stage 2 now runs
   on every surface and a governed document/vector write would actually be policy-checked.
   Nothing else in this queue blocks it.
2. **C2 → C3 → C4** — [#256](https://github.com/ronsse/trellis-ai/issues/256) Bolt plugin
   extraction (halves #194's enforcement surface, so it precedes it),
   [#194](https://github.com/ronsse/trellis-ai/issues/194) classification enforcement,
   [#264](https://github.com/ronsse/trellis-ai/issues/264) judged-memory logging (measured
   as 2 of 5 stages missing emitters).
3. **D1–D4** query-history curation, then **E1** ([#306](https://github.com/ronsse/trellis-ai/issues/306)) / **E2**.

**There is deliberately no in-flight table here.** One existed for a day and was **4/4
wrong** within 24 hours — every lane it listed as blocked had merged, and one row still
instructed an agent not to merge a PR that was already on `main`. An agent reading a stale
in-flight row does the *opposite* of the right thing, which is strictly worse than reading
nothing.

**Ask `gh pr list --state open` and `git log origin/main` instead.** Those cannot go stale.
This section carries only what a merge leaves behind that *is* durable — the ledger entry,
the filed follow-up, the §5 trap. That is the general rule this document now follows:
**mechanism does not rot; status does.** Write down why something is true, and link to the
live source for whether it currently holds.

The four lanes dispatched 2026-08-30/31 are all merged; their durable residue is ledger
**T-4** and **T-5**, issues [#438](https://github.com/ronsse/trellis-ai/issues/438) /
[#439](https://github.com/ronsse/trellis-ai/issues/439) /
[#440](https://github.com/ronsse/trellis-ai/issues/440), and the §5 trap rows.

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

**A corollary learned the hard way (#336):** *a rule of the form `rate < threshold` is
not a measurement until someone has measured the base rate the rate is drawn from.* The
noise-demotion gate demoted below `helpful/appearances < 0.3` against a distribution whose
mean was **0.103** — a threshold 3x its own base rate, so demotion was the default outcome
rather than an inference, and 81% of scored items were flagged. It read as unfalsifiability
for weeks. Apply this to any future "served often, rarely used" heuristic.

**And a second (#374, #373):** when a metric reads zero, **enumerate every path that could
produce the zero before naming one.** "0 advisories served" drew three successive wrong
single-cause explanations. Genuinely independent *and* sufficient: **two** — the writer/reader
path split (#373), and the flat pack path never rendering advisories at all. Item attribution
is **not** one — `AdvisoryGenerator` reads `injected_item_ids` from the *pack* side and never
touches `helpful_item_ids`, and it was never starved (51 rows, ~2/night since 2026-08-08).
Nor is degeneracy (#383): a degenerate advisory still renders, since every live confidence
clears `_ADVISORY_MIN_CONFIDENCE`. Counting a quality defect as an availability one is the
same error one level down.

**The panel never caught any of these. Measurement did, every time.** Before implementing
against a number, verify the number can move — query it, and check it can return more than
one answer. Before trusting a test, confirm it runs. Treat "it reports success" as the
weakest possible evidence.
