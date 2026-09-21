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

Eight PRs edit the same block (#552, #564, #572, #578, #583, #589, #595, #601). All 28
pairs conflict.

Measured, and it refutes the obvious plan:

- Each of the eight merges into `main` **clean today** — zero `CLAUDE.md` conflicts.
- Merge **any one** of them first, and **all seven** remaining conflict. Tested with #583,
  #552, #601 and #589 as first-mover: 7 of 7 every time.

So there is no ordering that avoids this, and **merging #583 first does not collapse the
others into no-ops** — a plausible guess that measurement refutes. Each conflict is a
single hunk. Budget seven trivial resolutions, in whatever order is convenient, and do not
spend effort optimizing the sequence.

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
