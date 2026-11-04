"""Process-global wiring for the MCP server.

The cached ``StoreRegistry``, the LLM client, the MinHash dedup index and the memory
extractor, with the module state each one keeps. A test that swaps one of these
globals patches it here, because the functions that read it live here.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog

from trellis.core.project import project_override, resolve_project
from trellis.core.write_config import MINHASH_SEED_MAX_DOCS_ENV, resolve_write_behaviour
from trellis.extract.entity_resolution import build_name_alias_resolver
from trellis.llm.routing import LLMConsumer, LLMRoutingError
from trellis.mcp.auth import TRANSPORT_HTTP, resolve_transport
from trellis.mcp.boundary import _exception_detail, _raise_internal
from trellis.ops.memory_extraction_health import (
    REASON_BUILD_FAILED,
    REASON_NO_LLM_CLIENT,
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    emit_memory_extraction_state,
)
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.pack_builder import PackBuilder
from trellis.schemas.trace import Trace
from trellis.stores.base.document import DocumentStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


_registry: StoreRegistry | None = None


def _get_registry() -> StoreRegistry:
    """Get or create a cached StoreRegistry singleton."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = StoreRegistry.from_config_dir()
    return _registry


def _session_project() -> str | None:
    """Return the project this server's packs and traces are stamped with.

    stdio: :func:`~trellis.core.project.resolve_project`.  http: the
    ``TRELLIS_PROJECT`` override only, because one server answers every
    caller and its own working directory names none of their projects.
    """
    try:
        if resolve_transport() == TRANSPORT_HTTP:
            return project_override()
        return resolve_project()
    except Exception:  # advisory; never fail a pack or a write
        return None


def _stamp_trace_project(trace: Trace) -> Trace:
    """Return ``trace`` with ``metadata["project"]`` set by this server.

    The server's value always owns ``project``, even when it is ``None``:
    a stamp the agent could overwrite would not attribute anything.  An
    agent-supplied value that disagrees is kept as ``project_unverified``,
    the ``<field>_unverified`` idiom of :mod:`trellis.extract.evidence`.
    """
    project = _session_project()
    metadata = dict(trace.metadata)
    supplied = metadata.get("project")
    if supplied not in (None, project):
        metadata["project_unverified"] = supplied
    metadata["project"] = project
    return trace.model_copy(update={"metadata": metadata})


def _build_pack_builder(registry: StoreRegistry) -> PackBuilder:
    """Create a PackBuilder wired to this deployment's advisory store.

    The wiring itself lives in
    :func:`~trellis.retrieve.builder_factory.build_pack_builder` (#410) —
    one construction for every pack surface, because four copies of an
    argument list is four chances to drift, and one of them already had.
    Only this surface passes a ``project``: the REST and CLI processes'
    working directories name no caller's project.
    """
    return build_pack_builder(registry, surface="mcp", project=_session_project())


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
#: Monotonic time before which a failed client resolution is not retried;
#: ``None`` means the outcome is settled. Read only while
#: ``_memory_extractor_attempted`` is set, so a test that resets that flag
#: needs nothing else reset for the cache.
_memory_extractor_retry_at: float | None = None
#: Last ``(state, reason)`` written to the event log, the time it was written
#: (monotonic), and the lock that serializes a resolution. A process-local
#: view: another process keeps its own. Keyed on the pair, not the state
#: alone, so a ``build_failed`` is not hidden behind an earlier
#: ``no_llm_client`` row (both read ``unavailable``).
_memory_extractor_reported: tuple[str, str | None] | None = None
_memory_extractor_reported_at: float = 0.0
_memory_extractor_lock = threading.Lock()

