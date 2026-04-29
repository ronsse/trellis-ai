"""Shared backend-construction helpers for multi-backend eval scenarios.

Scenarios 5.1, 5.3, and 5.5 each build a list of ``BackendHandle`` —
SQLite always, plus Postgres / Neo4j conditional on env credentials.
The boilerplate (env probing, path mgmt, ``ExitStack`` registration,
graceful skip on construction failure) is identical across all three;
only the per-handle config dicts differ. This module centralises the
boilerplate; each scenario assembles its own configs and calls
:func:`register_handle` to add them to its handle list.

Private to ``eval/`` (leading underscore in the filename). Production
code does not need scenario-construction helpers.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


@dataclass
class BackendHandle:
    """A named registry that a scenario will exercise.

    Scenarios build a list of these during setup and iterate over them
    in their measurement loop. The ``name`` is the label that appears
    in metrics (``ingest_seconds.<name>``) and findings ("compared
    backends: <name>, ...").
    """

    name: str
    registry: StoreRegistry


def get_postgres_dsn() -> str | None:
    """Return the Postgres DSN for eval, or ``None`` if not configured.

    Honors ``TRELLIS_KNOWLEDGE_PG_DSN`` first (production-shaped),
    then ``TRELLIS_PG_DSN`` (legacy fallback). The same env var
    convention scenario 5.1 has used since A.2 landed.
    """
    return os.environ.get("TRELLIS_KNOWLEDGE_PG_DSN") or os.environ.get(
        "TRELLIS_PG_DSN"
    )


def get_neo4j_config(*, dimensions: int | None = None) -> dict[str, Any] | None:
    """Build the Neo4j connection config block, or ``None`` if creds missing.

    Returns a dict suitable for use as the ``graph`` (or, with
    ``dimensions`` set, ``vector``) entry of a ``StoreRegistry`` config.
    Requires ``TRELLIS_NEO4J_URI`` + ``TRELLIS_NEO4J_USER`` +
    ``TRELLIS_NEO4J_PASSWORD``; ``TRELLIS_NEO4J_DATABASE`` is added when
    set (AuraDB instances need it; default Neo4j installs don't).
    """
    uri = os.environ.get("TRELLIS_NEO4J_URI")
    user = os.environ.get("TRELLIS_NEO4J_USER")
    password = os.environ.get("TRELLIS_NEO4J_PASSWORD")
    if not (uri and user and password):
        return None
    cfg: dict[str, Any] = {
        "backend": "neo4j",
        "uri": uri,
        "user": user,
        "password": password,
    }
    if dimensions is not None:
        cfg["dimensions"] = dimensions
    database = os.environ.get("TRELLIS_NEO4J_DATABASE")
    if database:
        cfg["database"] = database
    return cfg


def register_handle(
    stack: ExitStack,
    handles: list[BackendHandle],
    *,
    name: str,
    config: dict[str, Any],
    stores_dir: Path,
) -> None:
    """Construct a registry from ``config``; append a handle on success.

    Logs ``eval.<name>_unavailable`` at warning level and drops the
    handle on construction failure — env-gated optional backends fall
    through cleanly rather than aborting the scenario. Scenarios
    surface the skip via an ``info`` Finding from their own bookkeeping.

    ``stores_dir`` is created if missing so callers don't have to
    repeat the ``mkdir(parents=True, exist_ok=True)`` boilerplate.
    """
    stores_dir.mkdir(parents=True, exist_ok=True)
    try:
        registry = stack.enter_context(
            StoreRegistry(config=config, stores_dir=stores_dir)
        )
        handles.append(BackendHandle(name=name, registry=registry))
    except Exception as exc:  # pragma: no cover — exercised in live runs
        logger.warning("eval.backend_unavailable", backend=name, error=str(exc))
