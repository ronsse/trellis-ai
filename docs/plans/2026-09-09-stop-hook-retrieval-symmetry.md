# Plan 2 — Make the Stop hook symmetric, so skipping retrieval stops being the cheap path

> **Nightly TODO.** Tracked as a trellis-ai issue on board #275, but the file it edits lives
> in `~/.claude`, which the trellis-ai code-authoring selector cannot reach. Owner-run.

**Repo:** `~/.claude` (a git repo with **no remote** — local-only versioning; commits there
are the audit trail).
**File:** `~/.claude/hooks/stop-memory-nudge.sh` (Python despite the extension).

## Verified premise

Measured over 14 days of transcripts under
`~/.claude/projects/*/`, counting `tool_use` blocks (not the MCP tool *definitions*, which
appear in every transcript and produce a flat false count):

| | sessions | calls |
|---|---|---|
| substantive sessions (≥40 tool uses) | 10 | — |
| wrote a trace (`save_experience`) | **10 / 10** | 113 |
| retrieved anything | **4 / 10** | 21 |
| graded (`record_feedback`) | 8 / 10 | — |

Write:read ratio **5.4 : 1**; roughly one retrieval per 52 user turns. Zero calls across the
whole window to `get_file_context`, `get_items`, `get_graph`, `get_lessons`,
`get_task_context`, `get_objective_context`, `query_observations`.

The hook is the mechanism. `stop-memory-nudge.sh:123-134` builds its ask list from exactly
two branches:

```python
if not wrote_trace:                       # line 124
    asks.append("record a trace with save_experience …")
if served_pack and not graded_with_items: # line 128
    asks.append("grade the context you were served with record_feedback …")
```

There is **no `if not served_pack:` branch**. The payoff matrix an agent faces at Stop:

| behaviour | asks |
|---|---|
| retrieved **and** graded with item ids | 0 |
| retrieved, did not grade | 1 |
| **never retrieved** | **0** |

Not retrieving strictly dominates. The hook was written to fix unattributed feedback
(commit `86abc97 fix(hooks): nudge for *attributed* feedback, not merely any feedback`) and
it did — grading is at 8/10. It made retrieval carry a second obligation while leaving
non-retrieval free, and the measured 5.4:1 asymmetry is what that predicts.

`served_pack` is computed at line 113-114 off `RETRIEVAL_TOOLS` (lines 40-46) and is
otherwise used only to *suppress* the grading ask — the flag needed for the missing branch
is already there.

## The change

One branch and one constant, in `main()`:

```python
if total >= MIN_TOOL_USES and not served_pack:
    asks.append(
        "check memory before you finish — this session did substantive work without a "
        "single get_context call. Run get_context(intent=..., session_id=...) now and say "
        "in one line whether anything it returns contradicts what you concluded; an empty "
        "pack is a real answer and worth stating. If retrieval was genuinely not warranted "
        "(a one-off edit, a question about this conversation), say that instead"
    )
```

Place it **first** in the `asks` list — it is the ask that changes what the next session
does. The `total >= MIN_TOOL_USES` guard is redundant with the existing early return at
line 120-121 but is written explicitly so the branch cannot be lifted above that return by
a later edit and start firing on trivial sessions.

Resulting matrix — retrieving *and* grading becomes the unique zero-ask path:

| behaviour | asks |
|---|---|
| retrieved and graded | 0 |
| retrieved, did not grade | 1 |
| never retrieved | **1** |

**Why a Stop-time ask for a start-time behaviour is still the right first move.** It cannot
inform the work already done — that is a real limitation, not a quibble. What it does is (a)
remove the dominance, and (b) make a late contradiction check cheap, which has caught real
errors: a pack consulted after the fact still surfaces the precedent that would have changed
the approach, and the transcript then records the omission where the next session's capture
sweep can see it. A `UserPromptSubmit` hook that fires *before* the work is the structurally
correct fix and is a **deliberate follow-up, not part of this change** — see Non-goals.

Update the module docstring (lines 2-29): the "Two distinct things keep the learning loop
fed" framing is now three, and the reason the third was missing (it was fixed for grading in
`86abc97` and the retrieval half was not) belongs in the file, not just here.

Update `~/.claude/hooks/README.md` — the kill switch (`CLAUDE_SKIP_MEMORY_NUDGE=1`) is
unchanged, but the README documents what each hook asks for.

## Tests

The hook has no test file. Add `~/.claude/hooks/test_stop_memory_nudge.py` — plain
`unittest`, no deps, runnable as `python3 ~/.claude/hooks/test_stop_memory_nudge.py`. Build
a synthetic transcript (one JSONL line per assistant message, `content[].type == "tool_use"`)
and assert the decision for each cell of the matrix:

| case | tool uses | expect |
|---|---|---|
| 39 uses, nothing else | 39 | **no block** (under `MIN_TOOL_USES`) |
| 40 uses, no retrieval, trace written | 40 | block, reason contains `get_context` |
| 40 uses, retrieval + attributed feedback + trace | 40 | **no block** |
| 40 uses, retrieval, feedback with empty `helpful_item_ids` | 40 | block, reason contains `record_feedback` and **not** `get_context` |
| 40 uses, no retrieval, no trace | 40 | block, reason contains **both** asks, retrieval ask first |
| `stop_hook_active: true` | 40 | **no block** (never nudge twice) |
| `CLAUDE_SKIP_MEMORY_NUDGE=1` | 40 | **no block** |

Mutants these must kill: dropping the new branch; hoisting it above the `MIN_TOOL_USES`
return; using `if not served_pack` without the `and`-guard ordering so it fires on trivial
sessions; treating an empty-list `helpful_item_ids` as attribution (already guarded at
lines 79-85 — pin it, since the population of one attribution key would hide a constant).

The fourth row is the one that matters: it proves the new ask does **not** fire when the
agent did retrieve, which is the failure that would train reflexive double-calling.

## Measurement

Re-run the same transcript count 14 days after the change lands, over sessions started
after it:

```
retrieval rate = sessions with ≥1 RETRIEVAL_TOOLS call / sessions with ≥40 tool uses
```

Baseline **4/10**. Also watch for the failure this change can cause: a `get_context` call
in the last three tool uses of a session followed by a `record_feedback` with a low rating
and no bearing on the work — that is the hook being *satisfied* rather than *used*, and it
would show up as retrieval rate rising while `pack_attribution_rate` stays flat and pack
ratings fall.

## Non-goals

- **No `UserPromptSubmit` hook in this change.** It is the structurally right place for a
  before-work nudge and should be the follow-up, but it costs tokens on every prompt, needs
  once-per-session state keyed on `session_id`, and needs a heuristic for "substantive"
  that cannot be computed from the first prompt. Landing the cheap symmetric fix first also
  gives the follow-up a baseline to be measured against.
- No change to `MIN_TOOL_USES`, to `RETRIEVAL_TOOLS`, or to the grading ask's wording.
- No change to `~/.claude/settings.json` — the hook is already registered.

## Risks

- **Nudge fatigue producing junk retrievals.** Mitigated by the ask's explicit escape hatch
  ("say that instead") — which the hook already honours, since `stop_hook_active` prevents a
  second block — and by the measurement above, which is written to detect exactly this.
- **The ask reads as busywork on sessions that legitimately have nothing to retrieve** (a
  long mechanical refactor in a greenfield repo). An empty pack is one call and a one-line
  statement; that is the intended cost.
- **No remote on `~/.claude`.** The commit is the only copy. Confirm it lands in the
  `/mnt/data/backups` tarball the `SessionStart` hook already watches.
