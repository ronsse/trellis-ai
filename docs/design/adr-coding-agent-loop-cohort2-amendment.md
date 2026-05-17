# ADR amendment: Coding-agent loop — Cohort 2 (autonomous spawn)

**Status:** Proposed 2026-05-16
**Amends:** [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) (Proposed 2026-05-11)
**Deciders:** Trellis core
**Related:**
- [`./plan-coding-agent-loop.md`](./plan-coding-agent-loop.md) — original implementation plan (Cohort 1 shipped via PRs #134, #135)
- [`./plan-coding-agent-loop-cohort2.md`](./plan-coding-agent-loop-cohort2.md) — Cohort 2 phase breakdown (sibling artifact, this swarm unit)
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 — program-level security model
- `TODO.md` — "Item 7 Cohort 2 — Sandboxed Claude Code spawn" deferred entry

---

## 0. What this amendment does and does not do

This amendment authorizes Cohort 2 (Phases 2 + 3 of the original plan) **on the security model spelled out below**. It does not:

- Loosen Cohort 1's proposals-only default. Cohort 2 ships behind a kill switch (`TRELLIS_AUTONOMOUS_SPAWN_ENABLED=false`) that defaults to off in every deployment shape — POC, self-hosted, and SaaS.
- Re-open the "auto-merge" question. The original ADR §2.5 stands unchanged: draft PR is the final artifact. The system never merges, never force-pushes, never modifies `main`.
- Replace the operator-review gate. TODO.md's gating criterion (a) — operator review of N real Cohort 1 proposals first — is preserved verbatim and restated in §2.0 below. This amendment is (b); both are required to ship Cohort 2.

What this amendment **does** do:

- Replaces the original ADR §2.4's "operator runs `trellis admin author-proposal`" flow with a scheduled, harness-driven `spawn-coder` flow gated by per-cycle budget ledgers.
- Adds explicit failure-mode contracts (timeout, budget exhaustion, empty diff) that the original ADR left implicit.
- Specifies the secret-scrubbing regex set + the file-allowlist glob enforcement at the diff level (not just the spawn-time env).
- Introduces a single, on/off config flag — `TRELLIS_AUTONOMOUS_SPAWN_ENABLED` — that gates the entire feature without per-call overrides.

## 1. Context

Cohort 1 (proposals-only, operator-driven authoring) shipped in PRs #134 / #135. The proposal generator emits `PROPOSAL_DRAFTED` / `PROPOSAL_UPDATED` events for `EXTRACTION_FAILED` clusters and `WELL_KNOWN_CANDIDATE` events. The CLI surface (`trellis admin generate-proposals` / `list-proposals` / `show-proposal`) is live. Operators can read proposals and act on them manually — the loop's signal side is closed.

What Cohort 1 deliberately did not ship:

- Sandboxed worktree creation.
- The `gh` CLI wrapper.
- Any budget ledger.
- Any code path that calls Claude Code SDK.

The original ADR §2.4 sketched the security model for operator-invoked authoring. It assumed the harness creates a worktree only when an operator types `trellis admin author-proposal <id>`. **This amendment scopes the autonomous variant** — a scheduled cycle (cron, systemd timer, scheduled-tasks MCP, or operator-fired one-shot) that picks an open proposal, spawns Claude Code against it, opens a draft PR, and stops. No human invokes a per-proposal command.

The shift from operator-invoked to scheduled-spawn is what TODO.md calls "autonomous spawn." The blast radius of getting this wrong is substantially higher than getting the operator-invoked path wrong — an unattended cycle that loops on a degenerate proposal would file dozens of low-quality PRs against itself before anyone noticed. This amendment specifies the controls that make the unattended path safe.

## 2. Decision

Cohort 2 is authorized to land **conditional on both:**

- **(a) Operator review of N real Cohort 1 proposals first.** Per TODO.md: target N = 5–10, generated against the dogfooded EventLog over an operational cycle. This is the existing gate; it is not relaxed by this amendment. The operator is the human who signs off that the proposals Cohort 1 produced are useful, accurate, and not garbage. Without (a), Cohort 2 does not unblock regardless of (b).
- **(b) This ADR amendment authorizing autonomous spawn.** The artifact you are reading. By accepting this amendment the operator authorizes the harness controls below; absent acceptance, Cohort 2 stays gated even if (a) is met.

### 2.0 Preserving (a)

The operator-review gate (a) is preserved by:

1. The CLI `trellis admin spawn-coder` exits 1 with the message `cohort-2 gate (a) not yet recorded` when invoked before an operator records review acceptance.
2. Review acceptance is recorded via `trellis admin spawn-coder --record-review-pass <N>` where `N` is the count of Cohort 1 proposals the operator has reviewed (must be ≥ 5). This writes a `COHORT2_REVIEW_RECORDED` event to the operational EventLog with `{reviewed_count, operator_id, accepted_at}` and a free-form `notes` field.
3. The harness reads the latest `COHORT2_REVIEW_RECORDED` event at startup; if absent or `reviewed_count < 5`, `spawn-coder` refuses. The check is a one-row EventLog read, not a separate config knob — gate (a) lives where the rest of the operational signal lives.

This is the only path to clear (a). The kill switch in §2.1 below is downstream — if (a) clears but the operator wants the feature off anyway, the kill switch overrides.

### 2.1 The kill switch

A single config flag gates the entire feature:

```
TRELLIS_AUTONOMOUS_SPAWN_ENABLED=false  # default
```

Set to `true` to opt in. When `false`:

- `spawn-coder` exits 1 immediately with `autonomous spawn disabled; set TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true to opt in`.
- The harness never invokes `git worktree add`, never spawns Claude Code, never calls `gh`.
- All scheduled-task callers see the same exit-1 behavior.

The flag is checked at the **first** line of `spawn-coder`'s `main()`, before any other I/O. There is no per-invocation override. The intent is that an operator who wants the feature off can `unset TRELLIS_AUTONOMOUS_SPAWN_ENABLED` in their environment and be confident no scheduled task can re-enable it without a deploy-shaped change.

Setting `TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true` is not sufficient on its own — gate (a) above and the budget controls in §2.4 also gate the actual spawn.

### 2.2 Sandboxed worktree creation per proposal

When `spawn-coder` decides to author a specific proposal:

1. The harness selects an open proposal (status = `DRAFTED`, no `PROPOSAL_AUTHORSHIP_REQUESTED` event in the last 30 days) via `ProposalSelector.pick()`. Tie-breaking rule: highest `source_event_count`, then oldest `created_at`. Deterministic.
2. `git fetch origin` is run; the working ref is `origin/main` at the fetched HEAD. The harness records this SHA on the `PROPOSAL_AUTHORSHIP_REQUESTED` event so the resulting PR is reproducible.
3. `git worktree add agent-proposals/<proposal_id>/worktree -b agent-proposals/<proposal_id> origin/main` creates an isolated worktree at the recorded SHA. The branch name is namespaced under `agent-proposals/` — never `main`, never a dev branch, never an existing branch.
4. The worktree directory is **per-proposal**. Two concurrent `spawn-coder` invocations on different proposals do not share state. Two concurrent invocations on the same proposal are rejected by an `idempotency.lock` file in `agent-proposals/<proposal_id>/`.
5. After Claude Code exits (regardless of outcome), the worktree is **preserved**. Cleanup is a separate CLI command (`trellis admin spawn-coder --cleanup <id>`), never automatic. This preserves forensic data for failed runs.

The worktree directory itself is in `.gitignore` (Cohort 1 already added `agent-proposals/`).

### 2.3 GitHub PR proposer via `gh` CLI

After a successful spawn + verification, the harness opens a draft PR:

```
gh pr create \
    --draft \
    --base main \
    --head agent-proposals/<proposal_id> \
    --title "[auto] <one-line summary>" \
    --body @agent-proposals/<proposal_id>/proposal.md
```

Constraints:

- `--draft` is **mandatory**. The harness rejects any code path that omits it.
- `--base main` is the only allowed base. PRs against feature branches, release branches, or any non-`main` target are rejected at the harness layer.
- `--title` is prefixed with `[auto]` so reviewers can distinguish system-authored PRs at a glance. The unprefixed title comes from the proposal markdown's H1.
- `--body` is the verbatim proposal markdown plus an appended `## Autonomous-spawn provenance` section recording `worktree SHA`, `Claude Code SDK version`, `budget consumed` (tokens + cents), and the `spawn-coder` run ID.
- The `gh` invocation runs with `GH_TOKEN` from the operator's environment. **The harness never writes `GH_TOKEN` to disk and never includes it in any event payload.**

If `gh pr create` exits non-zero, the harness records a `PROPOSAL_AUTHORSHIP_FAILED` event with `{proposal_id, reason: "gh_pr_create_failed", stderr_excerpt}` and exits. The worktree is preserved for the operator to inspect and recover. **No retry loop.**

### 2.4 Per-cycle budget ledger

Two caps, both enforced per 7-day rolling window:

- **LOC cap:** default 1000 LOC across all merged-or-open `agent-proposals/*` branches in the trailing 7 days. Lines counted as `git diff --stat origin/main..agent-proposals/<id> | tail -1` (insertions + deletions, the standard git accounting).
- **Token cap:** default 1,000,000 tokens (input + output, summed across all Claude Code invocations) in the trailing 7 days.

Both are tracked in a `_BudgetLedger` class that persists to the operational EventLog as `BUDGET_CONSUMED` events. The ledger is **operational-plane** by definition — it is data about Trellis's own resource consumption, not user content. The event schema (see [`plan-coding-agent-loop-cohort2.md`](./plan-coding-agent-loop-cohort2.md) for the full JSON shape):

```
EventType.BUDGET_CONSUMED
payload = {
    "run_id": "<ulid>",
    "proposal_id": "<sha256 hex>",
    "loc_delta": <int>,             # this run's contribution
    "tokens_input": <int>,          # Claude Code's reported input tokens
    "tokens_output": <int>,         # Claude Code's reported output tokens
    "cents_estimated": <int>,       # tokens_output × output_rate + tokens_input × input_rate
    "window_start": "<iso8601>",    # 7d-trailing start, recorded for audit
    "window_end": "<iso8601>",      # ULID timestamp; same as recorded_at
}
```

At spawn time, `_BudgetLedger.check(loc_cap, token_cap)` reads `BUDGET_CONSUMED` events in the trailing 7 days, sums `loc_delta` and `tokens_input + tokens_output`, and:

- If either sum is **at or above** the cap, `spawn-coder` exits 1 with `budget exhausted (loc=X/Y, tokens=A/B); next eligible YYYY-MM-DDTHH:MMZ` and emits a `BUDGET_EXHAUSTED` event. **No silent skip.** No partial-budget spawn.
- If both sums are below the caps, the spawn proceeds. After the spawn completes (success or failure), a `BUDGET_CONSUMED` event is recorded with this run's `loc_delta` and token counts. A failed run with zero LOC still emits the event with `loc_delta=0` and the consumed tokens — failures cost tokens too.

Cap values are env-configurable:

```
TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK=1000
TRELLIS_AUTONOMOUS_SPAWN_TOKEN_CAP_WEEK=1000000
```

Both default to the conservative values above. The original ADR's `TRELLIS_LLM_BUDGET_CENTS_WEEK` envelope is preserved as an additional **upper bound** — if it's set and the cents-estimated sum exceeds it, the spawn is rejected at the cents level regardless of LOC/token state.

### 2.5 File-allowlist enforcement on the diff (not just the spawn-time env)

The original ADR §2.4 specified file-allowlist enforcement via spawn-time environment scoping — Claude Code is told which files it may write. This amendment adds a **second** enforcement layer at the diff level, after Claude Code exits:

1. Each proposal's frontmatter declares `files_allowed: list[glob]`. Globs are evaluated against the worktree's `git diff --name-only origin/main..HEAD` output.
2. **Deny by default.** A file matches the allowlist if and only if at least one glob in `files_allowed` matches its path. Globs that resolve to paths outside the worktree (via `..`, symlinks, or absolute paths) are rejected at parse time — a malformed allowlist fails the spawn at the harness layer, before Claude Code is invoked.
3. After Claude Code exits, the harness runs `verify_diff_allowlist(worktree, proposal.files_allowed)`:
   - Compute `git diff --name-only origin/main..HEAD` in the worktree.
   - For each path in the diff, check it against the glob set.
   - If any path is **not** allowlisted, the harness raises `AllowlistViolation(path, glob_set)` and the run fails. The worktree is preserved so the operator can inspect what Claude Code tried to write.

Hard exclusions that override `files_allowed` (any glob in `files_allowed` that matches one of these is rejected at parse time):

- `src/trellis_api/auth.py` (and the whole `src/trellis_api/auth/` subtree).
- `src/trellis/mutate/policies/**`.
- `src/trellis/mutate/executor.py` (the MutationExecutor itself).
- `src/trellis/stores/registry.py` (StoreRegistry).
- Anything matching `*_security_*.py` or `*_secret_*.py`.
- `.github/workflows/**`, `.github/actions/**` (CI configuration).
- Any file named `secrets.*`, `credentials.*`, `.env*`.

These match the original ADR §4.1's exclusions plus the additions this amendment makes explicit. Updating this exclusion list is a separate ADR amendment — the harness reads it from a frozen constant, not from config.

### 2.6 Secret scrubbing on diffs

Before opening a PR, the harness scans the diff text for secret-shaped tokens. The scan operates on the raw `git diff origin/main..HEAD` output. If any pattern matches, the harness:

- Records `SECRET_SCRUB_TRIGGERED` event with `{proposal_id, match_count, pattern_names, file_paths}`.
- Aborts the PR creation.
- Preserves the worktree.

Pattern set (frozen constant; updates are a separate ADR amendment):

| Pattern name | Regex | Notes |
|---|---|---|
| `aws_access_key_id` | `AKIA[0-9A-Z]{16}` | AWS access key prefix. |
| `aws_secret_access_key` | `(?i)aws[_\-]?secret[_\-]?(access[_\-]?)?key\s*[:=]\s*["']?[A-Za-z0-9/+=]{40}` | 40-char base64-ish. |
| `openai_api_key` | `sk-[A-Za-z0-9]{32,}` | OpenAI SK prefix. |
| `anthropic_api_key` | `sk-ant-[A-Za-z0-9\-_]{32,}` | Anthropic SK prefix. |
| `generic_api_key_assignment` | `(?i)\b(api[_\-]?key\|secret[_\-]?key)\s*[:=]\s*["']?[A-Za-z0-9_\-]{16,}` | Catch-all assignment. |
| `password_assignment` | `(?i)\bpassword\s*[:=]\s*["']?\S{6,}` | Cleartext password literal. |
| `dotenv_filename` | `\.env(?:\.\w+)?` | Files named `.env`, `.env.local`, etc. — never legitimate to commit. |
| `bearer_token_literal` | `Bearer\s+[A-Za-z0-9_\-\.=]{20,}` | Hardcoded bearer token. |
| `private_key_block` | `-----BEGIN (RSA \|EC \|DSA \|OPENSSH \|PGP )?PRIVATE KEY-----` | PEM private key block. |

The scrubber is **deliberately conservative** — false positives are operator-recoverable (read the proposal, decide), false negatives (a real secret committed) are not. The aborted-run policy applies even if the operator believes the match is benign; the recovery path is to manually amend the worktree and re-run `gh pr create` outside the harness.

The scrubber runs **after** Claude Code's allowlist enforcement (§2.5) — secrets-in-allowlist-violating-files is two failures, both reported.

### 2.7 Draft PR opening only; per-PR LOC ceiling

Restated from the original ADR §2.5 and tightened:

- The harness opens **draft** PRs only. There is no `gh pr ready` invocation anywhere in `trellis_workers/code_authoring/`.
- The harness never invokes `gh pr merge`. Even a `--merge-when-ready` flag is rejected at the CLI layer.
- The harness never invokes `git push --force` or `git push -f`. Refspec mutation on `agent-proposals/<id>` is allowed only as a normal fast-forward push of the initial worktree.
- The harness never modifies `main`. There is no `git checkout main` in any code path.

Additional per-PR LOC ceiling: a single authoring run that produces a diff exceeding 300 LOC (insertions + deletions) is **rejected**. The harness emits `PROPOSAL_AUTHORSHIP_FAILED` with `{reason: "loc_ceiling_exceeded", loc_delta}` and preserves the worktree. The 300 LOC ceiling is independent of the weekly 1000 LOC cap — both must hold. Rationale: a single PR that touches 500 LOC is too big to review inside the time budget operators have for system-authored work; chunking it into smaller proposals is preferred.

Env override: `TRELLIS_AUTONOMOUS_SPAWN_PR_LOC_CEILING=300`. The default is the published number.

### 2.8 Failure modes

Explicit contracts for the failure modes the original ADR left implicit:

| Failure | Detected by | Action | Event |
|---|---|---|---|
| **Claude Code spawn timeout** | `subprocess.run(..., timeout=N)` where N defaults to 1800s (30 min) | Kill the process; preserve worktree; record event; exit 1. | `PROPOSAL_AUTHORSHIP_FAILED` with `{reason: "spawn_timeout", elapsed_seconds}` |
| **Budget cap hit at spawn time** | `_BudgetLedger.check()` before spawn | Refuse to spawn; record event; exit 1. | `BUDGET_EXHAUSTED` with `{loc_used, loc_cap, tokens_used, token_cap}` |
| **Budget cap hit mid-run** (token caller exceeds remaining headroom) | Claude Code SDK token usage reported on each turn; harness checks against remaining cap. | Send SDK `cancel`; preserve partial worktree state; record event; exit 1. | `BUDGET_EXHAUSTED` with `{reason: "exceeded_mid_run"}` |
| **Empty diff** (Claude Code exits cleanly but produces no changes) | `git diff --stat origin/main..HEAD` returns empty | Do not open PR; record event; preserve worktree; exit 0 (not 1 — empty diff is a benign outcome, not an error). | `PROPOSAL_AUTHORSHIP_EMPTY` with `{proposal_id, reason: "claude_code_produced_no_diff"}` |
| **Allowlist violation in diff** | `verify_diff_allowlist()` | Reject; record event; preserve worktree; exit 1. | `PROPOSAL_AUTHORSHIP_FAILED` with `{reason: "allowlist_violation", violating_paths}` |
| **Secret-scrub match** | §2.6 scanner | Reject PR creation; record event; preserve worktree; exit 1. | `SECRET_SCRUB_TRIGGERED` |
| **`gh pr create` failure** | `gh` non-zero exit | Record event; preserve worktree; exit 1. | `PROPOSAL_AUTHORSHIP_FAILED` with `{reason: "gh_pr_create_failed", stderr_excerpt}` |
| **Verification (lint/test) failure** | `make lint` or `make test` non-zero in the worktree | Reject PR creation; record event; preserve worktree; exit 1. | `PROPOSAL_AUTHORSHIP_FAILED` with `{reason: "lint_failed" \| "test_failed", stderr_excerpt}` |
| **Lock file present** (concurrent spawn on same proposal) | `agent-proposals/<id>/idempotency.lock` exists | Refuse; exit 1 with the lock-holder's run_id. | `PROPOSAL_AUTHORSHIP_BLOCKED` |

All failures preserve the worktree. None retry automatically. The recovery path is operator inspection, then either manual continuation or `trellis admin spawn-coder --cleanup <id>` to drop the worktree.

The `PROPOSAL_AUTHORSHIP_EMPTY` outcome is interesting and worth surfacing: an empty diff means Claude Code read the proposal, decided no code change was warranted, and exited cleanly. This is **valid** — not every proposal corresponds to an actual code defect. The event payload includes the SDK's stop reason so an operator can distinguish "I read this and decided nothing to do" from "I crashed."

### 2.9 New EventTypes this amendment introduces

These get full docstrings in `src/trellis/stores/base/event_log.py` per the POC directive "new event types get docstrings before they're emitted":

| EventType | When emitted | Payload contract |
|---|---|---|
| `BUDGET_CONSUMED` | After every spawn (success, failure, or empty), records LOC + tokens this run contributed. | `{run_id, proposal_id, loc_delta, tokens_input, tokens_output, cents_estimated, window_start, window_end}` |
| `BUDGET_EXHAUSTED` | At spawn time when the ledger denies a run, OR mid-run when token caps are exceeded. | `{run_id, proposal_id, reason, loc_used, loc_cap, tokens_used, token_cap, cents_used, cents_cap}` |
| `PROPOSAL_AUTHORSHIP_REQUESTED` | Before `git worktree add`, recording the operator-confirmed intent to author. | `{proposal_id, run_id, base_sha, files_allowed, started_at}` |
| `PROPOSAL_AUTHORSHIP_SUCCEEDED` | After a draft PR is opened. | `{proposal_id, run_id, pr_url, pr_number, loc_delta, tokens_input, tokens_output}` |
| `PROPOSAL_AUTHORSHIP_FAILED` | Any of the §2.8 failure modes that aborts the run. | `{proposal_id, run_id, reason, detail}` where `reason` is a closed enum |
| `PROPOSAL_AUTHORSHIP_EMPTY` | Claude Code exited cleanly with no diff. | `{proposal_id, run_id, claude_code_stop_reason}` |
| `PROPOSAL_AUTHORSHIP_BLOCKED` | Concurrent spawn on same proposal blocked by lock. | `{proposal_id, conflicting_run_id, requested_run_id}` |
| `SECRET_SCRUB_TRIGGERED` | §2.6 scanner matched. | `{proposal_id, run_id, match_count, pattern_names, file_paths}` |
| `COHORT2_REVIEW_RECORDED` | Operator records gate-(a) compliance via `--record-review-pass N`. | `{reviewed_count, operator_id, accepted_at, notes}` |

All `proposal_id` values refer to the existing `Proposal.proposal_id` (SHA-256 hex digest of the cluster signature) — Cohort 2 does not introduce a new ID space.

## 3. Why this shape

### 3.1 Why a single kill switch rather than per-feature flags

The original ADR had two flags (per-program budgets at `TRELLIS_LLM_BUDGET_CENTS_WEEK=0`, plus implicit operator-invocation). Cohort 2 collapses to one binary: `TRELLIS_AUTONOMOUS_SPAWN_ENABLED`. Reasoning: the security-relevant question is "is the autonomous loop on?" not "which sub-feature is on?" Operators don't reason about a system that's partially-autonomous; they reason about on/off. The budget knobs (§2.4) compose on top — they tune the on state, they don't gate it.

### 3.2 Why a 7-day rolling window for budgets

Matches the existing `TRELLIS_LLM_BUDGET_CENTS_WEEK` envelope in the original ADR §2.6. Operators already think in weekly review cadence; adding a daily or monthly window would mean two budget systems with different semantics. The rolling window (not calendar-week) avoids the "Monday midnight reset" cliff where the system bursts a week's budget in 2 hours.

### 3.3 Why diff-level allowlist enforcement on top of spawn-time

Belt and suspenders. The spawn-time env scoping (original ADR §2.4) is enforced by Claude Code SDK — a future SDK change could weaken it without the operator noticing. The diff-level check (this amendment §2.5) is enforced by the harness in code we own. Both have to succeed for a PR to open. The cost of a redundant check is negligible; the cost of a single point of failure on the security boundary is unacceptable.

### 3.4 Why the secret-scrub is on the diff, not the worktree

The diff is what becomes a public artifact (a draft PR is visible to everyone with repo access). Files in the worktree that aren't in the diff don't appear in the PR. Scanning the diff is the minimal-surface check that catches every secret-leak vector that matters and avoids false positives on pre-existing test fixtures or example files that include placeholder secrets.

### 3.5 Why empty-diff exits 0

An empty diff is the correct outcome when Claude Code reads a proposal and decides the code is already fine. The original ADR called the proposal generator's empty-result case "no clusters above threshold; INFO log; exit 0" — the same logic applies at the authoring layer. Operators should not be paged for a spawn run that reasoned correctly to a no-op. The `PROPOSAL_AUTHORSHIP_EMPTY` event preserves the signal without raising.

### 3.6 Why per-PR 300 LOC ceiling (not just the weekly cap)

The weekly cap (§2.4) bounds throughput. The per-PR ceiling bounds individual reviewability. A 700 LOC PR is hard to review even if the week's budget allows it; chunking it into multiple proposals (each its own PR) is the right shape. The 300 LOC number is calibrated to the typical Cohort 1 proposal scope — single-file changes plus tests — and to what a reviewer can read in 10 minutes.

### 3.7 Why preserve the worktree on every failure

Forensic data. A spawn that fails for `lint_failed` is interesting — *what* did Claude Code generate that fails lint? Deleting the worktree drops the only artifact that can answer the question. Disk cost is bounded by the explicit `--cleanup` CLI; operators clean up when they're done investigating. This matches Cohort 1's posture on the proposals themselves (kept on disk in `agent-proposals/<id>/proposal.md` until explicitly removed).

### 3.8 Why no automatic retry

A retry loop on a spawn that hit `allowlist_violation` would just hit it again. A retry loop on `spawn_timeout` would burn budget on the same proposal. A retry loop on `gh_pr_create_failed` would hit GitHub rate limits. Every Cohort 2 failure mode is operator-actionable, not transient — the recovery contract is "operator reads the event, decides whether to re-run after fixing root cause." This matches the project's broader hard rule: no silent fallbacks, loud-on-misuse.

## 4. Guardrails (composed with original ADR §4)

Original ADR §4 guardrails carry over verbatim:

- Branch isolation: `agent-proposals/<proposal_id>` only.
- No auto-merge, no auto-rebase, no force-push.
- Sandboxed execution: read on `src/`, write only on allowlisted files.
- Secret scrubbing on env vars before spawn.
- Allowlist of modifiable files per proposal frontmatter.

Original ADR §4.1 additions carry over:

- No proposal modifies `MutationExecutor` or `StoreRegistry` (now enforced at parse-time on `files_allowed`).
- No proposal modifies security/auth code paths (now enforced at parse-time on `files_allowed`).
- Cumulative weekly LOC cap (now §2.4 with explicit ledger).

New guardrails this amendment adds:

- **Kill switch.** `TRELLIS_AUTONOMOUS_SPAWN_ENABLED=false` default; checked at the first line of `spawn-coder`.
- **Per-PR LOC ceiling** (§2.7). 300 LOC max per single authoring run.
- **Diff-level secret scrub** (§2.6). Pattern-set enforced before PR creation.
- **Diff-level allowlist verification** (§2.5). Redundant with spawn-time enforcement on purpose.
- **Empty diff is a first-class outcome** (§2.8). `PROPOSAL_AUTHORSHIP_EMPTY` event; exit 0.
- **Worktree preservation on every failure** (§2.8). Cleanup is operator-driven.
- **No retry, ever** (§3.8). Every failure surfaces to the operator.
- **Per-proposal idempotency lock** (§2.2). Two concurrent spawns on the same proposal are blocked.
- **Gate (a) machine-checked** (§2.0). `COHORT2_REVIEW_RECORDED` event required; not satisfiable via config alone.

## 5. Consequences

### 5.1 What this enables

- The closed authoring loop: signal → proposal → autonomous spawn → draft PR → human merge. The capstone of the self-improvement program.
- A scheduled cadence: operator sets up a cron that runs `spawn-coder --cycle` every N hours; the system files draft PRs against itself; the operator reviews on their own schedule.
- A measurable convergence story: Scenario 5.4 (loop convergence) can report "system filed N PRs in week W, M were merged, K were closed."

### 5.2 What this still does not do

- Does not enable auto-merge. The harness never invokes `gh pr merge`, `git merge`, or any mutation of `main`.
- Does not enable cross-repo proposals (still scoped to the local Trellis repo).
- Does not enable `MutationExecutor` / `StoreRegistry` / auth-path modifications (still hard-excluded).
- Does not enable the system to bypass `make lint` / `make test` (still verification-gated).
- Does not enable a "merge if all green" workflow (still draft-only, regardless of CI state).

### 5.3 What this costs

- LLM spend per spawn (estimated $1–$15 depending on proposal complexity; tighter than Cohort 1's $0.10–$2.00 because Cohort 2 invocations are full code-authoring spawns, not just proposal narratives).
- Disk for preserved-on-failure worktrees. Cleanup is operator-driven; a `du -sh agent-proposals/` after a month of running gives the operator a sense of the carrying cost.
- GitHub API rate (each spawn-cycle that produces a PR is one `gh pr create` call; small absolute cost, bounded by the per-PR LOC ceiling making single-PR runs the norm).
- Operator review time. A draft PR per spawn that produces output, on whatever schedule the operator chose.

### 5.4 What surfaces in Cohort 1 we depend on

- The proposal artifact shape (`agent-proposals/<id>/proposal.md`) and its frontmatter — Cohort 2 reads `files_allowed`, `budget_cents`, `proposal_id` from there.
- The `PROPOSAL_DRAFTED` / `PROPOSAL_UPDATED` event stream — `ProposalSelector` reads from here.
- The stable `proposal_id` hash from `compute_proposal_id()` in `src/trellis_workers/code_authoring/proposal.py`.
- The cooldown logic in the generator (30 days post-action) — Cohort 2 honors this; the harness checks for any `PROPOSAL_AUTHORSHIP_*` event on the proposal in the last 30 days before spawning.

## 6. Alternatives considered

- **Schedule via GitHub Actions, not local cron.** Rejected for the original ADR §3.4 reasons — the signal source is local to the operator's deployment, secrets exposure on Actions is broader, and the file-allowlist model leaks proposal metadata into workflow files. Cohort 2's `spawn-coder` is still operator-machine-local; the autonomy is in *who triggers it* (the operator's cron, not the operator's keyboard), not *where it runs*.
- **Use Anthropic Managed Agents instead of Claude Code SDK.** Defer. Same reasoning as the original ADR — Managed Agents would replace `ClaudeCodeAuthor`; the rest of the security model (worktree, allowlist, budget, scrub) carries over.
- **Single budget cap (cents only) rather than two (LOC + tokens).** Rejected. LOC bounds review burden; tokens bound LLM cost. Either one can be tight while the other is loose, and the operator-facing reasoning is different ("too many lines for me to review" vs. "too much LLM spend"). Both caps are cheap to enforce.
- **Per-proposal opt-in via operator action.** That's Cohort 1. Cohort 2 is by definition the autonomous path; an operator who wants the per-proposal opt-in keeps Cohort 1 and never sets `TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true`.
- **Auto-cleanup of failed worktrees after N days.** Rejected. Forensic data is too valuable; disk cost is bounded by explicit cleanup. Adding an auto-cleanup turns "the worktree where Claude Code crashed yesterday" into "gone before I looked at it Monday morning."
- **Allow re-running on a closed proposal after some window.** Rejected. The 30-day cooldown is the discipline; a proposal that was closed unactioned should not auto-re-spawn. If the underlying signal recurs, the generator's `PROPOSAL_UPDATED` path handles it — that produces a new authoring eligibility window.

## 7. Open questions for future amendments

These are explicitly left **unresolved** by this amendment. They are noted so a future amendment can pick them up cleanly:

- **Cross-cycle learning from PR outcomes.** When a Cohort 2 PR merges, does that signal feed back to the proposal generator (boosting the priority of similar clusters next time)? Today no; the loop is open at the PR-merged side. A future amendment can wire `pull_request.merged` events into the proposal generator's priority signal.
- **Multi-PR proposals.** A single proposal spawns one draft PR today. A "this needs three coordinated PRs" proposal shape would require splitting `files_allowed` across PRs and tracking dependencies. Out of scope for Cohort 2.
- **PR comments as continuation signal.** If a reviewer leaves a comment on a draft PR ("please also do X"), can the system spawn a follow-up authoring run? Tempting but probably the wrong default; operators reason about comments-as-continuation in their head, not in the harness.

## 8. References

- Original ADR: [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md)
- Implementation plan (Cohort 2 phase breakdown): [`plan-coding-agent-loop-cohort2.md`](./plan-coding-agent-loop-cohort2.md)
- Program security model: [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9
- TODO.md "Item 7 Cohort 2 — Sandboxed Claude Code spawn" entry — gating criteria (a) preserved verbatim in §2.0 above.
- Cohort 1 PRs: #134 (proposal generator core), #135 (CLI surface).
