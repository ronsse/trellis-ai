# Plan 3 — Rewrite `retrieve-before-task` to cover mid-session retrieval, not just the first call

> **Nightly TODO.** Tracked as a trellis-ai issue on board #275; the file lives in
> `~/projects/my-claude-skills`, which the trellis-ai code-authoring selector cannot reach.
> Owner-run.

**Repo:** `ronsse/my-claude-skills` (symlinked into `~/.claude/skills/` — edit in the repo).
**File:** `skills/retrieve-before-task/SKILL.md` (60 lines, last touched 2026-08-15).

## Verified premise

This is the plan for the question that started the review: *"are there mechanisms outside
the initial pack delivery for an agent to gather more packs from certain domains and seek
information out on its own, if the session pivots or incorporates a new domain?"*

**The mechanisms all exist. The skill documents almost none of them.** From
`src/trellis/mcp/server.py`:

| Mechanism | Signature | In the skill? |
|---|---|---|
| session dedup | `get_context(..., session_id=...)` — "items already returned by recent calls in this session are excluded" | mentioned, as a hygiene note only |
| **re-query after truncation/pivot** | `get_context(..., refresh=True)` — "bypass session dedup for this call only … when the caller's context window was truncated" | **no** |
| **survey → drill workflow** | `get_context(..., index=True)` → `get_graph(entity_id=...)` → `get_items(item_ids=[...])` | **no** |
| **custom section layout** | `get_context(..., sections=[{name, retrieval_affinities, content_types, scopes, entity_ids, max_tokens, max_items}])` | **no** |
| run attribution | `get_context(..., run_id=...)` — "narrower than a session … so later feedback can credit the runs a memory actually helped" | **no** |
| entity neighbourhood | `get_graph(entity_id, depth=1)` | **no** |
| targeted lookup | `search(query, index=False)` | one clause |
| file-scoped | `get_file_context(paths=[...])` | **no** (and currently returns nothing — see Plan 4) |

`get_context`'s own docstring (`server.py:1085-1130`) says it plainly: *"This is the single
retrieval entry point — pass `sections` for the sectioned layout that
`get_sectioned_context` used to provide, or `index=True` for a survey-first workflow: scan
the index, walk `get_graph` from an interesting item to its evidence pointers, then
batch-fetch chosen ids with `get_items`."* None of that reaches an agent, because the
docstring is only visible in the tool schema and the skill is what agents actually read.

What the skill *does* say (`SKILL.md:14`) is **"At the start of any task"** — a
once-per-session framing — followed by one follow-up line (`:51-52`): *"If the first call
doesn't surface what you need, follow up with `search` … or `get_lessons`."* That is the
whole of its mid-session guidance.

The measured consequence, over 14 days of transcripts: **zero** calls to `get_items`,
`get_graph`, `get_lessons`, `get_file_context`, `get_task_context`,
`get_objective_context`, `query_observations`; 21 retrieval calls total across 10
substantive sessions, all of them `get_context` or `search`. The unused half of the
retrieval surface is exactly the half the skill does not describe.

