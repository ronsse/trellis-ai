# Integrate Your Agent with Trellis

The one-page decision tree for wiring an external agent system into Trellis. Pick the path that matches how your agent is built, run the steps, then confirm it worked with the verification step at the end of each path.

```
How does your agent talk to tools?
│
├─ It speaks MCP (Claude Code, Cursor, Claude Desktop, any MCP client)
│     → Path 1: MCP + skills  (one command, no code)
│
├─ It's a Python framework (LangGraph, CrewAI, a custom loop)
│     → Path 2: SDK + hooks  (pip install, wrap your tasks)
│
└─ It's a script, a CI job, or a non-Python service
      → Path 3: REST + CLI  (HTTP calls / shell commands)
```

All three paths read and write the **same substrate**. An agent that records traces over MCP and a CI job that posts traces over REST land in the same graph; a Python agent retrieves a pack that includes everything every other surface wrote.

---

## Path 1 — MCP-speaking agent

For any agent that loads MCP servers: Claude Code, Cursor, Claude Desktop, or any other MCP client. This is the no-code path.

### Steps

1. **Install Trellis and wire it up in one command.**

   ```bash
   pip install trellis-ai          # or, from a checkout: pip install -e ".[dev]"
   trellis admin quickstart --with-skills user
   ```

   `quickstart` initializes local SQLite stores, registers the `trellis` MCP
   server in your Claude Code `settings.json`, and (with `--with-skills user`)
   installs three drop-in skills into `~/.claude/skills/`. Use
   `--with-skills project` to install into `./.claude/skills/` instead, and
   `--scope project` to keep stores beside your code. Run with `--format json`
   if you want machine-readable output.

2. **Restart your agent** so it picks up the new MCP server and skills.

3. **(Optional) Seed demo data** so retrieval has something to return on day one:

   ```bash
   trellis demo load
   ```

### What the three skills make your agent do automatically

The skills install as `/retrieve-before-task`, `/record-after-task`, and
`/link-evidence`. Together they close the loop without you prompting for it
every session:

- **Pre-task retrieve** (`retrieve-before-task`): before non-trivial work, the
  agent calls `get_context` and reasons from prior traces, precedents, and
  graph knowledge instead of re-deriving them.
- **Post-task record** (`record-after-task`): after meaningful work, the agent
  writes a structured trace via `save_experience` and grades it with
  `record_feedback` — this is what makes future retrievals useful.
- **Evidence linking** (`link-evidence`): when the agent learns a durable fact,
  it stores it via `save_memory` and attaches it to the relevant graph entity
  with `save_knowledge`, so the fact resurfaces from any future task that
  touches the same area.

You can install or reinstall the skills on their own at any time with
`trellis admin install-skills user` (or `project`). It is idempotent — existing
skills are skipped and reported; pass `--force` to overwrite.

### How you know it worked

In your agent, ask it to **call `get_context`** with a one-sentence intent
(e.g. *"call get_context for: improve retry handling in the payments client"*).
A working install returns token-budgeted markdown — a context pack — rather than
a "tool not found" error. If you ran `trellis demo load`, the pack will contain
seeded items; on an empty store it returns an explicit "no relevant context"
note, which still proves the round-trip works.

You can also confirm the binary and stores from a shell:

```bash
trellis-mcp --help          # MCP server is on PATH
trellis admin health        # stores are healthy
```

### Per-client setup notes

- **Claude Code** — [mcp-claude-code.md](mcp-claude-code.md)
- **Cursor** — [mcp-cursor.md](mcp-cursor.md)
- **Claude Desktop** — [mcp-claude-desktop.md](mcp-claude-desktop.md)

---

## Path 2 — Python agent framework

> **Status: landing shortly.** The `trellis_sdk.hooks` module described below
> lands on branch `wp1-sdk-hooks`. It is **not on `main` yet** — use Path 3
> (REST + CLI) today if you need to integrate before that branch merges. The
> steps here document the target shape so you can plan for it.

For LangGraph, CrewAI, or a custom Python loop, integrate through the SDK and
its task hooks rather than MCP. The hooks are deliberately thin wrappers around
the same REST surface.

### Steps

1. **Install and start a server** the SDK can talk to (the SDK is HTTP-only —
   there is no in-process "local mode"):

   ```bash
   pip install trellis-ai
   trellis admin serve            # REST API + UI on http://127.0.0.1:8420
   ```

2. **Wrap your tasks** with the three hooks from `trellis_sdk.hooks`:

   - `ContextInjector` — pulls a context pack before a task runs and injects it
     into the agent's prompt/state.
   - `TraceRecorder` — records a structured trace when the task completes.
   - `ResultFeedback` — grades the outcome so item-level attribution can tune
     future retrieval.

   Reference implementations (these example files land alongside the hooks
   branch): `examples/hooks_generic_workflow.py` for a framework-agnostic loop,
   and `examples/langgraph_agent.py` for a LangGraph node wiring.

