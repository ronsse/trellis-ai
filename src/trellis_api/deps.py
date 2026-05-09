"""FastAPI dependencies."""

from __future__ import annotations

from trellis.stores.base import (
    DocumentStore,
    EventLog,
    GraphStore,
    TraceStore,
    VectorStore,
)
from trellis_api.app import get_registry


def get_trace_store() -> TraceStore:
    """Return the trace store from the global registry."""
    return get_registry().operational.trace_store


def get_document_store() -> DocumentStore:
    """Return the document store from the global registry."""
    return get_registry().knowledge.document_store


def get_graph_store() -> GraphStore:
    """Return the graph store from the global registry."""
    return get_registry().knowledge.graph_store


def get_event_log() -> EventLog:
    """Return the event log from the global registry."""
    return get_registry().operational.event_log


def get_vector_store() -> VectorStore:
    """Return the vector store from the global registry."""
    return get_registry().knowledge.vector_store
