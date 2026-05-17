# Plan: Coding-agent loop — Cohort 2 (autonomous spawn)

**Status:** Proposed 2026-05-16
**Owner:** swarm-pickable, but **do not pick up before** both gating criteria in [`adr-coding-agent-loop-cohort2-amendment.md`](./adr-coding-agent-loop-cohort2-amendment.md) §2 are satisfied.
**ADR:** [`adr-coding-agent-loop-cohort2-amendment.md`](./adr-coding-agent-loop-cohort2-amendment.md) (amends [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md))
**Predecessor plan:** [`plan-coding-agent-loop.md`](./plan-coding-agent-loop.md) (Cohort 1, Phases 0+1+4, landed via PRs #134, #135)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) Item 7 capstone, autonomous half.

**Depends on:**

- Cohort 1 (PRs #134, #135) on main. The `Proposal` dataclass, `ProposalGenerator`, the `PROPOSAL_DRAFTED` / `PROPOSAL_UPDATED` events, and the `trellis admin {generate,list,show}-proposal{s}` CLI surface are prerequisites; this plan extends rather than replaces them.
- TODO.md gating criterion (a): operator review of ≥ 5 real Cohort 1 proposals recorded as a `COHORT2_REVIEW_RECORDED` event via `trellis admin spawn-coder --record-review-pass N`.
- TODO.md gating criterion (b): the amendment ADR accepted (this plan ships alongside it).

**Unblocks:** none — this is the capstone of the self-improvement program.

## 1. Scope

**In scope:**

- `src/trellis_workers/code_authoring/sandbox.py` — worktree creation, file-allowlist enforcement (parse-time + diff-time), secret scrubbing, verification gate (lint + test).
- `src/trellis_workers/code_authoring/budget.py` — `_BudgetLedger` reading + writing `BUDGET_CONSUMED` / `BUDGET_EXHAUSTED` events; LOC + token caps.
- `src/trellis_workers/code_authoring/claude_code_author.py` — Claude Code SDK spawn adapter; timeout enforcement; mid-run token-cap check.
- `src/trellis_workers/code_authoring/github_proposer.py` — `gh pr create --draft` wrapper.
- `src/trellis_workers/code_authoring/selector.py` — `ProposalSelector` reading `PROPOSAL_DRAFTED` events; tie-break by `source_event_count` then `created_at`; cooldown enforcement.
- `src/trellis_cli/admin_spawn_coder.py` — `trellis admin spawn-coder` CLI subcommand with `--cycle`, `--proposal-id`, `--record-review-pass`, `--cleanup`, `--dry-run` flags.
- `src/trellis/stores/base/event_log.py` — 9 new `EventType` members per the amendment §2.9 with full docstrings.
- `src/trellis_workers/code_authoring/budget_schema.json` — JSON schema for `BUDGET_CONSUMED` payload (the contract the operational EventLog row must satisfy).
- One eval scenario: `eval/scenarios/spawn_coder_dry_run/` — synthetic injection of N proposals → harness picks one → budget check → mock Claude Code spawn → mock `gh` invocation → assertions on event emission. Live Claude Code spawn is operator-only; never runs in CI.

**Out of scope:**

- Anything that calls `gh pr merge`, `gh pr ready`, `git push --force`, `git merge`, or modifies `main`.
- Auto-retry of failed spawns.
- Cross-repo proposals (proposals targeting downstream-integrator repos).
- Auto-cleanup of preserved-on-failure worktrees.
- PR-comment continuation flow (reviewer comment → follow-up spawn).
- Cohort 1 surface mutations (the generator, the proposal artifact shape, the existing `admin` subcommands stay frozen; this cohort only adds).

## 2. POC directives applied (strict, restated)

The amendment ADR §3.8 says no automatic retry. The amendment ADR §3.5 says empty diff is a benign exit-0 outcome. The amendment ADR §2.1 says kill switch defaults off. These compose with the predecessor plan's directives:

- A `spawn-coder` invocation with the kill switch off **raises** (exits 1 with a clear message), never silently no-ops.
- A budget cap hit **raises** (exits 1 with the spend ledger), never silently skips.
- A spawn that produces an allowlist-violating diff **raises** (exits 1), never quietly drops the violating file.
- A spawn that exits cleanly with no diff exits 0 and emits `PROPOSAL_AUTHORSHIP_EMPTY` — this is the **only** benign exit-0 path that isn't "PR opened."
- `TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK` / `..._TOKEN_CAP_WEEK` / `..._PR_LOC_CEILING`: any value other than a non-negative integer **raises** at startup. The amendment's cap defaults (1000 LOC / 1M tokens / 300 LOC per PR) are the published numbers — env overrides them with loud-on-bad-input semantics.

## 3. Phases

Five phases. Each is independently shippable; phase ordering matches dependency direction.

### Phase 2.1 — Budget ledger + EventType definitions

**Files:**

- `src/trellis/stores/base/event_log.py` — add 9 EventType members from amendment §2.9 with docstrings.
- `src/trellis_workers/code_authoring/budget.py` — `_BudgetLedger` class.
- `src/trellis_workers/code_authoring/budget_schema.json` — JSON schema for `BUDGET_CONSUMED` payload.
- `tests/unit/workers/code_authoring/test_budget.py` — 12 tests (see below).

**API:**

```python
@dataclass(frozen=True, slots=True)
class BudgetState:
    """Snapshot of the trailing-7d budget ledger."""

    loc_used: int
    loc_cap: int
    tokens_used: int
    token_cap: int
    cents_used: int
    cents_cap: int | None  # None when TRELLIS_LLM_BUDGET_CENTS_WEEK unset
    window_start: datetime
    window_end: datetime

    @property
    def loc_exhausted(self) -> bool:
        return self.loc_used >= self.loc_cap

    @property
    def tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.token_cap

    @property
    def any_exhausted(self) -> bool:
        return self.loc_exhausted or self.tokens_exhausted or (
            self.cents_cap is not None and self.cents_used >= self.cents_cap
        )


class _BudgetLedger:
    """Reads + writes BUDGET_CONSUMED / BUDGET_EXHAUSTED events.

    Stateless — every call reads the operational EventLog. No in-memory
    cache: a multi-process spawn-coder cron would risk cache desync if
    two cycles started concurrently. The EventLog is authoritative.
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        loc_cap: int,
        token_cap: int,
        cents_cap: int | None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None: ...

    def check(self) -> BudgetState:
        """Return the current state. Does not raise.

        Caller decides whether to spawn based on `state.any_exhausted`.
        """

    def record_consumption(
        self,
        *,
        run_id: str,
        proposal_id: str,
        loc_delta: int,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        """Emit a BUDGET_CONSUMED event. Always emits, even for failed runs.

        cents_estimated is computed from a frozen rate table at module level;
        operators do not configure rates (rate changes are model-version-dependent
        and belong in an ADR amendment, not an env var).
        """

    def record_exhaustion(
        self,
        *,
        run_id: str,
        proposal_id: str | None,
        reason: Literal["pre_spawn", "mid_run"],
    ) -> None:
        """Emit a BUDGET_EXHAUSTED event."""
```

**Test list (12):**

1. Empty ledger → all-zero state, not exhausted.
2. One BUDGET_CONSUMED event at 500 LOC → state.loc_used=500, not exhausted.
3. Two BUDGET_CONSUMED events summing to 1000 LOC at default cap → exhausted.
4. Two BUDGET_CONSUMED events summing to 1001 LOC → still exhausted (≥ check).
5. Event older than 7d window → excluded from sum.
6. Event at the window boundary (exactly 7d ago) → included (inclusive on the older side).
7. `record_consumption(loc_delta=0, tokens=N)` → emits with zero LOC; failed-run accounting works.
8. `cents_cap=None` → `any_exhausted` ignores cents component.
9. `cents_cap=500, cents_used=600` → exhausted on cents.
10. Concurrent writes (two ledgers, same EventLog backend) → both events recorded, ledger sees both on next `check()` (EventLog is authoritative; no race).
11. Bad env value `TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK=abc` → constructor raises `ValueError` with the offending value.
12. JSON schema validation: `BUDGET_CONSUMED` payload validates against `budget_schema.json`; a missing required field raises.

**Estimated size:** ~350 LOC + ~400 LOC tests.

### Phase 2.2 — Sandbox: worktree + allowlist + secret scrub + verification

**Files:**

- `src/trellis_workers/code_authoring/sandbox.py` — context-manager `sandbox()` + helpers.
- `src/trellis_workers/code_authoring/secret_patterns.py` — frozen pattern dict from amendment §2.6.
- `src/trellis_workers/code_authoring/allowlist.py` — `parse_files_allowed()`, `verify_diff_allowlist()`, hard exclusion set from amendment §2.5.
- `tests/unit/workers/code_authoring/test_sandbox.py` — 14 tests.
- `tests/unit/workers/code_authoring/test_allowlist.py` — 10 tests.
- `tests/unit/workers/code_authoring/test_secret_patterns.py` — 9 tests (one per pattern + one each for false-positive and clean-diff cases).

**API:**

```python
@contextmanager
def sandbox(
    proposal_id: str,
    *,
    base_sha: str,
    files_allowed: Sequence[str],
    base_dir: Path = Path("agent-proposals"),
) -> Iterator[Worktree]:
    """Create a fresh git worktree at base_sha; preserve on exit (no auto-cleanup).

    Yields a Worktree object. On context exit:
    - The worktree directory is NOT removed.
    - The branch is NOT deleted.
    - The lock file is removed (so a non-concurrent re-run is possible).
    """


def verify_diff_allowlist(
    worktree: Worktree,
    files_allowed: Sequence[str],
) -> None:
    """Raise AllowlistViolation if `git diff --name-only base..HEAD` has any
    path outside files_allowed. Path comparison is glob-based; symlink
    resolution happens before comparison.
    """


def scan_diff_for_secrets(
    diff_text: str,
) -> list[SecretMatch]:
    """Return all matches of patterns in SECRET_PATTERNS. Empty list = clean."""


def run_verification(
    worktree: Worktree,
) -> None:
    """Run `make lint` then `make test` in the worktree. Raise on non-zero exit
    with stderr_excerpt populated. The two commands run serially — a lint
    failure short-circuits before tests run.
    """
```

**Test list (sandbox, 14):**

1. `sandbox()` creates the worktree dir + branch.
2. Re-entering the same proposal_id when a lock file exists → raises `ConcurrentSpawn`.
3. Worktree dir preserved after context exit even on internal raise.
4. Worktree branch is namespaced under `agent-proposals/`.
5. Worktree initialized at the specified `base_sha`, not local main HEAD.
6. `files_allowed` containing `..` → parse-time raise.
7. `files_allowed` containing absolute path → parse-time raise.
8. `files_allowed` matching a hard-excluded path → parse-time raise.
9. Allowed file written + committed → `verify_diff_allowlist` passes.
10. Out-of-allowlist file written → `verify_diff_allowlist` raises with the violating path.
11. Symlink pointing outside worktree → resolution rejects.
12. `verify_diff_allowlist` glob-matches: `src/trellis/extract/*.py` matches `src/trellis/extract/llm.py` but not `src/trellis/mutate/executor.py`.
13. `run_verification` with passing lint + tests → returns None.
14. `run_verification` with failing lint → raises with stderr; tests are not run.

**Test list (allowlist, 10):**

1. Parse empty list → empty tuple (legal? amendment says deny-by-default, so legal but no file is writeable; this case fails at spawn time but parses fine).
2. Parse single glob → single-tuple.
3. Glob with `**` → expanded correctly.
4. Hard-excluded `src/trellis_api/auth.py` literal → parse raise.
5. Hard-excluded glob `**/auth*.py` matches `src/trellis_api/auth.py` → parse raise.
6. Hard-excluded `.github/workflows/**` → parse raise.
7. `*_secret_*.py` → parse raise.
8. Glob with `..` → parse raise.
9. Absolute path → parse raise.
10. Diff with all paths allowlisted → verification passes.

**Test list (secret_patterns, 9):**

1–8. One match-positive test per pattern (each pattern fires on a known good example).
9. Diff with no secret-shaped tokens → empty match list.

False-positive cases (e.g., `password=` in a docstring within a `tests/fixtures/` file) are explicitly **not** suppressed — the design is to raise on every match. Test 9 validates the no-secrets case; the false-positive recovery path is operator-driven.

**Estimated size:** ~600 LOC + ~700 LOC tests.

### Phase 2.3 — Claude Code author adapter

**Files:**

- `src/trellis_workers/code_authoring/claude_code_author.py` — `ClaudeCodeAuthor` class.
- `tests/unit/workers/code_authoring/test_claude_code_author.py` — 8 tests with the SDK mocked.

**API:**

```python
@dataclass(frozen=True, slots=True)
class AuthorshipResult:
    proposal_id: str
    run_id: str
    outcome: Literal["succeeded", "failed", "empty"]
    pr_url: str | None  # populated only on succeeded
    loc_delta: int
    tokens_input: int
    tokens_output: int
    stop_reason: str  # SDK-reported


class ClaudeCodeAuthor:
    def __init__(
        self,
        *,
        sdk_version: str,
        timeout_seconds: int = 1800,
        on_token_update: Callable[[int, int], None] | None = None,
    ) -> None: ...

    def author(
        self,
        worktree: Worktree,
        proposal: Proposal,
        budget_state: BudgetState,
    ) -> AuthorshipResult:
        """Spawn Claude Code against the worktree.

        Calls `on_token_update(tokens_input, tokens_output)` after each
        SDK turn. The caller wires this to a mid-run budget-cap check
        that can cancel the SDK if remaining headroom is exhausted.

        Raises:
            SpawnTimeout: subprocess.TimeoutExpired hit.
            BudgetExhausted: on_token_update triggered cancellation.
            ClaudeCodeFailed: non-zero exit with no salvageable diff.
        """
```

**Test list (8):**

1. Happy-path: SDK reports clean exit + non-empty diff → `outcome="succeeded"`.
2. SDK exits cleanly with empty diff → `outcome="empty"`.
3. SDK timeout → raises `SpawnTimeout`.
4. SDK non-zero exit → raises `ClaudeCodeFailed`.
5. `on_token_update` raises `BudgetExhausted` mid-run → SDK cancelled; raises `BudgetExhausted`.
6. SDK reports tokens correctly into result.
7. Env-var scrubbing strips `*_KEY|*_SECRET|*_TOKEN|*_PASSWORD` before spawn (matches `subprocess.run(env=...)` call).
8. `working_directory` is the worktree path; `cwd` not inherited from harness.

**Estimated size:** ~250 LOC + ~350 LOC tests.

### Phase 2.4 — GitHub proposer + ProposalSelector

**Files:**

- `src/trellis_workers/code_authoring/github_proposer.py` — `GitHubProposer` class.
- `src/trellis_workers/code_authoring/selector.py` — `ProposalSelector` class.
- `tests/unit/workers/code_authoring/test_github_proposer.py` — 6 tests.
- `tests/unit/workers/code_authoring/test_selector.py` — 8 tests.

**GitHubProposer API:**

```python
@dataclass(frozen=True, slots=True)
class PRProposeResult:
    pr_url: str
    pr_number: int
    base_sha: str
    head_branch: str


class GitHubProposer:
    def __init__(
        self,
        *,
        gh_cli_path: str = "gh",
        dry_run: bool = False,
    ) -> None: ...

    def propose(
        self,
        *,
        worktree: Worktree,
        proposal: Proposal,
        authorship_result: AuthorshipResult,
    ) -> PRProposeResult:
        """Open a draft PR via `gh pr create --draft --base main`.

        Body is proposal.markdown + an appended autonomous-spawn-provenance section.
        Raises GhPrCreateFailed on non-zero exit; stderr_excerpt is bounded at 200 chars.
        """
```

**Test list (github_proposer, 6):**

1. Happy path: `gh` invocation correct; PR URL parsed from stdout.
2. `--draft` flag present in invocation; absence is a test failure.
3. `--base main` only; alternative bases raise at the wrapper.
4. Body includes the provenance suffix (SHA, SDK version, budget consumed, run_id).
5. `gh` non-zero exit raises `GhPrCreateFailed` with stderr.
6. `dry_run=True` returns a fake PRProposeResult with `pr_url="(dry-run)"` and does not invoke `gh`.

**ProposalSelector API:**

```python
class ProposalSelector:
    def __init__(
        self,
        *,
        event_log: EventLog,
        cooldown_days: int = 30,
    ) -> None: ...

    def pick(self) -> Proposal | None:
        """Return the next eligible proposal, or None if nothing is eligible.

        Eligibility: PROPOSAL_DRAFTED in the last <window>, no
        PROPOSAL_AUTHORSHIP_REQUESTED in the trailing cooldown_days,
        no idempotency.lock present.

        Tie-break: highest source_event_count, then oldest created_at.
        """
```

**Test list (selector, 8):**

1. Empty event log → returns None.
2. One eligible proposal → returns it.
3. One proposal but `PROPOSAL_AUTHORSHIP_REQUESTED` 7d ago → ineligible (cooldown).
4. One proposal but `PROPOSAL_AUTHORSHIP_REQUESTED` 31d ago → eligible.
5. Two proposals with different `source_event_count` → higher count wins.
6. Two proposals with equal `source_event_count` → older `created_at` wins.
7. Lock file present → ineligible.
8. Mixed lifecycle (DRAFTED + UPDATED for same id) → uses most-recent payload data.

**Estimated size:** ~300 LOC + ~400 LOC tests.

### Phase 2.5 — CLI surface + eval scenario

**Files:**

- `src/trellis_cli/admin_spawn_coder.py` — the typer subcommand.
- `src/trellis_cli/cli.py` — register the subcommand.
- `tests/unit/cli/test_admin_spawn_coder.py` — 14 tests with all the I/O mocked.
- `eval/scenarios/spawn_coder_dry_run/__init__.py`, `scenario.py` — synthetic harness exercise.
- `tests/unit/eval/test_spawn_coder_dry_run.py` — 4 tests.

**CLI signature:**

```
trellis admin spawn-coder [OPTIONS]

  Autonomous coding-agent loop (Cohort 2). Defaults to a cycle that picks
  one eligible proposal and runs the spawn → verify → propose-PR flow.

Options:
  --cycle / --no-cycle              Run a single cycle (default: --cycle).
  --proposal-id TEXT                Author this specific proposal (overrides
                                    selector). Mutually exclusive with --cycle.
  --record-review-pass INTEGER      Record gate-(a) compliance with N reviewed
                                    proposals (must be >= 5). Exits after
                                    writing COHORT2_REVIEW_RECORDED event.
                                    Mutually exclusive with --cycle and
                                    --proposal-id.
  --cleanup TEXT                    Remove worktree at agent-proposals/<id>/.
                                    Exits after cleanup; no spawn occurs.
                                    Mutually exclusive with all spawn options.
  --dry-run                         Run selection + budget check; print what
                                    WOULD be spawned. Does not invoke Claude
                                    Code; does not invoke `gh`.
  --loc-cap INTEGER                 Override TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK.
  --token-cap INTEGER               Override TRELLIS_AUTONOMOUS_SPAWN_TOKEN_CAP_WEEK.
  --pr-loc-ceiling INTEGER          Override TRELLIS_AUTONOMOUS_SPAWN_PR_LOC_CEILING.
  --timeout-seconds INTEGER         Override the 1800s spawn timeout.
  --format [text|json]              Output format. Default: text.
  --help                            Show this message and exit.

Environment variables read:
  TRELLIS_AUTONOMOUS_SPAWN_ENABLED       Kill switch. Default: false.
  TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK  Default: 1000.
  TRELLIS_AUTONOMOUS_SPAWN_TOKEN_CAP_WEEK Default: 1000000.
  TRELLIS_AUTONOMOUS_SPAWN_PR_LOC_CEILING Default: 300.
  TRELLIS_LLM_BUDGET_CENTS_WEEK          Optional upper bound on cents (orig ADR).
  GH_TOKEN                                Required for PR creation; never persisted.

Exit codes:
  0  Success — PR opened, or empty diff (PROPOSAL_AUTHORSHIP_EMPTY), or
     no eligible proposal (selector returned None — INFO log, no-op),
     or --record-review-pass / --cleanup / --dry-run completed.
  1  Internal error (uncaught exception, kill switch off, gate (a) not
     satisfied). Also: budget exhausted, allowlist violation, secret
     scrub match, spawn timeout, lint/test failure, gh pr create failure.
  2  Usage error (mutually exclusive flags, --record-review-pass < 5,
     malformed env var values).
  5  Backend / EventLog failure during read or write.

Examples:
  # Default scheduled cycle (cron / systemd timer / scheduled-tasks MCP):
  TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true trellis admin spawn-coder

  # Record review-pass acceptance (one-time gate (a) clearance):
  trellis admin spawn-coder --record-review-pass 8

  # Author a specific proposal (operator override):
  TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true \
    trellis admin spawn-coder --proposal-id prop-abc123

  # Inspect what the next cycle would do without running it:
  TRELLIS_AUTONOMOUS_SPAWN_ENABLED=true \
    trellis admin spawn-coder --dry-run --format json

  # Drop the worktree for a closed proposal:
  trellis admin spawn-coder --cleanup prop-abc123
```

**Test list (CLI, 14):**

1. Kill switch off + `--cycle` → exits 1 with the kill-switch message.
2. Gate (a) not recorded + kill switch on + `--cycle` → exits 1 with "gate (a) not yet recorded".
3. `--record-review-pass 3` → exits 2 (must be ≥ 5).
4. `--record-review-pass 8` → emits `COHORT2_REVIEW_RECORDED` event; exits 0.
5. Kill switch on + gate (a) recorded + selector returns None → exits 0 with INFO log "no eligible proposal" and no spawn.
6. Mutually-exclusive flag pairs (`--cycle` + `--proposal-id`, `--cycle` + `--cleanup`, etc.) → exits 2.
7. Budget exhausted at spawn time → emits `BUDGET_EXHAUSTED`; exits 1.
8. Allowlist violation in diff → emits `PROPOSAL_AUTHORSHIP_FAILED` with `reason="allowlist_violation"`; exits 1.
9. Secret scrub match → emits `SECRET_SCRUB_TRIGGERED`; exits 1.
10. Spawn timeout → emits `PROPOSAL_AUTHORSHIP_FAILED` with `reason="spawn_timeout"`; exits 1.
11. Empty diff → emits `PROPOSAL_AUTHORSHIP_EMPTY`; exits 0.
12. Happy path → emits `PROPOSAL_AUTHORSHIP_SUCCEEDED` + `BUDGET_CONSUMED`; exits 0; PR URL printed.
13. `--dry-run` → no spawn; prints selected proposal + budget state; exits 0.
14. Bad env var `TRELLIS_AUTONOMOUS_SPAWN_LOC_CAP_WEEK=abc` → exits 2.

**Test list (eval scenario, 4):**

1. Inject 3 `PROPOSAL_DRAFTED` events; run `spawn-coder --dry-run` → selector picks highest-count one; no spawn fires.
2. Inject 1 `PROPOSAL_DRAFTED` + recent `PROPOSAL_AUTHORSHIP_REQUESTED` → selector returns None; exit 0.
3. Inject budget-exhausted state (1100 LOC consumed in last 7d) → `--cycle` exits 1; `BUDGET_EXHAUSTED` event emitted.
4. Inject 1 proposal + fresh budget → `--cycle` with mocked Claude Code + mocked `gh` → happy-path event chain (`PROPOSAL_AUTHORSHIP_REQUESTED` → `PROPOSAL_AUTHORSHIP_SUCCEEDED` → `BUDGET_CONSUMED`).

**Estimated size:** ~450 LOC CLI + ~350 LOC tests + ~250 LOC eval scenario.

## 4. Total size estimate

| Phase | LOC code | LOC tests |
|---|---:|---:|
| 2.1 — Budget ledger + EventTypes | 350 | 400 |
| 2.2 — Sandbox + allowlist + scrubber | 600 | 700 |
| 2.3 — Claude Code author | 250 | 350 |
| 2.4 — GitHub proposer + selector | 300 | 400 |
| 2.5 — CLI + eval scenario | 700 | 450 |
| **Total** | **~2200** | **~2300** |

Phases 2.1 and 2.2 land in any order (no inter-dep). Phase 2.3 depends on 2.2 (uses Worktree). Phase 2.4 depends on 2.1 (selector reads BUDGET_CONSUMED). Phase 2.5 depends on all four. Swarm-decomposable as `(2.1 || 2.2) → 2.3 → 2.4 → 2.5`.

## 5. Budget-ledger JSON schema

Inline schema for the `BUDGET_CONSUMED` event payload. The harness validates against this before emitting; the eval scenario validates EventLog rows match this.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://trellis.ai/schemas/coding-agent-loop/budget_consumed.json",
  "title": "BUDGET_CONSUMED payload",
  "description": "Recorded after every spawn-coder authoring run (success, failure, or empty diff). Persisted to the operational EventLog. Cohort 2 amendment §2.4.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "run_id",
    "proposal_id",
    "loc_delta",
    "tokens_input",
    "tokens_output",
    "cents_estimated",
    "window_start",
    "window_end"
  ],
  "properties": {
    "run_id": {
      "type": "string",
      "description": "ULID identifying the spawn-coder invocation. Stable across all events of the run.",
      "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"
    },
    "proposal_id": {
      "type": "string",
      "description": "SHA-256 hex digest of the originating cluster signature. Matches Proposal.proposal_id.",
      "pattern": "^[0-9a-f]{64}$"
    },
    "loc_delta": {
      "type": "integer",
      "description": "Insertions + deletions in this run's diff vs. base_sha. Failed runs and empty diffs record 0.",
      "minimum": 0
    },
    "tokens_input": {
      "type": "integer",
      "description": "Input tokens consumed by Claude Code SDK in this run. Reported by SDK.",
      "minimum": 0
    },
    "tokens_output": {
      "type": "integer",
      "description": "Output tokens generated by Claude Code SDK in this run. Reported by SDK.",
      "minimum": 0
    },
    "cents_estimated": {
      "type": "integer",
      "description": "tokens_input * input_rate_per_1m + tokens_output * output_rate_per_1m, rounded up. Rate table frozen in module; updates via ADR amendment.",
      "minimum": 0
    },
    "window_start": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 UTC. window_end - 7d at the moment of record."
    },
    "window_end": {
      "type": "string",
      "format": "date-time",
      "description": "ISO 8601 UTC. Same as the event's recorded_at."
    }
  }
}
```

Validation rules beyond the JSON schema (enforced by `_BudgetLedger.record_consumption`):

- `window_end >= window_start` (7d span; equality only on a clock skew edge).
- `loc_delta == 0` is legal (failed-run accounting); `tokens_input + tokens_output == 0` is legal for runs that fail before reaching Claude Code (kill switch, gate-(a) miss, budget pre-check fail).
- `cents_estimated == 0` only when both token counts are zero (no rate table can produce nonzero cents from zero tokens).

The schema lives at `src/trellis_workers/code_authoring/budget_schema.json` and is loaded via `importlib.resources` at module import. The schema file is the contract; code changes that drift from it are caught by Phase 2.1 test 12.

## 6. EventLog schemas for the 9 new EventTypes

Restated from the amendment §2.9 for implementer convenience. Each is added to `src/trellis/stores/base/event_log.py` with a docstring matching the existing `PROPOSAL_DRAFTED` style.

| EventType | source | entity_id | entity_type | payload required fields |
|---|---|---|---|---|
| `BUDGET_CONSUMED` | `trellis_workers.code_authoring.budget` | `<run_id>` | `"AuthorshipRun"` | per schema in §5 |
| `BUDGET_EXHAUSTED` | `trellis_workers.code_authoring.budget` | `<run_id>` | `"AuthorshipRun"` | `run_id, proposal_id (nullable), reason ("pre_spawn"\|"mid_run"), loc_used, loc_cap, tokens_used, token_cap, cents_used, cents_cap (nullable)` |
| `PROPOSAL_AUTHORSHIP_REQUESTED` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, run_id, base_sha, files_allowed (list[str]), started_at` |
| `PROPOSAL_AUTHORSHIP_SUCCEEDED` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, run_id, pr_url, pr_number, loc_delta, tokens_input, tokens_output` |
| `PROPOSAL_AUTHORSHIP_FAILED` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, run_id, reason (closed enum), detail (str), stderr_excerpt (str, optional)` |
| `PROPOSAL_AUTHORSHIP_EMPTY` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, run_id, claude_code_stop_reason` |
| `PROPOSAL_AUTHORSHIP_BLOCKED` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, conflicting_run_id, requested_run_id` |
| `SECRET_SCRUB_TRIGGERED` | `trellis_workers.code_authoring.author` | `<proposal_id>` | `"Proposal"` | `proposal_id, run_id, match_count, pattern_names (list[str]), file_paths (list[str])` |
| `COHORT2_REVIEW_RECORDED` | `trellis_cli.admin_spawn_coder` | `<operator_id>` | `"Operator"` | `reviewed_count, operator_id, accepted_at, notes (str)` |

