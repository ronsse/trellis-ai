# Trellis — LangGraph Reference Template

> **Reference template — not a published package.** Copy [`tools.py`](tools.py) into your own project rather than importing from this repo. The Trellis core package (`trellis-ai`) is what's published to PyPI; integrations live here as starting points you adapt.

Add structured institutional memory to LangGraph agents. Agents retrieve context before tasks, record traces of their work, and build a shared knowledge graph — so your team's agents learn from each other.

## How to use this template

1. Copy `tools.py` into your project (e.g. `myproject/trellis_tools.py`).
2. Install the dependencies it needs:
   ```bash
   pip install "trellis-ai" langgraph langchain-core
   ```
3. Initialize the store substrate once: `trellis admin init`.
4. Import and use:

```python
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from myproject.trellis_tools import create_xpg_tools

# Local mode — no Trellis API server needed
xpg_tools = create_xpg_tools()

# Or remote mode (via REST API)
# xpg_tools = create_xpg_tools(base_url="http://localhost:8420")

model = ChatOpenAI(model="gpt-4o")
agent = create_react_agent(model, xpg_tools)

response = agent.invoke({
    "messages": [{"role": "user",
                  "content": "Check what we know about auth-service before making changes"}]
})
```

For a full runnable demo, see [`examples/langgraph_agent.py`](../../langgraph_agent.py) at the repo root.

## Available Tools

| Tool | Purpose |
|------|---------|
| `xpg_get_context` | Retrieve relevant context before starting a task |
| `xpg_search` | Search documents and entities |
| `xpg_save_trace` | Record a trace of completed work |
| `xpg_save_knowledge` | Create entities in the knowledge graph |
| `xpg_recent_activity` | Summarize recent activity |

## Patterns

### Retrieve-Act-Record Loop

The core pattern for agents with institutional memory:

```python
from langgraph.graph import StateGraph, MessagesState

from myproject.trellis_tools import create_xpg_tools

xpg_tools = create_xpg_tools()

# In your graph, the agent will naturally:
# 1. Call xpg_get_context to check for prior art
# 2. Do its work using other tools
# 3. Call xpg_save_trace to record what happened
```

### Custom Agent with XPG

```python
from langgraph.prebuilt import create_react_agent

# Combine XPG tools with your domain tools
all_tools = xpg_tools + [your_search_tool, your_code_tool]
agent = create_react_agent(model, all_tools)
```

### System Prompt Integration

Guide the agent to use XPG tools at the right time:

```python
system_prompt = """You have access to an experience graph for institutional memory.

Before starting non-trivial work:
- Call xpg_get_context with your task intent

After completing meaningful work:
- Call xpg_save_trace with a JSON trace of what you did

When you discover important entities:
- Call xpg_save_knowledge to add them to the knowledge graph
"""
```

## Configuration

By default, tools use local SQLite stores. For remote mode:

```python
xpg_tools = create_xpg_tools(base_url="http://localhost:8420")
```

Start the API server with:

```bash
trellis admin serve --port 8420
```

## Further Reading

- [Agent Guide — Operations](../../../docs/agent-guide/operations.md) — Full API reference
- [Agent Guide — Playbooks](../../../docs/agent-guide/playbooks.md) — Step-by-step procedures
- [Agent Guide — Schemas](../../../docs/agent-guide/schemas.md) — Data models
