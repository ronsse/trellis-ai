# Roadmap-Driver — Cloud Routine Playbook

**What this is.** The committed operating procedure for the Anthropic-cloud `/schedule` routine (Layer 1 of the roadmap-driver program) that keeps the `Productionization` board coherent and reports progress. The routine prompt is thin and points here; this file is the spec. Program plan: `~/.claude/plans/lets-plan-how-you-synchronous-sunrise.md` (owner-approved 2026-07-15). Memory: `trellis-roadmap-driver-2026-07`.

**Architecture — GitHub is the only shared bus.** This routine runs in Anthropic's cloud and can reach **github.com for both `ronsse/trellis-ai` and `ronsse/skynet-hub`** — but **cannot** reach the skynet host (tailnet-only). Live-instance reality (loop-starvation metric, container drift, `:8420` bind, backups) is produced by the **skynet Layer-2 job** (`skynet-hub/stacks/trellis/roadmap-nightly.sh`) and reaches this routine **only** as fenced-JSON status blocks posted to the board issue. Repo-vs-host is the load-bearing boundary; never assume host reality you can't read from GitHub.

---

## Guardrails (read first — this routine is intentionally low-privilege)

- **Read / label / comment only.** NEVER: edit the roadmap markdown or any design doc; author or modify code; open code PRs; merge, approve, or close PRs; touch the skynet host or its deployment.
- **Human-owned labels are never auto-changed.** The routine computes only `ready` and `blocked:dep`. It must never add or remove `mechanical`, `keystone`, `owner-only`, `blocked:owner-decision`, `blocked:signal`, `ops` — those are set by a human and encode judgment.
- **Idempotent.** Every run recomputes from live GitHub state; running it twice changes nothing the second time.
- **Loud, not silent.** Coherence violations are surfaced as a single warning comment on the board issue, never silently "fixed."
- **Report-only bootstrap.** For the first 2–3 weeks, run in `REPORT_ONLY` mode: compute the label diffs and digest but **do not apply label writes** — just print what it *would* change. Flip to active only after the computed `ready`-set has matched reality across several runs.