The closed-enum `reason` values for `PROPOSAL_AUTHORSHIP_FAILED`:

- `"spawn_timeout"`
- `"allowlist_violation"`
- `"secret_scrub_match"`
- `"loc_ceiling_exceeded"`
- `"lint_failed"`
- `"test_failed"`
- `"gh_pr_create_failed"`
- `"claude_code_nonzero_exit"`

Adding a new failure reason is a code change + amendment update; the enum is closed.

## 7. Done when

- All 5 phases' PRs merged to main.
- `trellis admin spawn-coder --record-review-pass 5+` records the gate-(a) event.
- `trellis admin spawn-coder --cycle --dry-run` against a synthetic EventLog selects + prints the expected proposal.
- Mocked end-to-end test: inject one cluster → generator emits proposal → spawn-coder picks it → mocked Claude Code returns diff → mocked `gh` returns PR URL → all 4 expected events recorded (`PROPOSAL_AUTHORSHIP_REQUESTED` → `BUDGET_CONSUMED` → `PROPOSAL_AUTHORSHIP_SUCCEEDED`).
- `make lint && make typecheck && make test` green on main.
- The eval scenario `eval/scenarios/spawn_coder_dry_run/scenario.py` passes via `python -m eval.runner spawn_coder_dry_run`.
- `TRELLIS_AUTONOMOUS_SPAWN_ENABLED=false` (default) → all `spawn-coder` invocations exit 1 cleanly with the kill-switch message; no spawn attempted in any test.

