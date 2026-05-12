# ADR: Coding-agent self-improvement loop

**Status:** Proposed
**Date:** 2026-05-11
**Deciders:** Trellis core
**Related:**
- [`./plan-coding-agent-loop.md`](./plan-coding-agent-loop.md) — implementation plan
- [`./adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md) — signal source 1
- [`./adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) — signal source 2
- [`./adr-dogfooding-meta-traces.md`](./adr-dogfooding-meta-traces.md) — signal source 3
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 — security model this ADR adopts wholesale

---

## 1. Context

The first six items in the self-improvement program produce signal. They generate `EXTRACTION_FAILED` clusters, `WELL_KNOWN_CANDIDATE` events, `ADVISORY_DRIFT_DETECTED` events, and meta-Activities. Today, all of that signal lands in the EventLog and the graph — and stops there. A human reads a CLI report and decides what to do.

The closed loop — *system observes its own degradation → drafts a code change → human reviews → merge updates parameters / schema → next run benefits* — is the missing capstone. It's also the one item in this program that is genuinely novel: no Trellis-shaped system in the prior art does it.

This ADR scopes that loop. It is **deliberately conservative**: the loop produces *proposals*, never merged code. The Claude Code spawn is **opt-in per invocation**, not automatic. The blast radius is bounded by branch isolation, file allowlists, and secret scrubbing.

The risk of building this badly is high: an under-secured coding agent that auto-merges its own changes is a recipe for production incidents. The risk of building this *at all* is also non-zero — a self-modifying system, even a well-bounded one, increases the system's behavioral surface. We accept the second risk and design around the first.

## 2. Decision

Introduce a `src/trellis_workers/code_authoring/` package. Three layers:

| Layer | Responsibility | Auto-firing? |
|---|---|---|
| **ProposalGenerator** | Read signal events; cluster; produce a markdown proposal with an explicit diff | Yes (scheduled or on-demand) |
| **ClaudeCodeAuthor** | Spawn Claude Code SDK against a proposal markdown; produce a working commit on an isolated branch | **No — opt-in per proposal via CLI** |
| **GitHubProposer** | Open a draft PR from the isolated branch | **No — opt-in per commit via CLI** |

The default state is **proposals-only**. Operators can invoke `trellis admin author-proposal <proposal_id>` to spawn the coding agent for a specific proposal. Auto-spawn is not in scope for this ADR — it requires a separate ADR amendment after the proposal-only path has been live for a release.

### 2.1 Proposal sources

The ProposalGenerator subscribes to three event types from Items 4, 5, and 6:

