"""MCP Macro Tools server — high-level, token-efficient tools for AI agents.

Error contract
--------------
Tool handlers raise :class:`mcp.shared.exceptions.MCPError` rather than
returning ``"Error: …"`` strings or dict-shape error payloads. The
FastMCP runtime forwards ``MCPError`` directly through the JSON-RPC
transport so clients see a structured error object with a stable
``code`` plus a human-readable ``message``. This is the loud-failure
contract from the silent-fallback cleanup track (C2 Phase 3):

* ``INVALID_PARAMS`` (-32602) — pre-flight argument validation
  (empty intent, unknown operation enum, missing required key, etc.).
* ``RESOURCE_NOT_FOUND`` (-32001, app-layer) — handler asked for an
  entity that doesn't exist (e.g. ``get_graph`` with an unknown id).
* ``POLICY_DENIED`` (-32002, app-layer) — the caller's scope does not
  cover what the call asked for (``execute_mutation`` naming a
  destructive operation without the ``admin`` scope). Distinct from
  ``MUTATION_FAILED``: nothing reached the executor.
* ``MUTATION_FAILED`` (-32003, app-layer) — a mutation went through
  the executor and came back non-success (REJECTED / FAILED). The
  ``data`` field carries the structured executor response.
* ``INTERNAL_ERROR`` (-32603) — unexpected failure inside a tool
  (store outage, pack builder crash, etc.). The original exception
  chains via ``from`` so server-side logs preserve the traceback.

Pre-flight validation returns no longer use string sentinels; callers
that previously did ``if result.startswith("Error:")`` need to catch
``MCPError`` instead. The contract is documented in
``docs/design/adr-mcp-contract.md`` and the per-site audit lives in
``audit/silent_fallbacks_2026-05.md``.

Layout
------
``server.py`` is the entry point (:func:`main`) and a façade: every name the
old single module exposed is still importable from it. The code lives in
submodules, and **a name a test patches must be patched on the submodule that
reads it, not on this façade**:

* ``boundary`` -- the error codes and ``_raise_*`` helpers
* ``rejections`` -- boundary rejection recording and the two middleware
* ``app`` -- the FastMCP instance and its middleware chain
* ``wiring`` -- the cached registry, LLM client, MinHash index and memory
  extractor, with the module state each keeps
* ``tools/`` -- one module per tool family; ``tools/__init__.py`` imports them
  in registration order, which is the order FastMCP lists the tools in

Two groups of names are deliberately not re-exported. Module state a function
rebinds (``_registry``, ``_minhash_index`` and the ``_memory_extractor*``
flags) would freeze at its import-time value here, so it is read and patched on
``wiring`` only. ``_build_llm_client`` and ``_build_llm_client_from_env`` are
left off because ``test_llm_consumer_rule`` reads a string equal to a
builder's name as an escaped reference, which an ``__all__`` entry would be.
"""

from __future__ import annotations

import sys
from typing import Any

