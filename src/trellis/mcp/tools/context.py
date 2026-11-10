"""Context assembly: ``get_context`` and the pack helpers it shares.

``search`` and the legacy context trio import ``_flat_context`` and
``_sectioned_context`` from here.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.shared.exceptions import MCPError

from trellis.auth import SCOPE_READ
from trellis.core.access_log import note_printed_items, note_served_items
from trellis.core.knobs import (
    GET_CONTEXT_MAX_ITEMS_DEFAULT,
    resolve_get_context_max_items,
    resolve_get_context_max_tokens,
)
from trellis.errors import PublicReadBusyError
from trellis.mcp.app import PACK_TOOL_TAG, mcp
from trellis.mcp.auth import current_clearance, current_principal_id, trellis_scope
from trellis.mcp.boundary import (
    _check_retrieval_phase,
    _exception_detail,
    _raise_internal,
    _raise_invalid_params,
)
from trellis.mcp.grading_trailer import with_grading_trailer
from trellis.mcp.wiring import _build_pack_builder, _get_registry
from trellis.ops import (
    check_capture_health,
    format_capture_warning,
    resolve_capture_thresholds,
)
from trellis.retrieve.builder_factory import (
    AxisReport,
    describe_axes,
    format_embedder_failed_note,
    format_failed_axes_note,
    format_misconfigured_semantic_note,
)
from trellis.retrieve.formatters import (
    format_pack_as_index_markdown,
    format_pack_as_markdown,
    format_sectioned_pack_as_markdown,
    index_render_overhead_tokens,
)
from trellis.retrieve.sensitivity import is_public_only
from trellis.retrieve.token_tracker import estimate_tokens, track_token_usage
from trellis.retrieve.withholding import withholding_from_payload
from trellis.schemas.pack import PackBudget, SectionRequest
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import load_settings_values

logger = structlog.get_logger(__name__)


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
#: Also ``get_context``'s ``get_context_max_items`` default, so it is owned by
#: :mod:`trellis.core.knobs`, which holds the three MCP pack-size resolvers
#: (``resolve_get_context_max_tokens`` / ``resolve_get_context_max_items`` /
#: ``resolve_search_max_tokens``) -- env wins over a stored override, which
#: wins over the default, and the ``sections=`` layout is deliberately NOT
#: wired to them (see the ``sections is not None`` branch below).
_FLAT_MAX_ITEMS = GET_CONTEXT_MAX_ITEMS_DEFAULT

#: ``get_context``'s historical literal ``max_tokens`` default (2000),
#: frozen here for the ``sections=`` branch alone. That branch treats
#: ``max_tokens <= 0`` as "no override, resolve via ``budget_config``" —
#: so before the flat-path knob existed, a caller who omitted ``max_tokens``
#: (landing on the old hardcoded signature default) always fed the
#: literal ``2000`` into that branch as an override. Kept as its own
#: constant rather than reusing the flat-path knob's default so a settings
#: override of that knob can never change what the sections branch does.
_GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS = 2000


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
        settings = load_settings_values(registry.stores_dir)
        threshold, window_hours = resolve_capture_thresholds(settings)
        warning = check_capture_health(
            registry.operational.event_log,
            threshold=threshold,
            window_hours=window_hours,
        )
        return format_capture_warning(warning) if warning is not None else ""
    except Exception:
        # GRACEFUL-DEGRADATION: the health check is advisory read-side
        # telemetry; its failure must never block or corrupt a
        # successfully assembled pack.
        logger.exception("capture_health_check_failed")
        return ""


def _axis_note(axes: AxisReport) -> str:
    """Every markdown axis note ``axes`` calls for, joined.

    Shared by ``_flat_context`` and ``_sectioned_context``. A failed axis
    (:func:`format_failed_axes_note`), a misconfigured semantic axis
    (:func:`format_misconfigured_semantic_note`) and an embedder that
    failed to resolve (:func:`format_embedder_failed_note`, Q5-A)
    are independent states — ``describe_axes`` can report any of them,
    or several together, for the same build — so each renders when
    present rather than one shadowing another.
    """
    notes = [
        format_failed_axes_note(axes["failed"]),
        format_misconfigured_semantic_note(axes["semantic"]),
        format_embedder_failed_note(
            axes["embedder_error_type"], axes["embedder_setting"]
        ),
    ]
    return "\n\n".join(note for note in notes if note)


def _context_banner(registry: StoreRegistry) -> str:
    """The capture-health banner for a pack reply; empty for a public-only caller.

    The banner is operational health text about the store's capture surfaces,
    not a document, so a public-only caller (an ``external`` key) is not shown
    it. Every other caller gets :func:`_capture_warning_banner` unchanged.
    """
    if is_public_only(current_clearance()):
        return ""
    return _capture_warning_banner(registry)


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
    retrieval_phase: str | None = None,
) -> str:
    """Assemble a flat pack through the one PackBuilder-backed path.

    Shared by ``get_context`` and ``search``. Domain default-pass scoping,
    session dedup (via ``PackBuilder._recently_served``), RRF fusion,
    recency/importance decay and the rich ``PACK_ASSEMBLED`` emission
    (with ``pack_id``) all come from :class:`PackBuilder`.

    ``refresh=True`` bypasses session dedup for this call (client
    compaction signal), forwarded to :meth:`PackBuilder.build`.

    ``retrieval_phase`` (optional) is forwarded to :meth:`PackBuilder.build`
    unchanged — validated by the caller before this helper runs.

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
    _check_retrieval_phase(retrieval_phase)
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
            clearance=current_clearance(),
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
            retrieval_phase=retrieval_phase,
            principal_id=current_principal_id(),
        )
    except MCPError:
        raise
    except PublicReadBusyError:
        # A refused public read is not an assembly failure: it travels as
        # itself so ``_SanitizeUncaughtToolErrors`` can give it its own code.
        raise
    except Exception as exc:
        logger.exception("flat_context_failed", operation=operation)
        detail = _exception_detail(exc)
        _raise_internal(
            f"failed to assemble {_TOOL_LABEL.get(operation, 'context')} "
            f"for intent={intent!r}: {detail}",
            cause=exc,
            data={"tool": operation, "intent": intent},
        )

    banner = _context_banner(registry)
    # #404: read the summary the builder stamped, do not re-derive one.
    withholding = withholding_from_payload(pack.metadata.get("withholding"))
    # A missing axis is reported in the pack's axes block on the REST
    # surface (#775); this is the markdown equivalent — an agent reading
    # the reply never sees a JSON axes block, so the gap is stated in the
    # one place it will read it. Names only, never the exception text.
    # Both kwargs come from the builder (set once, inside the try above,
    # from build_strategies' own resolve attempt) rather than a second
    # ``registry.embedding_fn`` read, which re-raises on every access when
    # misconfigured and would turn this line into the same uncaught
    # failure the try above exists to avoid (Q5-A).
    axes = describe_axes(
        builder,
        pack.retrieval_report.strategies_used,
        embedder_configured=builder.embedder_configured,
        embedder_resolve_failure=builder.embedder_resolve_failure,
        graph_axis=pack.retrieval_report.graph_axis,
        public_only=is_public_only(current_clearance()),
    )
    axis_note = _axis_note(axes)

    if not pack.items:
        # The case #404 was filed about. An empty pack whose candidates
        # were *all* removed by a gate reads as greenfield — identical
        # text to a corpus that genuinely held nothing — and that is the
        # single most misleading pack this server can return. The note
        # goes here first, before it goes anywhere else.
        empty = empty_message or f"No context found for: {intent}"
        # Advisory selection (PackBuilder._select_advisories) does not
        # depend on whether any items were selected, so an empty pack can
        # still carry advisories — except while the pack holdout is on
        # (``holdout_rate > 0``), where ``PackBuilder`` blinds an
        # item-less pack's advisories too
        # (``_blind_advisories_for_empty_pack``, R1/#844), so a withheld
        # pack (zeroed via ``_withhold_flat``) and a genuinely empty one
        # carry the same nothing and stay look-alike. Render whatever
        # ``pack.advisories`` holds here too — matching
        # ``_sectioned_context``, which never special-cases empty sections
        # before appending advisories — rather than silently dropping the
        # only content an empty-item response would otherwise carry.
        #
        # Every empty pack renders through the formatter, whatever the
        # holdout rate. At rate 0 nothing is withheld, but the server's own
        # instructions tell an agent to grade the pack it was served by
        # ``pack_id``, and a bare one-line reply left it nothing to name.
        # One shape for all empty packs also keeps the look-alike rule
        # above true by construction: a withheld pack and a genuinely empty
        # one take this same path, so neither can differ by its header.
        # Nothing is served here, so the count is a real zero.
        note_served_items([])
        formatter = format_pack_as_index_markdown if index else format_pack_as_markdown
        result = formatter(
            [],
            title or intent,
            max_tokens=max_tokens,
            pack_id=pack.pack_id,
            withholding=withholding,
            empty_text=empty,
            axis_note=axis_note,
            advisories=pack.advisories,
        )
        result = f"{banner}\n\n{result}" if banner else result
        return with_grading_trailer(registry, result, pack.pack_id)

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
        axis_note=axis_note,
        # Advisories ride outside the item budget on both pack shapes, and
        # the formatter places them above the citation footer so the
        # footer stays the last thing read (see ``_advisories_block``).
        advisories=pack.advisories,
    )
    # The pack's own ids, kept only if the formatter printed them: it stops
    # listing items when the token budget runs out.
    note_printed_items(result, (item.item_id for item in pack.items))
    if banner:
        result = f"{banner}\n\n{result}"
    result = with_grading_trailer(registry, result, pack.pack_id)
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
    retrieval_phase: str | None = None,
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
    ``retrieval_phase`` (optional) is forwarded unchanged — validated by
    the caller before this helper runs.
    """
    _check_retrieval_phase(retrieval_phase)
    try:
        builder = _build_pack_builder(registry)
        sections = [SectionRequest.model_validate(s) for s in section_specs]
        sectioned_pack = builder.build_sectioned(
            intent,
            clearance=current_clearance(),
            sections=sections,
            domain=domain or None,
            session_id=session_id or None,
            run_id=run_id or None,
            refresh=refresh,
            retrieval_phase=retrieval_phase,
            principal_id=current_principal_id(),
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
        # As on the sectioned REST route: every section carries the one
        # strategies_used list build_sectioned() computed, so the first
        # section's stands for all. No caller passes sections=[].
        axis_note = ""
        if sectioned_pack.sections:
            # Builder-sourced, not a second ``registry.embedding_fn`` read
            # — see the flat path's identical note (Q5-A).
            axes = describe_axes(
                builder,
                sectioned_pack.sections[0].retrieval_report.strategies_used,
                embedder_configured=builder.embedder_configured,
                embedder_resolve_failure=builder.embedder_resolve_failure,
                graph_axis=sectioned_pack.sections[0].retrieval_report.graph_axis,
                public_only=is_public_only(current_clearance()),
            )
            axis_note = _axis_note(axes)
        result = format_sectioned_pack_as_markdown(
            section_dicts,
            intent,
            max_tokens=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
            withholding=withholding_from_payload(
                sectioned_pack.metadata.get("withholding")
            ),
            axis_note=axis_note,
            advisories=sectioned_pack.advisories,
        )
        note_printed_items(
            result,
            (
                item.item_id
                for section in sectioned_pack.sections
                for item in section.items
            ),
        )
        banner = _context_banner(registry)
        if banner:
            result = f"{banner}\n\n{result}"
        result = with_grading_trailer(registry, result, sectioned_pack.pack_id)
        _track_tokens(
            registry,
            operation=tool,
            result=result,
            budget=resolved_tokens,
            pack_id=sectioned_pack.pack_id,
        )
    except MCPError:
        # Already structured by a deeper helper — let it propagate.
        raise
    except PublicReadBusyError:
        # See the flat path: a refused public read keeps its own type.
        raise
    except Exception as exc:
        logger.exception("sectioned_context_failed", tool=tool)
        detail = _exception_detail(exc)
        _raise_internal(
            f"failed to assemble {_TOOL_LABEL.get(tool, 'context')} "
            f"for intent={intent!r}: {detail}",
            cause=exc,
            data={"tool": tool, "intent": intent},
        )
    return result


# ---------------------------------------------------------------------------
# Macro Tool 1: get_context — the one parameterized retrieval tool (#262)
# ---------------------------------------------------------------------------


@mcp.tool(auth=trellis_scope(SCOPE_READ), tags={PACK_TOOL_TAG})
def get_context(
    intent: str,
    domain: str | None = None,
    max_tokens: int | None = None,
    session_id: str = "",
    run_id: str = "",
    sections: list[dict[str, Any]] | None = None,
    refresh: bool = False,
    index: bool = False,
    retrieval_phase: str | None = None,
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
        max_tokens: Maximum response size in tokens. Leave unset to use the
            deployment's configured default (2000 unless an operator has
            overridden it via settings).
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
        retrieval_phase: Optional — when in the task this retrieval
            happens, relative to the work it's meant to inform: one of
            "pre_work", "mid_task", "post_hoc". Pass "post_hoc" when
            retrieving after the work is done, e.g. to check conclusions.
            Leave unset when you don't know.
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
        # Deliberately NOT routed through the new settings knob (see
        # ``_GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS``'s docstring) — this
        # branch's resolution is budget_config's alone.
        sections_max_tokens = (
            max_tokens
            if max_tokens is not None
            else _GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS
        )
        budget = registry.budget_config.resolve(
            tool="get_context",
            domain=domain or None,
            caller_override_tokens=(
                sections_max_tokens if sections_max_tokens > 0 else None
            ),
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
            retrieval_phase=retrieval_phase,
        )

    settings = load_settings_values(registry.stores_dir)
    resolved_max_tokens = (
        max_tokens
        if max_tokens is not None
        else resolve_get_context_max_tokens(settings)
    )
    return _flat_context(
        registry,
        intent,
        domain=domain,
        max_tokens=resolved_max_tokens,
        max_items=resolve_get_context_max_items(settings),
        session_id=session_id,
        run_id=run_id,
        operation="get_context",
        refresh=refresh,
        index=index,
        retrieval_phase=retrieval_phase,
    )
