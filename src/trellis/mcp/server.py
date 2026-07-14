"""MCP Macro Tools server — high-level, token-efficient tools for AI agents.

Error contract
--------------
Tool handlers raise :class:`mcp.shared.exceptions.McpError` rather than
returning ``"Error: …"`` strings or dict-shape error payloads. The
FastMCP runtime forwards ``McpError`` directly through the JSON-RPC
transport so clients see a structured error object with a stable
``code`` plus a human-readable ``message``. This is the loud-failure
contract from the silent-fallback cleanup track (C2 Phase 3):

* ``INVALID_PARAMS`` (-32602) — pre-flight argument validation
  (empty intent, unknown operation enum, missing required key, etc.).
* ``RESOURCE_NOT_FOUND`` (-32001, app-layer) — handler asked for an
  entity that doesn't exist (e.g. ``get_graph`` with an unknown id).
* ``MUTATION_FAILED`` (-32003, app-layer) — a mutation went through
  the executor and came back non-success (REJECTED / FAILED). The
  ``data`` field carries the structured executor response.
* ``INTERNAL_ERROR`` (-32603) — unexpected failure inside a tool
  (store outage, pack builder crash, etc.). The original exception
  chains via ``from`` so server-side logs preserve the traceback.

Pre-flight validation returns no longer use string sentinels; callers
that previously did ``if result.startswith("Error:")`` need to catch
``McpError`` instead. The contract is documented in
``docs/design/adr-mcp-contract.md`` and the per-site audit lives in
``audit/silent_fallbacks_2026-05.md``.
"""

from __future__ import annotations

import json
import threading
from typing import Any, NoReturn