import structlog

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.write_provenance import get_write_provenance
from trellis.errors import TrellisError
from trellis.logging import configure_stderr_logging
from trellis.mcp.app import mcp
from trellis.mcp.auth import (
    TRANSPORT_HTTP,
    HttpSettings,
    TrellisApiKeyVerifier,
    current_clearance,
    resolve_http_settings,
    resolve_transport,
    set_auth_enforced,
)
from trellis.mcp.boundary import (
    _SANITIZED_RESULT_STATUSES,
    MUTATION_FAILED,
    POLICY_DENIED,
    PUBLIC_READ_BUSY,
    RESOURCE_NOT_FOUND,
    _check_retrieval_phase,
    _exception_detail,
    _raise_internal,
    _raise_invalid_params,
    _raise_mutation_failed,
    _raise_not_found,
    _raise_policy_denied,
    _refuse_public_caller,
    _result_message,
)
from trellis.mcp.rejections import (
    _RECORDED_ARGUMENT_TOOLS,
    _REJECTION_RECOVERY,
    _ExplainArgumentRejections,
    _note_accepted_call,
    _note_noop_call,
    _record_boundary_rejection,
    _SanitizeUncaughtToolErrors,
)
from trellis.mcp.tools.admin import (
    _LIST_API_KEYS_FAULT,
    _REVOKE_API_KEY_FAULT,
    list_api_keys,
    revoke_api_key,
)
from trellis.mcp.tools.context import (
    _FLAT_MAX_ITEMS,
    _GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS,
    _MIN_INDEX_BUDGET_TOKENS,
    _TOOL_LABEL,
    _axis_note,
    _capture_warning_banner,
    _context_banner,
    _flat_context,
    _sectioned_context,
    _track_tokens,
    get_context,
)
from trellis.mcp.tools.experience_knowledge import (
    _apply_knowledge_supersedes,
    _plan_knowledge_supersedes,
    _raise_create_failed,
    _raise_if_supersede_failed,
    _resolve_evidence_pointer,
    save_experience,
    save_knowledge,
)
from trellis.mcp.tools.feedback import (
    _MAX_CITABLE_IDS_IN_ERROR,
    _MAX_CITABLE_IDS_IN_MESSAGE,
    _MAX_ECHOED_ID_CHARS,
    _MAX_UNSERVED_IDS_IN_NOTE,
    _UNKNOWN_PACK_NOTE,
    _echo_id,
    _FailureNotingStore,
    _id_list_clause,
    _readable_ids_if_checked,
    _readable_served_ids,
    _require_bodied_attribution,
    _require_pack_attribution,
    _unserved_citation_note,
    record_feedback,
)
from trellis.mcp.tools.legacy_context import (
    get_objective_context,
    get_sectioned_context,
    get_task_context,
    search,
)
from trellis.mcp.tools.lookup import (
    _GET_ITEMS_MAX_IDS,
    _render_fetched_item,
    get_file_context,
    get_graph,
    get_items,
    get_lessons,
)
from trellis.mcp.tools.memory import (
    SAVE_MEMORY_SURFACE,
    _candidate_is_stale,
    _check_memory_supersedes,
    _commit_reconcile_verdict,
    _compute_reconcile_outcome,
    _downgraded_unapplied,
    _emit_memory_stored_and_enrich,
    _fuzzy_duplicate_response,
    _gather_reconcile_candidate,
    _index_stored_memory,
    _memory_exists_response,
    _reconcile_egress_permits,
    _reconcile_result_message,
    _reconcile_subject,
    _refuse_near_duplicate_supersedes,
    _reverify_candidate,
    _save_memory_lock,
    _save_memory_reconciled,
    _store_new_memory,
    _supersede_saved_memory,
    save_memory,
)
from trellis.mcp.tools.memory_common import (
    _append_note,
    _match_readable,
    _readable_document_exists,
    _refuse_label_below_request,
    _refuse_memory_pointer_keys,
    _stamp_sensitivity,
)
from trellis.mcp.tools.mutation import (
    _refuse_evidence_pointer_keys,
    _resolve_execute_mutation_target,
    _resolve_operation,
    execute_mutation,
)
from trellis.mcp.tools.observation import query_observations, record_observation
from trellis.mcp.wiring import (
    _MEMORY_EXTRACTOR_REFRESH_SECONDS,
    _MEMORY_EXTRACTOR_RETRY_SECONDS,
    _MINHASH_SEED_PAGE_SIZE,
    _build_alias_resolver,
    _build_pack_builder,
    _close_registry,
    _get_memory_extractor,
    _get_minhash_index,
    _get_registry,
    _memory_extractor_lock,
    _memory_extractor_resolution_due,
    _report_memory_extractor_state,
    _seed_minhash_index,
    _session_project,
    _stamp_trace_project,
    _warn_index_holds_nothing,
)
from trellis.stores.registry import StoreRegistry