#: A failed client resolution is retried at most this often (#987). One
#: resolution at startup can fail for a transient reason, and pinning it
#: for the process lifetime turned extraction off until a restart. Bounded
#: so the write path does not build an LLM client on every save: a
#: ``save_memory`` inside the interval reads one float and returns.
_MEMORY_EXTRACTOR_RETRY_SECONDS = 60.0
#: A state that persists is written to the event log again after this long,
#: so an ``unavailable`` older than ``trellis analyze health``'s window does
#: not drop out of the report while the process is still broken. The refresh
#: rides a save that arrives after the retry interval, not a timer: a process
#: that stops receiving saves stops refreshing.
_MEMORY_EXTRACTOR_REFRESH_SECONDS = 86400.0


def _report_memory_extractor_state(
    registry: StoreRegistry,
    state: str,
    *,
    reason: str | None = None,
    error_type: str | None = None,
) -> None:
    """Write a ``MEMORY_EXTRACTION_STATE`` row on a state change (#987).

    Called on every resolution, written only when the ``(state, reason)``
    pair differs from the last one this process wrote or a persistent
    ``unavailable`` of either reason is due its daily refresh. Advisory and
    wrapped whole: it runs on the ``save_memory`` write path, where one
    escaped exception fails the write.
    """
    global _memory_extractor_reported, _memory_extractor_reported_at  # noqa: PLW0603
    try:
        now = time.monotonic()
        key = (state, reason)
        if (
            key == _memory_extractor_reported
            and now - _memory_extractor_reported_at < _MEMORY_EXTRACTOR_REFRESH_SECONDS
        ):
            return
        if state == STATE_UNAVAILABLE and key != _memory_extractor_reported:
            logger.warning("memory_extractor_unavailable", reason=reason)
        if emit_memory_extraction_state(
            registry.operational.event_log,
            state=state,
            reason=reason,
            error_type=error_type,
        ):
            _memory_extractor_reported = key
            _memory_extractor_reported_at = now
    except Exception:
        # GRACEFUL-DEGRADATION: the signal is lost, the save is not.
        logger.warning("memory_extractor_state_report_failed", exc_info=True)


def _get_memory_extractor(registry: StoreRegistry) -> Any:
    """Build or fetch the cached save_memory extractor.

    Returns ``None`` when:
      * ``TRELLIS_ENABLE_MEMORY_EXTRACTION`` is not set truthy, OR
      * No LLM client can be constructed from the environment, OR
      * Constructing the client or the extractor raised.

    The first is settled for the process. The other two are not (#987): each
    is recorded as a ``MEMORY_EXTRACTION_STATE`` event (``no_llm_client`` /
    ``build_failed``) and resolution is retried by a later call once
    ``_MEMORY_EXTRACTOR_RETRY_SECONDS`` have passed, while the flag stays on.

    **Never raises.** This runs inside ``save_memory`` after the document is
    stored, so an exception here would fail a save that already persisted,
    skip embed-on-ingest, and (before #987) leave extraction off for good. A
    construction failure is a defect rather than a transient outage, but
    raising it at a save is the wrong place to surface it: it is logged with
    its traceback on every attempt and shows up in ``trellis analyze
    health`` through the event, and the next interval tries again, so a
    fixed resolver or a restored dependency recovers without a restart.
    """
    global _memory_extractor, _memory_extractor_attempted  # noqa: PLW0603
    global _memory_extractor_retry_at  # noqa: PLW0603
    if not _memory_extractor_resolution_due():
        return _memory_extractor
    with _memory_extractor_lock:
        # Another thread may have resolved while this one waited.
        if not _memory_extractor_resolution_due():
            return _memory_extractor
        _memory_extractor_attempted = True
        _memory_extractor_retry_at = None

        from trellis.extract.memory_ingest_hook import (  # noqa: PLC0415
            memory_extraction_env_enabled,
        )

        if not memory_extraction_env_enabled(registry):
            return None

        try:
            from trellis.extract.save_memory import (  # noqa: PLC0415
                build_save_memory_extractor,
            )

            llm_client = _build_llm_client(registry, LLMConsumer.MEMORY_EXTRACTION)
            if llm_client is None:
                logger.info("memory_extractor_skipped_no_llm_client")
                _memory_extractor_retry_at = (
                    time.monotonic() + _MEMORY_EXTRACTOR_RETRY_SECONDS
                )
                _report_memory_extractor_state(
                    registry, STATE_UNAVAILABLE, reason=REASON_NO_LLM_CLIENT
                )
                return None

            alias_resolver = _build_alias_resolver(registry)
            _memory_extractor = build_save_memory_extractor(
                alias_resolver=alias_resolver,
                llm_client=llm_client,
            )
        except Exception as exc:
            # GRACEFUL-DEGRADATION: wrapped whole, McpError included (an
            # LLM-provider construction failure arrives as one). The save
            # that triggered this has already stored its document.
            logger.exception("memory_extractor_init_failed")
            _memory_extractor_retry_at = (
                time.monotonic() + _MEMORY_EXTRACTOR_RETRY_SECONDS
            )
            _report_memory_extractor_state(
                registry,
                STATE_UNAVAILABLE,
                reason=REASON_BUILD_FAILED,
                error_type=type(exc).__name__,
            )
            return None
        logger.info("memory_extractor_enabled")
        _report_memory_extractor_state(registry, STATE_AVAILABLE)
        return _memory_extractor


