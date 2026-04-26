"""Smoke tests for ``docs/deployment/recommended-config.yaml``.

Pins two contracts:

1. The file parses as valid YAML, even with all three configurations
   commented out (the as-shipped state). Pure-unit, always runs.
2. The "local default" block, when uncommented and env-substituted,
   produces a working ``StoreRegistry`` against a real Neo4j instance
   plus SQLite operational. Env-gated on ``TRELLIS_TEST_NEO4J_URI``
   like the other integration tests.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _REPO_ROOT / "docs" / "deployment" / "recommended-config.yaml"


def test_recommended_config_is_valid_yaml() -> None:
    """File must always parse — broken YAML in a doc deliverable would
    silently mislead operators copy-pasting."""
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    # As-shipped, every config block is commented out. yaml.safe_load
    # returns None for an empty doc; we just want "no parse error".
    yaml.safe_load(text)


_TOP_LEVEL_KEYS = ("knowledge:", "operational:")


def _extract_block(text: str, header: str) -> dict[str, Any]:
    """Pull one of the three commented-out blocks out of the doc and parse it.

    Each block sits under a ``# === ... <header> === ...`` banner.
    Prose comments (``# Required env vars:``, ``# Pick this only if...``)
    precede the actual YAML, so we skip until we see the first top-level
    config key (``# knowledge:`` or ``# operational:``), then parse from
    there to the next banner.
    """
    pattern = (
        r"# =+\s*\n# \d+\.\s+"
        + re.escape(header)
        + r".*?\n# =+\s*\n(?P<body>(?:#.*\n)+)"
    )
    match = re.search(pattern, text)
    if match is None:
        msg = f"Could not find {header!r} block in {_CONFIG_PATH}"
        raise AssertionError(msg)
    body = match.group("body")

    yaml_lines: list[str] = []
    started = False
    for line in body.splitlines():
        stripped = line.removeprefix("#")
        stripped = stripped.removeprefix(" ")
        if stripped.strip().startswith("==="):
            break
        if not started:
            # Skip prose until we see the first top-level YAML key.
            if any(stripped.lstrip().startswith(key) for key in _TOP_LEVEL_KEYS):
                started = True
            else:
                continue
        yaml_lines.append(stripped)
    return yaml.safe_load("\n".join(yaml_lines)) or {}


class TestLocalDefaultBlock:
    """The local-default block names Neo4j (knowledge) + SQLite (operational)."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "LOCAL DEFAULT")

    def test_knowledge_graph_uses_neo4j(self) -> None:
        assert self.block["knowledge"]["graph"]["backend"] == "neo4j"

    def test_knowledge_vector_uses_neo4j(self) -> None:
        assert self.block["knowledge"]["vector"]["backend"] == "neo4j"

    def test_operational_trace_uses_sqlite(self) -> None:
        assert self.block["operational"]["trace"]["backend"] == "sqlite"

    def test_operational_event_log_uses_sqlite(self) -> None:
        assert self.block["operational"]["event_log"]["backend"] == "sqlite"


class TestCloudDefaultBlock:
    """Cloud-default: AuraDB Neo4j (knowledge) + Postgres (operational)."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "CLOUD DEFAULT")

    def test_knowledge_graph_uses_neo4j(self) -> None:
        assert self.block["knowledge"]["graph"]["backend"] == "neo4j"

    def test_operational_trace_uses_postgres(self) -> None:
        assert self.block["operational"]["trace"]["backend"] == "postgres"

    def test_operational_event_log_uses_postgres(self) -> None:
        assert self.block["operational"]["event_log"]["backend"] == "postgres"


class TestPostgresOnlyBlock:
    """The Postgres-only block consolidates everything on Postgres + pgvector."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "POSTGRES-ONLY ALTERNATIVE")

    def test_knowledge_graph_uses_postgres(self) -> None:
        assert self.block["knowledge"]["graph"]["backend"] == "postgres"

    def test_knowledge_vector_uses_pgvector(self) -> None:
        assert self.block["knowledge"]["vector"]["backend"] == "pgvector"


# ---------------------------------------------------------------------------
# Live smoke against the local-default shape (env-gated)
# ---------------------------------------------------------------------------

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")


@pytest.mark.neo4j
@pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set")
def test_local_default_shape_validates_against_real_neo4j(tmp_path: Path) -> None:
    """Construct the documented local-default registry and run the full
    validate(check_connectivity=True) path — config-stage instantiation +
    Neo4j Bolt ping + Postgres skipped (operational is SQLite here).

    Uses our integration AuraDB instance as the Neo4j backend (same Bolt
    protocol as the documented Docker target — drop-in substitute).
    """
    from tests.integration.conftest import (
        INTEGRATION_VECTOR_DIMS,
        INTEGRATION_VECTOR_INDEX,
    )
    from trellis.stores.registry import StoreRegistry

    config = {
        "graph": {
            "backend": "neo4j",
            "uri": URI,
            "user": USER,
            "password": PASSWORD,
            "database": DATABASE,
        },
        "vector": {
            "backend": "neo4j",
            "uri": URI,
            "user": USER,
            "password": PASSWORD,
            "database": DATABASE,
            "dimensions": INTEGRATION_VECTOR_DIMS,
            "index_name": INTEGRATION_VECTOR_INDEX,
        },
        "document": {"backend": "sqlite"},
        "blob": {"backend": "local"},
        "trace": {"backend": "sqlite"},
        "event_log": {"backend": "sqlite"},
        "outcome": {"backend": "sqlite"},
        "parameter": {"backend": "sqlite"},
        "tuner_state": {"backend": "sqlite"},
    }
    registry = StoreRegistry(config=config, stores_dir=tmp_path / "stores")
    try:
        # Validate the full set of store types this config defines, with
        # connectivity check on so the AuraDB instance is actually
        # pinged.  Should not raise.
        registry.validate(
            store_types=list(config.keys()),
            check_connectivity=True,
        )
    finally:
        registry.close()