| Signal | Source | What the proposal drafts |
|---|---|---|
| `EXTRACTION_FAILED` clusters with count ≥ N (default 50) | [`adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md) | "Extractor X's `failure_kind=Y` cluster is at count={N}. Suggested fix: tighten the JSON schema in `<extractor>.py`. Sample errors: {...}." |
| `WELL_KNOWN_CANDIDATE` events | [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) | "Open-string type `{name}` qualifies for promotion. Proposed diff: add `{Canonical}` to `well_known.py`. Update alias map. Add 6 tests. Draft ADR amendment to `adr-graph-ontology.md`." |
| `ADVISORY_DRIFT_DETECTED` events | existing in [`effectiveness.py`](../../src/trellis/retrieve/effectiveness.py); not new to this program | "Advisory {advisory_id} shows lift sign-flip in the last {window}. Review: confidence dropped from {prior} to {recent}. Suggested action: review parameter registry settings for `(component, domain, intent_family)`." |

### 2.2 Proposal artifact shape

A proposal is a markdown file written to `agent-proposals/<proposal_id>/proposal.md`:

```markdown
---
proposal_id: prop-abc123
signal_type: extraction_failure_cluster
created_at: 2026-05-11T12:00:00Z
cluster_evidence:
  cluster_key: ...
  count: 73
  first_seen: ...
  extractor_id: LLMExtractor
  prompt_hash: ...
suggested_change_kind: tighten_extractor_schema
files_allowed: ["src/trellis/extract/llm.py", "tests/unit/extract/test_llm.py"]
budget_cents: 200
---

## Problem

[generated narrative naming the cluster, the count, and the failure_kind]

## Suggested fix

[generated narrative with a starter diff and test plan]

## Evidence

[sample event_ids, with redacted error_excerpts]
```

The proposal is **declarative** — it states the problem and a suggested direction. It does not encode the implementation. The Claude Code spawn (if invoked) reads this proposal and writes the implementation.

### 2.3 Stable proposal_id

`proposal_id` is `prop-{shorthash(signal_type + cluster_key)}`. Idempotency:

- Re-running the ProposalGenerator does **not** produce a duplicate proposal for the same cluster.
- A cluster whose evidence changes materially (count grows by ≥ 50%, or new failure_kind appears) produces a `PROPOSAL_UPDATED` event referencing the same `proposal_id` and refreshes the markdown.
- A proposal that has been actioned (a PR was opened) and either merged or closed is **not** re-proposed for a cooldown window of 30 days from action.

### 2.4 Claude Code spawn — security model

When an operator runs `trellis admin author-proposal <proposal_id>`:

1. The ProposalGenerator emits `PROPOSAL_AUTHORSHIP_REQUESTED`.
2. The harness creates a fresh git worktree at `agent-proposals/<proposal_id>/worktree/`.
3. The Claude Code SDK is spawned with:
   - **Working directory:** the worktree.
   - **Allowed files:** exactly the `files_allowed` list from the proposal frontmatter. Anything else is read-only.
   - **Read access:** the whole repo (so Claude can see context).
   - **Write access:** only the allowed files.
   - **Environment:** scrubbed of `*_KEY|*_SECRET|*_TOKEN|*_PASSWORD` env vars before spawn.
   - **Budget:** `budget_cents` from frontmatter caps the spend.
4. On exit, the harness verifies:
   - Only allowlisted files were modified (via `git diff --name-only`).
   - No new files outside `tests/` and `docs/design/`.
   - `make lint` and `make test` pass on the worktree.
5. If verification passes, the commit is left in the worktree branch. Operator runs `trellis admin propose-pr <proposal_id>` to open a draft PR.

### 2.5 No auto-merge, ever

The harness never merges a PR. It never force-pushes. It never modifies `main`. The draft PR is the **final** artifact the system produces; humans take it from there.

### 2.6 Per-program budgets

`TRELLIS_LLM_BUDGET_CENTS_WEEK=0` (default) — proposals are generated, no Claude Code spawn. Operator sets a positive value to opt in.

Spawn-time check: cumulative spend in the trailing 7-day window must be `< budget`. If at the limit, `author-proposal` exits 1 with the spend ledger. No silent skip.

## 3. Why this shape

### 3.1 Why proposals-only by default

A self-modifying system that runs Claude Code on every signal change without human review *will* eventually do something stupid. The conservative default keeps the system useful (proposals are valuable on their own — an operator can act on them manually) without ceding the keys to the kingdom. Auto-spawn requires a separate ADR amendment after at least one release of operational experience.

### 3.2 Why the proposal artifact is markdown

Markdown is human-readable, version-controllable, and stable across Claude Code versions. A JSON schema would change every time we want to add a field; a markdown template degrades gracefully. The frontmatter carries the machine-readable bits.

### 3.3 Why the Claude Code spawn is run from the operator's machine, not the server

The Trellis API server should not have `git push` access to its own repository. Spawning Claude Code from operator-controlled CLI invocations keeps the API server's auth surface minimal. The trade-off is that scheduled auto-spawn requires the operator's machine to be available — which is the desired property.

### 3.4 Why we don't reuse a generic CI workflow

GitHub Actions could in principle do the same thing. We're not using it because:

- The proposal-to-spawn flow is interactive (operator decides which proposals to author).
- The signal source (Trellis EventLog) is local to the operator's deployment, not naturally exposed to CI.
- The security model (file allowlists per proposal) is fine-grained; replicating it in a generic Actions workflow would mean leaking proposal metadata into the workflow file.

If we later want auto-spawn on a server, a separate Actions integration can be added then. The current shape doesn't preclude it.

## 4. Guardrails (from [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9)

Restated here for the artifact authors who read this ADR cold:

- Branch isolation: every proposal targets `agent-proposals/<proposal_id>`. Never `main`. Never any active dev branch.
- No auto-merge, no auto-rebase. Draft PR or local commit only.
- Sandboxed execution: read access on `src/`, write access only on allowlisted files.
- Secret scrubbing: spawn-time env-var filter rejects anything matching the secret pattern.
- Allowlist of modifiable files: only the files named in `proposal.md` frontmatter. Anything else → harness rejects the resulting commit.

### 4.1 New guardrails this ADR adds

- **No proposal for `MutationExecutor` or `StoreRegistry` modifications.** Both are core safety-critical surfaces. Proposals touching either are gated to ADR-only (proposal generator can suggest the ADR, never the code).
- **No proposal for security/auth code paths.** Static allowlist excludes `src/trellis_api/auth.py`, `src/trellis/mutate/policies/`, anything matching `*_security_*.py`.
- **Cumulative weekly LOC cap.** No more than 500 LOC across all authored commits in a 7-day window without an explicit operator override. Prevents a runaway day from filling the repo with agent-written code.

## 5. Consequences

### 5.1 What this enables

- The closed loop: degradation signal → proposal → human review → merge → improvement.
- A demonstrable convergence story: Scenario 5.4 (loop convergence) can produce not just a "system got better at retrieval" curve but also "system filed N PRs against itself, M were merged".
- A novel public story for Trellis if/when the project goes public.

### 5.2 What this does not do

- Does not enable the system to merge its own changes.
- Does not enable the system to bypass human review.
- Does not generalize to a coding-agent platform — it is a Trellis-specific self-improvement loop, not a generic harness.

### 5.3 What this costs

- LLM spend per proposal authorship (estimated $0.10–$2.00 depending on proposal complexity).
- Storage for `agent-proposals/` worktrees (bounded by cleanup CLI command after merge/close).
- Operator attention: a "review N system-authored proposals" item enters the operator's weekly workflow.

## 6. Alternatives considered

- **Auto-merge proposals that pass tests.** Rejected — auto-merge against a 0-feedback signal is a recipe for incidents.
- **Issue creation instead of PRs.** Rejected — issues are easy to ignore; a draft PR with a diff is reviewable in minutes.
- **No coding agent, just CLI suggestions.** Considered — this is the fallback if Phase 3 (Claude Code spawn) is too costly. The proposal-generator-only mode is shippable in isolation and provides ~70% of the value.
- **Use GitHub Actions / Copilot Workspace.** Rejected for the reasons in §3.4.
- **Use the Anthropic Managed Agents API instead of Claude Code SDK.** Defer. The Claude Code SDK matches the local-developer model the rest of Trellis uses. If Managed Agents become the better fit operationally, the ClaudeCodeAuthor layer is the only thing that changes — the proposal artifact and the security model carry over.

## 7. References

- Item 4: [`adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md)
- Item 5: [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md)
- Item 6: [`adr-dogfooding-meta-traces.md`](./adr-dogfooding-meta-traces.md)
- Program security model: [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9
- Claude Code SDK docs (external reference; not committed)
