"""Smoke tests for ``docs/deployment/recommended-config.yaml``.

Pins four contracts:

1. The file parses as valid YAML with all four configurations commented
   out (the as-shipped state). Pure-unit, always runs.
2. Each block, uncommented, names the backends its banner promises.
3. Each block, uncommented verbatim and given only the env vars its
   ``Required env vars:`` list names, hands every listed value to a store
   (``test_shipped_block_delivers_documented_env``). Connection entry
   points are recorded instead of dialled, so it needs no infrastructure.
4. Two live tests validate hand-built configs of the Neo4j local and
   ArcadeDB blessed shapes against real instances. They do not read the
   shipped blocks; contract 3 is what ties those to the stores. Env-gated
   on ``TRELLIS_TEST_NEO4J_URI`` / ``TRELLIS_TEST_ARCADEDB_URI``.
"""

from __future__ import annotations

import os
import re
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from trellis.stores.registry import StoreRegistry

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


def _block_body(text: str, header: str) -> str:
    """The commented lines under one block's ``# === ... <header>`` banner."""
    pattern = (
        r"# =+\s*\n# \d+\.\s+"
        + re.escape(header)
        + r".*?\n# =+\s*\n(?P<body>(?:#.*\n)+)"
    )
    match = re.search(pattern, text)
    if match is None:
        msg = f"Could not find {header!r} block in {_CONFIG_PATH}"
        raise AssertionError(msg)
    return match.group("body")


def _block_yaml(text: str, header: str) -> str:
    """One block's YAML, uncommented exactly as an operator would paste it.

    Prose comments (``# Required env vars:``, ``# Pick this only if...``)
    precede the actual YAML, so we skip until we see the first top-level
    config key (``# knowledge:`` or ``# operational:``), then keep
    everything from there to the next banner.
    """
    yaml_lines: list[str] = []
    started = False
    for line in _block_body(text, header).splitlines():
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
    return "\n".join(yaml_lines) + "\n"


def _required_env(text: str, header: str) -> list[str]:
    """The variables a block's ``# Required env vars:`` list names, in order."""
    required: list[str] = []
    in_list = False
    for line in _block_body(text, header).splitlines():
        if line.startswith("# Required env vars:"):
            in_list = True
        elif in_list and line.rstrip() == "#":
            break
        elif in_list and (match := re.match(r"#\s+(TRELLIS_[A-Z0-9_]+)=", line)):
            required.append(match.group(1))
    return required


def _extract_block(text: str, header: str) -> dict[str, Any]:
    """Pull one of the four commented-out blocks out of the doc and parse it."""
    return yaml.safe_load(_block_yaml(text, header)) or {}


class TestArcadeDBBlessedBlock:
    """The blessed shape names ArcadeDB on graph + vector + SQLite operational."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "ARCADEDB (BLESSED)")

    def test_knowledge_graph_uses_arcadedb(self) -> None:
        assert self.block["knowledge"]["graph"]["backend"] == "arcadedb"

    def test_knowledge_vector_uses_arcadedb(self) -> None:
        assert self.block["knowledge"]["vector"]["backend"] == "arcadedb"

    def test_operational_trace_uses_sqlite(self) -> None:
        assert self.block["operational"]["trace"]["backend"] == "sqlite"

    def test_operational_event_log_uses_sqlite(self) -> None:
        assert self.block["operational"]["event_log"]["backend"] == "sqlite"


class TestNeo4jLocalBlock:
    """Neo4j local: Docker Neo4j (knowledge) + SQLite (operational)."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "NEO4J LOCAL")

    def test_knowledge_graph_uses_neo4j(self) -> None:
        assert self.block["knowledge"]["graph"]["backend"] == "neo4j"

    def test_knowledge_vector_uses_neo4j(self) -> None:
        assert self.block["knowledge"]["vector"]["backend"] == "neo4j"

    def test_operational_trace_uses_sqlite(self) -> None:
        assert self.block["operational"]["trace"]["backend"] == "sqlite"

    def test_operational_event_log_uses_sqlite(self) -> None:
        assert self.block["operational"]["event_log"]["backend"] == "sqlite"


class TestNeo4jCloudBlock:
    """Neo4j cloud: AuraDB Neo4j (knowledge) + Postgres (operational)."""

    def setup_method(self) -> None:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        self.block = _extract_block(text, "NEO4J CLOUD")

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
# Each shipped block delivers its documented env to the stores (no network)
# ---------------------------------------------------------------------------

# (banner header, count of its ``Required env vars:`` lines read off the
# file by hand, needs the neo4j driver package). The count comes from
# outside the scan, so a scan that finds fewer variables cannot pass.
_SHIPPED_BLOCKS = [
    pytest.param("ARCADEDB (BLESSED)", 5, True, id="arcadedb"),
    pytest.param("NEO4J LOCAL", 3, True, id="neo4j-local"),
    pytest.param("NEO4J CLOUD", 7, True, id="neo4j-cloud"),
    pytest.param("POSTGRES-ONLY ALTERNATIVE", 3, False, id="postgres-only"),
]

