# Context Economy Strategy: MCP vs CLI vs Skills

A strategic layering of MCP, CLI, and agent skills to maximize token efficiency while maintaining broad integration compatibility.

---

## The Problem: MCP Context Bloat

While the Model Context Protocol (MCP) is a fantastic standardization effort, it is notorious for context bloat. A well-designed CLI paired with specific agent skills (function calls) is frequently much more effective at managing the token budget.

When dealing with complex data architectures, ML pipelines, or massive context graphs, token economy is everything. If the agent spends 30k tokens just reading tool descriptions and raw JSON responses, its reasoning degrades.

## Why MCP Bloats Context

### Schema Overhead (The Dictionary)
An MCP server exposes its capabilities by sending massive JSON schemas to the client (like Cline or Cursor). If the Trellis has 20 different endpoints for querying traces, evidence, and relationships, the LLM has to carry that massive "tool dictionary" in its system prompt for every single turn of the conversation.

### Unfiltered Payloads
MCP servers typically return raw, structured data. If an agent asks for a user's past dbt modeling traces, an MCP server might dump 15 complete JSON trace objects into the context window.

### Chatty Protocols
MCP often requires multi-step handshakes (list tools, call tool, read resource) which eats up conversational context fast.

## Why CLI + Skills is Leaner

### Single Tool Definition
Instead of giving the agent 20 different graph-querying tools, you give it one skill: `execute_bash_command`.

### Native Filtering
A coding agent can use standard Unix pipelines to filter out the noise before it ever hits the context window. Instead of loading a massive JSON payload, the agent can run:

```bash
trellis retrieve search "dbt models" --format json | jq '.[].summary'
```

### Piping to Files
If a precedent or trace is massive, the agent can use the CLI to pipe the output into a temporary local file, read just the first 50 lines, or run a Python script against it, keeping the LLM's context window pristine.

## The Playbook: When to Use Which (The Hybrid Approach)

The most robust architecture uses a combination of all three, treating them as progressive layers of abstraction.

### Layer 1 -- The Core: CLI (The Execution Layer)

**What it is:** The `trellis` binary that directly interacts with the REST API or local SQLite databases.

**When to use it:** For heavy lifting, bulk ingestion (like parsing Spark or dbt manifests), and local scripting.

**Agent Interaction:** Autonomous coding agents (like OpenDevin or local terminal-bound agents) should primarily interact with XPG through this layer using bash skills. It forces them to be deliberate about what data they print to stdout.

### Layer 2 -- The Middle: Skills / Function Calling (The Routing Layer)

**What it is:** Hand-crafted, highly specific Python functions provided to orchestrators like LangGraph or CrewAI.

**When to use it:** When building a custom, closed-loop agentic workflow where you know exactly what context the agent needs.

**Agent Interaction:** Instead of giving a LangGraph node full access to XPG, you give it a specific skill like `get_latest_successful_trace(task_type)`. The skill executes the XPG query under the hood, summarizes the result into a tight markdown paragraph, and returns only that summary to the agent.

### Layer 3 -- The Surface: MCP (The UX Layer)

**What it is:** A lightweight MCP server wrapping the CLI/API.

**When to use it:** For frictionless, out-of-the-box integration with IDEs like Cursor or coding assistants like Cline, where you want the human and the AI to collaborate easily without writing custom LangChain wrappers.

**Agent Interaction (The Trick):** Do not expose the entire `trellis` raw API through MCP. Instead, build specific "Macro Tools" into the XPG MCP server that are designed to return summarized markdown, not raw JSON.

**Example MCP Tool:** `get_experience_context(intent="AWS deployment")`. The server does the heavy graph traversal, assembles the "Pack," truncates it to the most relevant 2000 tokens, and returns it as plain text.

## The Verdict

Build the CLI first; it's the bedrock. Wrap it in a REST API for distributed deployments. Then, build a lightweight MCP Server that acts strictly as a curator, using the CLI under the hood to fetch data, but formatting and summarizing it aggressively before handing it back to the coding assistant.
