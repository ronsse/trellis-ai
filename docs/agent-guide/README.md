# Agent Guide

Operational reference for AI agents interacting with the Trellis. These documents provide exact schemas, CLI commands, REST API endpoints, MCP tools, SDK methods, and step-by-step playbooks needed to read from and write to the shared experience store. They are framework-agnostic and applicable to any LLM-based agent.

## Contents

| Document | Description |
|----------|-------------|
| [modeling-guide.md](modeling-guide.md) | **Read first before designing an ingestion runner.** How to decide what becomes a node, a property, or a document. Covers the three node roles (structural / semantic / curated), the four-question test, anti-patterns with named failure modes, and worked examples for database catalogs and code repositories. |
| [trace-format.md](trace-format.md) | Complete reference for constructing and ingesting valid trace JSON |
| [schemas.md](schemas.md) | Machine-readable catalog of all Pydantic schemas with field definitions and examples |
| [operations.md](operations.md) | Full CLI, REST API, MCP, SDK, and Python mutation API reference |
| [playbooks.md](playbooks.md) | Step-by-step operational procedures for common agent tasks |
| [tiered-context-retrieval.md](tiered-context-retrieval.md) | Sectioned pack assembly for multi-agent workflows |
| [tagging-for-retrieval.md](tagging-for-retrieval.md) | How to write knowledge base content so the tagging pipeline lands it in the right retrieval tier |
| [pack-quality-evaluation.md](pack-quality-evaluation.md) | Assembly-time pack scoring — five dimensions, built-in profiles, scenario fixtures, optional `PackBuilder` hook |

## Integration Layers

| Layer | Entry Point | Best For |
|-------|-------------|----------|
| CLI (`trellis`) | `trellis <command>` | Scripts, CI/CD, human operators, agent tool calls |
| REST API | `trellis admin serve` / `trellis-api` | Distributed deployments, SDK remote mode |
| MCP Macro Tools | `trellis-mcp` | IDE integrations (Cursor, Cline, Claude Code) |
| Python SDK | `from trellis_sdk import TrellisClient` | Orchestrators (LangGraph, CrewAI), custom agents |
