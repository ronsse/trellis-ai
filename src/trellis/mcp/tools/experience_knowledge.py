"""``save_experience`` and ``save_knowledge``.

Trace ingest and evidence-anchored knowledge, with the supersede planning
``save_knowledge`` uses.
"""

from __future__ import annotations

from typing import Any, NoReturn

from trellis.auth import SCOPE_INGEST
from trellis.core.error_sanitize import (
    describe_validation_error,
    sanitize_error_message,
)
from trellis.core.write_config import knowledge_link_or_mint_enabled
from trellis.extract.trace_ingest_hook import (
    run_trace_extraction,
    trace_already_ingested,
)
from trellis.mcp import supersession as _supersession
from trellis.mcp.app import mcp
from trellis.mcp.auth import current_clearance, trellis_scope
from trellis.mcp.boundary import (
    _raise_invalid_params,
    _raise_mutation_failed,
    _result_message,
)
from trellis.mcp.knowledge_links import (
    attach_to_note,
    find_attach_target,
    link_knowledge_node,
)
from trellis.mcp.rejections import _record_boundary_rejection
from trellis.mcp.tools.memory_common import (
    _match_readable,
    _readable_document_exists,
    _refuse_label_below_request,
    _stamp_sensitivity,
)
from trellis.mcp.wiring import _get_registry, _stamp_trace_project
from trellis.mutate import (
    Command,
    CommandResult,
    CommandStatus,
    Operation,
    build_curate_executor,
    ensure_evidence_document,
)
from trellis.ops.write_health import record_trace_coercion
from trellis.schemas.trace_repair import describe_repairs, validate_trace_json
from trellis.stores.registry import StoreRegistry

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
            ``started_at``, and ``ended_at`` inside ``context``. A step takes
            ``step_type`` and ``name`` (both required) and, optionally,
            ``args``, ``result``, ``error``, ``duration_ms``, and
            ``started_at``; there is no ``action`` or ``observation`` field.
            A step's ``args`` and ``result`` are objects, never a bare string:
            write ``{"summary": "12 passed"}``, not ``"12 passed"``. Record a
            step that failed in its ``error`` string. Put how the work ended
            in top-level ``outcome``: ``status`` (``success``, ``failure``,
            ``partial`` or ``unknown``) and ``summary``. Validation is
            all-or-nothing: an unknown top-level or context field rejects the
            **entire trace** and nothing is recorded — not just that field. So
            put additional data in top-level ``metadata`` (or outcome
            measurements in ``outcome.metrics``).

            Example valid trace:
            {
              "source": "agent",
              "intent": "Fix a failing test",
              "context": {
                "agent_id": "coder-1",
                "domain": "bugfix",
                "started_at": "2026-08-27T00:00:00Z",
                "ended_at": "2026-08-27T00:01:00Z"
              },
              "steps": [
                {
                  "step_type": "tool_call",
                  "name": "run_tests",
                  "args": {"path": "tests/unit"},
                  "result": {"summary": "1 failed, 11 passed"}
                },
                {
                  "step_type": "tool_call",
                  "name": "edit_file",
                  "args": {"path": "src/parser.py"},
                  "error": "patch did not apply: file changed on disk"
                },
                {
                  "step_type": "tool_call",
                  "name": "edit_file",
                  "args": {"path": "src/parser.py"},
                  "result": {"summary": "patched"}
                }
              ],
              "outcome": {
                "status": "success",
                "summary": "Fixed the failing parser test"
              },
              "metadata": {"repo": "my/repo"}
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
        trace, repairs = validate_trace_json(trace_json)
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
            f"invalid trace JSON: {describe_validation_error(exc)}{hint_suffix}",
            data={
                "field": "trace_json",
                "error_class": type(exc).__name__,
                "hints": hints,
            },
        )

    # Stamped before ingest: a stored trace is immutable.
    trace = _stamp_trace_project(trace)
    registry = _get_registry()
    executor = build_curate_executor(registry, clearance=current_clearance())
    # Read before the write: the handler answers a stored trace_id as a
    # success that stores nothing, and only its message says which.
    already_ingested = trace_already_ingested(registry, trace.trace_id)
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
            f"failed to store trace: {_result_message(result)}",
            data={
                "status": result.status.value,
                "command_id": result.command_id,
                "message": _result_message(result),
            },
        )
    # One health row for a trace the boundary repaired, so a repaired write
    # is neither silent nor counted as a rejection. Advisory: wrapped whole
    # inside, because the write has already landed.
    record_trace_coercion(
        registry,
        tool="save_experience",
        repairs=repairs,
        trace_id=result.created_id or trace.trace_id,
    )
    repaired_note = f" | {describe_repairs(repairs)}" if repairs else ""
    if already_ingested:
        # A duplicate's agent and artifacts would land on the stored
        # trace's node, so it is not extracted either.
        return (
            f"Trace already ingested: {result.created_id}; "
            f"this submission was not stored or extracted.{repaired_note}"
        )

    # Feature-flagged post-ingest trace->graph extraction
    # (TRELLIS_ENABLE_TRACE_EXTRACTION=1). Runs the deterministic
    # TraceExtractor through the governed MutationExecutor after the trace
    # is durably stored. Fail-soft inside the hook -- never blocks the save.
    run_trace_extraction(registry, trace, requested_by="mcp:save_experience")

    return f"Trace saved: {result.created_id}{repaired_note}"


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
    label: dict[str, Any] | None = None,
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
        # A document above the caller's clearance is "not found": linking an
        # entity to it must not work, and must not differ from a missing id.
        if not _readable_document_exists(registry, evidence_ref):
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
        from trellis.errors import MutationError  # noqa: PLC0415

        try:
            document_id = ensure_evidence_document(
                registry,
                content,
                metadata={
                    "entity_name": name,
                    "entity_type": entity_type,
                    **(label or {}),
                },
                source="mcp:save_knowledge",
                clearance=current_clearance(),
            )
        except MutationError as exc:
            # ensure_evidence_document wraps a non-SUCCESS evidence.ingest
            # result's CommandResult.message whole (evidence.py); that
            # message can carry a store's own rejection detail on FAILED
            # or REJECTED, so it is sanitized here too, unconditionally —
            # evidence.ingest is built with derive_idempotency=False and
            # the content-hash dedup returns before execute, so DUPLICATE
            # cannot reach this raise.
            detail = sanitize_error_message(str(exc))
            _raise_mutation_failed(
                f"failed to store evidence: {detail}",
                data={
                    "command_id": exc.command_id,
                    "message": detail,
                },
            )
        # Content dedup can resolve to a document that already existed, with
        # its own label. Above the caller's clearance it cannot be linked or
        # named, and below the requested label it would keep serving the text
        # at the old level; neither writes anything (#194). The match test is
        # exact-hash, so a caller can still learn that identical text exists
        # by being refused: the residual oracle a content-addressed store has.
        if not _match_readable(registry, document_id):
            _raise_invalid_params(
                "content matches an existing document this credential cannot "
                "read; nothing was written",
                data={"field": "content"},
            )
        _refuse_label_below_request(registry, document_id, label or {})
        return (
            document_id,
            False,
        )
    return None, False


