# ROADMAP-EDITS (draft) — proposed edit set for `docs/design/implementation-roadmap.md`

```yaml
last-review: 2026-07-11
```

> **This is not a roadmap.** `docs/design/implementation-roadmap.md` remains the authoritative
> planning doc. This file proposes a surgical edit set for it: one new milestone section (§A)
> and corrections for sections that are stale versus the repo today (§B). Each edit:
> location → current gist → proposed change → why. Apply into the roadmap, then delete this file.

## A. Proposed addition — new roadmap section "§3.H — Productionization"

**Location:** `implementation-roadmap.md` §3, after "G — Corpus ingestion" (line ~299).
**Current:** the 7 open GitHub issues exist only as rows in the §4 gate table — no scope, no acceptance checks, so they fail the roadmap's own "fully scoped, a fresh agent can execute" bar.
**Proposed change:** insert the milestone below.
**Why:** the dogfood gate fired 2026-07-11 (roadmap §4 blockquote); the open-issue set is now the milestone between "works on skynet" and "safe to recommend to a second deployer". Security items especially shouldn't live in a gate table: #252 (MCP-over-HTTP) expanded the exposed surface while #194 enforcement stays unbuilt.

### Proposed text

---

### H — Productionization (open-issue closeout)

Goal: close the gap between the live dogfood deployment and a second deployer — security floor first, then the query-history curation primitives, with every item acceptance-checked.

**H.0 — defect half (already queued).** The 2026-07-11 dogfood defect queue (TODO.md § Dogfood gap analysis) is this milestone's other half — cross-reference, don't duplicate. Open GitHub issues for its two [H] items (`domain=` hard-exclusion; Claude Code session auto-capture) so the milestone is fully issue-tracked.

**H.1 — security floor**

- [ ] **#250 credential hygiene** (operator-only): rotate/purge the dead AuraDB credentials in 1Password, delete Aura API client IDs `985676d4`/`d664924e`, recreate repo-root `.env` from `.env.example` — acceptance: #250 closed with its checklist ticked; `test -f .env` locally; `gh secret list --repo ronsse/trellis-ai` shows the `TRELLIS_TEST_NEO4J_*` secrets rotated or removed (CI has been container-based since `586aee6` and no longer needs them). Blocked-on: resolve the AuraDB status contradiction first (edit B5).
- [ ] **#194 classification enforcement, minimal slice**: populate `DataClassification` on write paths; PackBuilder/search filter and mutation policy-gate deny by caller scope — acceptance: new tests green (`pytest tests/unit/ -k classification`) proving (a) a restricted document is excluded from packs and search for an unscoped caller, (b) a mutation touching restricted content is denied without the scope. Caller identity already exists on REST (#242) and MCP-over-HTTP (#252); stdio-MCP/local-CLI stay trusted-local — document that boundary explicitly. Note: this pulls one slice of tag-vocab Phase 4 (§D) ahead of its "design partner asks" gate — owner sign-off required (see edit B6).

**H.2 — query-history curation primitives (#200–#203; spec: `adr-query-history-promotion.md` §2–§5)**

These are implementable and fixture-testable now; the consumer-kg pilot restart remains the *validation* gate, not the implementation gate. Owner must confirm this reading versus the anti-pre-build doctrine.

- [ ] **#200 usage families** — pipeline-operational vs analyst usage as separate families with distinct promotion rules — acceptance: a pipeline-only fixture produces zero analyst business-rule promotions; curation report separates analytical/pipeline/skipped/candidate rows (test named in the PR).
- [ ] **#202 matching guardrails** — acceptance: test where discovery keyword `user` does not match `vendor_user_id`; a query-history domain with only keyword predicates (no table/anchor predicates) warns or requires an explicit unsafe flag.
- [ ] **#203 scouting primitive** — aggregate-only readiness scout — acceptance: scout output over a fixture asserts row/candidate counts present and `statement_text` / `executed_by` / raw SQL / query hashes absent.
- [ ] **#201 BI/dashboard metadata source** — the largest item; the one allowed to slip to pilot restart — acceptance: connector interface + reference emitter produce graph-safe evidence (no raw SQL by default) ranked separately from pipeline usage.

**H.3 — issue hygiene**

- [ ] **#208** — pilot-infra blockage (ArcadeDB secret + expired AWS SSO), not a trellis-ai code defect — acceptance: re-homed to the consumer-kg repo or closed with a disposition comment.

---

## B. Stale-section corrections

**B1 — §1 heading date.** Location: line 14, `## 1. State of the project — 2026-07-02`. Current: heading frozen at 07-02 while its bullets include 2026-07-07/07-08 items and line 3 says "Last updated 2026-07-11". Proposed: drop the date from the heading (it duplicates "Last updated") or bump it on every §1 edit. Why: two dates that can disagree, one of them already does.

**B2 — test-suite counts (two spots).** Location: lines 48–50 ("collects **3962 tests by default** (4510 total; 548 …)") and line 340 ("expect ~3962 collected"). Current: counts predate the 2026-07-11 landings. Verified today: **4186 collected by default / 4734 total / 548 deselected** (`.venv/bin/python -m pytest tests/unit/ -q --co`). Proposed: update both, or state numbers only in §1 and have §5 point there. Rider for the same pass: `README.md:485` still says "~2300" — worst-stale count in the repo.

**B3 — §4 opening contradiction.** Location: line 304, "**As of 2026-07-02 there is no unblocked queue.**" followed by the 2026-07-11 blockquote saying the dogfood gate fired. Current: a reader gets "no work" then "here is the work" in consecutive paragraphs. Proposed: rewrite the §4 opener to lead with the unblocked dogfood queue (and §3.H once added); keep the gate table for what genuinely remains gated. Why: the roadmap's own rule — "Section 1 is the live truth" — should apply to §4's first sentence too.

**B4 — gate table "Dogfood signal" row is stale.** Location: line 325. Current: gates "Corpus-ingestion follow-ups **§G.2** (transcript / `--extract` / PDF handlers, chunk rollup…)" on the owner ingesting the real vault. But G.2 (conversation capture) and G.3 (`--extract`) landed 2026-07-11; the remaining follow-ups are **§G.4**, and TODO.md (§ Dogfood gap analysis, closing note) states the dogfood-gap items are no longer gated. Proposed: re-label the row §G.4, remove `--extract` from it, and move the un-gated dogfood items out of the gate table into §3.H/H.0. Why: an agent obeying this table today would wrongly treat landed and unblocked work as gated.

**B5 — AuraDB status contradicts issue #250.** Location: lines 53–56 ("The AuraDB free-tier instance is **GONE** (auto-deleted; DNS-dead)"). Current: #250's body says the instance was rebuilt 2026-06-16 (`neo4j+s://3760dbe7.databases.neo4j.io`) with a lapse watch ~2026-07-16. Both cannot be right. Proposed: check DNS (`nslookup 3760dbe7.databases.neo4j.io`), then fix whichever text lost, and sync #250's "Reference" section in the same commit. Why: #250's tasks (what to rotate/delete) depend on which credentials still correspond to a live instance.

**B6 — consequential gate-table edits if §3.H lands.** Location: lines 317–325. Current: #200–#203 sit under "Production pilot resumes", #194 under "Design partner asks", #250 under "Operator console access", #208 under "Infra access". Proposed: point those rows at §3.H, retaining "pilot restarts" as the validation gate for H.2 and noting #194's pull-forward as an owner-approved exception to the §D gating. Why: one queue, not two — the gate table should never disagree with §3 about the same issue numbers.
