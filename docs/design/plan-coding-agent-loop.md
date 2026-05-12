# Plan: Coding-agent self-improvement loop

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable (Phases 0+1 first; Phases 2+3 only after Phases 0+1 have been live for a release)
**ADR:** [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 7 (capstone)
**Depends on:**
- Item 4 ([`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md)) — produces `EXTRACTION_FAILED` events.
- Item 5 ([`plan-well-known-promotion-loop.md`](./plan-well-known-promotion-loop.md)) — produces `WELL_KNOWN_CANDIDATE` events.
- Item 6 ([`plan-dogfooding-meta-traces.md`](./plan-dogfooding-meta-traces.md)) — meta-Activity provenance for proposals.

**Unblocks:** none — this is the capstone.

## 1. Scope

**In scope (Phases 0+1 — first PR cohort):**

- `src/trellis_workers/code_authoring/` package.
- `ProposalGenerator` reading signal events, clustering, emitting proposal markdown.
- Stable `proposal_id` and idempotency.
- CLI: `trellis admin generate-proposals`, `trellis admin list-proposals`, `trellis admin show-proposal <id>`.
- EventLog event types: `PROPOSAL_DRAFTED`, `PROPOSAL_UPDATED`.
- One eval scenario: inject one signal cluster → verify one proposal generated → re-run → no duplicate.

**In scope (Phases 2+3 — second PR cohort, gated):**

- `ClaudeCodeAuthor` adapter (Claude Code SDK spawn with sandboxed worktree).
- CLI: `trellis admin author-proposal <id>`.
- `GitHubProposer` (gh CLI wrapper) and `trellis admin propose-pr <id>`.
- Budget ledger and rate limiting.
- File-allowlist enforcement; secret scrubbing; verification gate.

**Out of scope:**

- Auto-spawn of Claude Code without operator invocation.
- Auto-merge of any PR.
- Reading from sources other than the three signal types in §2.1 of the ADR.
- A generic coding-agent platform usable outside Trellis.

## 2. POC directives applied (strict)

This item has the highest blast radius in the program. Strict reading of the POC directives:

- A proposal generator that finds zero signals produces an empty list and exits 0 — but **logs INFO with a one-line summary** ("no clusters above threshold; analyzed N events"). No silent empty exit.
- A proposal whose generation requires an LLM call (Phases 2+) **raises** if the budget ledger is at the cap. No silent skip.
- A Claude Code spawn that exits with non-zero status **raises** in the CLI; the worktree is preserved for inspection, never auto-deleted.
- A verification failure (allowlist violated, lint failed, tests failed) **raises** with the specific failure in the message. The worktree is preserved.
- The `TRELLIS_LLM_BUDGET_CENTS_WEEK` env var: any value other than a non-negative integer **raises** at startup.

## 3. Phases

### Phase 0 — proposal generator core

**Files:**
- `src/trellis_workers/code_authoring/__init__.py`
- `src/trellis_workers/code_authoring/generator.py`
- `src/trellis_workers/code_authoring/clustering.py`
- `src/trellis_workers/code_authoring/proposal.py` (artifact shape + serializer)
- `src/trellis/schemas/event.py` (add `PROPOSAL_DRAFTED`, `PROPOSAL_UPDATED`)
- `tests/unit/workers/code_authoring/test_generator.py`
- `tests/unit/workers/code_authoring/test_clustering.py`

**API:**

```python
@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    signal_type: Literal["extraction_failure_cluster", "well_known_candidate", "advisory_drift"]
    created_at: datetime
    cluster_evidence: dict[str, Any]
    suggested_change_kind: str
    files_allowed: tuple[str, ...]
    budget_cents: int
    markdown: str  # the human-readable body

class ProposalGenerator:
    def __init__(
        self,
        *,
        event_log: EventLog,
        graph_store: GraphStore,
        config: ProposalGeneratorConfig,
    ): ...

    def generate(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> list[Proposal]:
        """Read signal events, cluster, emit Proposals (and PROPOSAL_DRAFTED events).

        Idempotent: re-running over the same window produces no duplicate events.
        """
```

**Clustering rules (per signal_type):**

- `extraction_failure_cluster`: group by `(extractor_id, prompt_hash, failure_kind)`; threshold count ≥ 50; first_seen ≥ 7d window.
- `well_known_candidate`: each candidate becomes its own proposal; threshold count ≥ 500 (per Item 5 ADR).
- `advisory_drift`: each drift event becomes one proposal.

**Tests (12):**

1. No events → empty list, INFO log.
2. One cluster above threshold → one proposal.
3. Two clusters above threshold → two proposals, distinct proposal_ids.
4. Below threshold → no proposal.
5. Re-run same window → no duplicate proposals (idempotency).
6. Cluster grows ≥ 50% → `PROPOSAL_UPDATED` emitted with same proposal_id.
7. Cluster decays below threshold → no `PROPOSAL_CLOSED` event (not in scope; document).
8. `well_known_candidate` proposal includes suggested canonical name and alignment.
9. `advisory_drift` proposal includes baseline and recent lift.
10. Proposal markdown is non-empty and contains the cluster_key in frontmatter.
11. Files_allowed is non-empty and points only to allowlisted-modifiable paths.
12. Proposal_id is stable across re-runs with the same input.

**Estimated size:** ~700 LOC + ~500 LOC tests.

### Phase 1 — CLI: generate / list / show proposals

**Files:**
- `src/trellis_cli/admin.py` (extend) — add three subcommands.
- `tests/unit/cli/test_admin.py` — 6 tests.

**CLI:**

```
trellis admin generate-proposals
    --since 7d
    --until now
    --kinds extraction_failure_cluster,well_known_candidate,advisory_drift  # default all
    --output-dir agent-proposals/                                            # default

trellis admin list-proposals
    --status open|actioned|closed   # default open
    --format table|json

trellis admin show-proposal <proposal_id>
    --format markdown|json
```

Each generated proposal is persisted to `agent-proposals/<proposal_id>/proposal.md`. The directory is in `.gitignore` (the proposals are operator-local artifacts, not committed).

**Estimated size:** ~250 LOC + ~200 LOC tests.

### Phase 2 — Claude Code author (gated; second PR cohort)

**Files:**
- `src/trellis_workers/code_authoring/claude_code_author.py` — adapter for Claude Code SDK.
- `src/trellis_workers/code_authoring/sandbox.py` — worktree creation, file-allowlist enforcement, env-var scrubbing, verification gate.
- `src/trellis_workers/code_authoring/budget.py` — budget ledger (reads from operational EventLog, tracks `LLM_COST_CENTS` events).
- `src/trellis_cli/admin.py` (extend) — `author-proposal` subcommand.
- `tests/unit/workers/code_authoring/test_sandbox.py` (8 tests) — file allowlist; env scrub; verification gate.
- `tests/unit/workers/code_authoring/test_budget.py` (4 tests).
- `tests/unit/cli/test_admin.py` — extend with author-proposal tests (mocked Claude Code spawn).

**Spawn flow:**

```python
def author_proposal(
    *,
    proposal_id: str,
    budget_ledger: BudgetLedger,
    config: AuthorConfig,
) -> AuthorshipResult:
    proposal = load_proposal(proposal_id)
    if budget_ledger.spent_in_last_7d() >= config.budget_cents_week:
        raise BudgetExhausted(...)

    with sandbox(proposal.files_allowed, secret_scrub=True) as worktree:
        # Spawn Claude Code SDK against worktree
        result = subprocess.run(
            ["claude-code", "--prompt-file", proposal.markdown_path, ...],
            cwd=worktree.path,
            env=worktree.scrubbed_env,
            check=False,
        )

        verify_sandbox_compliance(worktree, allowed=proposal.files_allowed)
        verify_lint_test(worktree)

        if result.returncode != 0:
            raise ClaudeCodeFailed(...)

    budget_ledger.record_spend(...)
    return AuthorshipResult(proposal_id=proposal_id, worktree_path=worktree.path, branch=...)
```

**Estimated size:** ~600 LOC + ~400 LOC tests.

### Phase 3 — GitHub proposer (gated; second PR cohort)

**Files:**
- `src/trellis_workers/code_authoring/github_proposer.py` — wraps `gh pr create --draft`.
- `src/trellis_cli/admin.py` (extend) — `propose-pr` subcommand.
- `tests/unit/workers/code_authoring/test_github_proposer.py` (4 tests, mocked `gh`).

**Flow:**

```python
def propose_pr(
    *,
    proposal_id: str,
    worktree_path: Path,
    config: ProposerConfig,
) -> PRProposeResult:
    if config.dry_run:
        return PRProposeResult(dry_run=True, would_push_to=...)

    # gh pr create --draft --base main --head agent-proposals/<id> --title <auto> --body <proposal.md>
    ...
```

**Estimated size:** ~200 LOC + ~150 LOC tests.

### Phase 4 — eval scenario

**File:** `eval/scenarios/proposal_generation.py` (new).

**Behavior:** synthetic event injection of one `EXTRACTION_FAILED` cluster (count=73). Run ProposalGenerator. Assert:

- One proposal generated.
- proposal_id stable across re-runs.
- `PROPOSAL_DRAFTED` event emitted; `PROPOSAL_UPDATED` not emitted on second run.
- Inject 40 additional failures → re-run → `PROPOSAL_UPDATED` emitted (count crossed +50% threshold).

**Phases 2+3 testing in this scenario is mock-only** — we do not spawn real Claude Code or call real GitHub in CI. Live testing is operator-only.

**Estimated size:** ~400 LOC.

## 4. Total size estimate

| Phase | LOC code | LOC tests | Cohort |
|---|---|---|---|
| 0 | 700 | 500 | 1st |
| 1 | 250 | 200 | 1st |
| 2 | 600 | 400 | 2nd |
| 3 | 200 | 150 | 2nd |
| 4 | 400 | 0 | 1st (scenario tests Phase 0/1 only) |
| **Total** | **~2150** | **~1250** | |

**Cohort 1 (Phases 0+1+4) ships first**, can land in isolation. Cohort 2 (Phases 2+3) ships only after Cohort 1 has run for one release cycle and no security/idempotency issues surfaced.

## 5. Done when

**Cohort 1:**
- `trellis admin generate-proposals` runs against a synthetic EventLog and produces N proposals.
- Re-run is idempotent.
- `trellis admin list-proposals` and `trellis admin show-proposal <id>` work.
- Eval scenario passes.
- mypy clean.

**Cohort 2:**
- `trellis admin author-proposal <id>` against a mock Claude Code spawn succeeds (real spawn is operator-only).
- Sandbox verification gate rejects an out-of-allowlist write.
- Budget ledger correctly blocks at the cap.
- `trellis admin propose-pr <id>` with `--dry-run` outputs the expected `gh` invocation.

## 6. Cleanup considerations

- After Cohort 1, the `agent-proposals/` directory should be added to `.gitignore` — operator-local artifacts.
- Any pre-existing `TODO.md` items about "the system should improve itself" should be retired and pointed at this plan.

## 7. Risks

- **Claude Code SDK API changes between releases.** Mitigation: pin the SDK version; verify the spawn flow in CI against the pinned version; update is a deliberate version bump.
- **Allowlist bypass via symlinks or paths-with-..**. The sandbox.py implementation must reject any allowlist entry containing `..`, must resolve symlinks before checking, and must reject paths outside the worktree. Contract test: try to write outside allowlist → harness raises with the offending path.
- **Budget ledger desync.** If the operational EventLog and the in-memory ledger disagree, the in-memory ledger wins for the current invocation but the persistent ledger is canonical. Reconciliation at startup. Documented.
- **Operator-trust regression.** A few low-quality auto-generated proposals could erode operator trust quickly. Mitigation: Phase 0 ships with a `--strict` mode that only generates proposals at very high thresholds (default thresholds × 2). Operator graduates to default thresholds once they've seen the system produce useful proposals.
- **Cross-repo proposals.** Out of scope. The system only proposes against the local repo (Trellis itself). Proposals targeting downstream-integrator repos require explicit ADR amendment.

## 8. Open question for the operator

After Phase 0+1 lands, an explicit operator decision is required: which signals should generate proposals at all? It's possible that, say, `advisory_drift` proposals turn out to be low-value (drift is often legitimate workload change, not a fix-target). The plan ships all three signal types; the operator turns off the ones that don't earn their keep via the `--kinds` flag and via per-kind threshold tuning in the parameter registry.
