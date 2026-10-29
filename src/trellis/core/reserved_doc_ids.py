"""Document ids a caller may not name because a worker's bookkeeping reads them.

The trace-embed worker decides a trace is already embedded by asking the vector
store whether a row exists at ``trace-summary:<trace_id>``. A caller who could
file a document at that id, with embed-on-ingest on, would get a vector row
there and the worker would skip the real trace for good. The prefix is
therefore the worker's alone, and every write door refuses it.

This module imports nothing from the package, so ``mutate``, the CLI, the API
and the worker can all import it without a cycle (``trellis_workers`` imports
``trellis.mutate``, so ``mutate`` cannot import ``trellis_workers``).
"""

from __future__ import annotations

#: The id namespace of a trace's rendered summary, document row and vector row
#: alike. Exact and case-sensitive, as the worker builds it: ``trace-summary``
#: without the colon, ``Trace-Summary:x`` and ``my-trace-summary:x`` are
#: ordinary ids.
TRACE_SUMMARY_DOC_ID_PREFIX = "trace-summary:"

#: ``ValidationError.code`` of a caller-chosen document id under the prefix,
#: shared by every surface that refuses one so they cannot disagree. Distinct
#: from ``RESERVED_DOCUMENT_ID_CODE`` so a caller can tell which namespace it hit.
RESERVED_TRACE_SUMMARY_ID_CODE = "reserved_trace_summary_id"


def is_trace_summary_doc_id(doc_id: object) -> bool:
    """Whether ``doc_id`` is under the trace-summary prefix.

    Reads the id and nothing else, as ``is_promoted_precedent_document`` does:
    metadata is a caller's to write.
    """
    return isinstance(doc_id, str) and doc_id.startswith(TRACE_SUMMARY_DOC_ID_PREFIX)


def reserved_trace_summary_id_message(field: str, doc_id: str) -> str:
    """The refusal text for a caller-chosen ``doc_id`` under the prefix.

    ``field`` names where the caller supplied it (``evidence.doc_id``,
    ``evidence_id``).
    """
    return (
        f"{field} {doc_id!r} starts with {TRACE_SUMMARY_DOC_ID_PREFIX!r}, "
        "which belongs to the trace embed worker: a row at that id makes the "
        "trace it names look already embedded, so its real summary would "
        "never be written"
    )


__all__ = [
    "RESERVED_TRACE_SUMMARY_ID_CODE",
    "TRACE_SUMMARY_DOC_ID_PREFIX",
    "is_trace_summary_doc_id",
    "reserved_trace_summary_id_message",
]