import structlog
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from trellis.auth import SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ
from trellis.extract.trace_ingest_hook import run_trace_extraction
from trellis.logging import configure_stderr_logging
from trellis.mcp.auth import (
    TRANSPORT_HTTP,
    HttpSettings,
    TrellisApiKeyVerifier,
    resolve_http_settings,
    resolve_transport,
    set_auth_enforced,
    trellis_scope,
)
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
    ensure_evidence_document,
)
from trellis.ops import ParameterRegistry
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.retrieve.formatters import (
    format_advisories_as_markdown,
    format_lessons_as_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
    format_subgraph_as_markdown,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.rerankers import build_reranker
from trellis.retrieve.strategies import build_strategies
from trellis.retrieve.token_tracker import estimate_tokens, track_token_usage
from trellis.schemas.pack import SectionRequest
from trellis.schemas.trace import Trace
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Custom JSON-RPC error codes (app-layer, -32000..-32099 reserved range)
# ---------------------------------------------------------------------------

#: Caller asked for an entity / resource that does not exist.
RESOURCE_NOT_FOUND = -32001

#: A policy gate denied the operation.
POLICY_DENIED = -32002

#: A governed mutation executed but came back non-success.
MUTATION_FAILED = -32003


def _raise_invalid_params(
    message: str, *, data: dict[str, Any] | None = None
) -> NoReturn:
    """Raise ``McpError(INVALID_PARAMS, …)`` — short for the common case."""
    raise McpError(ErrorData(code=INVALID_PARAMS, message=message, data=data))


def _raise_internal(
    message: str,
    *,
    cause: BaseException | None = None,
    data: dict[str, Any] | None = None,
) -> NoReturn:
    """Raise ``McpError(INTERNAL_ERROR, …)`` chaining the cause if given.

    Centralising this keeps the ``from exc`` chaining consistent — losing
    the chain hides the original traceback from operator logs.
    """
    err = McpError(ErrorData(code=INTERNAL_ERROR, message=message, data=data))
    if cause is not None:
        raise err from cause
    raise err


def _raise_not_found(message: str, *, data: dict[str, Any] | None = None) -> NoReturn:
    """Raise ``McpError(RESOURCE_NOT_FOUND, …)`` — app-layer code."""
    raise McpError(ErrorData(code=RESOURCE_NOT_FOUND, message=message, data=data))


def _raise_mutation_failed(
    message: str, *, data: dict[str, Any] | None = None
) -> NoReturn:
    """Raise ``McpError(MUTATION_FAILED, …)`` — app-layer code."""
    raise McpError(ErrorData(code=MUTATION_FAILED, message=message, data=data))


mcp = FastMCP(
    "trellis",
    instructions=(
        "Trellis — structured memory and learning for AI agents. "
        "All responses are concise markdown optimized for LLM context windows."
    ),
)


_registry: StoreRegistry | None = None


def _get_registry() -> StoreRegistry:
    """Get or create a cached StoreRegistry singleton."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = StoreRegistry.from_config_dir()
    return _registry


def _build_pack_builder(registry: StoreRegistry) -> PackBuilder:
    """Create a PackBuilder with advisory store if available."""
    advisory_store: AdvisoryStore | None = None
    stores_dir = registry.stores_dir
    if stores_dir is not None:
        adv_path = stores_dir / "advisories.json"
        if adv_path.exists():
            advisory_store = AdvisoryStore(adv_path)
    param_registry = ParameterRegistry(registry.operational.parameter_store)
    return PackBuilder(
        strategies=build_strategies(registry, parameter_registry=param_registry),
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
        reranker=build_reranker("rrf", parameter_registry=param_registry),
    )


_minhash_index: Any = None

#: Serializes ``save_memory``'s dedup-and-store critical section. The
#: MinHashIndex lock makes each index call atomic, but the dedup DECISION
#: spans exact-hash check → fuzzy find → document_store.put → index add,
#: which the per-method lock cannot make atomic. Without this, two http
#: worker threads saving the same (or near-identical) content both miss
#: dedup and both persist. save_memory is a write, not a hot path, so
#: serializing its dedup section is cheap. Held only around the decision;
#: event emit / extraction / embedding run outside it.
_save_memory_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Memory extraction pipeline (feature-flagged)
# ---------------------------------------------------------------------------
#
# ``TRELLIS_ENABLE_MEMORY_EXTRACTION=1`` turns on an opt-in extraction stage
# that runs *after* a memory is stored in save_memory.  It routes the
# memory text through the deterministic AliasMatchExtractor + an LLM
# residue extractor (via build_save_memory_extractor), then submits the
# resulting entity / edge drafts through the governed MutationExecutor.
#
# The flag is off by default so existing deployments see no behavior
# change.  All failures are non-fatal — save_memory's success never
# depends on the extraction pipeline.

_memory_extractor: Any = None
_memory_extractor_attempted: bool = False


def _get_memory_extractor(registry: StoreRegistry) -> Any:
    """Build or fetch the cached save_memory extractor.

    Returns ``None`` when:
      * ``TRELLIS_ENABLE_MEMORY_EXTRACTION`` is not set truthy, OR
      * No LLM client can be constructed from the environment.

    Raises ``McpError(INTERNAL_ERROR)`` if the flag is on and the
    extractor module fails to import / construct — the agent asked
    for the feature and a build failure is a real problem they should
    see, not a silently-disabled enhancement.
    """
    global _memory_extractor, _memory_extractor_attempted  # noqa: PLW0603
    if _memory_extractor_attempted:
        return _memory_extractor
    _memory_extractor_attempted = True

    import os  # noqa: PLC0415

    flag = os.environ.get("TRELLIS_ENABLE_MEMORY_EXTRACTION", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None

    try:
        from trellis.extract.save_memory import (  # noqa: PLC0415
            build_save_memory_extractor,
        )

        llm_client = _build_llm_client(registry)
        if llm_client is None:
            logger.info("memory_extractor_skipped_no_llm_client")
            return None

        alias_resolver = _build_alias_resolver(registry)
        _memory_extractor = build_save_memory_extractor(
            alias_resolver=alias_resolver,
            llm_client=llm_client,
        )
        logger.info("memory_extractor_enabled")
    except McpError:
        # Already structured — let it propagate.
        raise
    except Exception as exc:
        logger.exception("memory_extractor_init_failed")
        _raise_internal(
            f"memory extractor construction failed: {exc}",
            cause=exc,
            data={"stage": "memory_extractor_init"},
        )
    return _memory_extractor


def _build_llm_client(registry: StoreRegistry) -> Any:
    """Construct an LLMClient, preferring the registry config over env vars.

    First tries ``registry.build_llm_client()`` (driven by the ``llm:``
    block in ``~/.trellis/config.yaml``). If that returns ``None`` — either
    because no config is present or the configured provider couldn't be
    instantiated — falls back to the env-var path in
    :func:`_build_llm_client_from_env`. Returns ``None`` when neither
    source yields a client.
    """
    try:
        client = registry.build_llm_client()
    except Exception:
        # GRACEFUL-DEGRADATION: registry config is the preferred source
        # but the env-var path below is an explicit, documented fallback
        # for deployments without a populated ``llm:`` block. Logged at
        # exception level so config drift is visible in stderr.
        logger.exception("llm_client_registry_failed")
        client = None
    if client is not None:
        logger.debug("llm_client_from_registry")
        return client

    client = _build_llm_client_from_env()
    if client is not None:
        logger.debug("llm_client_from_env")
    return client


def _build_llm_client_from_env() -> Any:
    """Construct an LLMClient from env-var-provided API keys.

    Prefers OpenAI when ``OPENAI_API_KEY`` is set, falls back to
    Anthropic when ``ANTHROPIC_API_KEY`` is set.  Returns ``None`` when
    neither is available or the corresponding optional extra isn't
    installed.
    """
    import os  # noqa: PLC0415

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from trellis.llm.providers.openai import (  # noqa: PLC0415
                OpenAIClient,
            )

            return OpenAIClient()
        except ModuleNotFoundError:
            # GRACEFUL-DEGRADATION: optional [llm-openai] extra not
            # installed — fall through to Anthropic.
            logger.debug("llm_client_openai_not_installed")
        except Exception as exc:
            logger.exception("llm_client_openai_init_failed")
            _raise_internal(
                f"OpenAI client construction failed: {exc}",
                cause=exc,
                data={"provider": "openai"},
            )

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from trellis.llm.providers.anthropic import (  # noqa: PLC0415
                AnthropicClient,
            )

            return AnthropicClient()
        except ModuleNotFoundError:
            # GRACEFUL-DEGRADATION: optional [llm-anthropic] extra not
            # installed — caller gets ``None`` and the extraction stage
            # is skipped.
            logger.debug("llm_client_anthropic_not_installed")
        except Exception as exc:
            logger.exception("llm_client_anthropic_init_failed")
            _raise_internal(
                f"Anthropic client construction failed: {exc}",
                cause=exc,
                data={"provider": "anthropic"},
            )

    return None


def _build_alias_resolver(registry: StoreRegistry) -> Any:
    """Build a callable that resolves @mention strings to entity IDs.

    Uses a case-insensitive name match against the graph store, scanned
    lazily on each invocation.  Not suitable for large graphs — the
    production implementation will want an indexed lookup — but fine
    for the feature-flagged Phase 2 rollout.
    """
    graph_store = registry.knowledge.graph_store

    def resolve(alias: str) -> list[str]:
        target = alias.lower()
        matches: list[str] = []
        try:
            nodes = graph_store.query(limit=2000)
        except Exception as exc:
            logger.exception("alias_resolver_query_failed", alias=alias)
            _raise_internal(
                f"alias resolver graph query failed: {exc}",
                cause=exc,
                data={"alias": alias},
            )
        for node in nodes:
            name = str(node.get("name", "")).lower()
            if name == target:
                entity_id = node.get("entity_id") or node.get("id")
                if entity_id:
                    matches.append(str(entity_id))
        return matches

    return resolve


def _run_memory_extraction(
    registry: StoreRegistry,
    extractor: Any,
    doc_id: str,
    content: str,
) -> None:
    """Dispatch extraction and route drafts through the MutationExecutor.

    Fully best-effort: any failure is logged at debug level and the
    caller continues.  save_memory's success contract is "the document
    is stored and MEMORY_STORED is emitted" — extraction is a bonus.
    """
    try:
        import asyncio  # noqa: PLC0415

        from trellis.extract.commands import result_to_batch  # noqa: PLC0415
        from trellis.extract.context import ExtractionContext  # noqa: PLC0415

        context = ExtractionContext(
            allow_llm_fallback=True,
            max_llm_calls=1,
            max_tokens=400,
        )
        result = asyncio.run(
            extractor.extract(
                {"doc_id": doc_id, "text": content},
                source_hint="save_memory",
                context=context,
            )
        )
        if not result.entities and not result.edges:
            return

        batch = result_to_batch(result, requested_by="mcp:save_memory")
        build_curate_executor(registry).execute_batch(batch)
    except Exception:
        # GRACEFUL-DEGRADATION: the save_memory contract is "the document
        # is stored + MEMORY_STORED emitted". Tiered extraction is a
        # feature-flagged bonus pass and its failure must never roll back
        # a successful memory write. Logged at exception level so the
        # operator can spot persistent extraction breakage in stderr.
        logger.exception(
            "memory_extraction_failed",
            doc_id=doc_id,
        )


def _get_minhash_index(registry: StoreRegistry) -> Any:
    """Get or create a cached MinHash index for fuzzy dedup.

    Lazily populates the index from the document store on first access.
    Raises ``McpError(INTERNAL_ERROR)`` if the dedup module is broken —
    silent disable used to mean memories were stored without fuzzy
    dedup, producing invisible duplicates.
    """
    global _minhash_index  # noqa: PLW0603
    if _minhash_index is not None:
        return _minhash_index
    try:
        from trellis.classify.dedup.minhash import MinHashIndex  # noqa: PLC0415

        _minhash_index = MinHashIndex()
        # Seed the index from existing documents (up to a reasonable limit).
        docs = registry.knowledge.document_store.search("", limit=500)
        for doc in docs:
            _minhash_index.add(doc["doc_id"], doc.get("content", ""))
        logger.debug("minhash_index_initialized", size=_minhash_index.size)
    except Exception as exc:
        logger.exception("minhash_index_init_failed")
        _raise_internal(
            f"MinHash dedup index initialisation failed: {exc}",
            cause=exc,
            data={"stage": "minhash_index_init"},
        )
    return _minhash_index


# ---------------------------------------------------------------------------
# Macro Tool 1: get_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_context(  # noqa: PLR0912, PLR0915
    intent: str,
    domain: str | None = None,
    max_tokens: int = 2000,
    session_id: str = "",
) -> str:
    """Get relevant context from the experience graph for a task or question.

    Searches documents, knowledge graph, and past traces, then returns
    a summarized markdown pack optimized for your context window.

    Args:
        intent: What you're trying to do or learn about.
        domain: Optional domain scope (e.g., "platform", "data").
        max_tokens: Maximum response size in tokens (default 2000).
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    registry = _get_registry()
    items: list[dict[str, Any]] = []

    # Search documents — a store outage here used to silently drop the
    # document axis from the merged pack. Loud raise instead.
    try:
        filters: dict[str, Any] = {}
        if domain:
            filters["domain"] = domain
        doc_results = registry.knowledge.document_store.search(
            intent, limit=10, filters=filters
        )
        items.extend(
            {
                "item_id": doc["doc_id"],
                "item_type": "document",
                "excerpt": doc.get("content", "")[:500],
                "relevance_score": abs(doc.get("rank", 0.0)),
            }
            for doc in doc_results
        )
    except Exception as exc:
        logger.exception("get_context_doc_search_failed")
        _raise_internal(
            f"document search failed: {exc}",
            cause=exc,
            data={"stage": "doc_search", "intent": intent},
        )

    # Search graph
    try:
        nodes = registry.knowledge.graph_store.query(limit=20)
        q_lower = intent.lower()
        for node in nodes:
            props = node.get("properties", {})
            name = str(props.get("name", "")).lower()
            desc = str(props.get("description", "")).lower()
            if q_lower in name or q_lower in desc:
                items.append(
                    {
                        "item_id": node["node_id"],
                        "item_type": "entity",
                        "excerpt": props.get("name", "")
                        or props.get("description", ""),
                        "relevance_score": 0.5,
                    }
                )
    except Exception as exc:
        logger.exception("get_context_graph_search_failed")
        _raise_internal(
            f"graph search failed: {exc}",
            cause=exc,
            data={"stage": "graph_search", "intent": intent},
        )

    # Recent traces
    try:
        traces = registry.operational.trace_store.query(domain=domain, limit=5)
        items.extend(
            {
                "item_id": t.trace_id,
                "item_type": "trace",
                "excerpt": t.intent[:300],
                "relevance_score": 0.3,
            }
            for t in traces
        )
    except Exception as exc:
        logger.exception("get_context_trace_search_failed")
        _raise_internal(
            f"trace search failed: {exc}",
            cause=exc,
            data={"stage": "trace_search", "intent": intent},
        )

    # Semantic search — only when an embedder + vector store are configured
    # (the same pair the embed-on-ingest hook writes through). Unlike the
    # three core axes above, this axis is additive and GRACEFUL-DEGRADATION:
    # the embedder is an external network service, and retrieval follows the
    # ``build_strategies`` precedent — a down embedder degrades to
    # keyword/graph/trace results instead of failing the whole tool call.
    # Vector hits share ``doc_id`` with document hits, so the dedup pass
    # below merges the two axes; the FTS item (appended first) wins.
    try:
        embedding_fn = registry.embedding_fn
        vector_store = getattr(registry.knowledge, "vector_store", None)
        if embedding_fn is not None and vector_store is not None:
            vec_filters: dict[str, Any] | None = {"domain": domain} if domain else None
            hits = vector_store.query(
                embedding_fn(intent), top_k=10, filters=vec_filters
            )
            items.extend(
                {
                    "item_id": hit["item_id"],
                    "item_type": "document",
                    "excerpt": hit.get("metadata", {}).get("content", "")[:500],
                    "relevance_score": hit.get("score", 0.0),
                }
                for hit in hits
            )
    except Exception:
        logger.exception("get_context_semantic_search_failed")

    # Session dedup: exclude items already served in this session.
    session_served: set[str] = set()
    if session_id:
        try:
            from datetime import UTC, datetime, timedelta  # noqa: PLC0415

            since = datetime.now(UTC) - timedelta(
                minutes=60,  # matches DEFAULT_SESSION_DEDUP_WINDOW_MINUTES
            )
            events = registry.operational.event_log.get_events(
                event_type=EventType.PACK_ASSEMBLED,
                since=since,
                limit=200,
            )
            for ev in events:
                payload = ev.payload or {}
                if payload.get("session_id") != session_id:
                    continue
                for iid in payload.get("injected_item_ids", []) or []:
                    session_served.add(iid)
                for section in payload.get("sections", []) or []:
                    for iid in section.get("item_ids", []) or []:
                        session_served.add(iid)
        except Exception as exc:
            logger.exception("get_context_session_dedup_failed")
            _raise_internal(
                f"session dedup query failed: {exc}",
                cause=exc,
                data={
                    "stage": "session_dedup",
                    "session_id": session_id,
                },
            )

    # Deduplicate and sort
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        if item["item_id"] in session_served:
            continue
        if item["item_id"] not in seen:
            seen.add(item["item_id"])
            unique.append(item)
    unique.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)

    if not unique:
        return f"No context found for: {intent}"

    result = format_pack_as_markdown(unique, intent, max_tokens=max_tokens)

    # Record what was served so subsequent calls with this session_id can
    # dedup against it. Mirrors PackBuilder's PACK_ASSEMBLED telemetry.
    if session_id:
        try:
            registry.operational.event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="get_context",
                entity_type="pack",
                payload={
                    "intent": intent,
                    "domain": domain,
                    "session_id": session_id,
                    "items_count": len(unique),
                    "injected_item_ids": [i["item_id"] for i in unique],
                },
            )
        except Exception:
            # GRACEFUL-DEGRADATION: pack already assembled and returned —
            # an event-log emit failure is a telemetry concern, not a
            # tool-result correctness concern. Phase 5 covers telemetry
            # site cleanup.
            logger.exception("get_context_pack_emit_failed")

    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_context",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry;
        # failure here must not invalidate a successful pack assembly.
        logger.exception("token_tracking_failed", operation="get_context")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 2: save_experience
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_experience(trace_json: str) -> str:
    """Save an experience trace to the graph.

    Args:
        trace_json: JSON string conforming to the Trace schema.
    """
    if not trace_json or not trace_json.strip():
        _raise_invalid_params(
            "trace_json must not be empty",
            data={"field": "trace_json"},
        )

    try:
        trace = Trace.model_validate_json(trace_json)
    except Exception as exc:
        _raise_invalid_params(
            f"invalid trace JSON: {exc}",
            data={"field": "trace_json", "error_class": type(exc).__name__},
        )

    registry = _get_registry()
    executor = build_curate_executor(registry)
    result = executor.execute(
        Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace},
            target_id=trace.trace_id,
            target_type="trace",
            requested_by="mcp:save_experience",
        )
    )
    if result.status != CommandStatus.SUCCESS:
        _raise_mutation_failed(
            f"failed to store trace: {result.message}",
            data={
                "status": result.status.value,
                "command_id": result.command_id,
                "message": result.message,
            },
        )

    # Feature-flagged post-ingest trace->graph extraction
    # (TRELLIS_ENABLE_TRACE_EXTRACTION=1). Runs the deterministic
    # TraceExtractor through the governed MutationExecutor after the trace
    # is durably stored. Fail-soft inside the hook -- never blocks the save.
    run_trace_extraction(registry, trace, requested_by="mcp:save_experience")

    return f"Trace saved: {result.created_id}"