**One thing the skill gets right and must keep:** the `domain=` paragraph (`:30-39`),
including *why* it reverses earlier guidance (#254 default-pass) and the 2026-08-15 live
re-verification. Do not compress that away — it is the correction of a real error.

## The change

Rewrite `SKILL.md`. Target ~120 lines; it is read by every agent on every non-trivial task,
so density matters more than completeness.

**1. Retitle the trigger.** Replace *"At the start of any task"* with a two-part trigger:
*at the start*, **and** *at every pivot* — the task moves to a system, repo, or domain the
first call did not name; the user introduces a new goal; you are about to debug something
the opening intent did not anticipate. State the reason: a pack is scoped to the intent you
gave it, so a new intent needs a new pack — retrieval is not a session-level ritual.

**2. New section: "Re-querying mid-session."** The three cases, each with the call:

- *Pivot to a new domain* — a fresh `get_context(intent=<the new thing>, domain=...)` with
  the **same** `session_id`. Dedup is the feature here: you get what is new about the pivot,
  not the same items again.
- *Context was truncated (compaction)* — same call with `refresh=True`. Say why it exists:
  after compaction the items you were served are gone from your window but the server still
  believes you have them, so without `refresh` the pack comes back thinner than it should.
- *You need depth on one item, not breadth* — `get_items(item_ids=[...], pack_id=...)`.

**3. New section: "Survey first when you don't know what you're looking for."** The
progressive-disclosure workflow, as three calls:

```
get_context(intent=..., index=True, session_id=...)   # one line per item: id, type, title, read cost
get_graph(entity_id="<an id from the index>", depth=1) # neighbourhood + evidence pointers
get_items(item_ids=[...], pack_id="<from the index pack>")  # bodies for the ids you chose
```

Carry the trap from the docstring verbatim, because it is not guessable: **an index survey
counts as a serve** — with a `session_id` every id it listed is deduped out of later packs,
so a follow-up full retrieval that needs them back must pass `refresh=True`. And: index mode
is flat-layout only; combining it with `sections` is an error the server raises.

**4. New section: "Shaping the pack."** `sections=[...]` with the six keys, one worked
example (a code-implementation intent wanting *precedents* and *failures* in separately
budgeted sections). Note that `get_sectioned_context`, `get_task_context` and
`get_objective_context` still exist but are the older surface — `get_context(sections=...)`
is the one entry point and the one to reach for.

**5. Fold `get_file_context` in — conditionally.** It is the natural "I am about to edit
this file" call and it currently returns nothing for any repo source file (Plan 4). Add it
to the skill **in the same change set that lands Plan 4's producer**, not before. A skill
that recommends a call which always returns empty teaches agents to stop calling it.

**6. Keep and update the failure modes.** Add two, both measured:
- *Don't treat retrieval as once-per-session.* One call per 52 user turns is the observed
  rate; the pivots went unqueried.
- *Don't grade a pack you never read.* Retrieval and `record_feedback` are one loop; the
  Stop hook (Plan 2) will ask for both.

**7. Cross-reference.** Point at `record-after-task` (already there) and add
`~/.claude/CLAUDE.md` § Memory as the authority on *when*, so the two do not drift.

## Tests

Skills have no test harness. The check is a **live probe**, run on skynet after the rewrite,
recorded in the commit message:

1. `get_context(intent=<a technical intent>, index=True, session_id="probe-1")` → returns an
   index, note the `pack_id` and 3 ids.
2. `get_graph(entity_id=<one id>)` → non-empty or an honest empty.
3. `get_items(item_ids=[3 ids], pack_id=<from 1>)` → bodies for exactly those ids.
4. `get_context(intent=<same>, session_id="probe-1")` → confirms the surveyed ids are
   **deduped out**.
5. Same call with `refresh=True` → confirms they come **back**.

Steps 4 and 5 are the ones worth running: they are the only way to know the trap in §3 is
still true of the deployed build, and the deployed build is 42 commits behind `main` until
Plan 1 lands. **Run this probe against the post-Plan-1 deployment**, or record which build
it was run against.

## Measurement

Same instrument as Plan 2 — count `tool_use` blocks by tool name across
`~/.claude/projects/*/*.jsonl` for sessions started after the change:

- retrieval calls per substantive session (baseline **2.1** = 21/10)
- distinct retrieval tools used (baseline **2** of 9)
- sessions with more than one `get_context` intent (baseline: not measured; measure it now)

The third is the one this plan is actually about. A rewrite that raises total calls but
leaves every session at one intent has not fixed the pivot problem.

## Non-goals

- No change to `record-after-task`.
- No new skill. The pivot guidance belongs in the skill that already owns retrieval;
  splitting it is how the two drift.
- No advice to call `get_lessons` more until it returns something — it currently answers
  *"No lessons found."* on this deployment, and recommending it would be the same mistake as
  recommending `get_file_context` today.

## Risks

- **Length.** A 120-line skill competes for the same context the pack does. Cut the worked
  examples before cutting the traps.
- **Documenting behaviour of a build that is not deployed.** `refresh` / `index` / `sections`
  are all present in `2e62ffe52` (the deployed commit), so the skill is accurate today — but
  the probe above is what proves it rather than assumes it.
- **Recommending a dead call.** Guard: `get_file_context` waits for Plan 4.
