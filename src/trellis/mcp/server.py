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
import time
from typing import Any, NoReturn

import structlog
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from trellis.auth import SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ
from trellis.classify.ingest import classify_metadata_on_write
from trellis.core.write_config import (
    MINHASH_SEED_MAX_DOCS_ENV,
    WriteBehaviourConfig,
)
from trellis.core.write_provenance import get_write_provenance
from trellis.extract.entity_resolution import build_name_alias_resolver
from trellis.extract.trace_ingest_hook import run_trace_extraction
from trellis.feedback.attribution import lookup_pack_item_ids
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import feedback_log_dir
from trellis.feedback.recording import record_feedback as record_pack_feedback
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
from trellis.mcp.reconcile import (
    LIFECYCLE_KEY,
    MARKER_SKIPPED,
    MARKER_STALE,
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    UPDATES_DOC_KEY,
    ReconcileCandidate,
    ReconcileDecision,
    ReconcileOutcome,
    configured_model_id,
    emit_reconcile_verdict,
    judge_reconcile,
    mark_document_superseded,
    reconcile_on_write_enabled,
    reconcile_timeout_seconds,
)
from trellis.mutate import (
    Command,
    CommandStatus,
    Operation,
    build_curate_executor,
    ensure_evidence_document,
)
from trellis.ops import (
    ParameterRegistry,
    check_capture_health,
    format_capture_warning,
)
from trellis.retrieve.embed_ingest_hook import run_embed_on_ingest
from trellis.retrieve.file_context import build_file_context
from trellis.retrieve.formatters import (
    format_advisories_as_markdown,
    format_entity_as_markdown,
    format_fetched_items_as_markdown,
    format_file_context_as_markdown,
    format_lessons_as_markdown,
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
    format_subgraph_as_markdown,
    index_render_overhead_tokens,
)
from trellis.retrieve.pack_builder import PackBuilder, SemanticDedupConfig
from trellis.retrieve.rerankers import build_reranker
from trellis.retrieve.strategies import build_strategies
from trellis.retrieve.token_tracker import estimate_tokens, track_token_usage
from trellis.retrieve.withholding import (
    format_withholding_note,
    withholding_from_payload,
)
from trellis.schemas.memory_op import REF_TYPE_DOCUMENT
from trellis.schemas.pack import PackBudget, SectionRequest
from trellis.schemas.trace import Trace
from trellis.stores.advisory_source import load_advisory_store
from trellis.stores.base.document import DocumentStore
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


# ---------------------------------------------------------------------------
# Surface labels
# ---------------------------------------------------------------------------

#: The ``save_memory`` write-surface label, shared by everything that has to
#: agree on it: the executor's ``requested_by`` on ``MUTATION_EXECUTED``, the
#: ``requested_by`` on the tool's unconditional ``MEMORY_STORED`` accept
#: signal, and the ``mcp:<tool>`` label ``record_write_rejection`` derives for
#: the rejection side. Named once because the capture-health check joins
#: accepts to rejections on exactly this string, and a drift between the two
#: sides is invisible until a banner will not clear (#461).
SAVE_MEMORY_SURFACE = "mcp:save_memory"


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


def _record_boundary_rejection(
    *,
    tool: str,
    error: Exception | None = None,
    rejections: list[dict[str, str]] | None = None,
    hints: list[str] | None = None,
    payload_chars: int | None = None,
) -> dict[str, Any]:
    """Record a tool-boundary rejection as a ``WRITE_REJECTED`` event.

    The executor audits every stage after a Command exists; this covers
    the stage before one does. Doubly fail-soft — a missing registry or a
    broken event log degrades to classification-only, because telemetry
    must never turn a rejected write into a crashed tool. Returns
    ``{rejections, hints}`` for folding into the raised error.
    """
    from trellis.ops.write_health import record_write_rejection  # noqa: PLC0415

    event_log = None
    try:
        event_log = _get_registry().operational.event_log
    except Exception:  # pragma: no cover - registry bootstrap failure
        logger.warning("write_rejection.registry_unavailable", tool=tool)
    return record_write_rejection(
        event_log,
        tool=tool,
        error=error,
        rejections=rejections,
        hints=hints,
        payload_chars=payload_chars,
    )


