"""Fixtures for integration tests against a real Neo4j instance.

These tests prove the cross-store wiring (graph + vector + document +
event log + mutation pipeline + retrieval) works end-to-end, not just
the GraphStore ABC in isolation. They sit one layer above the
``tests/unit/stores/contracts/`` suites which only exercise a single
store at a time.

All fixtures here skip cleanly when ``TRELLIS_TEST_NEO4J_URI`` is unset.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")

# Why: only require the neo4j package when an instance URI is configured. The
# per-fixture `pytest.skip(...)` calls below cover the "no URI" case; gating
# the importorskip behind URI lets sibling integration tests (e.g. cli/) collect
# cleanly on machines without the neo4j driver installed.
if URI:
    pytest.importorskip("neo4j")
# AuraDB Free hosts user data under the instance ID, not under the
# canonical "neo4j" name. Match the unit-suite default.
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

# Why: `pytest.mark.neo4j` lets the suite filter neo4j-dependent tests by
# marker, but the skipif on URI lives inside each fixture body rather than
# at module scope. A module-level skipif on `pytestmark` propagates through
# sub-directory conftests (e.g. ``tests/integration/cli/``) and would skip
# CLI tests that never touch Neo4j when the env var is unset. Per-fixture
# skip means: tests that pull ``registry``/``executor`` skip cleanly; tests
# that don't (cli/, api/ minus live_api_server, etc.) collect and run.
pytestmark = [pytest.mark.neo4j]


#: Vector index name matching the production default on AuraDB.
#:
#: Neo4j allows only one vector index per ``(label, property)`` pair, so
#: a second index on ``(:Node, embedding)`` under a different name is
#: silently rejected (``CREATE ... IF NOT EXISTS`` returns success but
#: never appears in ``SHOW INDEXES``). The shared AuraDB instance was
#: provisioned with the production-default name ``trellis_node_embeddings``,
#: and an outside-in test of the deployment shape should exercise the
#: same name production uses anyway. Dimensions must match the existing
#: index — provisioned at 3 and that's what AuraDB has on disk today.
INTEGRATION_VECTOR_INDEX = "trellis_node_embeddings"
INTEGRATION_VECTOR_DIMS = 3


def _wipe_neo4j() -> None:
    """Drop everything the Trellis stores might have created.

    Both stores share the same ``:Node`` rows under the shape #2 layout,
    so a single ``DETACH DELETE`` on Node + Alias clears state for both.

    The vector index is NOT dropped between tests: AuraDB provisions
    vector indexes asynchronously, so an immediate query after a fresh
    ``CREATE`` can race ahead of materialisation and fail with "no
    such vector schema index". Sharing the unit-suite's index
    (``INTEGRATION_VECTOR_INDEX``) at pinned dimensions makes
    ``CREATE ... IF NOT EXISTS`` a true no-op across runs.
    """
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        with driver.session(database=DATABASE) as session:
            session.run("MATCH (n) WHERE n:Node OR n:Alias DETACH DELETE n")
    finally:
        driver.close()


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[Any]:
    """A ``StoreRegistry`` wired to Neo4j (graph+vector) and SQLite for the rest.

    Knowledge plane:
      - graph: Neo4jGraphStore (live AuraDB / docker)
      - vector: Neo4jVectorStore (shape #2 — embeddings on the same :Node rows)
      - document: SQLite (tmp_path)
      - blob: local (tmp_path)

    Operational plane:
      - trace, event_log: SQLite (tmp_path)

    The Neo4j database is wiped before yield so each test starts clean.
    Embedding dimensions are pinned to ``INTEGRATION_VECTOR_DIMS`` to
    match the shared vector index — see that constant's docstring for
    the rationale.

    Skipping lives inside the fixture body (not on a module-level
    ``pytestmark``) so that sibling integration tests under
    ``tests/integration/cli/`` etc., which don't pull this fixture,
    still collect and run when the live-Neo4j URI is unset. A future
    test that *does* request ``registry`` without the env set will see
    a clean skip instead of a connection error.
    """
    if not URI:
        pytest.skip("TRELLIS_TEST_NEO4J_URI not set")

    from trellis.stores.registry import StoreRegistry

    _wipe_neo4j()

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
    }

    reg = StoreRegistry(config=config, stores_dir=tmp_path / "stores")

    # Force vector store instantiation now (which runs the idempotent
    # CREATE VECTOR INDEX) and then block until AuraDB reports every
    # index online. Vector index provisioning is asynchronous on Aura;
    # without this wait the first ``query`` after a brand-new index
    # races ahead and fails with "no such vector schema index". 60s is
    # well above observed materialisation time and bounded so a hung
    # provision still surfaces as a test failure rather than a hang.
    _ = reg.knowledge.vector_store
    _await_neo4j_indexes()

    try:
        yield reg
    finally:
        reg.close()


def _await_neo4j_indexes(timeout_seconds: int = 60) -> None:
    """Block until all schema indexes on the AuraDB instance are online."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        with driver.session(database=DATABASE) as session:
            session.run("CALL db.awaitIndexes($t)", t=timeout_seconds).consume()
    finally:
        driver.close()


@pytest.fixture(scope="session")
def neo4j_vector_search_supported() -> bool:
    """Whether the connected Neo4j can run the vector store's ``SEARCH`` query.

    :class:`Neo4jVectorStore.query` uses the Cypher ``SEARCH ... VECTOR INDEX``
    clause. AuraDB supports it; a self-hosted Docker instance (community *or*
    enterprise, through at least 2025.12) does not — it raises
    ``51N26 'not supported in this version'`` — and a Cypher-5-default server
    rejects the keyword at parse time (``Invalid input 'SEARCH'``). The probe
    mirrors the production query exactly (no ``CYPHER 25`` prefix) against a
    throwaway index name, so it is a faithful "can the real query run here?"
    check: an index-resolution error means SEARCH is available, while a
    feature/parse error means it is not. Vector-search tests skip when this is
    ``False`` so the containerized live-infra CI stays green and hermetic.
    """
    if not URI:
        return False
    from neo4j import GraphDatabase

    probe = (
        "MATCH (n:Node) SEARCH n IN ( VECTOR INDEX __vs_probe__ "
        "FOR [0.0] LIMIT 1 ) SCORE AS s RETURN n LIMIT 0"
    )
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        with driver.session(database=DATABASE) as session:
            try:
                session.run(probe).consume()
            except Exception as exc:
                # Classify any driver/Cypher failure: a feature/parse error
                # means SEARCH is unavailable here; anything else (e.g. a
                # missing-index error) means it is available.
                msg = str(exc)
                unsupported = (
                    "51N26" in msg
                    or "not available in this implementation" in msg
                    or "Invalid input 'SEARCH'" in msg
                )
                return not unsupported
            else:
                return True
    finally:
        driver.close()


@pytest.fixture
def executor(registry: Any) -> Any:
    """A ``MutationExecutor`` wired to the integration ``registry``.

    Has the standard curate handlers (ENTITY_CREATE, LINK_CREATE, etc.)
    registered and emits to the event log so tests can assert on the
    audit trail.
    """
    from trellis.mutate.executor import MutationExecutor
    from trellis.mutate.handlers import create_curate_handlers

    return MutationExecutor(
        event_log=registry.operational.event_log,
        handlers=create_curate_handlers(registry),
    )
