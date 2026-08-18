# Roadmap-Driver — Cloud Routine Playbook

**What this is.** The committed operating procedure for the Anthropic-cloud `/schedule` routine (Layer 1 of the roadmap-driver program) that keeps the `Productionization` board coherent and reports progress. The routine prompt is thin and points here; this file is the spec. Program plan: `~/.claude/plans/lets-plan-how-you-synchronous-sunrise.md` (owner-approved 2026-07-15). Memory: `trellis-roadmap-driver-2026-07`.

**Architecture — GitHub is the only shared bus.** This routine runs in Anthropic's cloud and can reach **github.com for both `ronsse/trellis-ai` and `ronsse/skynet-hub`** — but **cannot** reach the skynet host (tailnet-only). Live-instance reality (loop-starvation metric, container drift, `:8420` bind, backups) is produced by the **skynet Layer-2 job** (`skynet-hub/stacks/trellis/roadmap-nightly.sh`) and reaches this routine **only** as fenced-JSON status blocks posted to the board issue. Repo-vs-host is the load-bearing boundary; never assume host reality you can't read from GitHub.

**Tooling — GitHub MCP tools, not the `gh` CLI.** The Anthropic-cloud sandbox this routine runs in does not have `gh` installed (confirmed 2026-08-05: not on `$PATH`, not present anywhere on the filesystem). Every `gh` invocation below is written as the command for readability, but the routine executes it via the equivalent `mcp__github__*` tool — see the mapping table right after the guardrails. One command has **no MCP equivalent**: `gh secret list` (DoD criterion #6, CI-secret purge). That criterion is not computable from this routine; report it as `NEEDS OWNER CHECK`, never as pass/fail. If a future environment ships `gh` authenticated, either tool path is fine — prefer whichever is available; do not install `gh` as a workaround if it's missing.

---

## Guardrails (read first — this routine is intentionally low-privilege)

- **Read / label / comment only.** NEVER: edit the roadmap markdown or any design doc; author or modify code; open code PRs; merge, approve, or close PRs; touch the skynet host or its deployment.
- **Human-owned labels are never auto-changed.** The routine computes only `ready` and `blocked:dep`. It must never add or remove `mechanical`, `keystone`, `owner-only`, `blocked:owner-decision`, `blocked:signal`, `ops` — those are set by a human and encode judgment.
- **Idempotent.** Every run recomputes from live GitHub state; running it twice changes nothing the second time.
- **Loud, not silent.** Coherence violations are surfaced as a single warning comment on the board issue, never silently "fixed."
- **Report-only bootstrap.** For the first 2–3 weeks, run in `REPORT_ONLY` mode: compute the label diffs and digest but **do not apply label writes** — just print what it *would* change. Flip to active only after the computed `ready`-set has matched reality across several runs.

**Scope of objects it manages:** `ronsse/trellis-ai` milestone `Productionization` (#1) · `ronsse/skynet-hub` issues labelled `ops` · the pinned board issue **`ronsse/trellis-ai#275`**.

---

## `gh` → MCP tool mapping

| `gh` command (spec shorthand below) | MCP tool | Notes |
|---|---|---|
| `gh issue list --milestone Productionization --state all --json ...` | `mcp__github__search_issues` — query `repo:ronsse/trellis-ai milestone:Productionization`, `fields: [number,title,state,labels,milestone]` | `list_issues` has no milestone filter; `search_issues` does via query syntax. |
| `gh api repos/.../milestones/1 --jq .description` | Read `milestone.description` off any one result from the `search_issues` call above (`fields` must include `milestone`) | No dedicated milestone-get tool exists. GitHub embeds the full milestone object, description included, on every issue's `milestone` field — one such issue is enough. |
| `gh issue list --repo ronsse/skynet-hub --label ops --state all --json ...` | `mcp__github__list_issues` — `owner: ronsse, repo: skynet-hub, labels: ["ops"]` | Direct equivalent. |
| `gh issue edit <n> --add-label ready --remove-label blocked:dep` | `mcp__github__issue_write` method `update`, `labels: [...]` | **Not additive** — `issue_write`'s `labels` replaces the full label set (a straight PATCH), unlike `gh issue edit --add-label/--remove-label`. Always read the issue's current labels first (`issue_read` method `get_labels`), compute the new full set with only `ready`/`blocked:dep` swapped, and pass that whole array back. Getting this wrong risks silently dropping a human-owned label (`mechanical`, `keystone`, etc.) — treat it as load-bearing. |
| `gh issue create ... --milestone 1 --label <gate-state>` | `mcp__github__issue_write` method `create`, `milestone: 1`, `labels: [...]` | Direct equivalent. |
| `gh issue edit 275 --body ...` / comment | `mcp__github__issue_write` method `update` (`body: ...`) for in-place board edits; `mcp__github__add_issue_comment` for the coherence-warning comment | Fetch the current body with `issue_read` method `get` first, edit it in memory, write the whole body back — same replace-not-patch caveat as labels. |
| `gh run list --repo ronsse/trellis-ai --branch main --limit 8` | `mcp__github__actions_list` method `list_workflow_runs`, `workflow_runs_filter: {branch: "main"}`, `per_page: 8` | Direct equivalent. |
| `gh secret list --repo ronsse/trellis-ai` | **none** | No MCP tool reads repo secrets (by design — secret *values* and even names are access-sensitive). DoD #6's "CI secrets rotated" half is **owner-only, manual**: the routine reports `NEEDS OWNER CHECK` for that half and computes only the `.env` / dead-AuraDB-creds half it can see from the repo. |

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

4. **Apply the diff** (skip in `REPORT_ONLY`): `gh issue edit <n> --add-label ready --remove-label blocked:dep` (and the inverse) — via `issue_write`, whose `labels` field is a full-set replace, not add/remove; see the mapping table's caveat above. Touch only `ready`/`blocked:dep`. Record each change for the run log.

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
   - **#6** — #250 closed **and** `.env`/dead-AuraDB-creds purge is verifiable from repo contents (cloud-computable). The `TRELLIS_TEST_NEO4J_*` CI-secret-purge half has no MCP equivalent (`gh secret list` cannot be run from this routine) — render that half as `NEEDS OWNER CHECK`, and treat criterion #6 overall as unmet until the owner confirms it, never as a silent pass.
   - **#8** — #208 closed or re-homed with a disposition comment.
   - **#1** (quickstart cold-install) — heavy; run at most monthly in a clean sandbox (`pip install trellis-ai && trellis admin init && trellis demo load && trellis retrieve pack …`), else carry the last recorded result.

2. **Ingest skynet reality** (criteria **#3, #4, #9, #10, #11**): read the latest fenced-JSON status block posted by Layer 2 on board **#275**. **If its timestamp is >36h old, render those five criteria as `STALE — skynet job silent`, never as pass.**

3. **Compute** combined `% to deployer-#2-ready = criteria met / 11`.

4. **Update the board #275 in place.** Refresh the milestone-state summary + the `%` in the issue body's status section; append a one-line dated entry to a `## Digest log` section (append-only, for history). Prefer editing the body over comment-spam.

5. **Emit** the digest as the run's final output.

---

## Reading the Layer-2 `loop_dod3` block (revised 2026-08-16)

The skynet job's `loop_dod3` was rewritten to stop conflating three different
stages of the loop. The pre-08-16 block reported `retrieve precedents` (a
**human-gated** promote *output*) and `advisory-effectiveness.advisory_scores`
(delivery effectiveness, *downstream* of generation) over a 7d window — so it
read `0 / 0 = starved` while generation was demonstrably working at width (2
advisories at 90d; injected coverage 1.0). Read the new 30d block as:

| Field | What it measures | How to read for DoD #3 |
|---|---|---|
| `graded_observations` | graded packs joined (PACK_ASSEMBLED ⋈ FEEDBACK, 30d) | loop **input** flowing when > 0 |
| `promote_candidates` | items recurring across ≥2 graded packs (real `min_support=2`) | the promote-half **generation** — the honest "lessons > 0" signal |
| `curate_signal` | `advisories_generated` / `all-zeros` from the 03:30 curate log | the demote-half **generation** signal ("advisories > 0") |
| `injected_coverage`, `attribution_rate` | the two join-coverage rates (30d) | join is **fed** when injected ≈ 1.0; attribution is item-signal quality |
| `precedents_promoted` | precedents promoted INTO the graph | **human-gated** — 0 until someone runs `learning-candidates → curate promote-learning`. **A 0 here is NOT "starved."** |

**DoD #3 mapping.** "advisories/lessons > 0" is met when `promote_candidates > 0`
**or** `curate_signal == advisories_generated`. Never read `precedents_promoted: 0`
as an unmet loop criterion — it is a human-review output, not a generation
metric. At single-user throughput `promote_candidates` can legitimately sit at 0
(no item recurs across ≥2 graded packs) even with a healthy loop, which is why
the criterion itself is under review — see `dod3-reframe-proposal.md`.

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