# ---------------------------------------------------------------------------
# Macro Tool 3: save_knowledge
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_knowledge(
    name: str,
    entity_type: str = "concept",
    properties: dict[str, Any] | None = None,
    relates_to: str | None = None,
    edge_kind: str = "entity_related_to",
    content: str | None = None,
    evidence_ref: str | None = None,
) -> str:
    """Create an entity in the knowledge graph, optionally linking it.

    Pointer-not-prose: when ``content`` is supplied without an
    ``evidence_ref``, the evidence *document* is auto-created first (embedded
    via the standard ingest hook) and the new entity carries a pointer to it
    (``evidence_ref`` property + ``document_ids`` link) — the graph node never
    holds the prose itself. Ordering is doc-first by design: if the graph
    write then fails, an orphaned document is acceptable (findable, prunable)
    but a graph node pointing at a nonexistent document is not. All graph
    writes go through the governed :class:`MutationExecutor`.

    Args:
        name: Entity name.
        entity_type: Type (e.g., "concept", "person", "system").
            Default: "concept".
        properties: Optional additional properties.
        relates_to: Optional entity ID to create a relationship to.
        edge_kind: Relationship type if relates_to is set.
            Default: "entity_related_to".
        content: Optional evidence prose. When given without ``evidence_ref``,
            an evidence document is auto-created and linked (pointer-not-prose).
        evidence_ref: Optional existing document id to point at. When set, no
            document is created — the provided pointer is attached as-is. Takes
            precedence over ``content`` for the pointer value.
    """
    if not name or not name.strip():
        _raise_invalid_params(
            "name must not be empty",
            data={"field": "name"},
        )

    registry = _get_registry()

    # Doc-FIRST: auto-create the evidence document when prose arrived without
    # an explicit pointer. On partial failure the orphaned doc is acceptable;
    # a dangling graph pointer is not — so this must complete before the graph
    # write below. An explicit ``evidence_ref`` attaches as-is (no doc create).
    if evidence_ref is None and content is not None and content.strip():
        evidence_ref = ensure_evidence_document(
            registry,
            content,
            metadata={"entity_name": name, "entity_type": entity_type},
            source="mcp:save_knowledge",
        )

    props = dict(properties or {})
    if evidence_ref is not None:
        props["evidence_ref"] = evidence_ref
    document_ids = [evidence_ref] if evidence_ref is not None else None

    executor = build_curate_executor(registry)
    create_result = executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_type": entity_type,
                "name": name,
                "properties": props,
                "document_ids": document_ids,
            },
            target_type=entity_type,
            requested_by="mcp:save_knowledge",
        )
    )
    if create_result.status != CommandStatus.SUCCESS:
        # Graph write failed. Any auto-created evidence doc is left as an
        # acceptable orphan — we never wrote a node, so there is no dangling
        # pointer to clean up.
        _raise_mutation_failed(
            f"failed to create entity: {create_result.message}",
            data={
                "status": create_result.status.value,
                "command_id": create_result.command_id,
                "message": create_result.message,
                "evidence_ref": evidence_ref,
            },
        )

    node_id = create_result.created_id
    result = f"Entity created: {node_id} ({entity_type}: {name})"
    if evidence_ref is not None:
        result += f"\nEvidence document: {evidence_ref}"

    if relates_to:
        if registry.knowledge.graph_store.get_node(relates_to) is None:
            # Entity already created — surface a warning string in the
            # response rather than raising, since the create succeeded.
            # Callers that want strict link semantics should call
            # ``execute_mutation`` with ``LINK_CREATE`` directly.
            result += (
                f"\nWarning: target entity not found: {relates_to} — edge not created"
            )
        else:
            link_result = executor.execute(
                Command(
                    operation=Operation.LINK_CREATE,
                    args={
                        "source_id": node_id,
                        "target_id": relates_to,
                        "edge_kind": edge_kind,
                    },
                    requested_by="mcp:save_knowledge",
                )
            )
            if link_result.status == CommandStatus.SUCCESS:
                result += (
                    f"\nEdge created: {link_result.created_id} "
                    f"--[{edge_kind}]--> {relates_to}"
                )
            else:
                result += (
                    f"\nWarning: edge not created: {link_result.message}"
                )

    return result


