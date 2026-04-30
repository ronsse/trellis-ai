"""MCP Macro Tools server — high-level, token-efficient tools for AI agents."""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp import FastMCP

from trellis.ops import ParameterRegistry
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
      * No LLM client can be constructed from the environment, OR
      * Construction raises for any reason.

    All failure paths log at debug level and cache ``None`` so we don't
    retry on every save_memory call.
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
    except Exception:
        logger.debug("memory_extractor_init_failed", exc_info=True)
        _memory_extractor = None
    return _memory_extractor


def _build_llm_client(registry: StoreRegistry) -> Any:
    """Construct an LLMClient, preferring the registry config over env vars.

    First tries ``registry.build_llm_client()`` (driven by the ``llm:``
    block in ``~/.config/trellis/config.yaml``). If that returns ``None`` — either
    because no config is present or the configured provider couldn't be
    instantiated — falls back to the env-var path in
    :func:`_build_llm_client_from_env`. Returns ``None`` when neither
    source yields a client.
    """
    try:
        client = registry.build_llm_client()
    except Exception:
        logger.debug("llm_client_registry_failed", exc_info=True)
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
            logger.debug("llm_client_openai_not_installed")
        except Exception:
            logger.debug("llm_client_openai_init_failed", exc_info=True)

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from trellis.llm.providers.anthropic import (  # noqa: PLC0415
                AnthropicClient,
            )

            return AnthropicClient()
        except ModuleNotFoundError:
            logger.debug("llm_client_anthropic_not_installed")
        except Exception:
            logger.debug("llm_client_anthropic_init_failed", exc_info=True)

    return None


def _build_alias_resolver(registry: StoreRegistry) -> Any:
    """Build a callable that resolves @mention strings to entity IDs.

    Uses a case-insensitive name match against the graph store, scanned
    lazily on each invocation.  Not suitable for large graphs — the
    production implementation will want an indexed lookup — but fine
    for the feature-flagged Phase 2 rollout.
    """
    graph_store = registry.graph_store

    def resolve(alias: str) -> list[str]:
        target = alias.lower()
        matches: list[str] = []
        try:
            nodes = graph_store.query(limit=2000)
        except Exception:
            logger.debug("alias_resolver_query_failed", alias=alias)
            return matches
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
        from trellis.mutate.executor import MutationExecutor  # noqa: PLC0415
        from trellis.mutate.handlers import (  # noqa: PLC0415
            create_curate_handlers,
        )

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

        batch = result_to_batch(result, requested_by="save_memory_extractor")
        executor = MutationExecutor(
            event_log=registry.event_log,
            handlers=create_curate_handlers(registry),
        )
        executor.execute_batch(batch)
    except Exception:
        logger.debug("memory_extraction_failed", doc_id=doc_id, exc_info=True)


def _get_minhash_index(registry: StoreRegistry) -> Any:
    """Get or create a cached MinHash index for fuzzy dedup.

    Lazily populates the index from the document store on first access.
    Returns ``None`` if the dedup module is unavailable.
    """
    global _minhash_index  # noqa: PLW0603
    if _minhash_index is not None:
        return _minhash_index
    try:
        from trellis.classify.dedup.minhash import MinHashIndex  # noqa: PLC0415

        _minhash_index = MinHashIndex()
        # Seed the index from existing documents (up to a reasonable limit).
        docs = registry.document_store.search("", limit=500)
        for doc in docs:
            _minhash_index.add(doc["doc_id"], doc.get("content", ""))
        logger.debug("minhash_index_initialized", size=_minhash_index.size)
    except Exception:
        logger.debug("minhash_index_init_failed")
        return None
    return _minhash_index