**Scope of objects it manages:** `ronsse/trellis-ai` milestone `Productionization` (#1) · `ronsse/skynet-hub` issues labelled `ops` · the pinned board issue **`ronsse/trellis-ai#275`**.

---

## Cadence

| Job | When | Cost |
|---|---|---|
| **A — Coherence + label maintenance** | Daily, 12:00 UTC | gh-only, seconds |
| **B — Milestone digest + %-to-deployer-#2** | Weekly, Monday 13:00 UTC | gh-only (+ optional cold-install check, monthly) |

One `/schedule` routine runs both: Job A every day; Job B additionally on Mondays.

---

## Gate-state label vocabulary

| Label | Meaning | Owner |
|---|---|---|
| `ready` | All dependencies closed; actionable now | **routine (computed)** |
| `blocked:dep` | Blocked on another in-milestone issue | **routine (computed)** |
| `blocked:owner-decision` | Blocked on an owner judgment gate (not code) | human |
| `blocked:signal` | Blocked on an external signal (pilot restart / partner / ≥30d feedback) | human |
| `mechanical` | Small, allowlist-scoped, auto-executable by the Layer-3 executor | human |
| `keystone` | Architecturally load-bearing; human authorship only | human |
| `owner-only` | Not code (1Password / infra / console) | human |
| `ops` | Deployment / skynet-hub half | human |

---

## Job A — Coherence + label maintenance (daily)

1. **Fetch live state.**
   - `gh issue list --repo ronsse/trellis-ai --milestone Productionization --state all --json number,title,state,labels`
   - `gh api repos/ronsse/trellis-ai/milestones/1 --jq .description` — the **Wave DAG** (dependency source of truth).
   - `gh issue list --repo ronsse/skynet-hub --label ops --state all --json number,title,state,labels`
   - Read `docs/design/implementation-roadmap.md` §3.H and §4 from the checked-out repo.

2. **Parse dependency edges.** From the milestone description's `Wave N` blocks and prose (`after #X`, `before #Y`, `needs ~N days`), plus each issue's existing `blocked:dep`. Current live edges (most of the 07-14 Wave DAG has resolved): **#194 depends on #256**; **#261 needs ≥30 days of feedback** (a `blocked:signal`, human-owned — do not auto-clear).

3. **Recompute the `ready` / `blocked:dep` set.** For each **open** milestone issue:
   - If it carries a human-owned `blocked:owner-decision` or `blocked:signal` → leave as-is (do not add `ready`).
   - Else if every dependency issue is **closed** → it should be `ready`.
   - Else → it should be `blocked:dep`.

4. **Apply the diff** (skip in `REPORT_ONLY`): `gh issue edit <n> --add-label ready --remove-label blocked:dep` (and the inverse). Touch only `ready`/`blocked:dep`. Record each change for the run log.

5. **Coherence invariants** (post one consolidated warning comment on #275 if any fails — do not fix the roadmap):
   - Every **open** milestone issue appears in roadmap §3.H (H.0–H.3 or the Adjacent list).
   - Every §3.H checkbox maps to a live issue (open or closed).
   - The §4 gate table names no issue whose status contradicts §3.H.

6. **File missing tracking issues.** If the roadmap names milestone work with no tracking issue, open one (milestoned + gate-state-labelled) and note it in the log. **Never edit the roadmap** — that is the design/narrative layer, human-owned.

7. **Emit the run log** into the digest buffer (what was relabelled, what warnings fired).

---

## Job B — Milestone digest + %-to-deployer-#2 (weekly)

1. **Compute the GitHub-verifiable DoD criteria** (see table): 
   - **#2** — `gh run list --repo ronsse/trellis-ai --branch main --limit 8` all green; note the collected test count.
   - **#5 / #7** — #194 / #200–#203 closed with their acceptance PRs merged.
   - **#6** — #250 closed; `gh secret list --repo ronsse/trellis-ai` shows the `TRELLIS_TEST_NEO4J_*` secrets purged.
   - **#8** — #208 closed or re-homed with a disposition comment.
   - **#1** (quickstart cold-install) — heavy; run at most monthly in a clean sandbox (`pip install trellis-ai && trellis admin init && trellis demo load && trellis retrieve pack …`), else carry the last recorded result.

2. **Ingest skynet reality** (criteria **#3, #4, #9, #10, #11**): read the latest fenced-JSON status block posted by Layer 2 on board **#275**. **If its timestamp is >36h old, render those five criteria as `STALE — skynet job silent`, never as pass.**

3. **Compute** combined `% to deployer-#2-ready = criteria met / 11`.

4. **Update the board #275 in place.** Refresh the milestone-state summary + the `%` in the issue body's status section; append a one-line dated entry to a `## Digest log` section (append-only, for history). Prefer editing the body over comment-spam.

5. **Emit** the digest as the run's final output.

---

## DoD — the 11 criteria (`cloud` = this routine verifies; `skynet` = arrives via Layer-2 status block)

| # | Criterion | Surface |
|---|---|---|
| 1 | Quickstart cold-install exits 0 | cloud (monthly deep-check) |
| 2 | `pytest tests/unit/` green + 6 workflows green on main | cloud |
| 3 | Loop unstarved: within 30d of #255, advisories/lessons > 0; curate not all-zeros | skynet |
| 4 | Attribution round-trip: flat `get_context` carries `pack_id`; `record_feedback`→`FEEDBACK_RECORDED` | skynet |
| 5 | #194 enforced (`pytest -k classification`) | cloud |
| 6 | #250 closed: `.env` exists, dead AuraDB creds purged, CI secrets rotated | cloud + owner |
| 7 | #200/#202/#203 fixture tests pass | cloud |
| 8 | #208 re-homed or closed | cloud |
| 9 | trellis-api container current + `llm:` block live (skynet-hub#4) | skynet |
| 10 | `:8420` locked down (skynet-hub#5) | skynet |
| 11 | Backup mirror working (skynet-hub#6) | skynet |

---

## Hand-off contract with the skynet Layer-2 job

- **Cloud → skynet:** this routine maintains `ready` / `blocked:dep`. The Layer-3 executor (skynet) selects `mechanical` + `ready` issues. This routine files/labels `ops` issues; the skynet metrics half verifies and reports against them.
- **Skynet → cloud:** the skynet job posts timestamped fenced-JSON status blocks on **#275** (and on each `ops` issue). This routine reads them for DoD criteria 3/4/9/10/11.
- **GitHub is the only channel.** No direct cloud↔skynet path exists or is required. Silence >36h → `STALE`, never assumed-pass.

---

## Scheduling

Create via `/schedule` (CronCreate). Thin routine prompt:

> Follow `docs/ops/roadmap-driver-cloud-playbook.md`. Run **Job A** every day; additionally run **Job B** on Mondays. Start in `REPORT_ONLY` mode. Output the run log / digest.

Flip out of `REPORT_ONLY` only after the computed `ready`-set has matched reality across ~2 weeks of daily runs. Do not enable until PR #274 (the roadmap reconciliation) is merged — Job A's coherence checks read §3.H from `main`.