# ---------------------------------------------------------------------------
# Macro Tool 4: save_memory
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_memory(
    content: str,
    metadata: dict[str, Any] | None = None,
    doc_id: str | None = None,
) -> str:
    """Store a document in the experience graph memory.

    Deduplicates by content hash: if an identical document already exists,
    returns its id without storing a duplicate. Emits a ``MEMORY_STORED``
    event on new stores so downstream workers (enrichment, promotion) can
    react.

    Args:
        content: Document content to store.
        metadata: Optional metadata (tags, source, domain, etc.).
        doc_id: Optional document ID. Auto-generated if not provided.
    """
    if not content or not content.strip():
        _raise_invalid_params(
            "content must not be empty",
            data={"field": "content"},
        )

    from trellis.core.hashing import content_hash  # noqa: PLC0415

    registry = _get_registry()
    metadata = metadata or {}

    chash = content_hash(content)

    # Dedup decision + store, serialized so concurrent http workers can't
    # both pass the checks and both persist. The returns for an existing
    # exact or fuzzy match release the lock on the way out.
    with _save_memory_lock:
        # Dedup stage 1: exact content hash match.
        existing = registry.knowledge.document_store.get_by_hash(chash)
        if existing is not None:
            existing_id = existing["doc_id"]
            logger.debug(
                "save_memory_dedup_exact", doc_id=existing_id, content_hash=chash
            )
            return f"Memory already exists: {existing_id}"

        # Dedup stage 2: fuzzy MinHash/LSH (catches typos, casing, punctuation).
        try:
            minhash_index = _get_minhash_index(registry)
            if minhash_index is not None:
                match = minhash_index.find_duplicate(content)
                if match is not None:
                    match_id, similarity = match
                    logger.debug(
                        "save_memory_dedup_fuzzy",
                        match_id=match_id,
                        similarity=round(similarity, 3),
                    )
                    return f"Fuzzy duplicate (similarity {similarity:.0%}): {match_id}"
        except McpError:
            # _get_minhash_index already wrapped the cause structurally.
            raise
        except Exception as exc:
            logger.exception("save_memory_minhash_failed")
            _raise_internal(
                f"fuzzy dedup query failed: {exc}",
                cause=exc,
                data={"stage": "minhash_find"},
            )

        stored_id = registry.knowledge.document_store.put(
            doc_id, content, metadata=metadata
        )

        # Add to MinHash index for future fuzzy dedup. The write already
        # succeeded; an index-add failure means future calls won't see this
        # doc in fuzzy lookups but the doc itself is persisted — surface as
        # a real error so operators can diagnose the dedup drift.
        try:
            minhash_index = _get_minhash_index(registry)
            if minhash_index is not None:
                minhash_index.add(stored_id, content)
        except McpError:
            raise
        except Exception as exc:
            logger.exception("save_memory_minhash_index_add_failed", doc_id=stored_id)
            _raise_internal(
                f"failed to index stored memory for fuzzy dedup: {exc}",
                cause=exc,
                data={"stage": "minhash_add", "doc_id": stored_id},
            )

    # Emit MEMORY_STORED so enrichment / promotion workers can react.
    try:
        registry.operational.event_log.emit(
            EventType.MEMORY_STORED,
            source="save_memory",
            entity_id=stored_id,
            entity_type="document",
            payload={
                "doc_id": stored_id,
                "content_hash": chash,
                "content_length": len(content),
                "metadata": metadata,
            },
        )
    except Exception as exc:
        logger.exception("memory_stored_event_emission_failed", doc_id=stored_id)
        _raise_internal(
            f"MEMORY_STORED event emit failed: {exc}",
            cause=exc,
            data={"stage": "memory_stored_emit", "doc_id": stored_id},
        )

    # Feature-flagged tiered extraction (TRELLIS_ENABLE_MEMORY_EXTRACTION=1).
    # Runs AliasMatch + LLM residue via the governed MutationExecutor.
    # Never blocks save_memory success — failures are logged and swallowed.
    memory_extractor = _get_memory_extractor(registry)
    if memory_extractor is not None:
        _run_memory_extraction(registry, memory_extractor, stored_id, content)

    # Feature-flagged embedding (TRELLIS_ENABLE_EMBED_ON_INGEST=1) so
    # SemanticSearch can retrieve the memory. Fail-soft inside the hook —
    # a broken embedder never fails save_memory.
    run_embed_on_ingest(
        registry, stored_id, content, metadata, source="mcp:save_memory"
    )

    return f"Memory saved: {stored_id}"


