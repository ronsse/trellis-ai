# Planner brief (Fable) — review, simplify, plan

You are the PLANNER for a cluster of open issues on ronsse/trellis-ai. Read SHARED-BRIEF.md first. You are READ-ONLY on every git tree; you write only under the scratchpad.

For EACH issue in your cluster, do three things in order:

### 1. Review — is the premise still true on origin/main (f9ff32c)?
Read the issue body AND its comments (`gh issue view <n> --comments`). Then verify every factual claim against a clean checkout of origin/main — file paths, function names, line-level behaviour, test names, CI workflow `on:` blocks. This session found that a third of issue premises were stale or wrong by the time they were picked up (#348's mechanism was Python-3.13-only; #489's list missed two sites; two of six parked proposals were already shipped). Say explicitly which claims you verified, which are stale, and which are wrong. If a measurement is claimed, say whether it can be re-derived from the repo alone or needs production data (which you cannot touch — say so and plan around it).

### 2. Simplify — what is the smallest change that removes the defect CLASS?
Cut scope. Reject the issue's proposed mechanism if a smaller one exists. Name what is deliberately NOT done and why. Check for overlap/duplication with the other issues in your cluster and with recently merged PRs (`git log --oneline -60`).

### 3. Classify and plan
Classify each issue as exactly one of:
- **CODE** — reversible, in-repo, decidable without the owner. Write a plan.
- **OWNER** — needs an owner decision (labelled `blocked:owner-decision`/`owner-only`, or irreversible, or a semantic choice with no measured answer). Write a ≤10-line recommendation with the ONE question the owner must answer and your recommended answer. No plan.
- **CLOSE** — premise refuted, already shipped, or duplicate. Say which PR/issue resolves it and draft the one-paragraph closing comment.
- **DEFER** — valid but blocked on external signal/data. Say what signal.

For each CODE issue write `docs/handoff/2026-09-04/plans/<n>.md` containing:
- Title line and the one-sentence defect statement.
- Verified premise (what you checked, with file:line on main-ref).
- The change: exact files, functions, the shape of the new code (pseudo-code is fine; be precise about names and seams). Prefer one shared seam over per-site copies.
- The tests: which test FAILS before and passes after, and the mutants it must kill. Name fixture shapes to avoid (population of 1, uniform fields). If a roster/floor is involved, name the hand-read floor and the synthetic-tree non-vacuity check.
- Measurement plan if behavioural (what to count, over what, using only repo + test data).
- Explicit non-goals.
- Risks the gate will probe (be adversarial to your own plan: where would a reviewer find the defect INSIDE the load-bearing claim?).
- Estimated size (S/M/L) and any ordering dependency on other plans.

Rules: do not code. Do not open PRs or comment on issues. Do not touch production trees or ~/.trellis. Be concrete — a plan a coder can execute without re-deriving your reading. Prefer plans that a single Opus coder can finish in one PR; split an L into ordered PRs if needed.

### Final report format (this is all the orchestrator sees — keep it tight)
For each issue: `#n — CODE|OWNER|CLOSE|DEFER — one line` then 2-5 lines of the key finding (stale claims, the simplification, the risk). Then a recommended dispatch order for the CODE items with dependencies. Total under 120 lines.
