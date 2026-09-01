"""The two document-count surfaces must be reconcilable by a reader (#412).

``GET /api/v1/stats`` and ``GET /api/v1/documents`` are both operator
surfaces and both once carried a field called "documents" that meant
different things — 1,319 against 579 on the reference deployment, because
the listing had counted its ``total`` under ``include_chunks`` since
#385/#391 while stats reported the raw store total. Neither number was
wrong; the field names simply did not say which population each described.

The fix reports both populations under names that do say. What keeps them
honest is this module, and it has to be a behavioural test rather than a
scan: ``tests/unit/test_chunk_visibility_rule.py`` can see whether a call
*names* ``include_chunks``, but "does this number agree with that number?"
is not a property an AST walk can check. Both stats surfaces are covered —
the REST endpoint here and ``trellis admin stats`` below — because #412's
resolution is that the two agree with each other as well as with the
listing.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import trellis_api.app as app_module
from tests.chunk_corpus import seed_chunked
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin, explore

#: Parents and chunks-per-parent for the seeded corpus. Chosen so the two
#: populations differ (12 rows, 3 documents) — equal counts would let a
#: regression that reported the same number twice pass.
_PARENTS = 3
_PER_PARENT = 3


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """One client mounting *both* surfaces — the point is the comparison."""

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    app.include_router(explore.router, prefix="/api/v1", tags=["explore"])
    with TestClient(app) as c:
        yield c


def test_stats_documents_reconciles_with_the_listing(client, registry):
    """``stats.documents`` is the listing's default ``total``."""
    seed_chunked(
        registry.knowledge.document_store, parents=_PARENTS, per_parent=_PER_PARENT
    )
    stats = client.get("/api/v1/stats").json()
    listing = client.get("/api/v1/documents").json()

    assert stats["documents"] == listing["total"] == _PARENTS
    assert listing["include_chunks"] is False


def test_stats_document_rows_reconciles_with_the_chunked_listing(client, registry):
    """``stats.document_rows`` is the listing's ``include_chunks=true`` total.

    This is the number that used to be reported as ``documents``. Keeping
    it addressable is why #412 was resolved by naming both populations
    rather than by making stats exclude chunks: an operator sizing a
    corpus or sanity-checking a prune wants the physical row count.
    """
    seed_chunked(
        registry.knowledge.document_store, parents=_PARENTS, per_parent=_PER_PARENT
    )
    stats = client.get("/api/v1/stats").json()
    listing = client.get("/api/v1/documents?include_chunks=true").json()

    expected = _PARENTS * (1 + _PER_PARENT)
    assert stats["document_rows"] == listing["total"] == expected
    assert stats["document_rows"] > stats["documents"]


def test_stats_counts_agree_on_a_corpus_with_no_chunks(client, registry):
    """With no chunk rows the two populations coincide, and must.

    A reader reconciling the two fields needs the difference to be the
    chunk rows and nothing else — an off-by-one predicate that happened to
    look right on a chunked corpus shows up here.
    """
    store = registry.knowledge.document_store
    for i in range(4):
        store.put(f"doc{i}", f"body {i}")

    stats = client.get("/api/v1/stats").json()
    assert stats["documents"] == stats["document_rows"] == 4


def test_cli_stats_matches_the_rest_fields(client, registry, tmp_path, monkeypatch):
    """``trellis admin stats --format json`` reports the same two keys.

    The CLI is the second stats surface #412 names. It reads its own
    registry rather than the API's, so the assertion is on the key set and
    on the same chunk arithmetic, not on the API's numbers.
    """
    from trellis_cli.admin import admin_app
    from trellis_cli.stores import _reset_registry

    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cli-config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "cli-data"))
    _reset_registry()

    result = CliRunner().invoke(admin_app, ["init"])
    assert result.exit_code == 0

    from trellis_cli.stores import get_document_store

    seed_chunked(get_document_store(), parents=_PARENTS, per_parent=_PER_PARENT)

    result = CliRunner().invoke(admin_app, ["stats", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    rest = set(client.get("/api/v1/stats").json())
    assert {"documents", "document_rows"} <= set(payload) & rest
    assert payload["documents"] == _PARENTS
    assert payload["document_rows"] == _PARENTS * (1 + _PER_PARENT)
