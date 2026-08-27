# Decision ledger

> **Purpose.** Autonomous work hits decisions that would otherwise stop and wait for the
> operator. Stopping makes the operator the bottleneck; guessing makes the work
> unreliable. This ledger is the third path: **the swarm never blocks on a decision.** It
> resolves what it may, records what it can't with a *recommendation*, and keeps going.
> The operator reviews this file in batch rather than being interrupted per-fork.
>
> Every entry is written to be actionable months later by someone who was not there.

## Protocol

When an agent hits a fork:

1. **Do I have a defensible answer?** If yes, take it and record it under *Taken* only if
   it is architecturally consequential. Routine calls do not belong here — a ledger that
   records everything gets read by nobody.
2. **Is it reversible?** (`git revert` undoes it.) If yes → `decision-panel` skill.
   - **Unanimous** → act, record under *Taken*, note the panel's `what_would_change_my_mind`
     as the reopen condition. **Verify every file/API/measurement claim the panel made
     before acting on it.**
   - **Split** → do *not* tie-break. Record under *Pending* with both positions, ship the
     safe default (usually: the feature exists but is off), and continue.
3. **Is it irreversible?** — publishing, deleting or redacting production data, credential
   operations, spend, history rewrite, repo settings. **Never panel-eligible.** Record
   under *Pending* with a recommendation, ship nothing that presumes an answer, continue
   with the rest of the item.

**The bar for *Pending*: an entry must carry a recommendation.** "This needs your input"
is not a ledger entry; it is a shrug. Name the options, say which one you would pick and
why, and state what it would cost to be wrong.

### Categories

| Category | Meaning |
|---|---|
| **Taken** | Resolved autonomously. Includes the reopen condition. No action needed unless that condition fires. |
| **Pending — decision** | A genuine fork. Needs a choice. Has a recommendation and a default already shipped. |
| **Pending — access** | Blocked on something only the operator can reach (a console, a credential, a paid account). Not a judgement call. |
| **Deferred** | Noticed, not urgent, not blocking. Revisit when the named trigger fires. |

---

## Pending — decision

### D-1 · `TRELLIS_REQUIRE_PACK_ATTRIBUTION`: refuse, warn, or degrade?

**Context.** When a pack *was* served and the caller supplies no item citations, should
`record_feedback` reject the call?

**Panel: SPLIT** (2026-08-26) — `openai/gpt-5.5` → refuse (0.78): only route callers to a
joinable call, and don't emit a second event for the same pack. `moonshotai/kimi-k3` →
degrade (0.62): the ceiling is ~1 event, and refusing risks losing the
highest-information event in the corpus. Both agreed a "warn + follow-up call" pattern
double-counts unless superseding is built.

**Shipped default:** the flag exists, **off**. Production behaviour unchanged. Turning it
on rejects an uncited pack-targeted call and returns the pack's *real* served ids in the
error. Fails open for unknown packs, sectioned packs, and event-log outages; never touches
trace-level feedback.

**Recommendation: leave it off.** Measured after the fact: `pack_attribution_rate` is
**0.933** (14 of 15). Callers holding item ids already supply them. This flag was built for
a problem worth **one event over 30 days**. The real constraint is retrieve-adoption —
24 of 39 feedback events describe work where no pack was ever served — and no setting of
this flag can reach that.

**Cost of being wrong:** one env var either way. Reversible in a deploy.

### D-2 · Is DoD-3b a launch blocker for deployer #2, or a maturity gate?

