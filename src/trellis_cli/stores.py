"""Store initialization and access for the CLI."""

from __future__ import annotations

from pathlib import Path

import structlog
import typer

from trellis.stores.base import (
    DocumentStore,
    EventLog,
    GraphStore,
    OutcomeStore,
    ParameterStore,
    TraceStore,
    TunerStateStore,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.config import TrellisConfig, get_data_dir

logger = structlog.get_logger(__name__)

_registry: StoreRegistry | None = None


def _reset_registry() -> None:
    """Reset the cached registry. Used by tests to avoid stale connections."""
    global _registry  # noqa: PLW0603
    _registry = None


def _get_registry() -> StoreRegistry:
    """Get or create a cached StoreRegistry singleton from CLI config."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        config = TrellisConfig.load()
        data_dir = Path(config.data_dir) if config.data_dir else get_data_dir()
        stores_dir = data_dir / "stores"
        if not stores_dir.exists():
            from rich.console import Console  # noqa: PLC0415

            Console().print(
                "[red]Stores not initialized. Run 'trellis admin init' first.[/red]"
            )
            raise typer.Exit(code=1)
        _registry = StoreRegistry(stores_dir=stores_dir)
    return _registry


def get_trace_store() -> TraceStore:
    """Open (or create) the trace store."""
    return _get_registry().operational.trace_store


def get_document_store() -> DocumentStore:
    """Open (or create) the document store."""
    return _get_registry().knowledge.document_store


def get_event_log() -> EventLog:
    """Open (or create) the event log."""
    return _get_registry().operational.event_log


def get_graph_store() -> GraphStore:
    """Open (or create) the graph store."""
    return _get_registry().knowledge.graph_store


def get_outcome_store() -> OutcomeStore:
    """Open (or create) the operational-plane outcome store."""
    return _get_registry().operational.outcome_store


def get_parameter_store() -> ParameterStore:
    """Open (or create) the operational-plane parameter store."""
    return _get_registry().operational.parameter_store


def get_tuner_state_store() -> TunerStateStore:
    """Open (or create) the operational-plane tuner-state store."""
    return _get_registry().operational.tuner_state_store