def _plan_knowledge_supersedes(
    registry: StoreRegistry,
    supersedes: str | None,
    *,
    content: str | None,
    evidence_ref: str | None,
) -> _supersession.EntityPlan | None:
    """Check ``save_knowledge(supersedes=…)`` before anything is written.

    A refusal is recorded and raised here rather than in
    :mod:`trellis.mcp.supersession`, so the rejection site carries the literal
    ``tool=`` the capture-surface roster reads. ``None`` means there is
    nothing to stamp: no ``supersedes``, or an ``evidence_ref`` naming no
    document, which :func:`_resolve_evidence_pointer` refuses next with its
    own message.
    """
    if supersedes is None:
        return None
    plan = _supersession.plan_entity_supersession(
        registry,
        supersedes,
        content=content,
        evidence_ref=evidence_ref,
        clearance=current_clearance(),
    )
    if isinstance(plan, _supersession.Refusal):
        _record_boundary_rejection(tool="save_knowledge", rejections=[plan.row()])
        _raise_invalid_params(
            plan.msg, data={"field": plan.loc, "supersedes": supersedes}
        )
    return plan


def _raise_if_supersede_failed(
    error: str | None,
    *,
    target: str,
    supersedes: str,
    successor: str,
    applied: list[str],
) -> None:
    """Raise when a ``save_knowledge`` stamp failed, naming what was applied.

    The stamps run before the entity is created, so no entity exists yet,
    and a re-send finishes the call without repeating a stamp already in
    place.
    """
    if error is None:
        return
    _raise_mutation_failed(
        f"failed to supersede {target}: {error}; supersession applied: "
        f"{', '.join(applied) or 'none'}; no entity was created; re-send the "
        "same call to finish",
        data={
            "stage": "supersede",
            "supersedes": supersedes,
            "evidence_ref": successor,
            "supersession_applied": list(applied),
        },
    )