# Store classes that would connect on construction (and need psycopg or
# boto3). Each module is swapped for one holding a recording spy.
_SPIED_STORES = {
    "trellis.stores.postgres.trace": "PostgresTraceStore",
    "trellis.stores.postgres.event_log": "PostgresEventLog",
    "trellis.stores.postgres.document": "PostgresDocumentStore",
    "trellis.stores.postgres.graph": "PostgresGraphStore",
    "trellis.stores.pgvector.store": "PgVectorStore",
    "trellis.stores.s3.blob": "S3BlobStore",
}

# Set alongside the listed vars and read by no store: if its value is ever
# "received", the collector is reading the environment, not the stores.
_DECOY_ENV = "TRELLIS_TB_DECOY"


class _SpyStore:
    """Records its constructor kwargs where ``vars()`` will find them."""

    def __init__(self, **kwargs: Any) -> None:
        self.received = kwargs

    def close(self) -> None:
        pass


def _env_value(var: str) -> str:
    """A value unique to ``var``, shaped like what its reader expects."""
    token = "tb-" + var.lower().replace("_", "-")
    if var.endswith("_URI"):
        return f"bolt://{token}:7687"
    if var.endswith("_DSN"):
        return f"postgresql://{token}/db"
    if var.endswith("_URL"):
        return f"http://{token}:2480"
    return token


def _record_bolt_entry_points(monkeypatch: pytest.MonkeyPatch) -> list[MagicMock]:
    """Record the neo4j / ArcadeDB connection calls instead of dialling them."""
    pytest.importorskip("neo4j")
    from trellis.stores.arcadedb import graph as arcadedb_graph
    from trellis.stores.arcadedb import vector as arcadedb_vector
    from trellis.stores.neo4j import base as neo4j_base
    from trellis.stores.neo4j import graph as neo4j_graph
    from trellis.stores.neo4j import vector as neo4j_vector

    graph_database = MagicMock(name="GraphDatabase")
    build_arcadedb_driver = MagicMock(name="build_arcadedb_driver")
    ensure_database = MagicMock(name="ensure_database")
    provenance_ddl = MagicMock(name="edge_provenance_schema")
    monkeypatch.setattr(neo4j_base, "GraphDatabase", graph_database)
    monkeypatch.setattr(arcadedb_graph, "build_arcadedb_driver", build_arcadedb_driver)
    monkeypatch.setattr(arcadedb_graph, "ensure_database", ensure_database)
    monkeypatch.setattr(
        arcadedb_graph.ArcadeDBGraphStore,
        "_init_arcadedb_edge_provenance_schema",
        provenance_ddl,
    )
    for store_cls in (
        neo4j_graph.Neo4jGraphStore,
        neo4j_vector.Neo4jVectorStore,
        arcadedb_graph.ArcadeDBGraphStore,
        arcadedb_vector.ArcadeDBVectorStore,
    ):
        monkeypatch.setattr(store_cls, "_init_schema", lambda self, **_: None)
    return [
        graph_database.driver,
        build_arcadedb_driver,
        ensure_database,
        provenance_ddl,
    ]


def _received_strings(*sources: object) -> set[str]:
    """Every string inside what the stores and connection calls were handed.

    Deliberately never reads ``os.environ``: a value counts as delivered
    only if it reached a store's attributes or a connection entry point.
    """
    found: set[str] = set()
    seen: set[int] = set()
    stack = list(sources)
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.add(item)
        elif id(item) in seen:
            continue
        elif isinstance(item, Mapping):
            seen.add(id(item))
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            seen.add(id(item))
            stack.extend(item)
    return found