__all__ = [
    "MUTATION_FAILED",
    "POLICY_DENIED",
    "PUBLIC_READ_BUSY",
    "RESOURCE_NOT_FOUND",
    "SAVE_MEMORY_SURFACE",
    "_ExplainArgumentRejections",
    "_FLAT_MAX_ITEMS",
    "_FailureNotingStore",
    "_GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS",
    "_GET_ITEMS_MAX_IDS",
    "_LIST_API_KEYS_FAULT",
    "_MAX_CITABLE_IDS_IN_ERROR",
    "_MAX_CITABLE_IDS_IN_MESSAGE",
    "_MAX_ECHOED_ID_CHARS",
    "_MAX_UNSERVED_IDS_IN_NOTE",
    "_MEMORY_EXTRACTOR_REFRESH_SECONDS",
    "_MEMORY_EXTRACTOR_RETRY_SECONDS",
    "_MINHASH_SEED_PAGE_SIZE",
    "_MIN_INDEX_BUDGET_TOKENS",
    "_RECORDED_ARGUMENT_TOOLS",
    "_REJECTION_RECOVERY",
    "_REVOKE_API_KEY_FAULT",
    "_SANITIZED_RESULT_STATUSES",
    "_SanitizeUncaughtToolErrors",
    "_TOOL_LABEL",
    "_UNKNOWN_PACK_NOTE",
    "_append_note",
    "_apply_knowledge_supersedes",
    "_axis_note",
    "_build_alias_resolver",
    "_build_pack_builder",
    "_candidate_is_stale",
    "_capture_warning_banner",
    "_check_memory_supersedes",
    "_check_retrieval_phase",
    "_close_registry",
    "_commit_reconcile_verdict",
    "_compute_reconcile_outcome",
    "_context_banner",
    "_downgraded_unapplied",
    "_echo_id",
    "_emit_memory_stored_and_enrich",
    "_exception_detail",
    "_flat_context",
    "_fuzzy_duplicate_response",
    "_gather_reconcile_candidate",
    "_get_memory_extractor",
    "_get_minhash_index",
    "_get_registry",
    "_id_list_clause",
    "_index_stored_memory",
    "_match_readable",
    "_memory_exists_response",
    "_memory_extractor_lock",
    "_memory_extractor_resolution_due",
    "_note_accepted_call",
    "_note_noop_call",
    "_plan_knowledge_supersedes",
    "_raise_create_failed",
    "_raise_if_supersede_failed",
    "_raise_internal",
    "_raise_invalid_params",
    "_raise_mutation_failed",
    "_raise_not_found",
    "_raise_policy_denied",
    "_readable_document_exists",
    "_readable_ids_if_checked",
    "_readable_served_ids",
    "_reconcile_egress_permits",
    "_reconcile_result_message",
    "_reconcile_subject",
    "_record_boundary_rejection",
    "_refuse_evidence_pointer_keys",
    "_refuse_label_below_request",
    "_refuse_memory_pointer_keys",
    "_refuse_near_duplicate_supersedes",
    "_refuse_public_caller",
    "_render_fetched_item",
    "_report_memory_extractor_state",
    "_require_bodied_attribution",
    "_require_pack_attribution",
    "_resolve_evidence_pointer",
    "_resolve_execute_mutation_target",
    "_resolve_operation",
    "_result_message",
    "_reverify_candidate",
    "_save_memory_lock",
    "_save_memory_reconciled",
    "_sectioned_context",
    "_seed_minhash_index",
    "_session_project",
    "_stamp_sensitivity",
    "_stamp_trace_project",
    "_store_new_memory",
    "_supersede_saved_memory",
    "_track_tokens",
    "_unserved_citation_note",
    "_warn_index_holds_nothing",
    "current_clearance",
    "execute_mutation",
    "get_context",
    "get_file_context",
    "get_graph",
    "get_items",
    "get_lessons",
    "get_objective_context",
    "get_sectioned_context",
    "get_task_context",
    "list_api_keys",
    "main",
    "mcp",
    "query_observations",
    "record_feedback",
    "record_observation",
    "revoke_api_key",
    "save_experience",
    "save_knowledge",
    "save_memory",
    "search",
]

logger = structlog.get_logger(__name__)


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
    handle it at call time (semantic search → keyword/graph, an embedder
    that failed to resolve degrades to keyword + graph with
    `semantic: embedder_failed` in the axes block, embed-on-ingest is
    fail-soft, memory extraction records its own failure as an event
    and retries). Forcing them to succeed would make each one a hard
    http boot dependency the stdio path never had.
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
        except Exception as exc:
            # GRACEFUL-DEGRADATION: log the component and the error type,
            # not a traceback. This is a startup signal to act on, not
            # evidence every component degrades quietly once the server is
            # serving traffic — the three fail differently at call time:
            #   - embedding_fn: build_strategies (retrieve/strategies.py)
            #     catches the resolve failure too, and degrades to
            #     keyword + graph with `semantic: embedder_failed` in the
            #     pack's axes block — it does not raise either.
            #   - vector_store: build_strategies (retrieve/strategies.py)
            #     catches the init failure and degrades retrieval to
            #     keyword + graph, with `semantic: misconfigured` in the
            #     pack's axes block — it does not raise.
            #   - memory_extractor: never reaches this branch, because
            #     _get_memory_extractor never raises (#987). A failed
            #     resolution is a MEMORY_EXTRACTION_STATE event, retried
            #     by a later save_memory.
            # run_embed_on_ingest is fail-soft for either resolve failure:
            # an embedder or a vector_store that raises here logs
            # embed_on_ingest_resolve_failed once per (component, cause)
            # per process and no-ops, rather than failing the ingest.
            logger.warning(
                "mcp_prewarm_optional_unavailable",
                component=label,
                error_type=type(exc).__name__,
            )

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


#: ``trellis_cli.exit_codes.EXIT_STORE``, the code the CLI gives a typed
#: configuration or store error. Written out because core does not import the
#: CLI; ``tests/unit/mcp/test_main_boundary.py`` pins the two equal.
_EXIT_TRELLIS_ERROR = 5


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
    try:
        transport = resolve_transport()
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
    except TrellisError as exc:
        # A bad ``TRELLIS_MCP_TRANSPORT``, an unusable http setting or a
        # store that will not open is an operator error whose message names
        # its own fix; a traceback buries it. One stderr line (stdout is the
        # stdio protocol's) and the code the CLI gives this class. An untyped
        # exception keeps its traceback.
        sys.stderr.write(
            f"trellis-mcp: {exc.code} — {sanitize_error_message(exc.message)}\n"
        )
        raise SystemExit(_EXIT_TRELLIS_ERROR) from None
    finally:
        _close_registry()
