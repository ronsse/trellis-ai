# Swarm Plan — Trellis handoff follow-up, both phases sequenced

**Date:** 2026-06-17
**Source:** [`2026-06-16-session-handoff.md`](./2026-06-16-session-handoff.md) §3 (Step 3) and §5 (open follow-ups).
**Shape:** Phase 1 (concrete code follow-ups) clears first, then Phase 2 (the Step 3 quality/impact assessment). Credential/console items are handed to the operator — an agent cannot touch 1Password or the Neo4j console.

---

## Phase 1 — §5 code follow-ups (fast, ends in green CI)

Two independent tasks → run concurrently in isolated worktrees → adversarial verify each → land sequentially on `main`.

### Task A — `consumed_event` dangling-edge fix
- **Where:** [`recorder.py:180`](../../src/trellis/meta/recorder.py) (`consumed_event`); live caller [`generator.py:184`](../../src/trellis_workers/code_authoring/generator.py). Sibling `consumed_observation` ([`recorder.py:201`](../../src/trellis/meta/recorder.py)) may share the shape — check it.
- **Problem:** writes a `wasInformedBy` edge to an `event_id` that is an EventLog event, not a graph node. SQLite tolerates the dangling edge; Bolt (Neo4j/ArcadeDB) rejects it ("source/target has no current version"). Not exercised by current live tests, so green today — would fail on Neo4j if a path hits it.
- **Design call (the real work):** `produced_finding`'s template (d3e3444) materializes-if-absent, but `consumed_event`'s docstring deliberately says the event node is *not* a graph node — the EventLog owns the canonical record. So the fix is a judgment call, not a copy-paste: either (a) create-if-absent a lightweight event-correlation stub node, or (b) skip the edge when the target isn't materialized. Agent proposes + justifies, and reconciles the docstring with whichever path it picks.
- **Done when:** a Bolt-backed test exercises the path and passes; SQLite still green; docstring matches behavior.

### Task B — `effectiveness.py` join cleanup
- **Where:** `analyze_effectiveness` ([`effectiveness.py:211`](../../src/trellis/retrieve/effectiveness.py)) and `analyze_advisory_effectiveness` ([`effectiveness.py:566`](../../src/trellis/retrieve/effectiveness.py)) inline the pack⋈feedback join that is canonical as `join_pack_feedback` (already used in `pack_observations.py`, `domains.py`, `metrics_timeseries.py`).
- **Risk:** touches stable analytics — the handoff deferred it for re-test churn. This is a **behavior-preserving** refactor: prove output is identical before/after (snapshot existing test outputs), not merely that tests pass.
- **Done when:** both functions call the helper, no behavior change, full `effectiveness` + `metrics` suites green.

### Verification gate (both tasks)
Each task verified by an independent adversarial agent prompted to **refute** "this is correct and behavior-preserving" before merge. Then on the integrated branch: `make lint && make typecheck && pytest tests/ -q`.

---

## Phase 2 — Step 3 swarm (quality / impact assessment)

Three read-only scouts in parallel, then a sequential synthesis.

1. **Alignment scout** — prove `eval/scenarios/_convergence_common.py` formulas == `retrieve/metrics_timeseries.py` dashboard formulas, line by line. Output: mapping table + any drift.
2. **Inventory scout** — read [`pack-quality-evaluation.md`](../agent-guide/pack-quality-evaluation.md) + `eval/scenarios/_example/`; catalog every convergence scenario and the claim each can substantiate.
3. **Run scout** — execute one convergence scenario end-to-end, capture the numbers, confirm the dashboard renders the same trend.
4. **Synthesis** — draft the assessment: three claims — (i) retrieval lifts success rate over N rounds, (ii) curation demotes noise measurably, (iii) the promote loop adds durable value — each bound to the scenarios + real-LLM runs that prove it. Written to `docs/plans/`.

---

## Out of band — operator checklist (not swarmed)

An agent can't touch 1Password or the Neo4j console. Handed over after Phase 1:

- [ ] Rotate AuraDB DB password → update `TRELLIS_TEST_NEO4J_PASSWORD` secret (low urgency — leaked to a chat transcript during the rebuild).
- [ ] Delete unused Aura API client IDs `985676d4` / `d664924e`.
- [ ] Recreate local `.env` from `.env.example` (only needed to run env-gated live suites locally; CI has the secrets).
- [ ] **Watch:** AuraDB Free auto-deletes after 30 days idle (next lapse ~2026-07-16). If live-infra goes red with DNS/`gaierror`, the instance lapsed — recreate per handoff §4 and update the four `TRELLIS_TEST_NEO4J_*` secrets.