## 8. Cleanup considerations

- `agent-proposals/` is already in `.gitignore` (Cohort 1).
- `agent-proposals/<id>/worktree/` is **not** in `.gitignore` directly — it's a git worktree, so git's own machinery skips it. The `.git/worktrees/<id>` index entry is the canonical handle; manual cleanup uses `git worktree remove agent-proposals/<id>/worktree` which `trellis admin spawn-coder --cleanup <id>` wraps.
- A `agent-proposals/`-wide `du -sh` after a month of running is a reasonable operator-driven cleanup checkpoint. No automated retention.
- Idempotency lock files at `agent-proposals/<id>/idempotency.lock` are auto-removed at sandbox context exit; a stale lock (process killed mid-run) requires manual `rm`.

## 9. Risks

- **Claude Code SDK API drift.** Same as Cohort 1 risk; tighter on Cohort 2 because spawn flow is more elaborate. Mitigation: pinned SDK version + `sdk_version` recorded on every event + a CI smoke test that exercises the spawn flow against a stub SDK.
- **`gh` CLI version skew.** Different `gh` versions produce different stdout shapes. Mitigation: parse `gh pr create --json url,number` (machine-readable) instead of regex on text; pin a minimum `gh` version in the harness startup check.
- **Allowlist bypass via `git add`-style trickery.** A proposal that gets allowlist `src/foo.py` could in principle `git add` a renamed-but-not-modified version of `src/auth.py`. Mitigation: `git diff --name-only` includes both old and new paths on renames; the diff-level check sees both and rejects.
- **Budget ledger desync under high concurrency.** Two concurrent `spawn-coder --cycle` invocations could both pass the pre-spawn budget check and consume budget that, summed, exceeds the cap. Mitigation: per-proposal lock prevents two cycles from working the same proposal; cap is rolling and conservative enough that a one-cycle overshoot does not breach the operator's intent.
- **Secret-scrub false positives in test fixtures.** A test fixture containing `password = "test_password_123"` would trigger the scrubber. Mitigation: deny-by-default is the design; the operator-recovery path is to edit the proposal markdown to exclude the fixture-touching file from `files_allowed`, or to fix the fixture to use a placeholder that doesn't match the password pattern.
- **`make test` flakes block legitimate PRs.** Verification-gate semantics mean a flaky test fails an entire authoring run. Mitigation: same as the human-developer experience — fix the flake. The harness does not auto-retry.
- **30-day cooldown lets a stale proposal block the queue.** If proposal A has been authoring-requested 29 days ago but the author run is stuck, no `--cycle` will pick a different proposal. Mitigation: `ProposalSelector` does not block on the stuck proposal — it picks the *next eligible* one; the stuck proposal simply waits its turn. The operator can `--cleanup` the stuck worktree to free it.

## 10. What this doesn't include

| Deferred | Reason |
|---|---|
| Auto-merge of any PR | Out of scope by ADR §2.5 + amendment §2.7. Hard-coded refusal. |
| PR comment continuation flow | Open question in amendment §7. |
| Cross-repo proposals | Out of scope by ADR §6 alternative. |
| Cross-cycle learning from merged PRs | Open question in amendment §7. |
| Multi-PR proposals | Open question in amendment §7. |
| Auto-cleanup of failed worktrees | Out of scope by amendment §3.7 (forensic data preservation). |
| Auto-retry on transient `gh` failures | Out of scope by amendment §3.8 (no silent fallbacks). |
| Per-failure-mode notification (Slack, email) | Operator wires that via the EventLog if they want it; not first-class. |
| Multi-tenant operator scopes | The harness assumes one operator per deployment; if that changes a future amendment scopes per-operator budget ledgers. |

All deferred items are explicitly "validate before designing" per the program's POC discipline.
