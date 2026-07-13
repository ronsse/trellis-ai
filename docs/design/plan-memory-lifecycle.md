# Plan: Memory lifecycle — capture, promotion, injection, decay

**Status:** draft for owner review, 2026-07-12
**Owner:** nronsse
**Self-contained:** yes — read top to bottom; no prior conversation context needed.

> Positions, not a survey. Every claim that can cite live evidence does. Live evidence
> base: the 2026-07-11 dogfood gap analysis (TODO.md), the 2026-07-12 enrichment pilot
> (docs 37→56, retrieval probes, finding F14), and the operator's parallel experience
> running Claude Code's file-based auto-memory on the same box.

## 0. Thesis

Memory is a **lifecycle, not a store**: capture → triage → promote → retrieve → inject
→ decay. Most memory products fail by making one stage-assignment mistake — always-on
unjudged capture (noise), capture-time keep/discard judgment (wrong time to judge),
per-call retrieval (context degradation), or no decay (permanent bloat). The design
rule this doc applies throughout:

**Deterministic triggers, model-judged content, evidence-driven retention.**

- *Triggers* (when capture/retrieval/consolidation happen) are deterministic — hooks,
  session boundaries, schedules. Judged triggers starve: 6 traces in 5 days of real
  use is the proof (dogfood analysis) — agents do not reliably decide to remember.
- *Content* (what is worth writing, what a merge should say) is model-judged —
  deterministic extraction can't tell "surprising and durable" from "true but inert."
- *Retention* (what keeps getting served) is evidence-driven — feedback attribution,
  not anyone's forecast at write time. This is the stage where Trellis's item-level
  attribution seam is a structural advantage no comparable system has.

### 0.1 North star: the local memory model

The stretch goal that orders everything else (owner decision, 2026-07-12): **a small
local model eventually performs every judged stage** — extraction, reconciliation,
distillation, curation verdicts — so frontier models are optional, never load-bearing.
The future this bets on is small local models doing tasks; it is already partially live
here (hermes3:8b extraction, nomic-embed embeddings, both local, both free).

Three consequences:

1. **The determinism thesis splits in two.** *Cost-motivated* judgment-avoidance
   ("keep the LLM out of this stage because calls are expensive") dissolves as local
   inference approaches free — the ladder's destination is
   `DETERMINISTIC > LOCAL > FRONTIER`, and the middle rung keeps widening.
   *Trust-motivated* determinism — governed writes, idempotency, immutable audit,
   LLM-never-in-the-write-path — is about reproducibility, not cost, and is
   **permanent regardless of where inference runs**. When this doc assigns a stage
   "deterministic," check which motivation applies before defending it.
2. **The system generates its own curriculum.** Every judged memory operation has the
   shape of a training example: (input context, decision, downstream outcome via
   feedback attribution). Log them from day one — extraction verdicts, reconciliation
   ADD/UPDATE/SUPERSEDE/NOOP calls, distillation summaries, each joined later to
   whether the resulting memory proved useful. Training the local memory model is
   years off; its dataset accrues now or never.
3. **Design ceiling on judgment.** No judged stage may be *designed* to require
   frontier-scale reasoning. If a stage only works with a heavy model, the stage is
   mis-factored — split it until an 8B-class model (or the deterministic tier) can
   carry each piece.

Every section below is one lifecycle stage; §8 maps the whole pipeline
deterministic-vs-judged in one table; §9 is the concrete fallout.

## 1. Taxonomy — memory types have different physics

The classical split maps cleanly onto existing Trellis schema and should be treated as
load-bearing, because **each type has a different capture mechanism, volume, trust
level, and half-life**:

