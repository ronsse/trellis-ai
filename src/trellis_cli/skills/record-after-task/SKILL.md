---
name: record-after-task
description: Save a Trellis trace after meaningful work — a fix, feature, refactor, instructive failure, or non-obvious discovery — so future agents inherit it. Skip trivial edits and conversational turns.
version: 1.1.0
status: preview
---

# Record After Task

> **Status: preview.** Tool signatures are in flux while parallel work lands. Expect revisions before the next minor release.

retrieve-before-task only helps if there is something to retrieve. When the
work is done: save a trace, grade the packs you were served, and save any
durable environment fact as knowledge.

## 1. Save the trace

`save_experience(trace_json=...)` validates the whole trace against a strict
schema: **one wrong key discards the entire trace**, and nothing is recorded.
Required top-level keys: `source`, `intent`, `context`. `source` is one of
`agent | human | workflow | system`; the name of your agent or client
(`"claude-code"`, say) is not one, and belongs in `context.agent_id`.

```
save_experience(trace_json='{
  "source": "agent",
  "intent": "<one sentence: what you set out to do>",
  "steps": [
    {"step_type": "tool_call", "name": "Edit",
     "args": {"file_path": "src/payments/client.py"},
     "result": {"summary": "added retry with jitter to charge()"}},
    {"step_type": "tool_call", "name": "Bash",
     "args": {"command": "make test"},
     "result": {"passed": 12, "failed": 0}},
    {"step_type": "tool_call", "name": "Bash",
     "args": {"command": "psql -l"},
     "result": {},
     "error": "psql: command not found; not installed on this host"}
  ],
  "artifacts_produced": [
    {"artifact_id": "payments@35a9978", "artifact_type": "commit"}
  ],
  "outcome": {
    "status": "success",
    "summary": "<2-3 sentences: what changed and why it works>",
    "metrics": {"tests_passed": 12}
  },
  "context": {"domain": "<backend/frontend/data-platform/infra/...>",
              "agent_id": "claude-code"},
  "metadata": {"repo": "acme/payments"}
}')
```

It replies `Trace saved: <trace_id>`.

### A step takes these keys and no others

| Key | Type | Holds |
|---|---|---|
| `step_type` | string, **required** | `"tool_call"` for a tool use (the only value trace extraction reads), or a label such as `"decision"` |
| `name` | string, **required** | the tool or action: `"Edit"`, `"Bash"` |
| `args` | object | the inputs, under the tool's own argument names; extraction reads touched files and commands run from `file_path` and `command` |
| `result` | **object, never a string** | what came back: `{"passed": 12}`, or `{"summary": "..."}` for prose |
| `error` | string | what went wrong, when the step failed |
| `duration_ms` | integer | optional |
| `started_at` | ISO 8601 datetime | optional; defaults to ingest time |
| `schema_version` | string | never set it; it has a default |

### Gotchas: the four mistakes behind most rejections

| Rejected | Write instead |
|---|---|
| `"description": "ran the tests"` | `"name": "Bash", "args": {"command": "make test"}` |
| `"output": "12 passed"` | `"result": {"summary": "12 passed"}` |
| `"result": "12 passed"` | `"result": {"summary": "12 passed"}` |
| a step without `step_type` | `"step_type": "tool_call"` |

The rest of the trace:

- Artifacts you produced (commits, files, PRs) go in top-level
  `artifacts_produced[]` as `{artifact_id, artifact_type}`, never in
  `outcome`, which takes only `status` (`success | failure | partial |
  unknown`), `summary` and `metrics`.
- `context` takes `domain`, `agent_id`, `team`, `workflow_id`,
  `parent_trace_id`, `started_at` and `ended_at`.
- A rejection names the field and a fix (`... | fix: ...`). Move what it names
  and retry. **Never drop content to get past the validator**:
  `outcome.metrics` and top-level `metadata` are free-form objects, so a stray
  fact always has a legal home.

### What to write

- **Steps**: 3–8 load-bearing actions, one line each, not a tool log. Keep the
  steps that failed, with `error` filled in: a failure you worked around is
  often the most reusable thing in the trace, and a trace with no `error` from
  a session that hit real failures is sanitized.
- **`outcome.summary`** is for a reader with no context: the change, what it
  accomplishes, and the constraint that drove it. Bad: "Fixed it." Good:
  "Added exponential-backoff retry with jitter to PaymentsClient.charge(),
  three attempts max, because Stripe drops requests during its nightly
  maintenance window."
- **A disproved belief**: if this session overturned something recorded
  earlier, name the superseded belief and why it was wrong. Memory only
  appends; nothing retracts it for you.
- **A failed task** is still recorded, with `outcome.status: "failure"` and
  the lesson in the summary. `trellis worker mine-precedents` learns from
  failures as much as from successes.
- **`context.domain`**, always: untagged traces are nearly invisible to
  retrieval.

## 2. Grade the packs you were served

For each `get_context` pack that informed the work:

```
record_feedback(pack_id="<pack_id>", rating=0.6,
                helpful_item_ids=["<id>"], unhelpful_item_ids=["<id>"])
```

- Cite ids. Feedback on a pack with no item ids joins to nothing and teaches
  the retrieval loop nothing. A pack that missed still gets graded: its noise
  goes in `unhelpful_item_ids`, the more valuable signal of the two.
  `ignored_item_ids` (read, not used) may be added but does not count as
  citing.
- `rating` is how useful the pack was, 0.0 (useless) to 1.0 (all on point).
  An honest low rating beats a polite high one.
- Only when no pack informed the work, grade the trace instead:
  `record_feedback(trace_id="<trace_id>", success=...)`. `success` means the
  user's intent was achieved, not that nothing errored.

## 3. Environment gotchas go in `save_knowledge`, not the trace

A durable fact about how a system behaves, one that cost you time and will
cost the next agent the same, outlives the task:

```
save_knowledge(name="<the fact, as a short title>",
               content="<the fact, and how you found it>",
               relates_to="<the tool, system or repo it is about>")
```

Triggers: a build names images differently than the deploy expects; CI pins a
tool to a different version than the lockfile; a host lacks a binary you
assumed; a port binds an interface you did not expect; a documented flag is a
silent no-op. It is chronically under-used: reach for it whenever something
surprised you. The `link-evidence` skill covers anchoring such a fact to an
entity, and what the reply tells you about whether the link was made.