def _apply_knowledge_supersedes(
    registry: StoreRegistry,
    supersedes: str | None,
    plan: _supersession.EntityPlan | None,
    successor: str | None,
) -> tuple[list[str], list[str]]:
    """Stamp the superseded entity, then its evidence document.

    The successor is the new entity's evidence document, the id a pack
    carries for it. Runs once that document exists and before the entity is
    created, so a failure here leaves no new entity beside an old version
    that is still served. Returns ``(applied, lines)``: the ids stamped, and
    the response lines reporting them — also on a re-send, where the stamps
    are already in place and nothing is written.
    """
    if supersedes is None or plan is None or successor is None:
        return [], []
    error = _supersession.supersede_entity(
        registry,
        node_id=supersedes,
        successor=successor,
        requested_by="mcp:save_knowledge",
        clearance=current_clearance(),
    )
    _raise_if_supersede_failed(
        error, target=supersedes, supersedes=supersedes, successor=successor, applied=[]
    )
    applied = [supersedes]
    lines = [f"Superseded: {supersedes} -> {successor}"]
    if plan.t_doc is None:
        return applied, lines
    if plan.t_doc_skip is not None:
        lines.append(
            f"Evidence document {plan.t_doc} left unchanged ({plan.t_doc_skip})"
        )
        return applied, lines
    error = _supersession.supersede_document(
        registry,
        doc_id=plan.t_doc,
        successor=successor,
        requested_by="mcp:save_knowledge",
        clearance=current_clearance(),
    )
    _raise_if_supersede_failed(
        error,
        target=plan.t_doc,
        supersedes=supersedes,
        successor=successor,
        applied=applied,
    )
    applied.append(plan.t_doc)
    lines.append(f"Superseded: {plan.t_doc} -> {successor}")
    return applied, lines


def _raise_create_failed(
    create_result: CommandResult,
    evidence_ref: str | None,
    *,
    supersedes: str | None,
    applied: list[str],
) -> NoReturn:
    """Raise for a failed entity create in ``save_knowledge``.

    Any auto-created evidence document is left as an acceptable orphan — no
    node was written, so there is no dangling pointer to clean up. When a
    ``supersedes=`` stamp was already applied the message says so, because
    the old version is now withheld wherever its successor is served; the
    re-send that finishes the call does not repeat the stamps.
    """
    message = f"failed to create entity: {_result_message(create_result)}"
    data: dict[str, Any] = {
        "status": create_result.status.value,
        "command_id": create_result.command_id,
        "message": _result_message(create_result),
        "evidence_ref": evidence_ref,
    }
    if supersedes is not None:
        stamped = ", ".join(f"{target} -> {evidence_ref}" for target in applied)
        message += (
            f"; supersession already applied: {stamped or 'none'} (re-send the "
            "same call to finish; the stamps are not repeated)"
        )
        data["supersession_applied"] = list(applied)
    _raise_mutation_failed(message, data=data)