**Context.** [#304](https://github.com/ronsse/trellis-ai/pull/304) split the definition of
done: **3a** (loop fed & wired — coverage ≥0.9, attribution ≥0.2, ≥5 graded observations,
curate non-zero) **passes today**. **3b** (standing-memory output — ≥1 promoted precedent)
is `blocked:signal`: at single-user volume no memory item recurs across two graded packs,
so the promote path has never executed in production.

**Why this is not panel-eligible.** It decides whether the product ships to another person.
Reputation is not `git revert`-able.

**Recommendation: maturity gate, not a launch blocker.** 3b measures *throughput*, and a
second deployer is the thing that produces throughput — blocking on it is circular. The
honest posture is to ship with 3a met and 3b declared openly as unproven, rather than to
lower `min_support` until a number moves (explicitly rejected — see T-1).

**Cost of being wrong:** deployer #2 finds the promote half inert. Mitigated by
[#342](https://github.com/ronsse/trellis-ai/issues/342), which exercises that chain in
evals so it is at least known-working rather than merely unexercised.

### D-3 · Require status checks on `main`?

**Context.** `main` has **no required status checks** and `required_approving_review_count:
0`; `allow_auto_merge` is false. Every CI workflow exists and runs on PRs — **nothing
enforces the result.** The gate that held all through 2026-08-26, including a multi-hour
GitHub Actions outage, was orchestration discipline, not a control.

**Why this is not panel-eligible.** Repo settings.

**Recommendation: require the eight PR checks** (`lint`, `typecheck`, `test (3.11/3.12/3.13)`,
`openapi-check`, `CodeQL`, `Analyze (python)`) and enable auto-merge. With required checks
present, auto-merge becomes safe and the orchestrator stops being the only thing standing
between an agent and `main`.

**Cost of being wrong:** during an Actions outage nothing can merge at all. Given the
alternative is *unverified merges* during an outage, that is the better failure.

**Measured state** (2026-08-27): branch protection *does* exist on `main` — force-pushes
and deletions are already blocked — but it carries **no `required_status_checks` key at
all**, and `allow_auto_merge` is `false`.

**Runnable, if the answer is yes.** `strict: true` is the mechanised form of the
"green against *current* `main`" rule in [`swarm-handoff.md`](./swarm-handoff.md) §4 —
it makes GitHub enforce the thing an orchestrator has so far been enforcing by hand.

```bash
gh api --method PUT /repos/ronsse/trellis-ai/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "typecheck", "test (3.11)", "test (3.12)", "test (3.13)",
                 "openapi-check", "CodeQL", "Analyze (python)"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null
}
JSON
gh api --method PATCH /repos/ronsse/trellis-ai --field allow_auto_merge=true
```

**Two honest caveats**, neither fatal:
- The matrix legs are pinned **by name**. Dropping 3.11 or adding 3.14 to
  `.github/workflows/tests.yml` blocks every merge until this payload is updated too.
  That coupling is a real maintenance cost, not a hypothetical one.
- `strict: true` means every PR must be rebased onto `main` before merging, so a busy
  queue serialises. At current volume (7 PRs in a day, one merger) that is free; it
  would not be with ten concurrent agents.

## Pending — access

### A-1 · Delete Aura API credentials `985676d4` / `d664924e`

[#250](https://github.com/ronsse/trellis-ai/issues/250), last task standing. Needs a
browser session at <https://console.neo4j.io> → Account → API credentials. No CLI or API
path exists. The other three tasks were verified done 2026-08-26. Instance re-confirmed
`NXDOMAIN`, so there is no live exposure — this is credential-surface hygiene.

### A-2 · Dispose of [#208](https://github.com/ronsse/trellis-ai/issues/208)

Pilot-infra blockage (ArcadeDB secret + expired AWS SSO), not a trellis-ai code defect.
**Recommendation: re-home to the consumer-kg repo** rather than close. Closing an issue
that still carries an unresolved gate is how [#312](https://github.com/ronsse/trellis-ai/issues/312)
hid an owner decision for three days — gate labels are only read on *open* issues.

---

## Taken

### T-1 · No production `min_support=1` flag; exercise the promote path in evals

**Panel: unanimous** (2026-08-26, $0.0144) — `openai/gpt-5.5` 0.78, `moonshotai/kimi-k3`
0.70. Lowering the production threshold manufactures promotions: the promote path would
*look* validated while testing nothing. Filed as
[#342](https://github.com/ronsse/trellis-ai/issues/342).

**Verified before acting** — both panelists warned eval fixtures might stub promotion and
become their own always-passing constant. Checked:
`eval/scenarios/parameter_registry_passthrough.py:160` calls the *real*
`analyze_learning_observations`, already with `min_support=1`. Not a stub, so the decision
holds; but that scenario asserts the analyzer's return shape, not the
promote→review→serve chain. **The test refined the scope rather than flipping the answer.**

**Reopen if:** an integration audit shows eval fixtures cannot execute the same promote
pipeline code production uses.

### T-2 · Ship #338's fix wider than the issue specified

The write-through [#338](https://github.com/ronsse/trellis-ai/issues/338) asked for would
have fixed nothing observable. `_build_filters` **returns early when `tag_filters is
None`**, so the noise default was never constructed on the path MCP `get_context` uses
without a `domain` — exclusion held on *neither* axis. Shipping only the specified change
would have made vector rows honest while the reported symptom continued.

**Blast radius, accepted knowingly:** this changes what production packs return for
**every** `get_context` call, not just the 45 divergent rows.

**Reopen if:** pack quality visibly degrades — the change excludes more than intended.

---

## Deferred

### F-3 · Should capture-health warn on a surface that has gone *quiet*, not just one being rejected?

**Measured 2026-08-27**, 7-day window on the reference deployment: `mcp:record_feedback`
shows **0 accepted, 1 rejected**. The grading half of the loop has produced nothing for a
week, and **nothing warned**.

The banner ([`ops/capture_health.py`](../../src/trellis/ops/capture_health.py), #309)
fires on ≥3 rejections **and** zero accepted for a surface. Here rejections were 1, so
the condition was not met — working as specified. But the specified condition cannot
detect a surface that simply stops being called, which for `record_feedback` is the more
likely failure: an agent that never grades produces no rejections at all.

This interacts with everything downstream. `pack_attribution_rate` is 0.875 over 8
pack-targeted events, and no amount of surface ergonomics can move it while nothing is
being graded — the same conclusion A4 reached about retrieve-adoption, one layer over.

**Recommendation: yes, but as a distinct signal — "was active, now silent" rather than
"silent".** Bare silence is not evidence: most surfaces are unused most of the time, and a
banner that fires on every quiet tool is one nobody reads. A surface with a *prior* accept
history that has since gone to zero is a real signal, and it is the shape of the
motivating incident. Keep it advisory, keep it failing soft, and keep it out of
`write_config.py` — like the existing thresholds these are **read-side** knobs.

**Cost of being wrong:** a noisier banner. Reversible in a threshold.

**Trigger:** anyone touching `capture_health.py`, or the next time a write surface goes
dark unnoticed.

### F-2 · `trellis-evals` has no CI at all

Measured 2026-08-27: `ronsse/trellis-evals` (private) has **no `.github/workflows/`
directory** and was last pushed 2026-07-12 — six weeks and roughly 25 merged PRs behind
`trellis-ai` `main`. Nothing runs the eval suite automatically, on any trigger.

This matters more than a normal missing-CI gap because of *what that repo is for*.
Evals are the mechanism that is supposed to catch behavioural regressions in retrieval
and the learning loop — and [#342](https://github.com/ronsse/trellis-ai/issues/342)
proposes putting the promote→review→serve chain there specifically so it is
"known-working rather than merely unexercised" (ledger T-1). A scenario added to a suite
nothing executes is unexercised in exactly the way T-1 was trying to fix.

It also breaks the merge gate: [`swarm-handoff.md`](./swarm-handoff.md) §4 is written
around eight green checks, and an agent opening a PR there has nothing to be green
against.

**Trigger:** before #342 lands, or before any agent is dispatched to that repo. Whoever
takes it should decide whether the suite is cheap enough to run per-PR or needs a
nightly, and whether it can run without live LLM credentials — several scenarios
(`*_real_llm`) plainly cannot.

### F-1 · Sectioned packs cannot contribute per-item rows

`build_sectioned` emits no `injected_items[]`, so sectioned packs (4 of 40 all-time) can
never join the learning loop however carefully an agent cites. Not urgent at 10% of
volume. **Trigger:** sectioned packs exceed ~25% of packs served, or a consumer asks why
their citations vanish.