| Type | Trellis form | Volume | Capture | Half-life |
|---|---|---|---|---|
| **Episodic** — what happened | traces | high | automatic (hooks; #255) | short; value is in aggregation |
| **Semantic** — what's true | docs + graph (entities, observations) | medium | ingest + promotion from episodic | long, but **rots** — needs verification |
| **Procedural** — what works | lessons, advisories, gotcha docs | low | mined (`mine-precedents`) + explicit failure capture | longest; highest per-item value |
| **Working** — what matters now | the pack | n/a | assembled, never stored | one task |

Two consequences the current system doesn't yet honor:

1. **Failures outrank successes for procedural capture.** Every high-value gotcha in
   the operator's corpus (Hermes OAuth race, caddy inode trap, `domain=` filter) is a
   *failure that cost debugging time*. A success trace mostly confirms priors; a
   failure trace prevents a repeat. Auto-capture (#255) should bias accordingly —
   sessions with errors/reverts/user-corrections are capture-mandatory; clean routine
   sessions can be sampled.
2. **User corrections are gold-tier semantic memory** ("actually, planning lives in
   TODO.md") — explicit, attributed, pre-verified by the human. These should never be
   lost to sampling; a correction detector belongs in the capture hook's heuristics.

## 2. Capture — what is worth writing (the triage gate)

Keep/discard judgment does **not** belong at capture time for episodic memory (capture
is cheap; you can't know at write time what will matter) — but a *worthiness gate*
does belong on semantic/procedural writes, or the store fills with derivable inertia.
The gate, four tests (all must pass):

1. **Non-derivable** — cannot be reconstructed from the repo, docs, or git history.
   ("The api container builds from the local checkout" = derivable, don't store;
   "editable venv means host tools track the working tree live while containers
   freeze" = derivable *in principle* but cost a real investigation — store.)
2. **Durable** — will still matter next month. Session-local state fails this.
3. **Actionable** — would change what a future agent *does*, not just what it knows.
4. **Attributed** — carries evidence (`evidence_ref`, a path, a command, a date).
   Unattributed memories can't be verified later and become unfalsifiable rot.

The strongest single capture signal is **prediction error**: the thing the agent (or
user) was wrong about before the session corrected it. "Surprise" is cheap to detect
in transcripts (errors, retries, "actually…", plan revisions) and correlates almost
perfectly with the operator's hand-written gotcha corpus.

## 3. Promotion — docs vs graph ("when does something earn a node?")

The funnel has three tiers with rising structure and rising write cost, and the
existing tiered-extraction ladder already implements the transitions. What's missing
is the explicit criterion for each promotion:

```
  traces/docs  ──extract──►  entities + observations  ──curate──►  lessons/advisories
  (cheap, lossless)          (structured, governed)               (distilled, ranked)
```

**A thing earns a graph node when it passes any of the three tests:**

1. **Identity** — it is referenced *by name* from ≥2 independent sources (an entity is
   a join key; one mention is just prose).
2. **Relation** — you would retrieve it by *relationship* ("what depends on X", "what
   was observed about Y") rather than by text similarity.
3. **Stability** — it is small and slowly-changing (hosts, services, people, repos —
   not opinions, not status).

Everything else **stays prose in a document**, findable by embedding. The graph node
carries **pointers, never prose**: `record_observation(subject_entity_id=…,
evidence_ref=<doc_id>)` is the canonical pattern — the observation anchors the claim
to an entity; the document holds the words. (This answers "reference location in a
parent": the parent doc *is* the storage; the graph is the index of identities and
relations over it.)

**Anti-pattern to reject explicitly: prose-in-graph.** Nodes with paragraph payloads
are an ungoverned document store with graph overhead — worst of both. The current
embed-gap defect (save_knowledge/record_observation content is never embedded,
TODO.md) should be fixed *in this direction*: don't make graph text a second
similarity surface; make every observation carry an `evidence_ref` into the embedded
doc store, and fix `save_knowledge` to auto-create the evidence doc when none exists.

## 4. Retention — keep/discard is a demotion problem, not a deletion problem

Position: **capture everything (episodic), promote selectively, demote by evidence,
delete almost never.**

- **Demotion, not deletion.** SCD-2 already gives "close the validity interval" —
  wrong facts get superseded, not erased, preserving time-travel and audit. Actual
  deletion is reserved for hazard (secrets, privacy) — and that path exists and stays.
- **Evidence-driven demotion is the differentiator.** The machinery exists and is the
  right design: pack feedback → item-level effectiveness → noise-tagging → rank decay.
  The dogfood finding was that this loop was *starved* (0 lessons, 0 advisories — no
  input), not wrong. Once #255 feeds it, retention policy = "items that get served
  and never help sink; items that help rise." No human curation queue required for
  the common case.
- **Facts rot; verify, don't trust.** Semantic memory needs `last_verified` +
  cheap verification sweeps: claims that name a checkable thing (path, port, repo,
  service) get re-checked mechanically (`test -e`, `gh repo view`, health probes) on
  a schedule — the operator's `claude-md-factcheck.sh` and `consolidate-memory` skill
  prove the pattern at small scale. Unverifiable claims age toward a lower trust band
  rather than being deleted.
- **Contradiction = SCD-2 supersede + recency-wins-at-retrieval**, with the losing
  version retrievable on demand. Never serve both sides of a contradiction in one
  pack (a pack-assembly invariant, cheap to enforce at build time).

## 5. Retrieval & injection cadence — "is it used on every call?"

**No. Per-call retrieval is an anti-pattern** on three independent grounds: token
cost, context *degradation* (irrelevant injected context measurably hurts task
performance), and attribution noise (feedback can't tell which of 40 injections
helped). The cadence that matches how agent sessions actually work:

| Moment | Action | Trigger type |
|---|---|---|
| Session/task start | ONE budgeted pack pull (retrieve-before-task) | deterministic nudge, judged params |
| Topic shift / explicit need | agent-initiated `search`/`get_context` | judged |
| Subagent spawn | fresh context = its own task-start pull | deterministic |
| Session end | capture (record-after-task / #255 hook) | deterministic trigger |
| Nightly | curate, consolidate, decay, verify sweep | deterministic |

Three bloat defenses, layered:

1. **Budget** (exists): `max_tokens` on the pack is a hard cap. Keep it small by
   default (~1,500) — memory should whisper, not lecture.
2. **Session-delta packs** (new, issue-ready): the server keeps a tiny
   `(session_id, item_id, content_hash, served_at)` table; `get_context` with a
   `session_id` returns only items **not already served to that session** (unless
   content changed). Re-serving the same pack every pull inside one session is pure
   bloat — this kills it server-side. Client signals context resets (compaction!)
   with a refresh flag, because only the client knows its window was truncated —
   which respects the #255 boundary: server provides the dedup primitive, client owns
   context-state knowledge. Bonus: the table enriches feedback joins (which serve
   actually reached which session).
3. **Near-dup suppression in PackBuilder** (new, issue-ready): before assembly,
   collapse items with embedding cosine ≳0.95 to the single highest-authority copy.
   Live motivation: pilot finding **F14** — the same fact now exists as a
   `save_memory` doc *and* a `corpus:claude-auto-memory` doc (hash-dedupe is
   per-source-system by design), and retrieval probes surfaced both copies in one
   pack. Cross-source duplication is inevitable at ingest; the *pack* is where it
   must not survive.

## 6. The static↔dynamic split — the promotion path nobody builds

The bloat question has a structural answer beyond dedup: **task-invariant memory does
not belong in retrieval at all.** A fact needed by essentially every session (hostname
conventions, secrets policy, "use 1Password, never hardcode") belongs in *standing
context* — CLAUDE.md / system prompt / the auto-memory index — loaded deterministically
at session start. Trellis should serve **task-variant** memory: the right gotcha for
*this* task, precedent for *this* intent.

That implies a two-way promotion path between the standing tier and Trellis, and the
effectiveness data is exactly the signal to drive it:

- **Graduate up:** an item served-and-marked-useful in >X% of sessions across ≥N
  sessions is task-invariant by definition → consolidation emits a "promote to
  standing context" advisory; the operator (or the consolidate-memory skill) moves it
  into CLAUDE.md/auto-memory and tags the Trellis copy `demoted-to-standing` so packs
  suppress it thereafter (else you've built a duplication machine — F14 again).
- **Graduate down:** standing-context facts that are never load-bearing should sink
  into Trellis. Honesty note: there is **no feedback signal on static context** (no
  one records "CLAUDE.md line 14 helped"), so this direction is judgment-only — a
  consolidation-time review question, not automation. The asymmetry is real; state it
  rather than pretend both directions are measurable.

Claude Code's own memory system is precedent for the two-tier shape: MEMORY.md (index,
always injected, ~1 line/fact) + memory files (bodies, loaded on demand). Trellis
packs are the third tier below both. Naming the tiers makes the "does constant
injection bloat context?" question answerable: tier 1 is capped by the index format,
tier 2 by relevance, tier 3 by budget+delta+dedup.

## 7. Spaces — personal vs work vs household

Partition by **audience, not topic**. The primitives exist — `DataClassification`
(closed, policy-relevant), `ContentTags` (open, retrieval-shaping), scoped API keys
(#252) — but classification is **unenforced** (#194), which currently makes spaces a
labeling exercise. Design:

- **Space = who may retrieve**, stamped at capture from the *source* (a session in
  `~/projects/resume-toolkit` for profile `kimberly` defaults to Kim's space; a
  work-laptop session defaults to `work`). Default-deny across spaces at
  PackBuilder/search *and* mutation, by caller scope — that is #194's minimal slice,
  verbatim.
- **Cross-space leakage is the #1 privacy failure mode**, and it's near-term real on
  this deployment: resume-toolkit is multi-profile (family members' job searches),
  `vitals` will put **two people's health data** (Nate + Kim) through agents against
  the same substrate, and the technical-writing `memories/` exclusion from the omen
  bundle was this same instinct applied manually. #194 stops being a security
  checkbox and becomes *the feature that makes multi-person memory possible*.
- Retrieval never merges spaces silently. An agent with multi-space scope must ask
  per-space (`space=work`), so provenance stays visible in the pack.

## 8. The determinism map — direct answer to "always on? always non-deterministic?"

How the field answers it today:

| System | Capture | Injection | Notes |
|---|---|---|---|
| ChatGPT memory | always-on, model-judged | always injected | the bloat/creep cautionary tale: no attribution, stale items resurface |
| Claude Code auto-memory | judged content, nudged triggers | index always; bodies on demand | the two-tier shape §6 borrows |
| Zep / Mem0 | always-on extraction per message | per-query | session/user-level attribution only — Trellis's wedge is item-level |
| Letta (MemGPT) | agent self-edits its memory blocks | core block always in-context | memory management as agent action; interesting, expensive |
| CLAUDE.md / Cursor rules | human-curated | always, deterministic | zero learning; goes stale silently |

**Trellis's position (the thesis, restated as the full pipeline):**

| Stage | Deterministic | Model-judged |
|---|---|---|
| Capture trigger | ✅ hooks, session end, schedules (#255) | — |
| Capture content | — | ✅ worthiness gate (§2) |
| Extraction | ✅ deterministic tier first | ✅ LLM tier opt-in (existing ladder) |
| Promotion to graph | — | ✅ against §3's three tests |
| Retention | ✅ feedback arithmetic, decay, verify sweeps | judged only at consolidation merges |
| Retrieval trigger | ✅ session/task start, subagent spawn | ✅ mid-session topic shifts |
| Injection content | ✅ budget + delta + dedup caps | ✅ ranking inside the caps |
| Decay/consolidation | ✅ nightly schedule | ✅ merge decisions |

Neither always-on-everything (ChatGPT's failure) nor deterministic-everything
(CLAUDE.md's failure). Every stage gets the assignment its failure mode dictates.

## 9. Fallout — concrete, ranked

Issue-ready (mechanical enough to scope now):

1. **Session-delta packs** (§5.2) — `session_id` on `get_context`, served-items table,
   content-hash re-serve rule, client refresh flag for compaction. Small schema, big
   bloat win, better feedback joins.
2. **Near-dup suppression in PackBuilder** (§5.3) — cosine-collapse at assembly;
   directly remediates F14 at the layer that matters.
3. **Capture-worthiness gate + failure bias in #255** (§1–§2) — amend the auto-capture
   issue: error/correction detection makes a session capture-mandatory; clean sessions
   sampled; the four-test gate in the distillation prompt.
4. **`save_knowledge` evidence-doc auto-create** (§3) — closes the embed-gap defect in
   the pointer-not-prose direction.
5. **Promote-to-standing advisory** (§6) — a consolidation output type; threshold on
   serve-rate × usefulness; `demoted-to-standing` suppression tag.

Owner-decision-first (judgment, not code):

6. **Space model** (§7) — enumerate the spaces (personal/household/work at minimum),
   default-stamping rules per source, and whether #194's minimal slice ships before
   `vitals` carries real health data (recommended: yes — sequencing with #256/#194 is
   already in the milestone).
7. **Standing-tier ownership** — which file *is* tier-1 truth per machine (CLAUDE.md
   vs auto-memory index), so graduation (§6) has a defined destination.

Explicitly rejected (so they don't creep back):

- Per-call retrieval middleware (§5) — degradation + attribution noise.
- Prose-payload graph nodes (§3) — the graph indexes; documents store.
- Capture-time keep/discard for episodic traces (§4) — judge at retention, with
  evidence.
- Always-injected full memory summary, ChatGPT-style (§8) — the bloat machine this
  doc exists to avoid.
