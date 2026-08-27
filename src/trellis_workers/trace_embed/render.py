"""Rendering a trace into the prose the semantic axis can actually reach.

A trace is not a document. :class:`~trellis.retrieve.strategies.SemanticSearch`
reads the **vector** store, ``KeywordSearch`` reads the **document** store and
``GraphSearch`` reads the **graph** store — none of the three reads the
``TraceStore``. Trace ingest writes no document row (``extract/trace.py`` says
so outright: "the common case, since trace ingest does not write a document"),
so the only surface a trace has ever presented to retrieval is the name-only
``trace:<trace_id>`` Activity node minted by trace extraction. That node
carries the intent string and nothing else, and it is exactly the substance-free
stub :mod:`trellis.retrieve.noise`'s content floor demotes.

This module turns the parts of a trace that *are* prose into a document. The
rest of the trace stays where it is: traces are immutable, and nothing here
writes back to one.

What goes in, and what does not
-------------------------------

**In:** ``intent``, ``outcome.status`` / ``outcome.summary``, and the ``error``
strings off the trace's steps.

**Out:** the step log. Step ``args`` and ``result`` payloads are tool traffic —
file contents, diffs, command output — and embedding them would swamp the
signal in a 2000-char window with material that is already in the repository.

The step *errors* are a deliberate departure from "intent + summary only", and
the reason is the project's own recording doctrine: ``record-after-task`` tells
every agent to "put failures in the step's ``error`` field; the workaround the
next agent would otherwise rediscover is the most reusable thing you can leave
behind." An error string is authored prose about a failure, not tool noise, and
it is the highest-value thing a trace holds. It is bounded
(:data:`MAX_STEP_ERRORS` entries, :data:`MAX_STEP_ERROR_CHARS` each) so a
pathological trace cannot crowd out the summary, and
``include_step_errors=False`` restores the narrower body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.core.elision import elide_text
from trellis.schemas.document_metadata import DocumentMetadata

if TYPE_CHECKING:
    from trellis.schemas.trace import Trace

__all__ = [
    "DOCUMENT_FORM",
    "DOC_ID_PREFIX",
    "MAX_STEP_ERRORS",
    "MAX_STEP_ERROR_CHARS",
    "SOURCE_SYSTEM",
    "build_trace_metadata",
    "render_trace_summary",
    "trace_summary_doc_id",
]

#: Id namespace for the derived document (and therefore for its vector row,
#: which ``build_vector_row`` keys 1:1 off the document id).
#:
#: Deliberately **not** ``trace:``, which is the graph store's Activity node id
#: for the same trace. On the Postgres and SQLite substrates those live in
#: different tables and could not collide — but on Neo4j and ArcadeDB the vector
#: store is *shape #2*: embeddings are a property on the graph store's
#: ``(:Node)`` rows, so the vector ``item_id`` **is** the ``node_id``. Reusing
#: ``trace:<id>`` there would have this worker upserting a vector onto a live
#: SCD-2 graph node — a write into the graph plane from a worker that has no
#: business touching it. A distinct namespace is correct on every backend.
DOC_ID_PREFIX = "trace-summary:"

#: ``metadata["source_system"]`` — the corpus namespace these documents belong
#: to. Read by ``SourceSystemClassifier`` and by the pack attribution fields.
SOURCE_SYSTEM = "trellis-trace"

#: ``metadata["document_form"]`` — open-vocabulary provenance marker, so a
#: consumer (or an operator running ``trellis admin resync-vector-metadata``)
#: can tell a derived trace summary from an authored memory.
DOCUMENT_FORM = "trace_summary"

#: Most step errors rendered into one summary.
MAX_STEP_ERRORS = 8

#: Longest single step error rendered, before elision.
MAX_STEP_ERROR_CHARS = 600


def trace_summary_doc_id(trace_id: str) -> str:
    """The derived document id for *trace_id*.

    Deterministic and total: the id is a pure function of the trace id, which
    is what makes "has this trace been embedded?" a question about store state
    rather than about a bookkeeping file that can drift from it.
    """
    return f"{DOC_ID_PREFIX}{trace_id}"


def trace_id_from_doc_id(doc_id: str) -> str | None:
    """Inverse of :func:`trace_summary_doc_id`, or ``None`` for a foreign id."""
    if not doc_id.startswith(DOC_ID_PREFIX):
        return None
    return doc_id[len(DOC_ID_PREFIX) :] or None


def _step_errors(trace: Trace) -> list[tuple[str, str]]:
    """``(step name, error)`` for every step that recorded a failure."""
    out: list[tuple[str, str]] = []
    for step in trace.steps:
        error = (step.error or "").strip()
        if not error:
            continue
        out.append(
            (
                (step.name or step.step_type or "step").strip(),
                elide_text(error, MAX_STEP_ERROR_CHARS, reason="step_error_cap"),
            )
        )
        if len(out) >= MAX_STEP_ERRORS:
            break
    return out


def render_trace_summary(trace: Trace, *, include_step_errors: bool = True) -> str:
    """Render *trace* into the markdown body that gets embedded.

    Returns ``""`` when the trace carries no prose at all — no intent, no
    outcome summary, and no recorded step errors. That is not an error, it is
    a trace with nothing to say; the caller counts it and refuses to advance
    past it rather than embedding a metadata line on its own.
    """
    lines: list[str] = []
    intent = (trace.intent or "").strip()
    if intent:
        lines.append(f"# {intent}")

    outcome = trace.outcome
    summary = (outcome.summary or "").strip() if outcome is not None else ""
    if outcome is not None:
        lines.append("")
        lines.append(f"**Outcome:** {outcome.status.value}")
        if summary:
            lines.append("")
            lines.append(summary)

    errors = _step_errors(trace) if include_step_errors else []

    # The facts line is metadata, not prose. On its own it is precisely the
    # substance-free stub ``trellis.retrieve.noise``'s content floor exists to
    # demote, so a trace with no intent, no outcome summary and no recorded
    # errors renders to nothing and the caller counts it rather than putting
    # five words of provenance on the semantic axis.
    if not intent and not summary and not errors:
        return ""

    facts: list[str] = []
    ctx = trace.context
    if ctx is not None:
        if ctx.domain:
            facts.append(f"domain `{ctx.domain}`")
        if ctx.agent_id:
            facts.append(f"agent `{ctx.agent_id}`")
        if ctx.workflow_id:
            facts.append(f"workflow `{ctx.workflow_id}`")
    facts.append(f"source `{trace.source.value}`")
    lines.append("")
    lines.append(" · ".join(facts))

    if errors:
        lines.append("")
        lines.append("## Failures recorded")
        lines.extend(f"- **{name}**: {error}" for name, error in errors)

    return "\n".join(lines).strip()


def build_trace_metadata(trace: Trace) -> dict[str, Any]:
    """Document metadata for a rendered trace summary.

    Routed through :meth:`DocumentMetadata.from_mapping` /
    :meth:`~DocumentMetadata.to_metadata` so the derived rows carry the same
    validated core as every other write path — the model is lenient by
    construction, so this normalises rather than rejects, and the stored shape
    is the same flat dict the store persists everywhere else.
    """
    ctx = trace.context
    raw: dict[str, Any] = {
        "title": (trace.intent or "").strip() or trace.trace_id,
        "source_system": SOURCE_SYSTEM,
        "source_path": f"trace/{trace.trace_id}",
        "document_form": DOCUMENT_FORM,
        "trace_id": trace.trace_id,
        "trace_source": trace.source.value,
    }
    if trace.outcome is not None:
        raw["outcome_status"] = trace.outcome.status.value
    if ctx is not None:
        if ctx.domain:
            raw["domain"] = ctx.domain
        if ctx.agent_id:
            raw["agent_id"] = ctx.agent_id
    return DocumentMetadata.from_mapping(raw).to_metadata()
