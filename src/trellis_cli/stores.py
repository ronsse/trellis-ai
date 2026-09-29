"""Store initialization and access for the CLI."""

from __future__ import annotations

import structlog
import typer
from rich.markup import escape

from trellis.stores.base import (
    ApiKeyStore,
    DocumentStore,
    EventLog,
    GraphStore,
    OutcomeStore,
    ParameterStore,
    TraceStore,
    TunerStateStore,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.config import get_config_dir
from trellis_cli.exit_codes import EXIT_INTERNAL

logger = structlog.get_logger(__name__)

# source_system value for aliases minted by this Trellis instance — distinct
# from external-system aliases ("github", "dbt", …). Demo loader seeds these;
# `retrieve entity` resolves memorable names through them.
#
# Aliases key on (source_system, raw_id), so two entities sharing the same
# raw_id under "local" will SCD-2 supersede each other — the older alias
# version is closed and the newer one wins. Pick raw_ids that are unique
# across entity types, or namespace them ("svc:foo", "team:foo").
LOCAL_SOURCE_SYSTEM = "local"

_registry: StoreRegistry | None = None


def _reset_registry() -> None:
    """Reset the cached registry. Used by tests to avoid stale connections."""
    global _registry  # noqa: PLW0603
    _registry = None


def _get_registry() -> StoreRegistry:
    """Get or create a cached StoreRegistry singleton from CLI config.

    Delegates to :meth:`StoreRegistry.from_config_dir`, which reads the
    plane-split ``knowledge:`` / ``operational:`` blocks of config.yaml.
    """
    global _registry  # noqa: PLW0603
    if _registry is None:
        # Check the registry's own stores_dir. It resolves config.yaml's
        # ``data_dir`` ahead of TRELLIS_DATA_DIR, and a guard that derived the
        # path again would pass on one directory while the stores opened
        # another. One registry, so config.yaml is read (and warned about) once.
        registry = StoreRegistry.from_config_dir(config_dir=get_config_dir())
        stores_dir = registry.stores_dir
        assert stores_dir is not None  # from_config_dir always sets it
        if not stores_dir.exists():
            from trellis_cli.output import build_console  # noqa: PLC0415

            # stderr, not stdout: callers like the meta-trace wiring swallow
            # the Exit and continue, and ``--format json`` consumers parse
            # stdout — an error line there poisons the machine output.
            build_console(stderr=True).print(
                f"[red]Stores not initialized at {escape(str(stores_dir))}."
                " Run 'trellis admin init' first.[/red]"
            )
            raise typer.Exit(code=EXIT_INTERNAL)
        _registry = registry
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


def get_api_key_store() -> ApiKeyStore:
    """Open (or create) the operational-plane API-key store."""
    return _get_registry().operational.api_key_store
