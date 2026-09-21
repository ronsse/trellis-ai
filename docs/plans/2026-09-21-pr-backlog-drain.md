# Draining the 44-PR backlog

**Measured 2026-09-21**, against `origin/main` at `ba18fb3` — the first green `live-infra`
in 13 days, unblocked when [#555](https://github.com/ronsse/trellis-ai/pull/555) merged the
ArcadeDB `AliasClaim` race fix.

Every number here is a timestamped measurement, not an estimate. Re-derive before acting:
the queue moves daily.

## 1. The population

| Bucket | Count | Disposition |
|---|---:|---|
| Stale-red — alias-race family only | 33 | Clear on rebase. No work. |
| Genuinely broken | 2 | #541, #542 — regenerate, don't repair |
| CI-dark (the stack) | 4 | #559, #561, #563, #588 |
| Fully green | 5 | #571, #580, #596, #602, #603 |
| **Total open** | **44** | |

### The stale-red 33 are stale, not broken — verified at full population

Not sampled. All 33 failing jobs were read. **32 fail exactly one test**,
`test_bind_alias_if_absent_is_atomic_for_concurrent_contenders`. **#591 fails two** — that
one plus `test_bind_alias_serializes_concurrent_stale_owner_replacement`, the other half of
the same race (the `UnknownError` channel, which #555's
`TestArcadeDBWidensOverItsGenericErrorChannel` also covers).

Both pass on `main` at `ba18fb3` (857 passed, 1 skipped, 0 failed), so all 33 clear on
rebase. Only **#571** touches any file #555 changed, and #571 is the known duplicate.

> **This paragraph exists because a six-PR sample got it wrong.** The sample said "all fail
> the identical single test". The population says 32 do and one fails two. The conclusion
> survived; the claim did not. This repo's recurring defect is a measurement wired to a
> constant or a roster that rots — a sample generalized to a population is the same shape.

## 2. The `CLAUDE.md` contention is order-independent

**Ten** PRs touch `CLAUDE.md`, not eight. Eight edit the same contended block (#552,
#564, #572, #578, #583, #589, #595, #601) and all 28 pairs conflict. Two more — #575 and
#581 — touch the file incidentally, at unrelated lines. The eight-PR figure counted the
*contention*; the ten-PR figure is what a file-level `gh pr view --json files` returns,
and the gap is why a "which PRs touch CLAUDE.md" query and a "which PRs conflict" query
disagree. Both are right about different questions.

Measured, and it refutes the obvious plan:

- Each of the eight merges into `main` **clean today** — zero `CLAUDE.md` conflicts.
- Merge **any one** of them first, and **all seven** remaining conflict. Tested with #583,
  #552, #601 and #589 as first-mover: 7 of 7 every time.

So there is no ordering that avoids this, and **merging #583 first does not collapse the
others into no-ops** — a plausible guess that measurement refutes.

> **"Each conflict is a single hunk. Budget seven trivial resolutions" was wrong**, and
> the drain refuted it. Four of the conflicts that actually materialised were genuine
> semantic collisions in *code*, not one-hunk prose edits: #587 (12 blocks), #573 (4),
> #598 (2), #590 (2). In every one of the four, neither `--ours` nor `--theirs` was
> correct — the answer was a **union**, because two PRs had independently added a
> different thing to the same signature. A hunk count measures textual overlap; it does
> not predict whether a resolution is mechanical. Do not budget from it.

#583 still belongs early, on unrelated grounds: it replaces the hand-maintained "what CI
actually covers" roster with a rule derived from the workflow files, so the block stops
rotting. That is a reason about durability, not about merge mechanics.

## 3. The stack: use merge commits, not squash

The stack is strictly linear and each child's branch contains its parent's tip:

```
#558 (base: main) -> #559 -> #561 -> #563 -> #588
```

#558 is **dual-member**: it is based on `main`, it is inside the `src/trellis/mcp/server.py`
contention cluster of seven, *and* it is the stack's bottom. Any `update-branch` on #558
moves the base under all four children.

Simulated both merge methods end to end:

| Method | #558 | #559 | #561 | #563 | #588 |
|---|---|---|---|---|---|
| **Merge commit** | clean | clean | clean | clean | clean |
| **Squash** (repo default) | clean | clean | **2 files / 8 hunks** | **1 file / 3 hunks** | **1 file / 1 hunk** |

Squash breaks ancestry, so from step three each child re-proposes its parents' content
against a base that already has it. At #561 every hunk has an empty `HEAD` side — pure
mechanical noise. At #563 and #588 both sides carry content and a hand-resolver could
plausibly merge them wrongly.

**Resolving every conflict with "take theirs" reproduces the stack tip's
`src/trellis/feedback` and `src/trellis/learning` content byte-exactly**, because each child
branch already holds the cumulative state. That is safe *here* and the reason is checkable:
the three files that conflict are **disjoint** from the three files `main` has changed since
the fork (`stores/arcadedb/graph.py`, `stores/bolt_opencypher/graph.py`,
`tests/unit/stores/test_bolt_opencypher_alias_claim_retry.py`). Verified: #555's work
survives the chain.

**That disjointness is a precondition, not a property.** The moment `main` gains a commit
touching `feedback/` or `learning/tuners/`, take-theirs stops being safe. Re-check the two
file sets before relying on it.

The repo's default merge method is SQUASH — the arm that conflicts. Use merge commits for
this stack, or squash and resolve take-theirs after re-checking disjointness.

## 4. Recommended order

1. **#565** — update-branch it first (it carries the inherited red like everything else),
   then merge. It conflicts with nothing and is the only PR that gives the four CI-dark
   PRs any checks at all. Every later step benefits.
2. **#583** — retires the rotting CI roster for a derived rule. Expect the other seven
   `CLAUDE.md` PRs to need a one-hunk resolve afterwards; that is unavoidable, not a
   consequence of this choice.
3. **The remaining stale-red.** Update-branch, confirm green, merge. Only contended files
   force serialization; the rest can be updated in parallel batches.
4. **The serial chains.** `src/trellis/mutate/executor.py` is a genuine three-way chain
   (#587, #598, #600 — all three pairs conflict). `src/trellis/mcp/server.py` has seven
   members but only one conflicting pair, so it is not the bottleneck it looks like.
   Ordering largest-first does **not** reduce total rebases in a cluster — every merge
   forces every other member to update regardless of order. It only spares the largest.
5. **#558 early within the `server.py` cluster**, since it blocks the stack. Then
   #559 -> #561 -> #563 -> #588 **with merge commits**.

### Drop rather than fix

- **#541 / #542** — both touch only `pyproject.toml`, so they conflict with each other, and
  their premise is 14 days stale. Regenerate.
- **#571** — duplicate of the merged #555. Before closing, salvage four behaviours #555
  never tests: transport-failure passthrough, the one-transaction cost of the happy path,
  the structural rule that both alias writers route through the runner, and observable
  exhaustion.

## 5. Sustainability

`live-infra` runs ~7 minutes. A strict serial update -> green -> merge across ~40 PRs is
multiple days of supervised babysitting, and `main` has **no required status checks** — so
the failure mode is not a broken build, it is the operator quietly dropping the
update-then-merge discipline partway through and the invariant going unenforced.

Two structural mitigations, neither taken yet: required status checks on `main`, or a merge
queue that rebases and retests immediately before merge. Without one of them, "update the
branch immediately before merging" does not protect against a second merge landing between
the update and the merge.

## 6. What is not verified

- Steps 4 and 5 of the squash chain were resolved take-theirs by this analysis, not by a
  human reading the hunks. #563 and #588 have no empty-`HEAD` marker, so they warrant a
  read before being resolved that way.
- Whether the seven `CLAUDE.md` PRs' content is still *correct* after #583 rewrites the
  block — only that they conflict. Several may be arguing about a roster that no longer
  exists.
- `delete_branch_on_merge` is `false`, so nothing auto-retargets a stacked PR when its
  parent merges. Each child must be retargeted by hand.

## 7. What the drain found — recorded 2026-09-21, mid-execution

Nineteen PRs merged under one discipline: update-branch, wait for 10/10 SUCCESS with
`MERGEABLE/CLEAN`, squash. No PR merged red. Four findings the plan did not predict.

### 7.1 The drain invalidated one of its own members (#575)

#575 (`fix(retrieve): derived rows carry their source clock, with a roster`) went from
inherited-red to **genuinely** red, and the cause was the drain. Its 761-line roster
enumerates every document write seam — `MIN_WRITE_SEAMS = 27`, with per-disposition
floors. On the post-drain tree the scan finds **13**, `primary_write` falls to **0**
against a floor of 5, and 14 roster keys match no site.

Nothing is broken. Main consolidated every direct `document_store.put` into the single
`put_document` seam in `core/document_write.py` (#553 / #569 / #590 — three PRs merged in
this drain). The 14 "missing" sites all now route through it, and `put_document` threads
`preserve_updated_at`, which is the clock-preservation mechanism #575 exists to guarantee.
The invariant survived; the seam moved.

**This is not a roster refresh and must not be done as one.** The roster's dispositions
(`primary_write`, `in_place_reput`, `derived_propagates`, …) describe what the *caller*
intends, and after consolidation that intent sits upstream of the seam the scan finds — so
the scan itself has to change (follow `put_document(` call sites rather than `.put(`), and
every one of the 14 keys has to be re-classified against the new seam. The file's own
doctrine forbids the shortcut: a rotted roster "gets 'repaired' by renumbering — which is
indistinguishable from re-classifying." Rewriting 14 keys from `doc_store.put` to
`put_document` *is* re-classifying. **Held for the author, with the measurement above.**

### 7.2 Two PRs were complementary halves of one fix (#568)

#569 (merged) fixed the `put_document` half of #568 and left a comment saying the classify
half was "a separate defect, deliberately not fixed here". #590 *is* that half. Merging
either alone leaves #568 half-fixed, and nothing in either PR's description says so — the
link existed only in a code comment on one side. The comment was deleted in the
resolution, because the merge falsifies its first clause; this is the recorded
"a docstring narrating a defect is past tense" shape, caught at merge time.

### 7.3 `mergeStateStatus` cannot distinguish pending from failing

`UNSTABLE` means "mergeable, checks not all SUCCESS" — and a *pending* check produces it
just as a failing one does. A rollup filter that keys on `.conclusion` alone reads every
in-progress check as a failure: in one sweep it reported 5 failing checks on a PR that had
zero. Key on `.status` (`QUEUED` / `IN_PROGRESS` / `COMPLETED`) and treat only
`FAILURE` / `TIMED_OUT` / `CANCELLED` as red. `MERGEABLE`/`UNKNOWN` is a *third* state —
GitHub recomputing after a base change — and is never safe to merge on.

### 7.4 The real constraint is runner concurrency, not conflicts

Every merge to `main` fires six workflow runs, and every `update-branch` fires ten checks.
Nineteen merges plus fifteen in-flight PRs put ~130 jobs in one queue behind a ~7-minute
`live-infra`. §5's worry was the operator dropping the discipline; the actual pressure to
drop it comes from the queue, because the update → green → merge cycle stops being minutes
and starts being hours. **Batch update-branch is the thing to avoid** — updating all eight
remaining `CLAUDE.md` PRs at once would queue eighty checks to merge one.

### 7.5 The checkpoint that replaced re-updating everything

Re-updating every open branch after each merge is O(n²) and never converges. All six
workflows also run on **push to `main`**, so each merge already retests the merged result.
That main-push run is the semantic-conflict checkpoint, and the stop condition is: **if
`main` goes red, halt the drain**. Across all nineteen merges it never did — zero failures
in 60 runs. The gap this leaves is honest and unchanged: a PR green on an older base can
still merge, and only the checkpoint catches it, *after* the fact.
