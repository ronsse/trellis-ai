# A system prompt for models that call Trellis tools

The Trellis skills teach the retrieve, record, grade loop to Claude Code. A
model you call with an API key, a framework's system message and a dispatched
subagent read no skill, so they get the loop from text you hand them. This
page ships that text and says where to paste it.

The loop, in three lines:

1. Before non-trivial work, call `get_context`; the pack it returns names a
   `pack_id` and an `item_id` for each item.
2. After the work, call `save_experience` with a trace of what the next agent
   would otherwise rediscover.
3. Grade the pack with `record_feedback`, naming `helpful_item_ids` and
   `unhelpful_item_ids`; feedback that names no items never reaches the
   learning loop.

The text is packaged with Trellis in three variants. Print one from a shell, or
read it from Python:

```bash
trellis admin show-prompt                          # the system prompt
trellis admin show-prompt --variant claude-md      # a CLAUDE.md snippet
trellis admin show-prompt --variant agent-rule     # the subagent paragraph
trellis admin show-prompt --format json | jq -r .text
```

```python
from trellis_cli.prompts import load_prompt

system_prompt = load_prompt("system")  # or "claude-md", "agent-rule"
```

The blocks below are the packaged files, and a test fails if this page and the
files differ.

## The system prompt

Use this variant wherever you control the system message of a model that has
the Trellis tools.

```text
You have Trellis memory tools. Use them as a loop, not a lookup.

1. Before any non-trivial task, call get_context(intent=<one sentence: what
   you are about to do and why>, session_id=<a stable id for this
   conversation>, domain=<area, if you know it>). Read the pack before you
   read anything else. Its header carries the pack_id on a "**pack_id:**"
   line, and each item is headed "## [type] `item_id`": keep both. If the
   response says "No context found", that is a real answer: proceed, and
   skip step 3 unless a pack_id was shown.

2. After the work, call save_experience(trace_json=<a JSON string>) with a
   trace: source "agent", intent, context {domain}, 2-6 steps (each has a
   step_type and a name; a step's `result` is a JSON object, never a bare
   string, and a failure goes in the step's `error` as a string), and an outcome
   {status: success|failure|partial, summary}. Record what the next agent
   would otherwise have to rediscover: the workaround, the wrong
   assumption, the command that worked.

3. Grade the pack you were served: record_feedback(pack_id=<from step 1>,
   rating=<0.0-1.0>, helpful_item_ids=[ids you used],
   unhelpful_item_ids=[ids that cost you time or were wrong]). Feedback
   that names no item ids does not reach the learning loop. If the pack
   listed advisories and you acted on one, add followed_advisory_ids=[...].
   An honest low rating is worth more than a polite high one.

4. When you learn a durable fact about a named thing (a service, a host, a
   dataset), call save_knowledge(name=..., content=<the fact and where it
   lives>, properties={"domain": ...}). It always creates a new node, so
   call search(query=<name>) first if the node may already exist.

Retrieval that never happens is the common failure, not retrieval that
returns nothing.
```

It names the MCP tools (`get_context`, `save_experience`, `record_feedback`,
`save_knowledge`, `search`). If you wrap Trellis under other tool names, change
the names in the text to match. The LangGraph template in
`examples/integrations/langgraph/` is one such wrapper: its tools are named
`trellis_get_context`, `trellis_save_trace` and so on, and it has no grading
tool, so grade packs from your own code with `TrellisClient.record_feedback`
and delete step 3 from the text you give the model.

## Where to paste it

| Host | Where the text goes | Variant |
|---|---|---|
| Messages API | the `system` parameter of `messages.create` | `system` |
| OpenAI | the `instructions` parameter of the Responses API, or the system message of Chat Completions | `system` |
| Cursor | a project rule (`.cursor/rules/`, or the older `.cursorrules`) | `claude-md` |
| Claude Desktop | the project's custom instructions | `system` |
| LangGraph or another framework | the system message of the agent node that holds the tools | `system` |

For the Messages API:

```python
import anthropic
from trellis_cli.prompts import load_prompt

client = anthropic.Anthropic()
reply = client.messages.create(
    model=MODEL,
    max_tokens=2048,
    system=load_prompt("system"),
    tools=TRELLIS_TOOLS,  # the Trellis tools, from your MCP client or wrappers
    messages=[{"role": "user", "content": task}],
)
```

For OpenAI's Responses API:

```python
from openai import OpenAI
from trellis_cli.prompts import load_prompt

client = OpenAI()
reply = client.responses.create(
    model=MODEL,
    instructions=load_prompt("system"),
    tools=TRELLIS_TOOLS,
    input=task,
)
```

Claude Desktop's per-client setup, including its own system-prompt note, is in
[mcp-claude-desktop.md](mcp-claude-desktop.md); Cursor's is in
[mcp-cursor.md](mcp-cursor.md).

## What we do not know

We do not know whether a given host forwards the MCP server's `instructions`
to the model, which is why the system prompt is the dependable channel: the
text above reaches the model because you put it there.

## The other two variants

### `claude-md`

A short section for `CLAUDE.md`, `AGENTS.md` or a Cursor rule: the standing
instructions a coding agent loads at the start of every session in a
repository. It is the system prompt cut to three bullets, plus the sentence
that it applies to every dispatched subagent.

```markdown
## Trellis memory

Trellis memory tools are connected. Use them as a loop, not a lookup.

- Before non-trivial work, call get_context(intent=<what you are about to
  do and why>, session_id=<a stable id for this session>). Keep the pack_id
  from the pack header and the item ids in backticks. "No context found" is
  a real answer: proceed.
- After meaningful work, call save_experience with a trace. A step's
  `result` is a JSON object, never a bare string; a failure goes in the
  step's `error` as a string.
- Grade the pack: record_feedback(pack_id=..., rating=0.0-1.0,
  helpful_item_ids=[...], unhelpful_item_ids=[...]). Feedback that names no
  item ids does not reach the learning loop. Skip it only if no pack_id was
  shown.
- This applies to every dispatched subagent. Retrieval and grading are part
  of the task, not extra scope.
```

### `agent-rule`

One paragraph for an agent definition, such as the body of a file in
`.claude/agents/`. A dispatched subagent loads its own definition and the
repository's `CLAUDE.md`, not skills, so a rule that lives only in a skill
never reaches it. The paragraph is short because it is paid for on every
subagent call.

```text
Before reading files, call get_context(intent=<your task>, session_id=<your
agent id or the task id>). Before you finish, grade the pack with
record_feedback(pack_id=..., rating=..., helpful_item_ids=[...],
unhelpful_item_ids=[...]). This is part of every task, not scope creep. An
empty pack is a valid answer: say so, continue, and grade it only if a
pack_id was shown.
```

Add it to every agent definition you dispatch, and to `CLAUDE.md`, so the
subagents see it.

`trellis admin install-skills` and `trellis admin quickstart --with-skills`
print this paragraph when they finish, and install a fourth skill,
`trellis-loop`, that carries it. The skill helps the session you install it
into; the paragraph is what reaches the subagents.

## Choosing

- You call a model with an API key, or write a framework's system message:
  `system`.
- You maintain a repository's `CLAUDE.md`, `AGENTS.md` or Cursor rules:
  `claude-md`.
- You maintain agent definitions that get dispatched: `agent-rule`, and
  `claude-md` in the repository.

Then check that it worked the way
[integrate-your-agent.md](integrate-your-agent.md) describes: ask the agent to
call `get_context`, and look for a `pack_id` in the reply.
