"""The other acceptance criterion: a trace written today is semantically
retrievable after one worker pass.

Asserted against the real :class:`~trellis.retrieve.strategies.SemanticSearch`
over a real SQLite vector store, not against the worker's own summary — a
report saying ``embedded: 1`` is the weakest available evidence that anything
became retrievable.

The embedder here is a token-hash bag of words rather than the length-derived
stub the other tests use, so "similar" means lexically similar and the test can
assert *ranking*, not just presence. A trace that is merely present in the
index but never outranks an unrelated one is not reachable in any sense a pack
consumer cares about, since every axis is truncated by ``limit_per_strategy``
and then again by the pack budget.

The baseline is measured in the same test rather than asserted from memory:
:meth:`test_semantic_axis_returns_nothing_before_the_pass` runs the same query
against the same store before the worker runs, so the "0 → 1" claim is a
before/after in one file.
"""

from __future__ import annotations

import hashlib
import math

import pytest

from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis_workers.trace_embed import (
    run_trace_embed_pass,
    trace_summary_doc_id,
)

from .conftest import make_trace

EMBED_DIMS = 96


def hashing_embed(text: str) -> list[float]:
    """L2-normalised token-hash bag of words. Deterministic, no dependencies."""
    vec = [0.0] * EMBED_DIMS
    for token in text.lower().split():
        clean = "".join(ch for ch in token if ch.isalnum())
        if not clean:
            continue
        digest = hashlib.sha256(clean.encode("utf-8")).digest()
        vec[digest[0] % EMBED_DIMS] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture
def semantic_registry(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from trellis_cli.admin import admin_app
    from trellis_cli.stores import _get_registry, _reset_registry

    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "TRELLIS_EMBEDDING_FN",
        "tests.unit.workers.trace_embed.test_retrievability.hashing_embed",
    )
    init = CliRunner().invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()
    yield _get_registry()
    _reset_registry()


TARGET_INTENT = "Fix the pgvector contract fixture so the suite actually executes"
TARGET_SUMMARY = (
    "The pgvector contract suite had never run anywhere: its fixture asked for "
    "a connection the container had not provisioned, so every test errored at "
    "setup and the collector reported them as skipped."
)
DECOY_INTENT = "Plan the family calendar wall display for the kitchen"
DECOY_SUMMARY = (
    "Chose Home Assistant with a Lovelace dashboard so the entity allowlist "
    "stays below the model, and wired the Raspberry Pi kiosk to it."
)


def _seed(registry):
    target = make_trace(1, intent=TARGET_INTENT, summary=TARGET_SUMMARY)
    decoy = make_trace(2, intent=DECOY_INTENT, summary=DECOY_SUMMARY, domain="home")
    for trace in (target, decoy):
        registry.operational.trace_store.append(trace)
    return target, decoy


class TestSemanticReachability:
    def test_semantic_axis_returns_nothing_before_the_pass(
        self, semantic_registry, tmp_path
    ) -> None:
        """The measured baseline. ``save_experience`` writes a trace and only a
        trace — no document row, no vector row — so the axis that reads the
        vector store has nothing of it to return."""
        _seed(semantic_registry)
        strategy = SemanticSearch(
            semantic_registry.knowledge.vector_store, hashing_embed
        )
        assert strategy.search("pgvector contract fixture never ran", limit=10) == []

    def test_one_pass_makes_the_trace_the_top_semantic_hit(
        self, semantic_registry, tmp_path
    ) -> None:
        target, decoy = _seed(semantic_registry)

        report = run_trace_embed_pass(
            semantic_registry, watermark_path=tmp_path / "wm.json"
        )
        assert report.embedded == 2

        strategy = SemanticSearch(
            semantic_registry.knowledge.vector_store, hashing_embed
        )
        hits = strategy.search("pgvector contract fixture never executed", limit=10)

        ids = [h.item_id for h in hits]
        assert trace_summary_doc_id(target.trace_id) in ids
        assert ids[0] == trace_summary_doc_id(target.trace_id), (
            f"the matching trace should outrank the decoy; got {ids}"
        )
        top = hits[0]
        assert top.item_type == "vector"
        assert "pgvector contract suite had never run" in top.excerpt
        assert top.metadata["trace_id"] == target.trace_id
        assert top.metadata["document_form"] == "trace_summary"
        assert top.metadata["source_system"] == "trellis-trace"
        # The decoy is present too — this is an index, not a filter.
        assert trace_summary_doc_id(decoy.trace_id) in ids

    def test_the_keyword_axis_reaches_them_as_well(
        self, semantic_registry, tmp_path
    ) -> None:
        """A side effect worth naming: ``KeywordSearch`` reads the document
        store, so the derived rows land on that axis too. Traces were
        previously reachable on *neither* — the brief's "reachable by keyword"
        does not hold, because trace ingest writes no document."""
        target, _ = _seed(semantic_registry)
        keyword = KeywordSearch(semantic_registry.knowledge.document_store)
        assert keyword.search("pgvector", limit=10) == []

        run_trace_embed_pass(semantic_registry, watermark_path=tmp_path / "wm.json")
        hits = keyword.search("pgvector", limit=10)
        assert [h.item_id for h in hits] == [trace_summary_doc_id(target.trace_id)]

    def test_recency_uses_the_trace_timestamp_not_the_embed_time(
        self, semantic_registry, tmp_path
    ) -> None:
        """A backfilled trace must not masquerade as fresh: recency decay reads
        ``metadata['created_at']``, and ``build_vector_row`` only stamps *now*
        when the caller passes nothing. The worker passes the trace's own
        stamp, the way ``reindex-vectors`` passes the document's."""
        target, _ = _seed(semantic_registry)
        run_trace_embed_pass(semantic_registry, watermark_path=tmp_path / "wm.json")
        row = semantic_registry.knowledge.vector_store.get(
            trace_summary_doc_id(target.trace_id)
        )
        assert row["metadata"]["created_at"] == target.created_at.isoformat()