# The ``instructions`` string is returned in the MCP ``initialize`` response and
# is the only guidance that travels with the connector itself — every client
# (Claude Code, claude.ai, Cowork, any other host) receives it without local
# setup. Host-side skills and hooks cannot be assumed: they exist on one machine
# and do not follow the server. So the behavioural contract lives here.
#
# It earns its tokens by carrying the three things measurement showed were
# actually going wrong on a live deployment (2026-08-15, skynet corpus), not a
# description of what the product is:
#   * retrieval was the starved half of the loop — 6 of 88 sessions ever called
#     a retrieval tool, against 1143 governed writes and 15 assembled packs;
#   * feedback arrived without item attribution, which cannot join to the pack
#     in ``trellis.learning.pack_observations`` and so drove nothing;
#   * ``domain=`` was widely believed unsafe after a since-fixed hard-exclusion
#     defect, so callers avoided a working scope.
# Keep it short — it is injected into every session on every client.
mcp = FastMCP(
    "trellis",
    instructions=(
        "Trellis — persistent memory for AI agents. Responses are concise "
        "markdown sized for LLM context windows.\n"
        "\n"
        "Use it as a loop, not a lookup table:\n"
        "\n"
        "1. BEFORE non-trivial work, call `get_context(intent=...)`. Prior "
        "traces and precedents are cheap; re-deriving them is not. Pass "
        "`session_id` to dedup across calls in one conversation. `domain=` is "
        "safe to pass — it scopes with default-pass semantics and never "
        "hard-excludes memories that carry no domain. An empty pack is a real "
        "answer: it means greenfield, so say so.\n"
        "\n"
        "2. AFTER meaningful work — a fix, a discovery, an instructive failure "
        "— call `save_experience`. Put failures in the step's `error` field; a "
        "workaround the next agent would otherwise rediscover is the most "
        "reusable thing you can leave behind.\n"
        "\n"
        "3. GRADE what you were served: `record_feedback(pack_id=...)` naming "
        "`helpful_item_ids` and `unhelpful_item_ids`. Feedback without item "
        "ids cannot join to the pack and is invisible to the learning loop, so "
        "a reflexive success=true teaches nothing. An honest low `rating` is "
        "worth more than a polite high one.\n"
        "\n"
        "The common failure is retrieval that never happens, not retrieval "
        "that returns nothing."
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
    """Create a PackBuilder wired to this deployment's advisory store.

    The store is resolved through :func:`load_advisory_store` (#373) rather
    than by joining a filename onto ``stores_dir`` here. That is the seam
    that keeps this reader on the same file the nightly advisory worker
    writes, and it is why there is no ``if path.exists()`` guard: a missing
    file yields an empty store plus a log line, not a silent ``None``.
    """
    advisory_store = load_advisory_store(registry.stores_dir, surface="mcp")
    param_registry = ParameterRegistry(registry.operational.parameter_store)
    return PackBuilder(
        strategies=build_strategies(registry, parameter_registry=param_registry),
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
        reranker=build_reranker("rrf", parameter_registry=param_registry),
        # F14 (#259): collapse near-duplicate pack items — the same fact stored
        # via save_memory AND via corpus ingestion surfaced both copies in one
        # pack. MinHash/LSH over item excerpts, relevance-ordered so the
        # highest-scoring copy wins. Default 0.85 Jaccard per the config's
        # guidance table; threshold is a first-class field for future tuning.
        semantic_dedup=SemanticDedupConfig(),
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

    from trellis.extract.memory_ingest_hook import (  # noqa: PLC0415
        memory_extraction_env_enabled,
    )

    if not memory_extraction_env_enabled():
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

    Delegates to :func:`trellis.extract.entity_resolution.build_name_alias_resolver`
    — an indexed ``entity_aliases`` lookup that falls back to a bounded
    scan and mints the binding so the next call is a single row read. The
    matching rule (exact after normalization, ambiguity never guessed) is
    documented on that module.

    A graph-store failure during the fallback scan is logged and yields no
    match, exactly as on the CLI ingest path. An earlier revision raised
    from here instead; it made no observable difference, because the raise
    originates inside ``extractor.extract(...)`` and
    :func:`_run_memory_extraction` wraps the whole pass in ``except
    Exception`` — save_memory still returned success, just with the entire
    extraction pass abandoned rather than one mention unresolved. Sharing
    the soft behaviour keeps the two write paths genuinely identical,
    which is what the resolver module claims.
    """
    return build_name_alias_resolver(registry.knowledge.graph_store)


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
        from trellis.extract.draft_policy import (  # noqa: PLC0415
            apply_memory_draft_policy,
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
        # Same draft policy as the CLI ingest hook (#299/#300): drop
        # participant drafts, stamp fresh mints with document_ids + the
        # unconfirmed/mentioned claim floor. The two write paths must
        # not drift.
        result = apply_memory_draft_policy(result, doc_id=doc_id)
        if not result.entities and not result.edges:
            return

        batch = result_to_batch(result, requested_by=SAVE_MEMORY_SURFACE)
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


#: Rows per ``list_documents`` page while seeding the fuzzy-dedup index.
#: Matches the capture sweep's reconcile walker, which reads the same rows
#: for the same index (``trellis_workers.session_capture.reconcile_pass``).
_MINHASH_SEED_PAGE_SIZE = 500


def _seed_minhash_index(
    index: Any,
    document_store: DocumentStore,
    *,
    max_docs: int,
) -> int:
    """Load up to *max_docs* stored documents into *index*; return rows read.

    **Chunk rows are excluded**, and the reason is measured rather than
    assumed. The obvious worry — a chunk is a near-duplicate of its parent
    by construction, so seeding both would have the parent reject itself —
    does not hold: Jaccard is taken over the *union* of character
    trigrams, and a ~3,000-character slice of a much longer parent cannot
    reach 0.85 against it. Over the reference deployment's 740 chunk rows
    the parent-to-chunk similarity is median 0.294, max **0.641**, and
    **zero** pairs clear the threshold; sibling chunks likewise, zero.

    So chunks are excluded for the two reasons #396 recorded instead, both
    of which survive the measurement. They double the seed's cost (1,475
    rows and ~59 s against 735 rows and ~24 s) for one extra match on the
    whole corpus. And that one match is a `Fuzzy duplicate: <parent>#chunk-1`
    verdict — a fragment id handed back to an agent that should be reading
    the document, when the parent it was sliced from is in the index too.

    Pagination is by ``offset`` over ``created_at DESC``, which both shipped
    backends leave without a tiebreak, so rows sharing a timestamp can
    repeat or be skipped across page boundaries. A repeat cannot corrupt the
    index (:meth:`MinHashIndex.add` is keyed by ``doc_id``) and is not
    indexed twice — but it *does* count toward ``rows_read``, which is both
    the offset and the bound, so repeats spend the operator's budget and the
    index can hold fewer documents than ``max_docs`` names. That is the
    intended reading — the bound is a ceiling on cost, and a re-read row was
    paid for — but the earlier "repeats are harmless" wording said the
    opposite (#455). A skip costs one unseeded document, which degrades
    recall rather than corrupting the index. Stated rather than relied on silently: a
    page that is entirely repeats ends the walk, so an unstable window
    cannot spin.
    """
    seen: set[str] = set()
    rows_read = 0
    while rows_read < max_docs:
        requested = min(_MINHASH_SEED_PAGE_SIZE, max_docs - rows_read)
        page = document_store.list_documents(
            limit=requested,
            offset=rows_read,
            include_chunks=False,
        )
        if not page:
            break
        fresh = 0
        for doc in page:
            # Subscripted, not ``.get``: ``doc_id`` and ``content`` are the
            # DocumentStore contract. A backend that omits one is broken,
            # and the outer handler turns that into a loud init failure —
            # better than seeding a row under the empty-string key.
            doc_id = doc["doc_id"]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            fresh += 1
            index.add(doc_id, doc["content"])
        rows_read += len(page)
        if len(page) < requested:
            break
        if fresh == 0:
            # A full page that contributed nothing new means the listing
            # window moved under the walk. Stopping is the safe choice —
            # the alternative re-reads the same rows forever — but it
            # under-seeds, and an under-seeded index rejects less than the
            # operator asked for, invisibly. So it is said out loud.
            logger.warning(
                "minhash_seed_walk_stalled",
                reason="a full page of rows had all been seen already",
                effect="the index holds fewer documents than the bound allows",
                rows_read=rows_read,
                indexed=len(seen),
            )
            break
    return rows_read


def _warn_index_holds_nothing(*, stored: int, rows_read: int, max_docs: int) -> None:
    """Warn that fuzzy dedup is covering nothing, saying which kind it is.

    Two different situations, two different events, because they want
    completely different fixes. A store with rows and no seeding is a
    *posture* — correct and deliberate, but an operator who believes fuzzy
    dedup covers their corpus is wrong, so it is still said out loud. A
    seed that ran and indexed nothing is a *defect* — the shape #402 was,
    where the read could not return rows at all. One shared warning would
    have let the defect hide behind the posture.
    """
    common = {
        "effect": "fuzzy dedup only sees memories written by this process",
        "stored_documents": stored,
        "issue": 402,
    }
    if max_docs <= 0:
        logger.warning(
            "minhash_index_seed_disabled",
            reason=(
                f"{MINHASH_SEED_MAX_DOCS_ENV} resolved to 0 — unset, 0, "
                "negative, or unparseable; the line naming which is above"
            ),
            **common,
        )
    else:
        logger.warning(
            "minhash_index_seed_empty",
            reason="seeding ran and the index is still empty",
            rows_read=rows_read,
            **common,
        )


def _get_minhash_index(registry: StoreRegistry) -> Any:
    """Get or create a cached MinHash index for fuzzy dedup.

    **Seeding from the store is opt-in and off by default**
    (``TRELLIS_MINHASH_SEED_MAX_DOCS``, see
    :mod:`trellis.core.write_config`). Unseeded — the shipped default —
    this index holds only documents written by the same process, so a
    fresh server cannot detect a fuzzy duplicate of anything already
    stored, and ``save_memory``'s stage-2 rejection is limited to
    within-process repeats. That is the same coverage the broken
    ``search("")`` seed delivered (#402); what changed is that it is now a
    stated posture with a switch, and that the switch works.

    Why it is not simply turned on, given the seed was always meant to
    work: the rejection set it would produce is small and correct on the
    reference deployment (13 of 735, all verified near-duplicates), but a
    complete seed costs ~24 s of blocking CPU on the first ``save_memory``
    of a process and grows with the corpus, and under ``stdio`` that is
    every session in every repository. Catching ~1.6 duplicates a week is
    not worth a 24-second stall imposed on operators who never asked for
    it. It *is* worth it to an operator who reads the number and chooses
    it — including any ``http`` deployment, where
    :func:`_prewarm_registry` pays the cost once at boot rather than on a
    caller's first write.

    A broken dedup module — or a failed seed — is raised rather than silently
    disabled: silent disable means memories are stored without fuzzy dedup,
    producing invisible duplicates. That posture predates the seed repair, and
    holding it *across calls* is why the index is published to the module
    global only after a successful build (see the comment below). The repair
    is what made the failure reachable at all: the old ``search("")`` seed
    could not fail, so nothing had to survive one.
    """
    global _minhash_index  # noqa: PLW0603
    if _minhash_index is not None:
        return _minhash_index
    try:
        from trellis.classify.dedup.minhash import MinHashIndex  # noqa: PLC0415

        # Built into a local and published to the module global only after
        # the seed completes. Assigning the global first would cache a
        # *partially* seeded index on the way out of a failure: the raise
        # below is loud exactly once, and every subsequent call takes the
        # early return above and gets an index holding an arbitrary prefix
        # of the corpus. That is the silent disable this function's docstring
        # says it refuses to do — memories stored without the fuzzy dedup the
        # operator asked for, producing invisible duplicates — and #402's
        # repair is what made it reachable, by turning a seed that could not
        # fail (``search("")`` returned ``[]``) into an O(corpus) read.
        # Republishing nothing means a later call retries the seed and, if it
        # fails again, fails loudly again.
        index = MinHashIndex()
        document_store = registry.knowledge.document_store
        max_docs = WriteBehaviourConfig.from_env().minhash_seed_max_docs
        started = time.perf_counter()
        rows_read = (
            _seed_minhash_index(index, document_store, max_docs=max_docs)
            if max_docs > 0
            else 0
        )
        # INFO, not DEBUG. The predecessor logged the seed size at DEBUG,
        # which is how a seed that had always read zero rows went unnoticed
        # — nobody watches DEBUG. The duration rides along because it is the
        # cost the operator agreed to when setting the bound, and it is the
        # one number that changes as the corpus grows.
        logger.info(
            "minhash_index_initialized",
            size=index.size,
            rows_read=rows_read,
            max_docs=max_docs,
            seconds=round(time.perf_counter() - started, 2),
        )
        if index.size == 0:
            # An empty store legitimately seeds nothing — that is a fresh
            # install and most of the test suite, and a warning that fires
            # there is noise nobody reads.
            stored = document_store.count(include_chunks=False)
            if stored > 0:
                _warn_index_holds_nothing(
                    stored=stored, rows_read=rows_read, max_docs=max_docs
                )
    except Exception as exc:
        logger.exception("minhash_index_init_failed")
        _raise_internal(
            f"MinHash dedup index initialisation failed: {exc}",
            cause=exc,
            data={"stage": "minhash_index_init"},
        )
    _minhash_index = index
    return _minhash_index


# ---------------------------------------------------------------------------
# One retrieval path (#262)
# ---------------------------------------------------------------------------
#
# get_context / search / get_objective_context / get_task_context /
# get_sectioned_context all route through PackBuilder — one place that runs
# keyword + graph + semantic strategies, fuses them with RRF, applies
# recency/importance decay, session-aware dedup and domain default-pass
# scoping, and emits a rich ``PACK_ASSEMBLED`` event carrying ``pack_id``.
# There is no hand-rolled axis merging, fixed-relevance heuristic, or
# duplicate session-dedup scan in this module any more. Domain default-pass
# lives in the strategy layer (``PackBuilder._apply_domain_scope`` +
# per-strategy filter handling), so every routed tool inherits it.

#: Default item ceiling for flat packs. The markdown formatter enforces the
#: token budget; this bounds the candidate list the budget walk considers.
_FLAT_MAX_ITEMS = 50

#: Floor for the index-mode builder budget once the rendering overhead is
#: reserved — roughly one index line. A budget too small to survey anything
#: should still answer with one id, not "no context found" about a corpus
#: that has some.
_MIN_INDEX_BUDGET_TOKENS = 20

#: Human-readable label per tool for INTERNAL_ERROR messages on the sectioned
#: path (keeps the pre-#262 wording the error-contract tests assert).
_TOOL_LABEL = {
    "get_context": "context",
    "get_objective_context": "objective context",
    "get_task_context": "task context",
    "get_sectioned_context": "sectioned context",
}


def _track_tokens(
    registry: StoreRegistry,
    *,
    operation: str,
    result: str,
    budget: int,
    pack_id: str | None = None,
) -> None:
    """Best-effort response-token telemetry — never fails the tool call.

    ``pack_id`` is passed by the two pack-assembling paths so response
    cost is attributable to the pack that caused it; pack-free callers
    (``get_items``) leave it ``None`` rather than inventing one.
    """
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation=operation,
            response_tokens=estimate_tokens(result),
            budget_tokens=budget,
            pack_id=pack_id,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry;
        # failure here must not invalidate a successfully assembled pack.
        logger.exception("token_tracking_failed", operation=operation)


def _capture_warning_banner(registry: StoreRegistry) -> str:
    """Best-effort capture-health banner for pack outputs — never raises.

    #309: write-boundary failures (#297) were invisible unless the
    operator ran ``trellis analyze health``; when a surface's recent
    rejections cross the threshold with no accepted write of its own, the
    warning now rides inside the packs agents already read — including
    the empty state, which is exactly where a dark capture path otherwise
    looks like greenfield. Empty string means healthy *or indeterminate*:
    an indeterminate check must present as healthy.

    Callers prepend the banner *after* budgeting the pack, so it rides
    outside ``max_tokens`` (~60 tokens) rather than competing with the
    context it warns about — see :func:`format_capture_warning`.
    """
    try:
        warning = check_capture_health(registry.operational.event_log)
        return format_capture_warning(warning) if warning is not None else ""
    except Exception:
        # GRACEFUL-DEGRADATION: the health check is advisory read-side
        # telemetry; its failure must never block or corrupt a
        # successfully assembled pack.
        logger.exception("capture_health_check_failed")
        return ""


def _flat_context(
    registry: StoreRegistry,
    intent: str,
    *,
    domain: str | None,
    max_tokens: int,
    session_id: str,
    run_id: str = "",
    max_items: int = _FLAT_MAX_ITEMS,
    title: str | None = None,
    empty_message: str | None = None,
    operation: str = "get_context",
    refresh: bool = False,
    index: bool = False,
) -> str:
    """Assemble a flat pack through the one PackBuilder-backed path.

    Shared by ``get_context`` and ``search``. Domain default-pass scoping,
    session dedup (via ``PackBuilder._recently_served``), RRF fusion,
    recency/importance decay and the rich ``PACK_ASSEMBLED`` emission
    (with ``pack_id``) all come from :class:`PackBuilder`.

    ``refresh=True`` bypasses session dedup for this call (client
    compaction signal), forwarded to :meth:`PackBuilder.build`.

    ``index=True`` (#305) assembles and renders the pack as an id index —
    one compact line per item, no excerpt bodies. Still the one
    PackBuilder path: same ``pack_id``, same ``PACK_ASSEMBLED`` telemetry,
    unchanged ``record_feedback`` attribution; only the budget charge and
    the rendering differ.

    ``run_id`` (optional) is forwarded so the ``PACK_ASSEMBLED`` event can
    credit the run this pack served — ``record_feedback`` carries no run
    identity, so the pack side is the only place it can enter the learning
    join. Empty string means "no run identity"; the join then keeps its
    ``unknown-run`` bucket rather than substituting ``session_id``, which
    spans many runs.

    Failure posture (adopted from PackBuilder for #262): a single-axis
    outage degrades to the surviving axes; only a total retrieval failure
    (``PackAssemblyError``) surfaces as ``INTERNAL_ERROR``.
    """
    # The index renderer spends tokens on its heading before the first
    # item line, and re-budgets the lines it is given. Reserve that here
    # so the builder's walk cannot admit a tail the rendering then drops:
    # such an item is recorded as served (session dedup suppresses it for
    # the rest of the session, the learning join grades it) while the
    # agent never saw its id.
    budget_tokens = max_tokens
    if index:
        budget_tokens = max(
            max_tokens - index_render_overhead_tokens(title or intent),
            _MIN_INDEX_BUDGET_TOKENS,
        )

    try:
        builder = _build_pack_builder(registry)
        pack = builder.build(
            intent,
            domain=domain or None,
            session_id=session_id or None,
            run_id=run_id or None,
            budget=PackBudget(max_items=max_items, max_tokens=budget_tokens),
            # Fetch at least as many candidates per axis as the item budget
            # allows — a caller raising ``limit`` above the PackBuilder
            # default (20) gains recall instead of being silently capped at
            # 20 candidates per axis. ``max(20, ...)`` keeps the default
            # fetch depth unchanged for small budgets.
            limit_per_strategy=max(20, max_items),
            refresh=refresh,
            index_mode=index,
        )
    except McpError:
        raise
    except Exception as exc:
        logger.exception("flat_context_failed", operation=operation)
        _raise_internal(
            f"failed to assemble {_TOOL_LABEL.get(operation, 'context')} "
            f"for intent={intent!r}: {exc}",
            cause=exc,
            data={"tool": operation, "intent": intent},
        )

    banner = _capture_warning_banner(registry)
    # #404: read the summary the builder stamped, do not re-derive one.
    withholding = withholding_from_payload(pack.metadata.get("withholding"))

    if not pack.items:
        # The case #404 was filed about. An empty pack whose candidates
        # were *all* removed by a gate reads as greenfield — identical
        # text to a corpus that genuinely held nothing — and that is the
        # single most misleading pack this server can return. The note
        # goes here first, before it goes anywhere else.
        empty = empty_message or f"No context found for: {intent}"
        note = format_withholding_note(withholding)
        if note:
            empty = f"{empty}\n\n{note}"
        return f"{banner}\n\n{empty}" if banner else empty

    item_dicts = [
        {
            "item_id": item.item_id,
            "item_type": item.item_type,
            "excerpt": item.excerpt,
            "relevance_score": item.relevance_score,
            # Read-cost + title inputs for the index line renderer;
            # format_pack_as_markdown ignores both.
            "estimated_tokens": item.estimated_tokens,
            "metadata": item.metadata,
        }
        for item in pack.items
    ]
    formatter = format_pack_as_index_markdown if index else format_pack_as_markdown
    result = formatter(
        item_dicts,
        title or intent,
        max_tokens=max_tokens,
        pack_id=pack.pack_id,
        withholding=withholding,
    )
    if banner:
        result = f"{banner}\n\n{result}"
    _track_tokens(
        registry,
        operation=operation,
        result=result,
        budget=max_tokens,
        pack_id=pack.pack_id,
    )
    return result


def _sectioned_context(
    registry: StoreRegistry,
    intent: str,
    *,
    section_specs: list[dict[str, Any]],
    resolved_tokens: int,
    domain: str,
    session_id: str,
    tool: str,
    run_id: str = "",
    refresh: bool = False,
) -> str:
    """Assemble a sectioned pack through the one PackBuilder-backed path.

    Shared by ``get_objective_context`` / ``get_task_context`` /
    ``get_sectioned_context`` and by ``get_context`` when called with a
    custom ``sections`` layout. ``section_specs`` are raw dicts validated
    into :class:`SectionRequest` inside the failure boundary so a malformed
    section surfaces as ``INTERNAL_ERROR`` (the pre-#262 contract).

    ``refresh=True`` bypasses session dedup for this call (client
    compaction signal), forwarded to :meth:`PackBuilder.build_sectioned`.
    ``run_id`` is forwarded for the same reason as on the flat path.
    """
    try:
        builder = _build_pack_builder(registry)
        sections = [SectionRequest.model_validate(s) for s in section_specs]
        sectioned_pack = builder.build_sectioned(
            intent,
            sections=sections,
            domain=domain or None,
            session_id=session_id or None,
            run_id=run_id or None,
            refresh=refresh,
        )
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
            withholding=withholding_from_payload(
                sectioned_pack.metadata.get("withholding")
            ),
        )
        adv_md = format_advisories_as_markdown(sectioned_pack.advisories)
        if adv_md:
            result = result + "\n\n" + adv_md
        banner = _capture_warning_banner(registry)
        if banner:
            result = f"{banner}\n\n{result}"
        _track_tokens(
            registry,
            operation=tool,
            result=result,
            budget=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
        )
    except McpError:
        # Already structured by a deeper helper — let it propagate.
        raise
    except Exception as exc:
        logger.exception("sectioned_context_failed", tool=tool)
        _raise_internal(
            f"failed to assemble {_TOOL_LABEL.get(tool, 'context')} "
            f"for intent={intent!r}: {exc}",
            cause=exc,
            data={"tool": tool, "intent": intent},
        )
    return result


# ---------------------------------------------------------------------------
# Macro Tool 1: get_context — the one parameterized retrieval tool (#262)
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_context(
    intent: str,
    domain: str | None = None,
    max_tokens: int = 2000,
    session_id: str = "",
    run_id: str = "",
    sections: list[dict[str, Any]] | None = None,
    refresh: bool = False,
    index: bool = False,
) -> str:
    """Get relevant context from the experience graph for a task or question.

    Fuses keyword, knowledge-graph and semantic retrieval (Reciprocal Rank
    Fusion, recency/importance decay, session-aware dedup) into one
    token-budgeted markdown pack with a citable ``pack_id``. This is the
    single retrieval entry point — pass ``sections`` for the sectioned
    layout that ``get_sectioned_context`` used to provide, or ``index=True``
    for a survey-first workflow: scan the index, walk ``get_graph`` from an
    interesting item to its evidence pointers, then batch-fetch chosen ids
    with ``get_items``.

    Args:
        intent: What you're trying to do or learn about.
        domain: Optional domain scope (e.g., "platform", "data"). Scoped
            with default-pass semantics — a memory that carries no domain is
            never hard-excluded; only an explicit mismatch is dropped.
        max_tokens: Maximum response size in tokens (default 2000).
        session_id: Optional conversation/session identifier. When supplied,
            items already returned by recent calls in this session are
            excluded, preventing repetition across calls.
        run_id: Optional identifier for the unit of work this context is
            for (one task / job / workflow run — narrower than a session).
            Recorded on the pack so later feedback can credit the runs a
            memory actually helped. Leave empty when you have no such id.
        sections: Optional custom section layout — a list of section config
            dicts (each with ``name`` plus optional ``retrieval_affinities``,
            ``content_types``, ``scopes``, ``entity_ids``, ``max_tokens``,
            ``max_items``). When provided, context is organised into
            independently budgeted sections instead of a flat list.
        refresh: Bypass session dedup for this call only (default False).
            Set when the caller's context window was truncated (e.g. after
            compaction) and it needs previously-served items re-injected.
            Only affects this call; ``session_id`` must still be supplied
            for it to matter, and later calls dedup normally.
        index: Return an id index instead of excerpt bodies (default
            False): one compact line per item — ``item_id``, type, title,
            estimated read cost — so the same ``max_tokens`` surveys many
            more items. The pack is real (same ``pack_id``, same
            telemetry), so ``record_feedback`` works unchanged; fetch the
            bodies you choose with ``get_items``. Because it is a real
            pack, an index survey **counts as a serve**: with a
            ``session_id``, every id it listed is deduped out of later
            packs in that session, so pass ``refresh=True`` on a
            follow-up full retrieval that needs them back. Flat layout
            only — combining with ``sections`` is an error.
    """
    if not intent or not intent.strip():
        _raise_invalid_params(
            "intent must not be empty",
            data={"field": "intent"},
        )

    registry = _get_registry()

    if sections is not None:
        if index:
            _raise_invalid_params(
                "index mode applies to the flat layout; drop sections or drop index",
                data={"fields": ["index", "sections"]},
            )
        if not sections:
            _raise_invalid_params(
                "sections must not be empty",
                data={"field": "sections"},
            )
        budget = registry.budget_config.resolve(
            tool="get_context",
            domain=domain or None,
            caller_override_tokens=max_tokens if max_tokens > 0 else None,
        )
        return _sectioned_context(
            registry,
            intent,
            section_specs=sections,
            resolved_tokens=budget.max_tokens,
            domain=domain or "",
            session_id=session_id,
            tool="get_context",
            run_id=run_id,
            refresh=refresh,
        )

    return _flat_context(
        registry,
        intent,
        domain=domain,
        max_tokens=max_tokens,
        session_id=session_id,
        run_id=run_id,
        operation="get_context",
        refresh=refresh,
        index=index,
    )


# ---------------------------------------------------------------------------
# Macro Tool 2: save_experience
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_experience(trace_json: str) -> str:
    """Save an experience trace to the graph.

    Args:
        trace_json: JSON string conforming to the Trace schema. The required
            top-level keys are ``source``, ``intent``, and ``context``.
            ``source`` must be one of ``agent``, ``human``, ``workflow``, or
            ``system``. Put execution details such as ``agent_id``, ``domain``,
            ``started_at``, and ``ended_at`` inside ``context``. Each entry in
            ``steps`` requires ``step_type`` and ``name``; do not use
            ``action``/``observation`` as field names. Validation is
            all-or-nothing: an unknown top-level or context field rejects the
            **entire trace** and nothing is recorded — not just that field. So
            put additional data in top-level ``metadata`` (or outcome
            measurements in ``outcome.metrics``).

            Example minimal valid trace:
            {
              "source": "agent",
              "intent": "Fix a failing test",
              "context": {
                "agent_id": "coder-1",
                "domain": "bugfix",
                "started_at": "2026-08-27T00:00:00Z",
                "ended_at": "2026-08-27T00:01:00Z"
              },
              "metadata": {"repo": "my/repo"},
              "steps": [
                {"step_type": "tool_call", "name": "search"}
              ]
            }
    """
    if not trace_json or not trace_json.strip():
        _record_boundary_rejection(
            tool="save_experience",
            rejections=[
                {"kind": "empty_required", "loc": "trace_json", "msg": "empty"}
            ],
            payload_chars=0,
        )
        _raise_invalid_params(
            "trace_json must not be empty",
            data={"field": "trace_json"},
        )

    try:
        trace = Trace.model_validate_json(trace_json)
    except Exception as exc:
        from trellis.ops.write_health import (  # noqa: PLC0415
            classify_rejection,
            hints_for_trace_rejections,
        )

        rows = classify_rejection(exc)
        hints = hints_for_trace_rejections(rows)
        _record_boundary_rejection(
            tool="save_experience",
            error=exc,
            rejections=rows,
            hints=hints,
            payload_chars=len(trace_json),
        )
        hint_suffix = f" | fix: {'; '.join(hints)}" if hints else ""
        _raise_invalid_params(
            f"invalid trace JSON: {exc}{hint_suffix}",
            data={
                "field": "trace_json",
                "error_class": type(exc).__name__,
                "hints": hints,
            },
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


def _resolve_evidence_pointer(
    registry: StoreRegistry,
    *,
    name: str,
    entity_type: str,
    content: str | None,
    evidence_ref: str | None,
) -> tuple[str | None, bool]:
    """Resolve the evidence-document pointer for ``save_knowledge``.

    Returns ``(evidence_ref, content_ignored)``. Runs BEFORE any graph
    mutation, enforcing the two halves of the pointer-not-prose ordering
    contract at the agent-facing boundary:

    * An explicit ``evidence_ref`` must reference an existing document.
      Stale or hallucinated doc ids are plausible agent inputs, and
      attaching one would create a permanent dangling graph pointer — the
      state the invariant forbids outright. Mirrors the FK-existence
      precedent ``LinkCreateHandler`` applies to edge endpoints. When both
      ``evidence_ref`` and ``content`` are supplied, the explicit pointer
      wins and ``content_ignored=True`` signals the caller to surface a
      notice rather than silently discarding the prose.

    * ``content`` without a pointer auto-creates the evidence document
      doc-FIRST: on partial failure the orphaned doc is acceptable
      (findable, prunable); a dangling graph pointer is never acceptable.
    """
    if evidence_ref is not None:
        if registry.knowledge.document_store.get(evidence_ref) is None:
            _record_boundary_rejection(
                tool="save_knowledge",
                rejections=[
                    {
                        "kind": "dangling_reference",
                        "loc": "evidence_ref",
                        "msg": f"no document {evidence_ref}",
                    }
                ],
            )
            _raise_invalid_params(
                f"evidence_ref does not reference an existing document: {evidence_ref}",
                data={"field": "evidence_ref", "evidence_ref": evidence_ref},
            )
        return evidence_ref, content is not None and bool(content.strip())
    if content is not None and content.strip():
        return (
            ensure_evidence_document(
                registry,
                content,
                metadata={"entity_name": name, "entity_type": entity_type},
                source="mcp:save_knowledge",
            ),
            False,
        )
    return None, False


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
        evidence_ref: Optional existing document id to point at. Must reference
            a document that already exists — a nonexistent id is rejected
            before any mutation, because a graph pointer at a missing document
            is exactly the dangling state this tool exists to prevent. When
            set, no document is created and ``content`` is ignored (a notice
            is appended to the result).
    """
    if not name or not name.strip():
        _raise_invalid_params(
            "name must not be empty",
            data={"field": "name"},
        )

    registry = _get_registry()

    # Existence-check any explicit pointer / auto-create the evidence doc
    # (doc-FIRST) before any graph mutation — see _resolve_evidence_pointer
    # for the two halves of the pointer-not-prose ordering contract.
    evidence_ref, content_ignored = _resolve_evidence_pointer(
        registry,
        name=name,
        entity_type=entity_type,
        content=content,
        evidence_ref=evidence_ref,
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
    if content_ignored:
        result += "\nWarning: content ignored — evidence_ref takes precedence"

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
                result += f"\nWarning: edge not created: {link_result.message}"

    return result


# ---------------------------------------------------------------------------
# Macro Tool 4: save_memory
# ---------------------------------------------------------------------------


def _emit_memory_stored_and_enrich(
    registry: StoreRegistry,
    stored_id: str,
    content: str,
    metadata: dict[str, Any],
    chash: str,
) -> None:
    """Post-store tail shared by every save_memory path that persists a doc.

    Emits ``MEMORY_STORED`` (a hard requirement — the store already happened),
    then runs the two feature-flagged, best-effort enrichment stages (tiered
    extraction, embed-on-ingest). Runs *outside* ``_save_memory_lock``; never
    for the NOOP verdict, which stores nothing.

    The payload carries ``requested_by="mcp:save_memory"`` — the same surface
    label the executor stamps on ``MUTATION_EXECUTED`` and
    ``record_write_rejection`` on ``WRITE_REJECTED`` — because this is the
    only *unconditional* success signal this tool has, and the capture-health
    banner needs one to clear against (#461). The tool's ``MUTATION_EXECUTED``
    comes solely from ``_run_memory_extraction``, which is gated on
    ``TRELLIS_ENABLE_MEMORY_EXTRACTION`` (default off) and returns early
    emitting nothing when extraction yields no drafts, so under the shipped
    defaults a perfectly healthy ``save_memory`` accepted nothing the banner
    could see. ``Event.source`` cannot stand in: ``MEMORY_STORED`` has three
    emitters and matching a coarse source is the looseness #458 refused.
    """
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
                "requested_by": SAVE_MEMORY_SURFACE,
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
        _record_boundary_rejection(
            tool="save_memory",
            rejections=[{"kind": "empty_required", "loc": "content", "msg": "empty"}],
            payload_chars=0,
        )
        _raise_invalid_params(
            "content must not be empty",
            data={"field": "content"},
        )

    from trellis.core.hashing import content_hash  # noqa: PLC0415

    registry = _get_registry()
    metadata = dict(metadata or {})

    chash = content_hash(content)

    # Model-judged verdict tier (TRELLIS_ENABLE_RECONCILE_ON_WRITE=1). When a
    # near — not exact — match exists, a local model decides
    # ADD/UPDATE/SUPERSEDE/NOOP instead of today's binary keep/drop. Off by
    # default: the deterministic-only path below is unchanged.
    if reconcile_on_write_enabled():
        return _save_memory_reconciled(registry, content, metadata, doc_id, chash)

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

        # Classify-on-write (see classify_metadata_on_write). Placement is
        # load-bearing: after both dedup stages (a hit stores nothing to tag)
        # and before the put, and the rebind carries the tags into the
        # MEMORY_STORED payload and the embed hook's vector row below.
        metadata = classify_metadata_on_write(metadata, content, doc_id=doc_id or "")

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

    _emit_memory_stored_and_enrich(registry, stored_id, content, metadata, chash)
    return f"Memory saved: {stored_id}"


# ---------------------------------------------------------------------------
# save_memory — model-judged reconcile-on-write verdict tier (#263)
# ---------------------------------------------------------------------------
#
# Layered on top of the deterministic tier (exact-hash → MinHash near-dup,
# serialized by ``_save_memory_lock``). Feature-flagged
# (TRELLIS_ENABLE_RECONCILE_ON_WRITE); off by default. The lock discipline is
# the whole point: an 8B verdict takes seconds, so the model call happens
# OUTSIDE the lock, and preconditions are re-verified UNDER the lock before the
# verdict commits (the world may have changed while the model thought).


def _index_stored_memory(registry: StoreRegistry, stored_id: str, content: str) -> None:
    """Add a freshly stored doc to the MinHash index (loud on failure)."""
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


def _store_new_memory(
    registry: StoreRegistry,
    document_store: Any,
    doc_id: str | None,
    content: str,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Persist a new memory doc and index it. Runs under ``_save_memory_lock``.

    Returns ``(stored_id, stored_metadata)``. The metadata comes back because
    classify-on-write may have added tags to it, and the post-store tail
    (MEMORY_STORED payload, embed hook) must see what was actually persisted —
    every reconcile verdict that stores a doc funnels through here.
    """
    # Classify-on-write — same seam as the deterministic tier's put.
    metadata = classify_metadata_on_write(metadata, content, doc_id=doc_id or "")
    stored_id: str = document_store.put(doc_id, content, metadata=metadata)
    _index_stored_memory(registry, stored_id, content)
    return stored_id, metadata


def _gather_reconcile_candidate(
    registry: StoreRegistry, document_store: Any, content: str
) -> ReconcileCandidate | None:
    """Return the top MinHash near-match to adjudicate, or ``None`` if clean.

    Runs under ``_save_memory_lock`` — the same near-dup scan the deterministic
    tier does, but it hands the match to the model instead of auto-dropping it.
    """
    minhash_index = _get_minhash_index(registry)
    if minhash_index is None:
        return None
    matches = minhash_index.query(content)
    if not matches:
        return None
    match_id, similarity = matches[0]
    doc = document_store.get(match_id)
    if doc is None:
        return None
    return ReconcileCandidate(
        doc_id=match_id, content=doc["content"], similarity=float(similarity)
    )


def _compute_reconcile_outcome(
    registry: StoreRegistry, content: str, candidate: ReconcileCandidate
) -> ReconcileOutcome:
    """Build the client and compute the verdict — **outside** the lock.

    The model is never a hard dependency: an unbuildable / unavailable client
    yields a fallback ADD outcome rather than raising.
    """
    model_id = configured_model_id()
    try:
        client = _build_llm_client(registry)
    except Exception:
        # A misconfigured / uninstalled provider must not fail a capture —
        # the memory is saved as a plain ADD, marked for a later sweep.
        logger.warning("reconcile_client_build_failed")
        client = None
    if client is None:
        logger.info("reconcile_model_unavailable")
        return ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id=model_id,
            fallback=True,
            fallback_reason="model_unavailable",
        )
    return judge_reconcile(
        client,
        new_content=content,
        candidate=candidate,
        timeout=reconcile_timeout_seconds(),
        model_id=model_id,
    )


def _candidate_is_stale(document_store: Any, candidate: ReconcileCandidate) -> bool:
    """True when the candidate no longer matches what the model judged.

    Two staleness axes, both re-checked under the lock:

    * **Content** — the candidate was edited or deleted while the model
      thought; the verdict is about text that no longer exists.
    * **Lifecycle** — the candidate was superseded/deprecated by a concurrent
      writer. ``mark_document_superseded`` preserves content (SCD-2), so a
      content-hash check alone would let two racing SUPERSEDE verdicts both
      commit and fork the supersession chain — ``superseded_by`` can only name
      one successor. Any non-``current`` lifecycle state fails re-verify.
    """
    from trellis.core.hashing import content_hash  # noqa: PLC0415

    current = document_store.get(candidate.doc_id)
    if current is None:
        return True
    if content_hash(current["content"]) != content_hash(candidate.content):
        return True
    lifecycle = (current.get("metadata") or {}).get(LIFECYCLE_KEY)
    return (
        isinstance(lifecycle, dict) and lifecycle.get("state", "current") != "current"
    )


def _reverify_candidate(
    document_store: Any,
    candidate: ReconcileCandidate,
    outcome: ReconcileOutcome,
) -> ReconcileOutcome:
    """Re-check the candidate under the lock; downgrade to ADD if it changed.

    Another writer may have edited, deleted, or (lifecycle axis) superseded
    the candidate while the model was thinking. Applying a stale verdict
    would act on a doc that no longer is what the model judged, so we
    downgrade to a plain ADD marked ``stale_recheck`` (data is never lost;
    no synchronous re-judge — the marker queues it for a later sweep).
    """
    if outcome.fallback:
        return outcome  # already an ADD — nothing to re-verify against
    if _candidate_is_stale(document_store, candidate):
        logger.info(
            "reconcile_candidate_changed_downgrade_to_add",
            candidate_id=candidate.doc_id,
        )
        return ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=outcome.confidence,
            model_id=outcome.model_id,
            fallback=True,
            fallback_reason="stale_recheck",
        )
    return outcome


def _commit_reconcile_verdict(
    registry: StoreRegistry,
    document_store: Any,
    doc_id: str | None,
    content: str,
    metadata: dict[str, Any],
    candidate: ReconcileCandidate,
    outcome: ReconcileOutcome,
) -> tuple[str | None, dict[str, Any] | None, ReconcileOutcome]:
    """Apply the verdict under ``_save_memory_lock``.

    Returns ``(stored_id, stored_metadata, applied_outcome)`` —
    ``(None, None, outcome)`` for NOOP, which stores nothing. SUPERSEDE rides
    SCD-2: a new doc plus Lifecycle stale-marking of the old, never a delete.

    The outcome comes back because it can be *downgraded* here: a SUPERSEDE
    whose target vanished did not supersede anything, and the caller renders
    the tool result and decides whether to emit ``MEMORY_OP_JUDGED`` from it.
    Returning the input outcome unchanged would put the caller back in the
    position #407 describes — asserting an effect it never checked.
    """
    if outcome.fallback:
        marker = (
            MARKER_STALE
            if outcome.fallback_reason == "stale_recheck"
            else MARKER_SKIPPED
        )
        meta = {**metadata, RECONCILIATION_KEY: marker}
        stored = _store_new_memory(registry, document_store, doc_id, content, meta)
        return stored[0], stored[1], outcome

    decision = outcome.decision
    if decision == ReconcileDecision.NOOP:
        return None, None, outcome
    if decision == ReconcileDecision.UPDATE:
        meta = {
            **metadata,
            RECONCILIATION_KEY: ReconcileDecision.UPDATE.value,
            UPDATES_DOC_KEY: candidate.doc_id,
        }
        stored = _store_new_memory(registry, document_store, doc_id, content, meta)
        return stored[0], stored[1], outcome
    if decision == ReconcileDecision.SUPERSEDE:
        meta = {
            **metadata,
            RECONCILIATION_KEY: ReconcileDecision.SUPERSEDE.value,
            SUPERSEDES_DOC_KEY: candidate.doc_id,
        }
        stored_id, stored_meta = _store_new_memory(
            registry, document_store, doc_id, content, meta
        )
        if mark_document_superseded(
            document_store, old_doc_id=candidate.doc_id, new_doc_id=stored_id
        ):
            return stored_id, stored_meta, outcome
        # The target vanished between the under-lock re-verify and this write
        # (``_save_memory_lock`` is process-local, so another process can
        # delete it). The memory is stored; the supersession is not — so strip
        # the claim from what was just written and hand the caller a
        # stale-downgraded outcome. The write is metadata-only, hence
        # ``preserve_updated_at`` (#397).
        meta = {k: v for k, v in stored_meta.items() if k != SUPERSEDES_DOC_KEY}
        meta[RECONCILIATION_KEY] = MARKER_STALE
        document_store.put(stored_id, content, metadata=meta, preserve_updated_at=True)
        return stored_id, meta, _downgraded_to_stale(outcome)
    # ADD
    meta = {**metadata, RECONCILIATION_KEY: ReconcileDecision.ADD.value}
    stored = _store_new_memory(registry, document_store, doc_id, content, meta)
    return stored[0], stored[1], outcome


def _downgraded_to_stale(outcome: ReconcileOutcome) -> ReconcileOutcome:
    """Mark a verdict as not applied: a fallback ADD, ``stale_recheck``.

    ``fallback=True`` is what suppresses the ``MEMORY_OP_JUDGED`` emit and
    makes :func:`_reconcile_result_message` render a plain save, which is the
    whole point — the model judged, but nothing was superseded, so the join
    key would point at a document that no longer exists.
    """
    return ReconcileOutcome(
        decision=ReconcileDecision.ADD,
        confidence=outcome.confidence,
        model_id=outcome.model_id,
        fallback=True,
        fallback_reason="stale_recheck",
    )


def _reconcile_subject(
    decision: ReconcileDecision, candidate: ReconcileCandidate, stored_id: str | None
) -> tuple[str, str]:
    """Pick the ``subject_ref`` the verdict is *about* (the feedback join key).

    ADD is about the new memory; UPDATE / SUPERSEDE / NOOP are about the
    pre-existing candidate that triggered the reconciliation.
    """
    if decision == ReconcileDecision.ADD and stored_id is not None:
        return REF_TYPE_DOCUMENT, stored_id
    return REF_TYPE_DOCUMENT, candidate.doc_id


def _reconcile_result_message(
    outcome: ReconcileOutcome, candidate: ReconcileCandidate, stored_id: str | None
) -> str:
    """Human-readable tool result for a reconciled write."""
    if outcome.fallback or outcome.decision == ReconcileDecision.ADD:
        return f"Memory saved: {stored_id}"
    if outcome.decision == ReconcileDecision.NOOP:
        return f"Memory already covered by: {candidate.doc_id}"
    if outcome.decision == ReconcileDecision.UPDATE:
        return f"Memory saved (update of {candidate.doc_id}): {stored_id}"
    return f"Memory saved (supersedes {candidate.doc_id}): {stored_id}"


def _save_memory_reconciled(
    registry: StoreRegistry,
    content: str,
    metadata: dict[str, Any],
    doc_id: str | None,
    chash: str,
) -> str:
    """Reconcile-on-write path: deterministic short-circuit + model verdict.

    Three phases, per the binding guide's lock discipline:
      A. under the lock — exact-hash short-circuit; gather the near-dup
         candidate; a *clean* ADD (no candidate) stores immediately;
      B. outside the lock — the (slow) model verdict;
      C. under the lock — re-verify the candidate, then commit the verdict.
    """
    document_store = registry.knowledge.document_store

    # -- Phase A: gather under the lock --------------------------------------
    with _save_memory_lock:
        existing = document_store.get_by_hash(chash)
        if existing is not None:
            existing_id = existing["doc_id"]
            logger.debug(
                "save_memory_dedup_exact", doc_id=existing_id, content_hash=chash
            )
            return f"Memory already exists: {existing_id}"

        candidate = _gather_reconcile_candidate(registry, document_store, content)
        if candidate is None:
            # No near match: an unambiguous ADD — no model call, no verdict.
            # Store under the lock exactly as the deterministic tier would.
            clean_id, clean_meta = _store_new_memory(
                registry, document_store, doc_id, content, metadata
            )

    if candidate is None:
        _emit_memory_stored_and_enrich(registry, clean_id, content, clean_meta, chash)
        return f"Memory saved: {clean_id}"

    # -- Phase B: verdict OUTSIDE the lock (never serialize saves on a model) -
    # _compute_reconcile_outcome is internally exhaustive (client build, model
    # transport, timeout, malformed JSON all resolve to fallback ADDs), but
    # "capture never fails because a judge failed" must be total, not
    # near-total — a belt-and-suspenders guard turns any escape into the same
    # offline fallback. Fail-soft, never silent: the traceback goes to stderr.
    try:
        outcome = _compute_reconcile_outcome(registry, content, candidate)
    except Exception:
        logger.exception("reconcile_phase_b_failed", candidate_id=candidate.doc_id)
        outcome = ReconcileOutcome(
            decision=ReconcileDecision.ADD,
            confidence=0.0,
            model_id=configured_model_id(),
            fallback=True,
            fallback_reason="judge_error",
        )

    # -- Phase C: re-verify + commit under the lock --------------------------
    with _save_memory_lock:
        raced = document_store.get_by_hash(chash)
        if raced is not None:
            # A concurrent writer stored identical content while we judged —
            # the deterministic dedup wins; drop our now-duplicate write.
            return f"Memory already exists: {raced['doc_id']}"
        outcome = _reverify_candidate(document_store, candidate, outcome)
        stored_id, stored_meta, outcome = _commit_reconcile_verdict(
            registry, document_store, doc_id, content, metadata, candidate, outcome
        )

    # -- Emit + enrich outside the lock --------------------------------------
    # A genuine model verdict (not a fallback / stale downgrade) is a training
    # pair: emit MEMORY_OP_JUDGED. Fallback ADDs judged nothing, so no event.
    if not outcome.fallback:
        subject_type, subject_id = _reconcile_subject(
            outcome.decision, candidate, stored_id
        )
        emit_reconcile_verdict(
            registry.operational.event_log,
            outcome=outcome,
            new_content=content,
            candidate=candidate,
            subject_ref_type=subject_type,
            subject_ref_id=subject_id,
        )

    # Everything but NOOP persisted a doc → MEMORY_STORED + enrichment.
    if stored_id is not None:
        _emit_memory_stored_and_enrich(
            registry, stored_id, content, stored_meta or metadata, chash
        )

    return _reconcile_result_message(outcome, candidate, stored_id)


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
# Macro Tool: get_items — batch fetch-by-id (#305)
# ---------------------------------------------------------------------------

#: Hard cap on ids per ``get_items`` call. A pack serves at most
#: ``_FLAT_MAX_ITEMS`` items, so one call can fetch a whole index pack;
#: anything larger is a bulk export, which is the REST/CLI surface's job.
_GET_ITEMS_MAX_IDS = 50


def _render_fetched_item(
    registry: StoreRegistry, item_id: str
) -> tuple[str, str] | None:
    """Resolve ``item_id`` to ``(kind, markdown body)``, or ``None``.

    Resolution order mirrors where pack item ids point: the document
    store (keyword/semantic items — vector ``item_id``s are doc ids),
    then the graph store (entity/observation items), then the trace
    store. A graph node goes through the same
    :func:`~trellis.retrieve.formatters.format_entity_as_markdown` block
    ``get_graph`` renders, so a fetched entity carries the ``document_ids``
    evidence pointers that make it a hop rather than a dead end.
    """
    doc = registry.knowledge.document_store.get(item_id)
    if doc is not None:
        return "document", doc.get("content", "")

    node = registry.knowledge.graph_store.get_node(item_id)
    if node is not None:
        return "entity", format_entity_as_markdown(node)

    trace = registry.operational.trace_store.get(item_id)
    if trace is not None:
        return "trace", f"```json\n{trace.model_dump_json()}\n```"

    return None


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_items(
    item_ids: list[str],
    pack_id: str = "",
    max_tokens: int = 4000,
) -> str:
    """Fetch full bodies for known item ids, token-budgeted (#305).

    The fetch layer of progressive disclosure: survey an index pack
    (``get_context(index=True)`` / ``search(index=True)``), follow
    ``get_graph`` evidence pointers, then batch-fetch the ids worth
    reading. Ids resolve against the document store, the knowledge
    graph, and the trace store — a fetched entity includes its
    ``document_ids`` pointers so you can keep following evidence.

    Items that do not fit ``max_tokens`` are omitted whole and reported
    as omitted — never truncated — so re-fetch them with a larger
    ``max_tokens`` (the index line's ``~N tok`` is the cost to budget
    for); unknown ids are listed as not found. Every call is recorded as
    a
    ``PACK_ITEMS_FETCHED`` event carrying the served ids, so pass the
    ``pack_id`` of the pack that surfaced the ids — that keeps the fetch,
    and your later ``record_feedback(pack_id=..., helpful_item_ids=...)``,
    attributable to the serving pack.

    Args:
        item_ids: Item ids to fetch (max 50). Copy them verbatim from
            index lines, pack items, or graph evidence pointers.
        pack_id: The pack that surfaced these ids (strongly recommended
            after an index retrieval; empty when there is none).
        max_tokens: Maximum response size in tokens (default 4000).
    """
    if not item_ids:
        _raise_invalid_params(
            "item_ids must not be empty",
            data={"field": "item_ids"},
        )
    if len(item_ids) > _GET_ITEMS_MAX_IDS:
        _raise_invalid_params(
            f"too many item_ids: {len(item_ids)} (max {_GET_ITEMS_MAX_IDS})",
            data={"field": "item_ids", "count": len(item_ids)},
        )
    if any(not isinstance(i, str) or not i.strip() for i in item_ids):
        _raise_invalid_params(
            "item_ids entries must be non-empty strings",
            data={"field": "item_ids"},
        )

    registry = _get_registry()
    # Dedup while preserving order — a repeated id must not be charged
    # (or recorded as served) twice.
    unique_ids = list(dict.fromkeys(i.strip() for i in item_ids))

    try:
        fetched: list[dict[str, Any]] = []
        not_found: list[str] = []
        for item_id in unique_ids:
            resolved = _render_fetched_item(registry, item_id)
            if resolved is None:
                not_found.append(item_id)
            else:
                kind, body = resolved
                fetched.append({"item_id": item_id, "kind": kind, "body": body})

        result, served_ids, omitted_ids = format_fetched_items_as_markdown(
            fetched,
            not_found=not_found,
            max_tokens=max_tokens,
            pack_id=pack_id.strip() or None,
        )

        # The serve record is the point of the tool (#305): a fetch that
        # cannot be recorded is invisible to attribution, so this emit is
        # loud — same posture as PackBuilder's PACK_ASSEMBLED emit on the
        # serving side. Response-token metering below stays fail-soft.
        registry.operational.event_log.emit(
            EventType.PACK_ITEMS_FETCHED,
            source="mcp:get_items",
            entity_id=pack_id.strip() or None,
            entity_type="pack" if pack_id.strip() else None,
            payload={
                "pack_id": pack_id.strip() or None,
                "requested_item_ids": unique_ids,
                "served_item_ids": served_ids,
                "not_found_item_ids": not_found,
                "omitted_item_ids": omitted_ids,
                "response_tokens": estimate_tokens(result),
                "budget_tokens": max_tokens,
            },
        )
    except McpError:
        raise
    except Exception as exc:
        logger.exception("get_items_failed")
        _raise_internal(
            f"failed to fetch items: {exc}",
            cause=exc,
            data={"tool": "get_items", "item_ids": unique_ids},
        )

    _track_tokens(registry, operation="get_items", result=result, budget=max_tokens)
    return result


# ---------------------------------------------------------------------------
# get_file_context — file-scoped retrieval (#307, server-side half)
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def get_file_context(
    paths: list[str],
    include_unconfirmed: bool = False,
    max_tokens: int = 2000,
) -> str:
    """Get stored context about specific files before reading or editing them.

    For each path, returns documents whose ``source_path`` names that file
    (exact match, or a ``/``-boundary suffix match so absolute paths find
    stored relpaths) plus graph entities doc-linked to those documents.
    Every item carries its store timestamps and each path a ``Newest
    memory`` line, so a caller can staleness-gate: if the file's mtime is
    newer than the newest memory, the context is about an older version
    of the file.

    Args:
        paths: File paths to look up (relative or absolute).
        include_unconfirmed: Also surface unconfirmed extraction mints
            (excluded by default — extraction attests mention, not fact).
        max_tokens: Maximum response size in tokens (default 2000).
    """
    cleaned = [p.strip() for p in paths if p and p.strip()] if paths else []
    if not cleaned:
        _raise_invalid_params(
            "paths must contain at least one non-empty path",
            data={"field": "paths"},
        )

    registry = _get_registry()
    file_context = build_file_context(
        registry.knowledge.document_store,
        registry.knowledge.graph_store,
        cleaned,
        include_unconfirmed=include_unconfirmed,
    )
    result = format_file_context_as_markdown(file_context, max_tokens=max_tokens)
    try:
        track_token_usage(
            registry.operational.event_log,
            layer="mcp",
            operation="get_file_context",
            response_tokens=estimate_tokens(result),
            budget_tokens=max_tokens,
        )
    except Exception:
        # GRACEFUL-DEGRADATION: token tracking is post-success telemetry.
        logger.exception("token_tracking_failed", operation="get_file_context")
    return result


# ---------------------------------------------------------------------------
# Macro Tool 7: record_feedback
# ---------------------------------------------------------------------------


#: Most item ids handed back in an attribution rejection. A pack is
#: budget-capped well below this; the cap only bounds the error payload
#: against a pathological pack, it is not a normal truncation point.
_MAX_CITABLE_IDS_IN_ERROR = 60


def _require_pack_attribution(registry: StoreRegistry, pack_id: str) -> None:
    """Reject an uncited pack-targeted feedback call, when configured to.

    Off by default — see
    :data:`trellis.core.write_config.REQUIRE_PACK_ATTRIBUTION_FLAG` for
    why the default is the operator's call and not this module's.

    Fails **open** in every uncertain case. The rejection is only raised
    when the pack resolves to at least one item the caller could have
    cited: an unknown ``pack_id``, a pack that predates
    ``injected_item_ids``, a sectioned pack (which emits no per-item rows
    at all), or an event-log outage each let the call through. Refusing a
    caller for not citing ids nobody can produce would convert a recorded
    rating into a lost one and teach the agent that the tool is
    unreliable — the opposite of the intent.

    Nothing here writes attribution. The served ids ride the error so the
    caller can *choose* among them; which of them helped is a judgement
    only the caller holds, and synthesising it would manufacture exactly
    the signal this whole change exists to measure honestly.
    """
    if not WriteBehaviourConfig.from_env().require_pack_attribution:
        return

    item_ids = lookup_pack_item_ids(registry.operational.event_log, pack_id)
    if not item_ids:
        logger.debug("pack_attribution_not_enforceable", pack_id=pack_id)
        return

    _record_boundary_rejection(
        tool="record_feedback",
        rejections=[
            {
                "kind": "missing",
                "loc": "helpful_item_ids|unhelpful_item_ids",
                "msg": f"pack {pack_id} served {len(item_ids)} item(s), none cited",
            }
        ],
        hints=[
            "cite the item_ids that helped in helpful_item_ids",
            "a pack that missed is cited in unhelpful_item_ids, not left blank",
        ],
    )
    _raise_invalid_params(
        f"feedback on pack {pack_id} must cite at least one item — it served "
        f"{len(item_ids)} and uncited pack feedback joins to nothing. Put the "
        "ids that helped in helpful_item_ids; if none did, put the ones that "
        "were noise in unhelpful_item_ids.",
        data={
            "pack_id": pack_id,
            "item_ids": item_ids[:_MAX_CITABLE_IDS_IN_ERROR],
            "fields": [
                "helpful_item_ids",
                "unhelpful_item_ids",
                "followed_advisory_ids",
            ],
        },
    )


@mcp.tool(auth=trellis_scope(SCOPE_MUTATE))
def record_feedback(
    trace_id: str = "",
    pack_id: str = "",
    success: bool | None = None,
    rating: float | None = None,
    notes: str | None = None,
    helpful_item_ids: list[str] | None = None,
    unhelpful_item_ids: list[str] | None = None,
    followed_advisory_ids: list[str] | None = None,
) -> str:
    """Record outcome feedback on a trace or context pack.

    Supply ``pack_id`` (preferred) to attribute feedback to a context
    pack returned by one of the ``get_*_context`` tools. The pack_id is
    shown in the response header of each pack and can be copied verbatim.

    Grade the pack with ``rating`` (0.0 to 1.0) whenever you can: a pack that
    contained one useful item out of six is not the same signal as one
    that nailed the task, and only graded feedback gives the fitness loops
    the variance they need. ``success`` alone still works and is treated
    as ``rating=1.0`` / ``0.0``.

    When citing specific elements:

    * ``helpful_item_ids`` — item_ids (shown in backticks in the pack)
      that actually helped the task succeed.
    * ``unhelpful_item_ids`` — items that were noise or misleading.
    * ``followed_advisory_ids`` — advisory_ids (shown in backticks in the
      Advisories section) that you followed.

    Element-level signals are stored on the feedback event for the fitness
    loops (``trellis analyze apply-noise-tags`` and ``trellis analyze
    advisory-effectiveness``) to attribute outcomes more precisely.

    **Feedback naming a pack but citing no items joins to nothing.** The
    learning loop matches per-item rows from the pack against the ids you
    cite; a pack-level rating with no ids grades the delivery but teaches
    nothing about which memories earned their tokens. If the pack was a
    miss, that is not a reason to cite nothing — it is what
    ``unhelpful_item_ids`` is for, and it is the more valuable signal of
    the two. Deployments can require this (``TRELLIS_REQUIRE_PACK_ATTRIBUTION``);
    where they do, an uncited pack-targeted call is rejected and the
    rejection carries the ids the pack served so the retry is a choice
    among them.

    ``trace_id`` is still accepted for trace-level feedback when no pack
    is involved — grading work that no pack informed is a legitimate,
    honest signal, and it is *not* what the requirement above is about.
    It carries no attribution because there is none to carry, and the
    health report counts it separately for exactly that reason.

    Args:
        trace_id: Trace ID for trace-level feedback (optional).
        pack_id: Pack ID for pack-level feedback (optional but preferred
            when feedback follows a context retrieval).
        success: Whether the task succeeded. Omit when passing ``rating``
            — it is then derived from the grade. Defaults to success only
            when neither is given.
        rating: Graded usefulness of the pack, 0.0 (useless) to 1.0
            (everything served was on point).
        notes: Optional notes about what worked or didn't.
        helpful_item_ids: IDs of pack items that were actually useful.
        unhelpful_item_ids: IDs of pack items that were noise.
        followed_advisory_ids: IDs of advisories the agent followed.
    """
    has_trace = bool(trace_id and trace_id.strip())
    has_pack = bool(pack_id and pack_id.strip())
    if not has_trace and not has_pack:
        _record_boundary_rejection(
            tool="record_feedback",
            rejections=[
                {
                    "kind": "missing",
                    "loc": "trace_id|pack_id",
                    "msg": "neither target provided",
                }
            ],
        )
        _raise_invalid_params(
            "one of trace_id or pack_id must be provided",
            data={"fields": ["trace_id", "pack_id"]},
        )
    if rating is not None and not 0.0 <= rating <= 1.0:
        _record_boundary_rejection(
            tool="record_feedback",
            rejections=[
                {
                    "kind": "value",
                    "loc": "rating",
                    "msg": f"out of range: {rating}",
                }
            ],
        )
        _raise_invalid_params(
            "rating must be between 0.0 and 1.0",
            data={"field": "rating", "value": rating},
        )

    registry = _get_registry()
    stores_dir = registry.stores_dir
    if stores_dir is None:
        _raise_internal(
            "stores_dir is not configured; cannot record feedback",
            data={"setting": "stores_dir"},
        )

    if has_pack and not (
        helpful_item_ids or unhelpful_item_ids or followed_advisory_ids
    ):
        _require_pack_attribution(registry, pack_id.strip())

    # Shared with the REST pack-feedback route so the two agent-facing
    # surfaces agree on what identical inputs mean — including deriving
    # ``success`` from ``rating`` and leaving ``items_served`` empty
    # rather than fabricating it from the cited ids.
    feedback = PackFeedback.from_agent_signal(
        run_id=trace_id if has_trace else pack_id,
        success=success,
        rating=rating,
        helpful_item_ids=helpful_item_ids or (),
        unhelpful_item_ids=unhelpful_item_ids or (),
        followed_advisory_ids=followed_advisory_ids or (),
        pack_id=pack_id if has_pack else None,
        trace_id=trace_id if has_trace else None,
        notes=notes,
    )

    # One path writes both sinks: the durable pack_feedback.jsonl row and
    # the authoritative FEEDBACK_RECORDED event. The emit fails soft in
    # there, so a sink outage degrades to a file row that
    # ``trellis admin reconcile-feedback`` replays — it no longer raises
    # out of an agent-facing tool.
    result = record_pack_feedback(
        feedback,
        log_dir=feedback_log_dir(stores_dir),
        event_log=registry.operational.event_log,
        pack_id=pack_id if has_pack else None,
        source="mcp",
        entity_id=None if has_pack else trace_id,
        entity_type=None if has_pack else "trace",
    )

    target = f"pack: {pack_id}" if has_pack else f"trace: {trace_id}"
    status = "positive" if feedback.succeeded else "negative"
    graded = feedback.effective_rating
    message = f"Feedback recorded ({status}, rating {graded:.2f}) for {target}"
    if not result.event_log_in_sync:
        message += (
            " — event log unavailable, audit row kept;"
            " run `trellis admin reconcile-feedback` to replay it"
        )
    return message


# ---------------------------------------------------------------------------
# Macro Tool 8: search
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ))
def search(
    query: str,
    limit: int = 10,
    max_tokens: int = 2000,
    index: bool = False,
) -> str:
    """Search the experience graph for documents and entities.

    A targeted flat lookup over the same one retrieval path as
    ``get_context`` (keyword + graph + semantic fused with RRF), returning
    a token-budgeted markdown pack with a citable ``pack_id``.

    Args:
        query: Search query.
        limit: Maximum results (default 10).
        max_tokens: Maximum response size in tokens (default 2000).
        index: Return an id index instead of excerpt bodies (default
            False) — one compact line per item; raise ``limit`` to survey
            more. Fetch chosen bodies with ``get_items``; feedback
            attribution via the ``pack_id`` is unchanged. The index is a
            real pack, so a survey counts as a serve for session dedup —
            use ``get_context(..., refresh=True)`` if a later retrieval
            in the same session needs the surveyed items back.
    """
    if not query or not query.strip():
        _raise_invalid_params(
            "query must not be empty",
            data={"field": "query"},
        )

    return _flat_context(
        _get_registry(),
        query,
        domain=None,
        max_tokens=max_tokens,
        session_id="",
        max_items=limit,
        title=f"Search: {query}",
        empty_message=f"No results found for: {query}",
        operation="search",
        index=index,
    )


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

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context`` with a ``sections`` layout — this
        tool is a fixed two-section preset and will be removed.

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

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_objective_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    resolved_tokens = budget.max_tokens
    section_specs = [
        {
            "name": "Domain Knowledge",
            "retrieval_affinities": ["domain_knowledge"],
            "max_tokens": resolved_tokens // 2,
            "max_items": 10,
        },
        {
            "name": "Operational Context",
            "retrieval_affinities": ["operational"],
            "max_tokens": resolved_tokens // 3,
            "max_items": 8,
        },
    ]
    return _sectioned_context(
        registry,
        intent,
        section_specs=section_specs,
        resolved_tokens=resolved_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_objective_context",
    )


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

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context`` with a ``sections`` layout (anchor
        via each section's ``entity_ids``) — this fixed two-section preset
        will be removed.

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

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_task_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    resolved_tokens = budget.max_tokens
    section_specs = [
        {
            "name": "Technical Patterns",
            "retrieval_affinities": ["technical_pattern"],
            "max_tokens": resolved_tokens // 2,
            "max_items": 10,
        },
        {
            "name": "Reference Data",
            "retrieval_affinities": ["reference"],
            # Passed through as-is so an invalid ``entity_ids`` (e.g. a
            # non-list) surfaces as INTERNAL_ERROR from the shared validator.
            "entity_ids": entity_ids if entity_ids is not None else [],
            "max_tokens": resolved_tokens // 3,
            "max_items": 10,
        },
    ]
    return _sectioned_context(
        registry,
        intent,
        section_specs=section_specs,
        resolved_tokens=resolved_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_task_context",
    )


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

    .. deprecated::
        Retained as a thin alias over the one retrieval path (#262) for one
        release. Prefer ``get_context(intent, sections=[...])`` — the
        canonical parameterized tool with the identical ``sections`` schema.

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

    registry = _get_registry()
    budget = registry.budget_config.resolve(
        tool="get_sectioned_context",
        domain=domain or None,
        caller_override_tokens=max_tokens if max_tokens > 0 else None,
    )
    return _sectioned_context(
        registry,
        intent,
        section_specs=sections,
        resolved_tokens=budget.max_tokens,
        domain=domain,
        session_id=session_id,
        tool="get_sectioned_context",
    )


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
    # Seeding the fuzzy-dedup index is O(corpus) and blocking (~24 s for
    # 735 documents on the reference deployment). Building it here means
    # an http deployment pays that at boot instead of inside the first
    # caller's save_memory. It is off unless the operator set a bound —
    # see _get_minhash_index.
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
    # Which build, applying which write semantics. Emitted before the
    # transport branch so *both* paths get it — a stdio server is spawned
    # per session and gone before anyone can query it, so this line is its
    # only live record; the durable record is the same stamp on every
    # event it writes.
    logger.info("mcp_write_provenance", **get_write_provenance())
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