# ---------------------------------------------------------------------------
# Macro Tool 5: get_lessons
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_lessons(
    domain: str | None = None,
    limit: int = 10,
    max_tokens: int = 2000,
) -> str:
    """Get lessons learned (promoted precedents) from past experiences.

    Args:
        domain: Optional domain filter.
        limit: Maximum lessons to return (default 10).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    from trellis.retrieve.precedents import list_precedents as _list_prec  # noqa: PLC0415, I001

    registry = _get_registry()
    lessons = _list_prec(registry.operational.event_log, domain=domain, limit=limit)

    result = format_lessons_as_markdown(lessons, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_lessons",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_lessons")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 6: get_graph
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_graph(
    entity_id: str,
    depth: int = 1,
    max_tokens: int = 2000,
) -> str:
    """Get an entity and its neighborhood from the knowledge graph.

    Args:
        entity_id: The entity ID to explore.
        depth: How many relationship hops to traverse (default 1).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    if not entity_id or not entity_id.strip():
        _raise_invalid_params(
            "entity_id must not be empty",
            data={"field": "entity_id"},
        )

    registry = _get_registry()
    node = registry.knowledge.graph_store.get_node(entity_id)
    if node is None:
        _raise_not_found(
            f"entity not found: {entity_id}",
            data={"entity_id": entity_id},
        )

    subgraph = registry.knowledge.graph_store.get_subgraph(
        seed_ids=[entity_id], depth=depth
    )
    result = format_subgraph_as_markdown(node, subgraph, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_graph",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_graph")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 7: record_feedback
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def record_feedback(
    trace_id: str = "",
    pack_id: str = "",
    success: bool = True,
    notes: str | None = None,
    helpful_item_ids: list[str] | None = None,
    unhelpful_item_ids: list[str] | None = None,
    followed_advisory_ids: list[str] | None = None,
) -> str:
    """Record outcome feedback on a trace or context pack.

    Supply ``pack_id`` (preferred) to attribute feedback to a context
    pack returned by one of the ``get_*_context`` tools. The pack_id is
    shown in the response header of each pack and can be copied verbatim.

    When citing specific elements:

    * ``helpful_item_ids`` — item_ids (shown in backticks in the pack)
      that actually helped the task succeed.
    * ``unhelpful_item_ids`` — items that were noise or misleading.
    * ``followed_advisory_ids`` — advisory_ids (shown in backticks in the
      Advisories section) that you followed.

    Element-level signals are stored on the feedback event for the fitness
    loops (``trellis analyze apply-noise-tags`` and ``trellis analyze
    advisory-effectiveness``) to attribute outcomes more precisely.

    ``trace_id`` is still accepted for trace-level feedback when no pack
    is involved.

    Args:
        trace_id: Trace ID for trace-level feedback (optional).
        pack_id: Pack ID for pack-level feedback (optional but preferred
            when feedback follows a context retrieval).
        success: Whether the task succeeded.
        notes: Optional notes about what worked or didn't.
        helpful_item_ids: IDs of pack items that were actually useful.
        unhelpful_item_ids: IDs of pack items that were noise.
        followed_advisory_ids: IDs of advisories the agent followed.
    """
    has_trace = bool(trace_id and trace_id.strip())
    has_pack = bool(pack_id and pack_id.strip())
    if not has_trace and not has_pack:
        _raise_invalid_params(
            "one of trace_id or pack_id must be provided",
            data={"fields": ["trace_id", "pack_id"]},
        )

    registry = _get_registry()

    payload: dict[str, Any] = {
        "success": success,
        "notes": notes or "",
        "rating": 1.0 if success else 0.0,
    }
    if helpful_item_ids:
        payload["helpful_item_ids"] = list(helpful_item_ids)
    if unhelpful_item_ids:
        payload["unhelpful_item_ids"] = list(unhelpful_item_ids)
    if followed_advisory_ids:
        payload["followed_advisory_ids"] = list(followed_advisory_ids)

    if has_pack:
        payload["pack_id"] = pack_id
        registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )
        target = f"pack: {pack_id}"
    else:
        registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp",
            entity_id=trace_id,
            entity_type="trace",
            payload=payload,
        )
        target = f"trace: {trace_id}"

    status = "positive" if success else "negative"
    return f"Feedback recorded ({status}) for {target}"