@pytest.mark.parametrize(("header", "floor", "needs_bolt"), _SHIPPED_BLOCKS)
def test_shipped_block_delivers_documented_env(
    header: str,
    floor: int,
    needs_bolt: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block pasted verbatim, plus exactly the env it lists, reaches the
    stores: every listed value arrives where a store or driver reads it."""
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    block_yaml = _block_yaml(text, header)
    required = _required_env(text, header)

    # config.yaml is read with yaml.safe_load, which expands nothing: a
    # ``${VAR}`` placeholder reaches the store as that literal string.
    placeholders = [line.strip() for line in block_yaml.splitlines() if "${" in line]
    assert not placeholders, f"{header}: config.yaml never expands {placeholders}"

    for var in [key for key in os.environ if key.startswith("TRELLIS_")]:
        monkeypatch.delenv(var)
    for var in [*required, _DECOY_ENV]:
        monkeypatch.setenv(var, _env_value(var))
    for module_name, class_name in _SPIED_STORES.items():
        module = types.ModuleType(module_name)
        setattr(module, class_name, type(class_name, (_SpyStore,), {}))
        monkeypatch.setitem(sys.modules, module_name, module)
    entry_points = _record_bolt_entry_points(monkeypatch) if needs_bolt else []

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(block_yaml, encoding="utf-8")
    registry = StoreRegistry.from_config_dir(config_dir, tmp_path / "data")
    stores: list[Any] = []
    try:
        for plane, entries in yaml.safe_load(block_yaml).items():
            for store_type, entry in entries.items():
                if entry["backend"] in {"sqlite", "local"}:
                    continue
                accessor = (
                    "event_log" if store_type == "event_log" else f"{store_type}_store"
                )
                stores.append(getattr(getattr(registry, plane), accessor))
    finally:
        registry.close()

    received = _received_strings(
        [vars(store) for store in stores],
        [mock.call_args_list for mock in entry_points],
    )
    missing = [var for var in required if _env_value(var) not in received]
    assert not missing, f"{header}: listed, but no store received it: {missing}"
    assert _env_value(_DECOY_ENV) not in received, "the collector read the environment"
    assert len(required) >= floor, (
        f"{header}: found {len(required)} required env vars, hand count is {floor}"
    )


# ---------------------------------------------------------------------------
# Live smoke against the Neo4j local-default shape (env-gated)
# ---------------------------------------------------------------------------

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

ARCADE_URI = os.environ.get("TRELLIS_TEST_ARCADEDB_URI", "")
ARCADE_USER = os.environ.get("TRELLIS_TEST_ARCADEDB_USER", "root")
ARCADE_PASSWORD = os.environ.get("TRELLIS_TEST_ARCADEDB_PASSWORD", "")
ARCADE_DATABASE = os.environ.get(
    "TRELLIS_TEST_ARCADEDB_DATABASE", "trellis_recommended_smoke"
)
ARCADE_HTTP_URL = os.environ.get(
    "TRELLIS_TEST_ARCADEDB_HTTP_URL", "http://localhost:2480"
)


@pytest.mark.live
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


# ---------------------------------------------------------------------------
# Live smoke against the ArcadeDB blessed shape (env-gated)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.arcadedb
@pytest.mark.skipif(not ARCADE_URI, reason="TRELLIS_TEST_ARCADEDB_URI not set")
def test_arcadedb_blessed_shape_validates_end_to_end(tmp_path: Path) -> None:
    """Construct the blessed ArcadeDB-everywhere registry and exercise
    the full ``validate(check_connectivity=True)`` path against a live
    instance.

    Covers:

    - Both graph and vector backends instantiating against the same
      ArcadeDB database.
    - Driver sharing across the graph backend (Bolt) — vector uses
      SQL-via-HTTP, so it doesn't enter the Bolt driver cache; the
      connectivity check pings only Bolt drivers and that's fine.
    - SQLite operational plane stores instantiating cleanly alongside.

    Run with the local Docker setup from
    ``docs/agent-guide/testing.md``::

        TRELLIS_TEST_ARCADEDB_URI=bolt://localhost:7687
        TRELLIS_TEST_ARCADEDB_USER=root
        TRELLIS_TEST_ARCADEDB_PASSWORD=playwithdata
        TRELLIS_TEST_ARCADEDB_HTTP_URL=http://localhost:2480
    """
    from trellis.stores.registry import StoreRegistry

    config = {
        "graph": {
            "backend": "arcadedb",
            "uri": ARCADE_URI,
            "user": ARCADE_USER,
            "password": ARCADE_PASSWORD,
            "database": ARCADE_DATABASE,
            "http_url": ARCADE_HTTP_URL,
        },
        "vector": {
            "backend": "arcadedb",
            "http_url": ARCADE_HTTP_URL,
            "user": ARCADE_USER,
            "password": ARCADE_PASSWORD,
            "database": ARCADE_DATABASE,
            "dimensions": 3,
            "index_name": "trellis_recommended_smoke_emb",
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
        registry.validate(
            store_types=list(config.keys()),
            check_connectivity=True,
        )
        # Smoke: round-trip a node + an embedding through both stores
        # to confirm cross-plane visibility (graph writes via Cypher,
        # vector reads via SQL).
        graph = registry.knowledge.graph_store
        vector = registry.knowledge.vector_store
        node_id = graph.upsert_node("smoke-1", "doc", {"k": "v"})
        vector.upsert(node_id, [0.1, 0.2, 0.3], metadata={"src": "smoke"})
        fetched = vector.get(node_id)
        assert fetched is not None
        assert fetched["item_id"] == node_id
        assert fetched["dimensions"] == 3
    finally:
        registry.close()