@mcp.tool(auth=trellis_scope(SCOPE_INGEST))
def save_knowledge(
    name: str,
    entity_type: str = "concept",
    properties: dict[str, Any] | None = None,
    relates_to: str | None = None,
    edge_kind: str = "entity_related_to",
    content: str | None = None,
    evidence_ref: str | None = None,
    supersedes: str | None = None,
    sensitivity: str | None = None,
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
        properties: Optional additional properties. A ``domain`` string (or
            list of strings) also links the entity ``appliesTo`` each
            existing ``domain:`` node it names; a domain with no node is
            reported, never created.
        relates_to: Optional entity to create a relationship to — a node id,
            or a name such as a tool, domain, agent, team or artifact name.
            A name linking to more than one node creates no edge and lists
            the candidates.
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
        supersedes: Optional id of an existing entity this one replaces.
            Requires ``content`` or ``evidence_ref``. The old entity and its
            evidence document are marked superseded by the new evidence
            document, so a pack holding both serves only the new version; an
            evidence document that cannot be marked (chunked, archived,
            superseded by something else, or a ``trace-summary:`` row, which
            the trace embed worker owns) is left unchanged and named in the
            reply. Refused, with nothing written, if the id is unknown, a
            document, archived, or already superseded by something else.
        sensitivity: Optional label for the evidence document created from
            ``content`` (see ``save_memory``). The entity itself carries no
            label, so keep sensitive prose in ``content``, not in ``name`` or
            ``properties``. Not valid with ``evidence_ref``: relabelling an
            existing document is not supported here.
    """
    if not name or not name.strip():
        _raise_invalid_params(
            "name must not be empty",
            data={"field": "name"},
        )
    if sensitivity is not None and (
        evidence_ref is not None or not (content or "").strip()
    ):
        _raise_invalid_params(
            "sensitivity labels the evidence document created from content; "
            "supply content and no evidence_ref",
            data={"field": "sensitivity"},
        )
    evidence_label = _stamp_sensitivity(None, sensitivity, tool="save_knowledge")

    registry = _get_registry()

    # A supersedes= declaration is refused before anything is written.
    plan = _plan_knowledge_supersedes(
        registry, supersedes, content=content, evidence_ref=evidence_ref
    )

    # Existence-check any explicit pointer / auto-create the evidence doc
    # (doc-FIRST) before any graph mutation — see _resolve_evidence_pointer
    # for the two halves of the pointer-not-prose ordering contract.
    evidence_ref, content_ignored = _resolve_evidence_pointer(
        registry,
        name=name,
        entity_type=entity_type,
        content=content,
        evidence_ref=evidence_ref,
        label=evidence_label,
    )
    # The stamps go on once their successor exists, and before the entity:
    # a failure between the two then leaves the old version withheld behind
    # a successor document that is served, never a new entity beside an old
    # version still served. Re-sending finishes either half.
    applied, supersede_lines = _apply_knowledge_supersedes(
        registry, supersedes, plan, evidence_ref
    )

    props = dict(properties or {})
    if evidence_ref is not None:
        props["evidence_ref"] = evidence_ref
    document_ids = [evidence_ref] if evidence_ref is not None else None

    executor = build_curate_executor(registry, clearance=current_clearance())
    # Knob on, no supersedes=: a note whose normalized name already resolves
    # to a current note of the same type is attached to, not duplicated.
    link_or_mint = knowledge_link_or_mint_enabled(registry)
    attach_node = (
        find_attach_target(
            registry.knowledge.graph_store, name=name, entity_type=entity_type
        )
        if link_or_mint and supersedes is None
        else None
    )
    if attach_node is not None:
        attached_id = str(attach_node["node_id"])
        result = (
            f"Entity exists: {attached_id} ({entity_type}: {name}) "
            "\u2014 attached, not created"
        )
        if evidence_ref is not None:
            result += f"\nEvidence document: {evidence_ref}"
        if content_ignored:
            result += "\nWarning: content ignored \u2014 evidence_ref takes precedence"
        # An attach writes no caller properties (see ``attach_to_note``), so
        # only the resolved evidence pointer is passed, never ``props``.
        attach_lines = attach_to_note(executor, attach_node, evidence_ref=evidence_ref)
        link_lines = link_knowledge_node(
            executor,
            registry.knowledge.graph_store,
            node_id=attached_id,
            properties=props,
            relates_to=relates_to,
            edge_kind=edge_kind,
            link_or_mint=True,
        )
        return "\n".join([result, *supersede_lines, *attach_lines, *link_lines])
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
        _raise_create_failed(
            create_result, evidence_ref, supersedes=supersedes, applied=applied
        )

    node_id = create_result.created_id
    result = f"Entity created: {node_id} ({entity_type}: {name})"
    if evidence_ref is not None:
        result += f"\nEvidence document: {evidence_ref}"
    if content_ignored:
        result += "\nWarning: content ignored — evidence_ref takes precedence"

    if node_id is None:
        return "\n".join([result, *supersede_lines])
    # Entity already created — link failures come back as response lines,
    # never as a raise. See trellis.mcp.knowledge_links.
    link_lines = link_knowledge_node(
        executor,
        registry.knowledge.graph_store,
        node_id=node_id,
        properties=props,
        relates_to=relates_to,
        edge_kind=edge_kind,
        link_or_mint=link_or_mint,
    )
    return "\n".join([result, *supersede_lines, *link_lines])