3. **Rely on the never-raise degradation contract.** The hooks are designed to
   never throw into your agent's control flow — if Trellis is unreachable or a
   call fails, the hook logs and degrades to a no-op rather than crashing the
   task. Your agent keeps running with or without Trellis; integration is
   additive, never load-bearing for correctness.

### How you know it worked

With the server running, hit it directly to confirm the surface the hooks call:

```bash
curl -s http://127.0.0.1:8420/api/version
```

Then run your hook-wrapped agent once and confirm a trace landed:

```bash
trellis retrieve traces --limit 5 --format json
```

A trace whose `intent` matches the task you just ran means `TraceRecorder` is
writing through. A subsequent run that surfaces that trace in its injected pack
means `ContextInjector` is reading the loop back.

---

## Path 3 — Scripts, CI, or non-Python services

For anything that can make an HTTP call or run a shell command: bash scripts,
CI pipelines, Go/Rust/Node services. Use the REST API and the CLI directly. This
path is fully available on `main` today.

### Steps

1. **Run the API** (or point at an existing shared deployment):

   ```bash
   trellis admin serve --port 8420
   ```

   Base path is `/api/v1/`.

2. **Record a trace** when a job finishes:

   ```bash
   curl -s -X POST http://127.0.0.1:8420/api/v1/traces \
     -H 'Content-Type: application/json' \
     -d '{
       "source": "ci.deploy",
       "intent": "deploy orders-api v2.3",
       "steps": [{"step_type": "note", "name": "deploy",
                  "result": {"ok": true}}],
       "outcome": {"status": "success", "summary": "Canary promoted, no errors."},
       "context": {"domain": "infra"}
     }'
   ```

   Or from the CLI, against the local stores without a server:

   ```bash
   trellis ingest trace --file ./trace.json --format json
   ```

3. **Retrieve a context pack** before a job acts:

   ```bash
   curl -s -X POST http://127.0.0.1:8420/api/v1/packs \
     -H 'Content-Type: application/json' \
     -d '{"intent": "diagnose deploy failure", "domain": "infra", "max_items": 20}'
   ```

### How you know it worked

The `POST /api/v1/traces` call returns JSON with the new trace's id and an
`ok`/success status. Confirm it persisted by reading it back:

```bash
curl -s 'http://127.0.0.1:8420/api/v1/search?q=orders-api&domain=infra'
```

A non-empty result set that includes your trace means the write committed and
is retrievable.

### Reference

- [docs/agent-guide/operations.md](../agent-guide/operations.md) — full REST,
  CLI, MCP, and Python mutation API reference (every endpoint and flag).
- [docs/agent-guide/trace-format.md](../agent-guide/trace-format.md) —
  constructing valid trace JSON.

---

## What runs on the Trellis side

Two server-side processes back all three paths. Path 1 (MCP) runs the stores
in-process and needs neither, but a shared or production deployment runs both.

### The API + UI server — `trellis admin serve`

```bash
trellis admin serve --port 8420
```

Serves the REST API (`/api/v1/`), the version/health endpoints, and the web UI
(`/ui`). This is the surface the SDK (Path 2) and HTTP clients (Path 3) talk to,
and the way multiple agents share one substrate. By default it binds loopback;
set `TRELLIS_API_HOST=0.0.0.0` (or `--host 0.0.0.0`) for container deployments.

### The curation loops — `trellis worker`

> **Status: landing shortly.** The `trellis worker` commands
> (`curate`, `tune`, `enrich`, `mine-precedents`) land on branch
> `wp3-worker-commands` and are **not on `main` yet**. The concepts they
> automate already exist — see below — but the scheduled-loop CLI is forthcoming.

These are the background loops that keep the substrate useful as production
moves: demoting noisy items, tuning retrieval weights from feedback
attribution, enriching items with LLM-derived tags, and mining recurring traces
into reusable precedents. They consume the same EventLog-authoritative feedback
signal that the skills and hooks produce. For the concepts — refresh modes,
schedule boundaries, and curator workflows — see
[docs/agent-guide/freshness-and-curation.md](../agent-guide/freshness-and-curation.md).
A dedicated operations guide, `running-trellis.md`, is forthcoming.

---

## Next steps

- **Path 1 details:** [mcp-claude-code.md](mcp-claude-code.md) and the
  [skills/](../../skills/) templates.
- **Decisions before a team / production rollout:**
  [setup-decisions.md](setup-decisions.md) — domains & ontology, ownership,
  API security.
- **The full surface map:** [../agent-guide/surfaces.md](../agent-guide/surfaces.md)
  — which of REST / MCP / SDK to use for each capability.