def _memory_extractor_resolution_due() -> bool:
    """``True`` when the cache has no settled answer, or a retry has come due."""
    if not _memory_extractor_attempted:
        return True
    retry_at = _memory_extractor_retry_at
    return retry_at is not None and time.monotonic() >= retry_at


def _build_llm_client(registry: StoreRegistry, consumer: LLMConsumer) -> Any:
    """Construct ``consumer``'s LLMClient, preferring the registry config over env vars.

    First tries ``registry.build_llm_client(consumer=...)`` (driven by the
    ``llm:`` block in ``~/.trellis/config.yaml``). If that returns ``None`` —
    either because no config is present or the configured provider couldn't
    be instantiated — falls back to the env-var path in
    :func:`_build_llm_client_from_env`. Returns ``None`` when neither
    source yields a client.

    The env-var fallback is skipped when ``llm.routes`` pins ``consumer`` to
    a tier, when the route is declared ``local`` and when ``llm.tiers`` /
    ``llm.routes`` is malformed: in each case the operator chose a model for
    this consumer, and ``OPENAI_API_KEY`` would silently run it on a
    different one, a hosted one for content cleared to go only to a local one.
    """
    try:
        route = registry.llm_route(consumer)
    except LLMRoutingError as exc:
        logger.error(  # noqa: TRY400 — a config defect; the setting is the fix
            "llm_routing_invalid",
            consumer=consumer.value,
            setting=exc.setting,
            error=str(exc),
        )
        return None
    try:
        client = registry.build_llm_client(consumer=consumer)
    except Exception:
        # GRACEFUL-DEGRADATION: registry config is the preferred source
        # but the env-var path below is an explicit, documented fallback
        # for deployments without a populated ``llm:`` block. Logged at
        # exception level so config drift is visible in stderr.
        logger.exception("llm_client_registry_failed", consumer=consumer.value)
        client = None
    if client is not None:
        logger.debug(
            "llm_client_from_registry", consumer=consumer.value, tier=route.tier
        )
        return client
    if route.routed or route.local:
        logger.warning(
            "llm_client_routed_tier_unbuildable",
            consumer=consumer.value,
            tier=route.tier,
        )
        return None

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
            detail = _exception_detail(exc)
            _raise_internal(
                f"OpenAI client construction failed: {detail}",
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
            detail = _exception_detail(exc)
            _raise_internal(
                f"Anthropic client construction failed: {detail}",
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
        max_docs = resolve_write_behaviour(registry).minhash_seed_max_docs
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
        detail = _exception_detail(exc)
        _raise_internal(
            f"MinHash dedup index initialisation failed: {detail}",
            cause=exc,
            data={"stage": "minhash_index_init"},
        )
    _minhash_index = index
    return _minhash_index


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
