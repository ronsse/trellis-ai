# Trellis Examples

> **Status: preview.** These examples are in flux while parallel work lands (SDK is going HTTP-only, stores are being reshaped). Expect breaking changes before the next minor release. Copy with care — treat them as illustrative scaffolds, not stable contracts.

Runnable examples that demonstrate the most common ways to integrate Trellis into an agent or workflow.

Every example assumes you have already run:

```bash
pip install -e ".[dev]"
trellis admin init
trellis demo load     # optional — seeds ~66 realistic items so retrieval has something to chew on
```

| Example | What it shows |
|---------|---------------|
| [sdk_local_demo.py](sdk_local_demo.py) | Use the Python SDK in **local mode** (no server) — ingest a trace, search, assemble a context pack. |
| [sdk_remote_demo.py](sdk_remote_demo.py) | Same flow against the **REST API** (`trellis admin serve`). One line change. |
| [retrieve_before_task.py](retrieve_before_task.py) | The "retrieve → act → record" loop, the canonical pattern for agents with institutional memory. |
| [custom_extractor.py](custom_extractor.py) | Define your own deterministic extractor that turns a JSON source into entity/edge drafts. |
| [custom_classifier.py](custom_classifier.py) | Add a domain-specific `Classifier` that tags items at ingest time. |
| [langgraph_agent.py](langgraph_agent.py) | Plug Trellis tools into a LangGraph ReAct agent. |
| [batch_ingest.sh](batch_ingest.sh) | Pipe a directory of trace JSON files through the CLI in one pass. |
| [mcp_claude_code.md](mcp_claude_code.md) | End-to-end walkthrough of using the MCP server from Claude Code, with example prompts. |

## Running an example

```bash
python examples/sdk_local_demo.py
```

For the remote examples, start the API in another terminal first:

```bash
trellis admin serve --port 8420
```

## Where to go next

- [docs/agent-guide/operations.md](../docs/agent-guide/operations.md) — full CLI / REST / MCP / SDK reference.
- [docs/agent-guide/playbooks.md](../docs/agent-guide/playbooks.md) — task-shaped recipes.
- [docs/getting-started/](../docs/getting-started/) — IDE-specific setup guides for Claude Code, Cursor, and Claude Desktop.