# ---------------------------------------------------------------------------
# Macro Tool 1: get_context
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: intent must not be empty"

    registry = _get_registry()
    items: list[dict[str, Any]] = []

    # Search documents
    try:
        filters: dict[str, Any] = {}
        if domain:
            filters["domain"] = domain
        doc_results = registry.document_store.search(intent, limit=10, filters=filters)
        items.extend(
            {
                "item_id": doc["doc_id"],
                "item_type": "document",
                "excerpt": doc.get("content", "")[:500],
                "relevance_score": abs(doc.get("rank", 0.0)),
            }
            for doc in doc_results
        )
    except Exception:
        logger.exception("get_context_doc_search_failed")

    # Search graph
    try:
        nodes = registry.graph_store.query(limit=20)
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
    except Exception:
        logger.exception("get_context_graph_search_failed")

    # Recent traces
    try:
        traces = registry.trace_store.query(domain=domain, limit=5)
        items.extend(
            {
                "item_id": t.trace_id,
                "item_type": "trace",
                "excerpt": t.intent[:300],
                "relevance_score": 0.3,
            }
            for t in traces
        )
    except Exception:
        logger.exception("get_context_trace_search_failed")

    # Session dedup: exclude items already served in this session.
    session_served: set[str] = set()
    if session_id:
        try:
            from datetime import UTC, datetime, timedelta  # noqa: PLC0415

            since = datetime.now(UTC) - timedelta(
                minutes=60,  # matches DEFAULT_SESSION_DEDUP_WINDOW_MINUTES
            )
            events = registry.event_log.get_events(
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
        except Exception:
            logger.debug("get_context_session_dedup_failed")

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
            registry.event_log.emit(
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
            logger.debug("get_context_pack_emit_failed")

    try:
        track_token_usage(
            registry.event_log,
            layer="mcp",
            operation="get_context",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        logger.debug("token_tracking_failed", operation="get_context")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 2: save_experience
# ---------------------------------------------------------------------------


@mcp.tool()
def save_experience(trace_json: str) -> str:
    """Save an experience trace to the graph.

    Args:
        trace_json: JSON string conforming to the Trace schema.
    """
    if not trace_json or not trace_json.strip():
        return "Error: trace_json must not be empty"

    try:
        trace = Trace.model_validate_json(trace_json)
    except Exception as exc:
        return f"Error: Invalid trace JSON — {exc}"

    try:
        registry = _get_registry()
        trace_id = registry.trace_store.append(trace)
    except Exception as exc:
        return f"Error: Failed to store trace — {exc}"

    return f"Trace saved: {trace_id}"


# ---------------------------------------------------------------------------
# Macro Tool 3: save_knowledge
# ---------------------------------------------------------------------------


@mcp.tool()
def save_knowledge(
    name: str,
    entity_type: str = "concept",
    properties: dict[str, Any] | None = None,
    relates_to: str | None = None,
    edge_kind: str = "entity_related_to",
) -> str:
    """Create an entity in the knowledge graph, optionally linking it.

    Args:
        name: Entity name.
        entity_type: Type (e.g., "concept", "person", "system").
            Default: "concept".
        properties: Optional additional properties.
        relates_to: Optional entity ID to create a relationship to.
        edge_kind: Relationship type if relates_to is set.
            Default: "entity_related_to".
    """
    if not name or not name.strip():
        return "Error: name must not be empty"

    props = dict(properties or {})
    props["name"] = name

    registry = _get_registry()
    node_id = registry.graph_store.upsert_node(
        node_id=None,
        node_type=entity_type,
        properties=props,
    )

    result = f"Entity created: {node_id} ({entity_type}: {name})"

    if relates_to:
        if registry.graph_store.get_node(relates_to) is None:
            result += (
                f"\nWarning: target entity not found: {relates_to} — edge not created"
            )
        else:
            edge_id = registry.graph_store.upsert_edge(
                source_id=node_id,
                target_id=relates_to,
                edge_type=edge_kind,
            )
            result += f"\nEdge created: {edge_id} --[{edge_kind}]--> {relates_to}"

    return result


# ---------------------------------------------------------------------------
# Macro Tool 4: save_memory
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: content must not be empty"

    from trellis.core.hashing import content_hash  # noqa: PLC0415

    registry = _get_registry()
    metadata = metadata or {}

    # Dedup stage 1: exact content hash match.
    chash = content_hash(content)
    existing = registry.document_store.get_by_hash(chash)
    if existing is not None:
        existing_id = existing["doc_id"]
        logger.debug("save_memory_dedup_exact", doc_id=existing_id, content_hash=chash)
        return f"Memory already exists: {existing_id}"

    # Dedup stage 2: fuzzy MinHash/LSH match (catches typos, casing, punctuation).
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
    except Exception:
        logger.debug("save_memory_minhash_failed")

    stored_id = registry.document_store.put(doc_id, content, metadata=metadata)

    # Add to MinHash index for future fuzzy dedup.
    try:
        minhash_index = _get_minhash_index(registry)
        if minhash_index is not None:
            minhash_index.add(stored_id, content)
    except Exception:
        logger.debug("save_memory_minhash_index_add_failed", doc_id=stored_id)

    # Emit MEMORY_STORED so enrichment / promotion workers can react.
    try:
        registry.event_log.emit(
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
    except Exception:
        logger.debug("memory_stored_event_emission_failed", doc_id=stored_id)

    # Feature-flagged tiered extraction (TRELLIS_ENABLE_MEMORY_EXTRACTION=1).
    # Runs AliasMatch + LLM residue via the governed MutationExecutor.
    # Never blocks save_memory success — failures are logged and swallowed.
    memory_extractor = _get_memory_extractor(registry)
    if memory_extractor is not None:
        _run_memory_extraction(registry, memory_extractor, stored_id, content)

    return f"Memory saved: {stored_id}"


# ---------------------------------------------------------------------------
# Macro Tool 5: get_lessons
# ---------------------------------------------------------------------------


@mcp.tool()
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
    lessons = _list_prec(registry.event_log, domain=domain, limit=limit)

    result = format_lessons_as_markdown(lessons, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.event_log,
            layer="mcp",
            operation="get_lessons",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        logger.debug("token_tracking_failed", operation="get_lessons")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 6: get_graph
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: entity_id must not be empty"

    registry = _get_registry()
    node = registry.graph_store.get_node(entity_id)
    if node is None:
        return f"Entity not found: {entity_id}"

    subgraph = registry.graph_store.get_subgraph(seed_ids=[entity_id], depth=depth)
    result = format_subgraph_as_markdown(node, subgraph, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.event_log,
            layer="mcp",
            operation="get_graph",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        logger.debug("token_tracking_failed", operation="get_graph")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 7: record_feedback
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: one of trace_id or pack_id must be provided"

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
        registry.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )
        target = f"pack: {pack_id}"
    else:
        registry.event_log.emit(
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


@mcp.tool()
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
        return "Error: query must not be empty"

    registry = _get_registry()

    # Search documents
    doc_results = registry.document_store.search(query, limit=limit)
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
    all_nodes = registry.graph_store.query(limit=limit * 2)
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

    if not items:
        return f"No results found for: {query}"

    items.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)
    result = format_pack_as_markdown(
        items[:limit], f"Search: {query}", max_tokens=max_tokens
    )
    try:
        track_token_usage(
            registry.event_log,
            layer="mcp",
            operation="search",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        logger.debug("token_tracking_failed", operation="search")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 9: get_objective_context
# ---------------------------------------------------------------------------


@mcp.tool()
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
        domain: Optional domain filter (e.g., "sportsbook", "data-pipeline").
        max_tokens: Token budget override. Pass ``0`` (default) to use the
            configured budget from ``retrieval.budgets`` in ``config.yaml``;
            pass a positive value to override.
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded from the result, preventing repetition across calls.
    """
    if not intent or not intent.strip():
        return "Error: intent must not be empty"

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
                registry.event_log,
                layer="mcp",
                operation="get_objective_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            logger.debug("token_tracking_failed", operation="get_objective_context")
    except Exception:
        logger.exception("get_objective_context_failed")
        return f"Error: failed to assemble objective context for: {intent}"
    else:
        return result


# ---------------------------------------------------------------------------
# Macro Tool 10: get_task_context
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: intent must not be empty"

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
                registry.event_log,
                layer="mcp",
                operation="get_task_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            logger.debug("token_tracking_failed", operation="get_task_context")
    except Exception:
        logger.exception("get_task_context_failed")
        return f"Error: failed to assemble task context for: {intent}"
    else:
        return result


# ---------------------------------------------------------------------------
# Macro Tool 11: get_sectioned_context
# ---------------------------------------------------------------------------


@mcp.tool()
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
        return "Error: intent must not be empty"
    if not sections:
        return "Error: sections must not be empty"

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
                registry.event_log,
                layer="mcp",
                operation="get_sectioned_context",
                response_tokens=estimate_tokens(result),
                budget_tokens=resolved_tokens,
            )
        except Exception:
            logger.debug("token_tracking_failed", operation="get_sectioned_context")
    except Exception:
        logger.exception("get_sectioned_context_failed")
        return f"Error: failed to assemble sectioned context for: {intent}"
    else:
        return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _configure_mcp_logging() -> None:
    """Route structlog output to stderr so it can't corrupt JSON-RPC.

    The MCP server speaks JSON-RPC over stdio: stdout is reserved for
    protocol frames. structlog's default ``PrintLoggerFactory`` writes
    to ``sys.stdout``, which means the very first store-init log line
    breaks the client's parser. Pinning the factory to ``sys.stderr``
    keeps logs visible to operators while leaving stdout exclusively
    for protocol traffic.

    Honours ``TRELLIS_LOG_LEVEL`` so operators can tune verbosity the
    same way they would for the API. Defaults to INFO.
    """
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415

    level_name = os.environ.get("TRELLIS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    """Run the Macro Tools MCP server."""
    _configure_mcp_logging()
    mcp.run()
