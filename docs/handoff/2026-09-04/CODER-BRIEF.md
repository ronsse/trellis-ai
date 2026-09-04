# Coder brief (Opus) — implement one plan as one PR

Read SHARED-BRIEF.md first. Then read your plan file (plans/<n>.md, same directory as this brief) and the issue (`gh issue view <n> --comments`). The plan was produced by a planner who verified the premise on origin/main — trust its file:line reading but RE-VERIFY anything you build on, because main may have moved; `git fetch` and base on `origin/main`.

Steps:
1. Branch: `git fetch origin && git worktree add -b <branch> ../<branch> origin/main` (or a fresh clone). Work ONLY there.
2. Baseline: run the full suite on the untouched branch base first and record the exact `passed/skipped/deselected` line. Use `--basetemp` under your own scratch dir if another pytest may run concurrently.
3. Implement the plan. If you find the plan wrong on a point, deviate and SAY SO in the PR body with the evidence — do not silently do something else. Do not widen scope beyond the plan.
4. Tests: write the failing-before test first and show it fails on base (run it against base by `git stash` or a second worktree — report the failure line). Then mutation-test the load-bearing claims the plan names: apply each mutant, run the relevant tests, record kill/survive. Report survivors honestly; do not delete a mutant because it survived.
5. `make lint`, `make typecheck`, full suite, all green. Report the final passed count against baseline.
6. Commit(s) with `git -c user.email=5297358+ronsse@users.noreply.github.com -c user.name=ronsse commit`; message style `fix|feat|test|docs(scope): one line (#<n>)`. Push. `gh pr create --repo ronsse/trellis-ai --base main` with a body that has: the defect in one paragraph; what changed and why that shape; the measurement (before/after numbers, mutant table); deviations from the plan; what is deliberately NOT done; `Closes #<n>` only if the issue is fully resolved (else `Refs #<n>`). Body ends with the two trailer lines from your Bash tool instructions.
7. Wait for CI: `gh pr checks <num> --watch`. Fix failures. Do NOT merge; the orchestrator merges after an independent gate review.
8. Do not remove your worktree; the orchestrator will.

Final report to the orchestrator (all it sees): PR number + URL, branch, baseline vs final test counts, mutant table (killed/survived with reason), deviations from plan, open risks you would probe if you were the reviewer. Under 60 lines.
