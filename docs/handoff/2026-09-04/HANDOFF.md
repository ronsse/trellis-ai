# Handoff — trellis-ai issue sweep, 2026-09-04 (written at rate-limit; resets 19:00 UTC)

## State of the world
- `origin/main` = f9ff32c. No open PRs. No branches or worktrees were created by the coders (both died on the 429 before their first git command) — nothing to clean up.
- This package is committed on branch `handoff/issue-sweep-2026-09-04` under docs/handoff/2026-09-04/ so it is visible from any machine. It is NOT meant to be merged to main — delete the branch when the work is absorbed.
- Briefs in this dir: SHARED-BRIEF.md (constraints), PLANNER-BRIEF.md (Fable review+simplify+plan), CODER-BRIEF.md (Opus implement+PR; orchestrator merges after an independent adversarial gate). Issue text: `gh issue view <n> --comments`.
- Skynet-only notes (ignore elsewhere): the production tree there is an editable install on `ui/sidebar-and-table-interaction`; a read-only `main-ref` worktree and a `venv-src` worktree exist under /mnt/ssd/trellis-worktrees and must not be touched.

## The user's instruction
"perform a review and simplify on all of the remaining issues, and have fable plan the remainder of the issues to have coding performed by opus." Discipline from earlier: plan review before dispatch; adversarial gate (MERGE / MERGE WITH FIXES / DO NOT MERGE) on every PR before merge; gates have returned zero clean MERGEs in ~25 PRs — the defect is always inside the PR's own load-bearing claim.

## Where each of the 37 issues stands
CODE — plan ready in plans/:
  #360 PR1 (S/M, no deps)      governed-write AST rule + CLAUDE.md rule narrowed          → then #360 PR2 (M)
  #256 PR1 (M, no deps)        registry prepare_registry_params hook + no-bolt-import rule (owner Q on separate dist stays open)
  #439 (M, no deps)            withholding on REST/SDK DTOs, shared renderer in trellis_wire
  #369 (M, no deps)            name-alias bind at write + backfill CLI                       → unblocks #375
  #342 (S, no deps, live-infra) promote-loop test asserts "served"
  #514 (M, lands before #264)  shared JSON-parse seam + provider capability flag
  #264 PR-A then PR-B          MEMORY_OP_JUDGED emitters + derived rule (reuses #514's generate_call_sites)
  Suggested parallel wave 1: 360-PR1, 256-PR1, 439, 369, 342, 514 (disjoint files). Wave 2: 360-PR2, 264-A, 264-B.
UNPLANNED (planner died) — re-dispatch Fable planners:
  Cluster A: #526 #525 #523 #356 #351 #350 #405   (note 525/526 likely duplicates; check `.[all]` extra exists; verify publish.yml `.[dev,vectors]` claim)
  Cluster B: #522 #494
  From cluster E: #515 (measurement-as-deliverable or DEFER on spend) #306 (review only)
CLOSE (draft comments in reports/): #371 (all shipped; #375 survives), #364 (PR #389), #208 (not a trellis-ai item)
OWNER (one question each, in reports/): #502, #365, #250, #257, #194, #474, #475, #476, #477, #200/#202/#203 (recommend park)
DEFER: #503 (on #502), #261, #375 (on #369), #463, #201, #478 (umbrella)
KEEP OPEN as board: #275

## Cross-plan facts a coder must not rediscover
- #503/#502: `injected_advisory_ids` has no reader in src/; attribution.py:100-113 docstring is false; ANTI_PATTERN rows carry entity_id.
- #360: no governed document DELETE exists at all; retention handlers DO write doc metadata (issue says none writes — wrong: none *creates*).
- #256: entry-point plugin resolution already shipped (plugins/loader.py) — the coupling is registry._instantiate only.
- #369: past the scan cap, a miss mints a twin ULID with no name dedupe (worse than the issue states).
- #439: issue names routes/packs.py and trellis/hooks.py — neither exists; real files trellis_wire/dtos.py, trellis_api/routes/retrieve.py, trellis_sdk/hooks.py.
- CLAUDE.md "live-infra runs on push to main" is stale (on: includes pull_request) — fix in whichever PR touches CLAUDE.md first (#360 PR1).