# ---------------------------------------------------------------------------
# Macro Tool 8: search
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def search(
    query: str,
    limit: int = 10,
    max_tokens: int = 2000,
) -> str:
    """Search the experience graph for documents and entities.

    Args:
        query: Search query.
        limit: Maximum results (default 10).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    if not query or not query.strip():
        _raise_invalid_params(
            "query must not be empty",
            data={"field": "query"},
        )

    registry = _get_registry()

    # Search documents
    doc_results = registry.knowledge.document_store.search(query, limit=limit)
    items: list[dict[str, Any]] = [
        {
            "item_id": doc["doc_id"],
            "item_type": "document",
            "excerpt": doc.get("content", "")[:300],
            "relevance_score": abs(doc.get("rank", 0.0)),
        }
        for doc in doc_results
    ]

    # Search graph nodes
    all_nodes = registry.knowledge.graph_store.query(limit=limit * 2)
    q_lower = query.lower()
    for node in all_nodes:
        props = node.get("properties", {})
        name = str(props.get("name", "")).lower()
        desc = str(props.get("description", "")).lower()
        if q_lower in name or q_lower in desc:
            items.append(
                {
                    "item_id": node["node_id"],
                    "item_type": "entity",
                    "excerpt": props.get("name", "") or props.get("description", ""),
                    "relevance_score": 0.5,
                }
            )

    # Semantic search — additive axis, same shape and degradation contract
    # as get_context's (see the comment there). Dedup below folds vector
    # hits into the FTS hits sharing the same doc_id.
    try:
        embedding_fn = registry.embedding_fn
        vector_store = getattr(registry.knowledge, "vector_store", None)
        if embedding_fn is not None and vector_store is not None:
            hits = vector_store.query(embedding_fn(query), top_k=limit)
            items.extend(
                {
                    "item_id": hit["item_id"],
                    "item_type": "document",
                    "excerpt": hit.get("metadata", {}).get("content", "")[:300],
                    "relevance_score": hit.get("score", 0.0),
                }
                for hit in hits
            )
    except Exception:
        logger.exception("search_semantic_search_failed")

    # Dedup by item_id (first occurrence wins — FTS before semantic).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        if item["item_id"] not in seen:
            seen.add(item["item_id"])
            unique.append(item)
    items = unique

    if not items:
        return f"No results found for: {query}"

    items.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)
    result = format_pack_as_markdown(
        items[:limit], f"Search: {query}", max_tokens=max_tokens
    )
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="search",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="search")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 9: get_objective_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_objective_context(
    intent: str,
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
) -> str:
    """Retrieve objective-level context for a workflow.

    Assembles domain knowledge (conventions, ownership, precedents,
    governance) and operational context (prior traces, known failures)
    for a user's business objective. Designed to be called once at
    workflow start and shared across all downstream agent phases.

    Args:
        intent: The user's original business objective in their own words.
        domain: Optional domain filter (e.g., "orders", "data-pipeline").
        max_tokens: Token budget override. Pass ``0`` (default) to use the
            configured budget from ``retrieval.budgets`` in ``config.yaml``;
            pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded from the result, preventing repetition across calls.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    try:
        registry = _get_registry()
        builder = _build_pack_builder(registry)

        budget = registry.budget_config.resolve(
            tool="get_objective_context",
            domain=domain or None,
            caller_override_tokens=max_tokens if max_tokens > 0 else None,
        )
        resolved_tokens = budget.max_tokens

        sections = [
            SectionRequest(
                name="Domain Knowledge",
                retrieval_affinities=["domain_knowledge"],
                max_tokens=resolved_tokens // 2,
                max_items=10,
            ),
            SectionRequest(
                name="Operational Context",
                retrieval_affinities=["operational"],
                max_tokens=resolved_tokens // 3,
                max_items=8,
            ),
        ]

        sectioned_pack = builder.build_sectioned(
            intent,
            sections=sections,
            domain=domain or None,
            session_id=session_id or None,
        )

        # Convert SectionedPack to list-of-dicts for the formatter
        section_dicts = [
            {
                "name": s.name,
                "items": [
                    {
                        "item_id": item.item_id,
                        "item_type": item.item_type,
                        "excerpt": item.excerpt,
                        "relevance_score": item.relevance_score,
                    }
                    for item in s.items
                ],
            }
            for s in sectioned_pack.sections
        ]

        result = format_sectioned_pack_as_markdown(
            section_dicts,
            intent,
            max_tokens=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
        )

        # Append advisories if present
        adv_md = format_advisories_as_markdown(sectioned_pack.advisories)
        if adv_md:
            result = result + "\n\n" + adv_md

        try:
            track_token_usage(
                registry.operational.event_log,
                layer="mcp",
                operation="get_objective_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
            logger.exception("token_tracking_failed", operation="get_objective_context")
    except McpError:
        # Already structured by a deeper helper — let it propagate.
        raise
    except Exception as exc:
        logger.exception("get_objective_context_failed")
        _raise_internal(
            f"failed to assemble objective context for intent={intent!r}: {exc}",
            cause=exc,
            data={"tool": "get_objective_context", "intent": intent},
        )
    return result


# ---------------------------------------------------------------------------
# Macro Tool 10: get_task_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_task_context(
    intent: str,
    entity_ids: list[str] | None = None,
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
) -> str:
    """Retrieve task-level context for a specific agent step.

    Assembles technical patterns and reference data relevant to a
    specific task (e.g., SQL generation, validation). Complements
    objective context with step-specific details.

    Args:
        intent: Description of the specific task being performed.
        entity_ids: Entity IDs being touched (e.g., table URIs).
        domain: Optional domain filter.
        max_tokens: Token budget override. Pass ``0`` (default) to use the
            configured budget from ``retrieval.budgets`` in ``config.yaml``;
            pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    try:
        registry = _get_registry()
        builder = _build_pack_builder(registry)

        budget = registry.budget_config.resolve(
            tool="get_task_context",
            domain=domain or None,
            caller_override_tokens=max_tokens if max_tokens > 0 else None,
        )
        resolved_tokens = budget.max_tokens

        sections = [
            SectionRequest(
                name="Technical Patterns",
                retrieval_affinities=["technical_pattern"],
                max_tokens=resolved_tokens // 2,
                max_items=10,
            ),
            SectionRequest(
                name="Reference Data",
                retrieval_affinities=["reference"],
                entity_ids=entity_ids or [],
                max_tokens=resolved_tokens // 3,
                max_items=10,
            ),
        ]

        sectioned_pack = builder.build_sectioned(
            intent,
            sections=sections,
            domain=domain or None,
            session_id=session_id or None,
        )

        # Convert SectionedPack to list-of-dicts for the formatter
        section_dicts = [
            {
                "name": s.name,
                "items": [
                    {
                        "item_id": item.item_id,
                        "item_type": item.item_type,
                        "excerpt": item.excerpt,
                        "relevance_score": item.relevance_score,
                    }
                    for item in s.items
                ],
            }
            for s in sectioned_pack.sections
        ]

        result = format_sectioned_pack_as_markdown(
            section_dicts,
            intent,
            max_tokens=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
        )

        # Append advisories if present
        adv_md = format_advisories_as_markdown(sectioned_pack.advisories)
        if adv_md:
            result = result + "\n\n" + adv_md

        try:
            track_token_usage(
                registry.operational.event_log,
                layer="mcp",
                operation="get_task_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
            logger.exception("token_tracking_failed", operation="get_task_context")
    except McpError:
        raise
    except Exception as exc:
        logger.exception("get_task_context_failed")
        _raise_internal(
            f"failed to assemble task context for intent={intent!r}: {exc}",
            cause=exc,
            data={"tool": "get_task_context", "intent": intent},
        )
    return result


# ---------------------------------------------------------------------------
# Macro Tool 11: get_sectioned_context
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_sectioned_context(
    intent: str,
    sections: list[dict[str, Any]],
    domain: str = "",
    max_tokens: int = 0,
    session_id: str = "",
) -> str:
    """Retrieve context organized into independently budgeted sections.

    Unlike get_objective_context and get_task_context (which use fixed
    section layouts), this tool lets you define your own sections with
    custom affinities, content types, scopes, entity IDs, and per-section
    token budgets.

    Args:
        intent: Natural language description of the task or question.
        sections: List of section configs. Each section is a dict with:
            - name (str, required): Section heading.
            - retrieval_affinities (list[str]): e.g. ["domain_knowledge"]
            - content_types (list[str]): e.g. ["code", "documentation"]
            - scopes (list[str]): e.g. ["universal", "project"]
            - entity_ids (list[str]): Entity IDs to anchor retrieval.
            - max_tokens (int): Token budget for this section (default 2000).
            - max_items (int): Max items for this section (default 10).
        domain: Optional domain filter applied across all sections.
        max_tokens: Total token budget override. Pass ``0`` (default) to use
            the configured budget from ``retrieval.budgets`` in
            ``config.yaml``; pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.

    Example sections:
        [
          {"name": "Schema Context", "retrieval_affinities": ["reference"],
           "entity_ids": ["uc://catalog.schema.table"], "max_tokens": 3000},
          {"name": "Past Patterns", "retrieval_affinities": ["technical_pattern"],
           "content_types": ["code", "procedure"], "max_tokens": 2000}
        ]
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )
    if not sections:
        _raise_invalid_params(
            "sections must not be empty",
            data={"field": "sections"},
        )

    try:
        registry = _get_registry()
        builder = _build_pack_builder(registry)

        budget = registry.budget_config.resolve(
            tool="get_sectioned_context",
            domain=domain or None,
            caller_override_tokens=max_tokens if max_tokens > 0 else None,
        )
        resolved_tokens = budget.max_tokens

        section_requests = [SectionRequest.model_validate(s) for s in sections]

        sectioned_pack = builder.build_sectioned(
            intent,
            sections=section_requests,
            domain=domain or None,
            session_id=session_id or None,
        )

        # Convert SectionedPack to list-of-dicts for the formatter
        section_dicts = [
            {
                "name": s.name,
                "items": [
                    {
                        "item_id": item.item_id,
                        "item_type": item.item_type,
                        "excerpt": item.excerpt,
                        "relevance_score": item.relevance_score,
                    }
                    for item in s.items
                ],
            }
            for s in sectioned_pack.sections
        ]

        result = format_sectioned_pack_as_markdown(
            section_dicts,
            intent,
            max_tokens=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
        )

        # Append advisories if present
        adv_md = format_advisories_as_markdown(sectioned_pack.advisories)
        if adv_md:
            result = result + "\n\n" + adv_md

        try:
            track_token_usage(
                registry.operational.event_log,
                layer="mcp",
                operation="get_sectioned_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
            logger.exception("token_tracking_failed", operation="get_sectioned_context")
    except McpError:
        raise
    except Exception as exc:
        logger.exception("get_sectioned_context_failed")
        _raise_internal(
            f"failed to assemble sectioned context for intent={intent!r}: {exc}",
            cause=exc,
            data={"tool": "get_sectioned_context", "intent": intent},
        )
    return result


# ---------------------------------------------------------------------------
# Macro Tool 12: execute_mutation
# ---------------------------------------------------------------------------


def _resolve_operation(operation: str) -> Any:
    """Resolve an ``operation`` string to an :class:`Operation` enum member.

    Accepts both the wire form (``"link.create"``) and the screaming-snake
    name (``"LINK_CREATE"``). Returns ``None`` when the string matches
    neither, leaving error reporting to the caller.

    The two ``except`` clauses below are intentionally tight: the wire
    form falls through ``ValueError`` to try the enum-name form, and the
    enum-name miss returns ``None`` so the caller can raise a single
    INVALID_PARAMS with the offending string and the registry context.
    Replacing either with a raise here would force the caller to mask
    the legitimate "try other form" flow.
    """
    from trellis.mutate.commands import Operation  # noqa: PLC0415

    try:
        return Operation(operation)
    except ValueError:
        # GUARD: tried the wire form ("link.create"); fall through and
        # try the enum-name form ("LINK_CREATE") below.
        pass
    try:
        return Operation[operation]
    except KeyError:
        # GUARD: neither wire form nor enum name matched. Caller
        # surfaces an INVALID_PARAMS McpError with the offending string.
        return None


# ---------------------------------------------------------------------------
# Macro Tool: record_observation / query_observations (Item 1 Phase 1)
# ---------------------------------------------------------------------------
#
# Measurement is intentionally *not* exposed as an MCP tool in Phase 1.
# Measurement rows are append-only by convention (see ADR
# ``adr-observation-entity-type.md`` §2.1 / §5.6) and are produced
# primarily by automated metric streams, where per-call MCP overhead is
# the wrong shape — those callers should use the REST endpoint
# (`POST /api/v1/measurements`) or the SDK (`record_measurement`) which
# both go through the same governed pipeline. Reconsider exposing
# Measurement on MCP if agent-driven scalar capture becomes a real
# workload.


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def record_observation(
    subject_entity_id: str,
    subject_entity_type: str,
    observer_agent_id: str,
    content: str,
    confidence: float,
    evidence_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Record an empirical Observation about a subject entity.

    See ``docs/design/adr-observation-entity-type.md`` for the data model
    rationale. The Observation lands as a graph node with a
    ``hasObservation`` edge from the subject entity. Missing required
    fields surface as a JSON error envelope — no silent defaults.

    Args:
        subject_entity_id: The entity the observation is *about*.
        subject_entity_type: Open-string entity type of the subject.
        observer_agent_id: Which agent (human or automated) produced this.
        content: Narrative description of the observation.
        confidence: Producer confidence in ``[0.0, 1.0]``.
        evidence_ref: Optional pointer to supporting evidence
            (e.g., a trace_id / document_id / URN).
        metadata: Optional bag for conventional keys (kind, window_start,
            window_end, sample_size, method, …). See ADR §2.3.

    Returns:
        A JSON object with ``status``, ``observation_id`` (on success),
        or ``message`` on failure. This tool never raises to MCP.
    """
    from trellis.schemas.observation import Observation  # noqa: PLC0415
    from trellis.schemas.well_known import OBSERVATION  # noqa: PLC0415

    try:
        obs = Observation(
            subject_entity_id=subject_entity_id,
            subject_entity_type=subject_entity_type,
            observer_agent_id=observer_agent_id,
            content=content,
            confidence=confidence,
            evidence_ref=evidence_ref,
            metadata=metadata or {},
        )
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client (see docstring). Returns structured {"status": "error"}
    # JSON so the caller can branch on the response.
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"Invalid observation: {exc}"})

    try:
        command = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": obs},
            target_id=obs.observation_id,
            target_type=OBSERVATION,
            requested_by="mcp:record_observation",
        )
        executor = build_curate_executor(_get_registry())
        result = executor.execute(command)
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client; execution failure is logged + surfaced as a structured
    # error response.
    except Exception as exc:
        logger.exception("record_observation_failed")
        return json.dumps({"status": "error", "message": f"Execution failed: {exc}"})

    if result.status != CommandStatus.SUCCESS:
        return json.dumps(
            {
                "status": result.status.value,
                "message": result.message,
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "observation_id": result.created_id or obs.observation_id,
        }
    )


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def query_observations(
    subject_entity_id: str = "",
    observer_agent_id: str = "",
    limit: int = 100,
) -> str:
    """Query Observation nodes by subject and/or observer.

    Args:
        subject_entity_id: Filter by subject entity id (empty = no filter).
        observer_agent_id: Filter by observer agent id (empty = no filter).
        limit: Maximum results to return (default 100).

    Returns:
        A JSON object with ``observations``: a list of Observation
        property dicts. Each dict carries ``node_id`` plus the
        schema's payload fields.
    """
    from trellis.schemas.well_known import OBSERVATION  # noqa: PLC0415

    registry = _get_registry()
    props: dict[str, Any] = {}
    if subject_entity_id.strip():
        props["subject_entity_id"] = subject_entity_id.strip()
    if observer_agent_id.strip():
        props["observer_agent_id"] = observer_agent_id.strip()

    try:
        rows = registry.knowledge.graph_store.query(
            node_type=OBSERVATION,
            properties=props or None,
            limit=limit,
        )
    # GRACEFUL-DEGRADATION: MCP tool surface — never raises to the
    # client; backend errors are logged and surfaced as a structured
    # error response.
    except Exception as exc:
        logger.exception("query_observations_failed")
        return json.dumps({"status": "error", "message": f"Query failed: {exc}"})

    projected: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row.get("properties", {}))
        item["node_id"] = row.get("node_id")
        item["node_type"] = row.get("node_type")
        projected.append(item)
    return json.dumps({"status": "ok", "observations": projected})


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def execute_mutation(
    operation: str,
    args: dict[str, Any],
    idempotency_key: str | None = None,
    actor: str | None = None,
) -> str:
    """Execute a governed mutation through the ``MutationExecutor``.

    Provides MCP-surface parity with the REST ``/api/v1/commands/batch``
    endpoint for operator scripting. Wraps a single command in the same
    five-stage pipeline (validate → policy → idempotency → execute →
    emit), so policy gates and audit events apply identically.

    Args:
        operation: Operation name. Accepts the wire value
            (e.g. ``"link.create"``) or the enum key
            (e.g. ``"LINK_CREATE"``).
        args: Operation-specific argument map. Required keys depend on
            the operation — see ``OperationRegistry`` in
            ``trellis.mutate.commands``.
        idempotency_key: Optional dedup key. Repeat submissions with the
            same key return ``status="duplicate"`` without re-executing.
        actor: Optional audit identifier for the submitter. Defaults to
            ``"mcp:execute_mutation"`` when not supplied.

    Returns:
        A JSON object string with fields ``status``, ``command_id``,
        ``operation``, ``message``, and (on success) ``created_id``.
        The executor's own non-success statuses (``rejected``, ``failed``,
        ``duplicate``) are still returned in the JSON body — those are
        structured outcomes, not transport-layer errors.

    Raises:
        McpError: With ``INVALID_PARAMS`` for pre-flight argument
            issues (empty operation, unknown enum, non-dict args,
            ``Command`` construction failure) and ``INTERNAL_ERROR``
            for unexpected executor-side crashes.
    """
    from trellis.mutate.commands import Command  # noqa: PLC0415

    if not operation or not operation.strip():
        _raise_invalid_params(
            "operation must not be empty",
            data={"field": "operation"},
        )

    op = _resolve_operation(operation)
    if op is None:
        _raise_invalid_params(
            f"unknown operation: {operation}",
            data={"field": "operation", "value": operation},
        )

    if not isinstance(args, dict):
        _raise_invalid_params(
            "args must be a dict",
            data={"field": "args", "type": type(args).__name__},
        )

    requested_by = actor.strip() if actor and actor.strip() else "mcp:execute_mutation"

    try:
        command = Command(
            operation=op,
            args=dict(args),
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
    except Exception as exc:
        _raise_invalid_params(
            f"invalid command: {exc}",
            data={"operation": str(op), "error_class": type(exc).__name__},
        )

    try:
        executor = build_curate_executor(_get_registry())
        result = executor.execute(command)
    except Exception as exc:
        logger.exception("execute_mutation_failed", operation=str(op))
        _raise_internal(
            f"execution failed: {exc}",
            cause=exc,
            data={
                "command_id": command.command_id,
                "operation": str(op),
                "error_class": type(exc).__name__,
            },
        )

    response: dict[str, Any] = {
        "status": result.status.value,
        "command_id": result.command_id,
        "operation": str(result.operation),
        "message": result.message,
    }
    if result.created_id is not None:
        response["created_id"] = result.created_id
    if result.warnings:
        response["warnings"] = list(result.warnings)
    return json.dumps(response)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _install_shutdown_signal_handlers() -> None:
    """Install best-effort signal handlers that trigger graceful shutdown.

    The natural shutdown path for an MCP stdio server is the parent
    process closing stdin (EOF) — :func:`fastmcp.FastMCP.run` returns
    and the ``finally`` block in :func:`main` closes the registry.
    These handlers are belt-and-braces for the case where the parent
    sends a signal instead of (or in addition to) closing stdio.

    The handler does NOT call ``sys.exit()`` — exiting from a signal
    handler can corrupt stdout if MCP is mid-write. It just logs and
    re-raises ``KeyboardInterrupt`` for SIGINT (matching the default
    behavior, so :meth:`mcp.run` unwinds and the ``finally`` runs) or
    swallows the SIGTERM (the parent will follow with stdin close).

    Platform notes:

    * On POSIX, both SIGTERM and SIGINT are supported.
    * On Windows, SIGTERM is not deliverable to a Python process in
      the same way; we still call :func:`signal.signal` for it but
      tolerate the platform-specific ``AttributeError`` /
      ``ValueError`` if it raises.
    """
    import signal  # noqa: PLC0415

    def _handler(signum: int, _frame: Any) -> None:
        logger.info("mcp_server_shutdown_signal", signal=signum)
        # SIGINT — re-raise as KeyboardInterrupt so mcp.run() unwinds
        # and the finally clause in main() closes the registry. SIGTERM
        # (and unknown signals) — return; the parent typically follows
        # with stdin close which is the natural EOF shutdown path.
        if signum == signal.SIGINT:
            raise KeyboardInterrupt

    _install_signal_handlers(_handler)


def _install_http_shutdown_signal_handlers() -> None:
    """Stop uvicorn's post-shutdown signal re-raise from skipping cleanup.

    ``uvicorn.Server.capture_signals`` swaps in its own SIGINT/SIGTERM
    handlers for the lifetime of ``serve()``, restores whatever was there
    before, and then calls ``signal.raise_signal(...)`` once per signal it
    caught, so the process exits the way the operator asked. If the
    restored handler is Python's default, that second delivery kills us
    immediately — *after* uvicorn has drained connections but *before*
    :func:`main`'s ``finally`` closes the registry, leaking the Postgres
    pool and the Neo4j driver on every restart.

    Installing no-op handlers first means that re-raise lands on us, is
    swallowed, and ``serve()`` returns normally into the ``finally``. They
    are live only before uvicorn installs its own and after it restores
    ours, so they never suppress the shutdown itself — uvicorn's handler
    is what's bound while the server is actually serving.
    """

    def _handler(signum: int, _frame: Any) -> None:
        logger.info("mcp_server_shutdown_signal", signal=signum)

    _install_signal_handlers(_handler)


def _install_signal_handlers(handler: Any) -> None:
    """Best-effort install of ``handler`` for SIGTERM and SIGINT.

    Shared by the stdio and http shutdown paths, which differ only in
    what the handler does. Tolerates platforms where a signal is absent
    or ``signal.signal`` refuses (not the main thread, Windows SIGTERM):
    falling back to the default handler is correct in both callers.
    """
    import signal  # noqa: PLC0415

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (AttributeError, ValueError, OSError):
            logger.debug("mcp_server_signal_unsupported", signal=sig_name)


#: Stores a tool cannot work without. A broken one SHOULD crash http boot
#: loudly, like the REST API's readiness gate — not surface as a per-request
#: 500. Built on the main thread so concurrent worker threads never race the
#: lock-free lazy init in ``StoreRegistry._get`` (the loser would leak its
#: connection pool and re-run ``_init_schema``). ``blob_store`` is absent on
#: purpose: no MCP tool touches it, so forcing it would make e.g. a missing
#: ``[s3]`` extra a hard boot dependency for a store the surface never uses.
_REQUIRED_KNOWLEDGE_STORES = ("document_store", "graph_store")
_REQUIRED_OPERATIONAL_STORES = (
    "trace_store",
    "event_log",
    "parameter_store",
    "api_key_store",
)


def _prewarm_registry(registry: StoreRegistry) -> None:
    """Force lazily-cached singletons to build, single-threaded.

    Only for the ``http`` transport, where one process serves many
    concurrent sessions and ``StoreRegistry._get`` plus the module-level
    ``_get_*`` caches are lock-free check-then-act. Under stdio there is
    one process per session and nothing is ever contended.

    Required stores build eagerly and fail loud. The degradable
    singletons below build best-effort: winning the init race is worth
    it, but a build failure must NOT sink the server — the tool paths
    already fall back (semantic search → keyword/graph, embed-on-ingest
    is fail-soft, memory extraction is feature-flagged). Forcing them to
    succeed would turn graceful degradation into a hard http boot
    dependency the stdio path never had.
    """
    for name in _REQUIRED_KNOWLEDGE_STORES:
        getattr(registry.knowledge, name)
    for name in _REQUIRED_OPERATIONAL_STORES:
        getattr(registry.operational, name)
    _ = registry.budget_config
    _get_minhash_index(registry)

    for label, build in (
        ("vector_store", lambda: registry.knowledge.vector_store),
        ("embedding_fn", lambda: registry.embedding_fn),
        ("memory_extractor", lambda: _get_memory_extractor(registry)),
    ):
        try:
            build()
        except Exception:
            # GRACEFUL-DEGRADATION: log the component, not the exception —
            # the same fail-soft posture the call sites take at runtime.
            logger.warning("mcp_prewarm_optional_unavailable", component=label)

    logger.info("mcp_registry_prewarmed")


def _configure_http_auth(settings: HttpSettings) -> None:
    """Attach (or deliberately detach) the API-key verifier."""
    if settings.auth_enforced:
        # Lazy provider: the store is resolved per request, after prewarm.
        mcp.auth = TrellisApiKeyVerifier(
            lambda: _get_registry().operational.api_key_store
        )
        set_auth_enforced(enforced=True)
        return

    mcp.auth = None
    # Without a verifier every AuthContext.token is None, so the per-tool
    # scope checks would deny everything. Turn them off together.
    set_auth_enforced(enforced=False)
    logger.warning(
        "mcp_auth_disabled",
        message=(
            "MCP is serving every tool without authentication. Set "
            "TRELLIS_MCP_AUTH_MODE=required and mint a key with "
            "'trellis admin api-keys create'."
        ),
    )


def _close_registry() -> None:
    global _registry  # noqa: PLW0603
    if _registry is None:
        return
    logger.info("mcp_server_shutting_down")
    try:
        _registry.close()
    except Exception:
        # GRACEFUL-DEGRADATION: this runs inside the ``finally`` of
        # ``main()``; re-raising would mask an in-flight ``mcp.run()``
        # exception and obscure the original cause of shutdown. Log
        # loudly, let the process exit.
        logger.exception("mcp_server_registry_close_failed")
    finally:
        _registry = None


def main() -> None:
    """Run the Trellis MCP server over the configured transport.

    ``stdio`` (the default) is unchanged: the parent agent host is the
    trust boundary, per-tool ``auth=`` checks are inert because FastMCP
    short-circuits them off-transport, and shutdown comes from stdin EOF.

    ``http`` turns the server into a network listener and is opt-in via
    ``TRELLIS_MCP_TRANSPORT``. It authenticates with scoped API keys,
    pre-warms the registry so concurrent worker threads never race a
    lazy initialiser, and leaves signal handling to uvicorn.

    Both paths wrap :meth:`mcp.run` in ``try`` / ``finally`` so the
    cached :class:`StoreRegistry` is closed on shutdown. Without this,
    the Postgres connection pool and the Neo4j driver leak until the
    process dies.
    """
    # Under stdio, stdout carries JSON-RPC frames and nothing else. Keep
    # structlog on stderr for both transports — under http it also stops
    # log lines interleaving with uvicorn's stdout access log.
    configure_stderr_logging()
    transport = resolve_transport()
    try:
        if transport == TRANSPORT_HTTP:
            settings = resolve_http_settings()
            _configure_http_auth(settings)
            _prewarm_registry(_get_registry())
            logger.info(
                "mcp_server_starting",
                transport=transport,
                host=settings.host,
                port=settings.port,
                path=settings.path,
                auth_mode=settings.auth_mode,
            )
            # Must precede mcp.run(): uvicorn overrides these while it
            # serves, then restores and re-raises the caught signal. If
            # that lands on Python's default handler the process dies
            # before the ``finally`` below closes the registry.
            _install_http_shutdown_signal_handlers()
            # stateless_http: the tools keep no per-session server state
            # (``session_id`` is a dedup key written to the event log, not
            # an in-memory object), so there is nothing to affinitise.
            mcp.run(
                transport="http",
                host=settings.host,
                port=settings.port,
                path=settings.path,
                stateless_http=True,
                show_banner=False,
            )
        else:
            _install_shutdown_signal_handlers()
            mcp.run()
    finally:
        _close_registry()
