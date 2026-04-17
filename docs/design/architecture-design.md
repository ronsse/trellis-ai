# Architecture Design Document: Trellis (XPG)

Architectural patterns, storage abstractions, integration strategies, and temporal features necessary to scale the Trellis from a local tool to a highly flexible, cloud-native memory layer.

---

## 1. Pluggable Storage Architecture

To seamlessly swap between SQLite, distributed Postgres, or dedicated graph/vector databases without rewriting core logic, the best approach is the **Repository Pattern** combined with the **Ports and Adapters (Hexagonal) Architecture**.

Instead of the core application knowing *how* to save a trace, it only knows *that* it needs to save a trace using a defined interface (the Port). The specific database connector (the Adapter) implements that interface.

**Implementation Strategy:**

* **Abstract Base Classes (ABCs):** Define strict Python ABCs for stores (`BaseGraphStore`, `BaseVectorStore`, `BaseTraceStore`, `BaseBlobStore`).
* **Dependency Injection (DI):** When initializing the XPG client or service, inject the specific storage classes based on a configuration file (e.g., `trellis.yaml`).
* **The Pattern:**
  * *Local Mode:* Injects `SQLiteGraphStore`, `ChromaVectorStore`, `LocalBlobStore`.
  * *Cloud Mode:* Injects `NeptuneGraphStore`, `PgVectorStore`, `S3BlobStore`.

## 2. Document & File Storage (The Obsidian Question)

Obsidian is fantastic for human-readable local curation, but it tightly couples the system to a local markdown folder structure. It should be treated as just one of many possible "Blob/Document" sources, not the core storage engine.

**The Abstraction:** Create a unified `BlobStore` interface.

* **Local Backend:** A simple filesystem wrapper that understands folder hierarchies (this handles Obsidian vaults natively).
* **Cloud Backend:** An S3/GCS wrapper.

When evidence or documents are ingested, XPG saves the raw file to the `BlobStore` and writes the *metadata and URI* (e.g., `file:///local/path.md` or `s3://bucket/path.md`) into the Graph/Trace store. This ensures the graph remains lightweight and queryable while large files are stored appropriately for the environment.

## 3. Context Retrieval: How Agents Know What to Pick Up

This is the core differentiator between RAG and an Trellis. Agents don't just search for text; they navigate memory.

**The Retrieval Flow:**

1. **Entry Point (Vector/Semantic):** The agent submits its current intent or task. XPG uses the `VectorStore` to find the most semantically relevant *Entities* or *Past Traces*.
2. **Graph Traversal:** Once an entry node is found, XPG traverses the graph to pull connected "Precedents" (curated learnings) and "Policies" (rules).
3. **Pack Assembly:** XPG packages this subgraph into a structured JSON/XML prompt block (the "Pack") and returns it to the agent.

**Context Observability (Not Agent Eval):**
You need to know if the context XPG provided actually helped.

* **Telemetry:** When XPG assembles a pack, it logs a `ContextRetrievalEvent` with the injected node IDs.
* **Feedback Loop:** When the agent finishes its task, the resulting trace must include a boolean or score indicating success. By joining the task success rate to the injected nodes, you can observe which "Precedents" are actually useful and which are hallucination-inducing noise.

## 4. API Service Layer: REST via FastAPI

To support both seamless local execution and cloud deployment, **REST via FastAPI** is the strongly recommended path.

* **Why REST over gRPC:** The modern AI agent ecosystem (LangGraph, CrewAI, local agent tools) predominantly communicates via standard HTTP/JSON or the newer Model Context Protocol (MCP). Forcing these tools to compile and use gRPC stubs adds immense friction.
* **Why FastAPI:** It automatically generates OpenAPI specifications. You can feed this spec directly into an LLM, and the LLM instantly knows how to call the XPG API to read/write memory.
* **Deployment:** Locally, it runs as a lightweight background process (`uvicorn`). In the cloud, it containerizes perfectly into Kubernetes or Cloud Run.

## 5. Automated Knowledge Ingestion (Data Lineage)

Auto-generating graph relationships from existing data stack tools is a massive value multiplier. Instead of manual ingestion, agents get instant institutional memory.

* **The dbt Example:** A simple Python worker can parse a `target/manifest.json` file.
  * `nodes` become Graph Entities (type: `model`, `seed`, `snapshot`).
  * `depends_on` lists become the directional edges.
  * `description` fields are chunked and sent to the vector store.

* **The Spark Example:** You can hook into the Spark `LogicalPlan` or use a lineage tool like OpenLineage/Spline. The worker parses the JSON event stream, creating entities for `DataSources` and `Transformations`, linking them via `reads_from` and `writes_to` edges.

## 6. MLOps & Agent Framework Bindings

There must be a clear separation of concerns: **XPG is the Memory Layer; Braintrust is the Evaluation/Experimentation Layer.**

* **The Braintrust Interaction:** When testing an agent prompt in Braintrust, the agent calls the XPG API to retrieve context. Braintrust scores the final output. You can use a webhook to send the successful Braintrust trace back to XPG to be stored as a new "Precedent."
* **Coding Assistants (Cline, Cursor):** The best hook here is implementing an **MCP (Model Context Protocol) Server** within XPG. Cline and other modern assistants natively support MCP. By exposing XPG via MCP, Cline can autonomously query `read_precedent` or execute `write_evidence` directly from your IDE without building custom extensions for each editor.
* **Orchestrators (LangGraph, CrewAI):** Provide a lightweight Python SDK (`trellis-sdk`) that wraps FastAPI endpoints. In LangGraph, XPG becomes a standard "Tool" node that agents can route to when they need historical context.

## 7. Curation Workers vs. Coding Agents

Having dedicated background workers (`trellis_workers`) utilizing local LLMs is an excellent design for *continuous graph maintenance* (e.g., entity deduplication, clustering similar traces into a new proposed Precedent).

However, you should design the curation pipeline as a set of standard API endpoints (e.g., `/api/v1/curate/merge_nodes`). This allows you to choose your worker:

1. A dedicated background cron job running a local LLM.
2. A general-purpose coding agent that you spin up, hand the XPG API spec to, and prompt: *"Review the traces from the last 24 hours and propose merged precedents."*

## 8. Temporal Graph Features

If you want an agent to ask, *"What was the deployment policy back in January?"*, you need temporal tracking. Since you are abstracting the storage layer, you cannot rely on database-specific temporal features (like Postgres temporal tables).

**Implementation:** Implement Slowly Changing Dimensions (SCD Type 2) or Bitemporal modeling directly in the Graph Schema layer.

* Every Edge and Node gets `valid_from` and `valid_to` timestamps.
* When an entity is updated, you do not overwrite it. You cap the `valid_to` of the old node and insert a new node.
* The `trellis` retrieval engine handles the logic: If a query doesn't specify a time, it automatically filters for `valid_to IS NULL` (current state). If the agent asks for a historical context, the engine filters for nodes valid during that timestamp.
