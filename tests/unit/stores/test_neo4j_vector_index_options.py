"""Unit-level tests for the CREATE VECTOR INDEX OPTIONS map.

Lives separate from ``test_neo4j_vector.py`` because that file is
``pytestmark``-gated on a live AuraDB connection. This module asserts on
the *generated Cypher* via an injected mock driver, so it runs as part
of the default unit suite — no live Neo4j required.

The constraint that motivates this approach: AuraDB allows only one
vector index per ``(:label, property)`` pair. A live test that creates
``trellis_test_hnsw_options`` collides with the persistent
``trellis_test_node_embeddings`` index, and the ``CREATE ... IF NOT
EXISTS`` silently no-ops. Trying to verify HNSW knobs end-to-end on a
shared instance is a dead end without dropping and recreating the
shared index — which would slow every other live test by 30+ seconds
each.

What we *do* assert here: the OPTIONS map embeds the right keys and
the right literal values (``true`` / ``false`` for quantization, raw
ints for ``m`` / ``ef_construction``, the dimension and similarity
literals from the constructor). That's the contract — Neo4j's own
behaviour given those options is not Trellis's to test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("neo4j")

_DUMMY_PASSWORD = "test-pw"  # noqa: S105 — placeholder for store construction


def _make_store_capturing_init_cypher(
    monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> str:
    """Construct a Neo4jVectorStore with a mock driver, return the CREATE Cypher.

    Stubs ``wait_for_vector_index_online`` so we don't poll a fake
    index forever. The ``session.run`` call inside ``_init_schema`` is
    captured via the MagicMock, and its first positional arg is the
    Cypher string the test asserts on.
    """
    monkeypatch.setattr(
        "trellis.stores.neo4j.vector.wait_for_vector_index_online",
        lambda *_, **__: None,
    )

    from trellis.stores.neo4j.vector import Neo4jVectorStore

    driver = MagicMock(name="driver")
    session = MagicMock(name="session")
    driver.session.return_value.__enter__.return_value = session

    Neo4jVectorStore("bolt://x", user="u", driver=driver, **kwargs)

    session.run.assert_called_once()
    return str(session.run.call_args.args[0])


class TestIndexOptionsCypher:
    def test_default_options_emit_quantization_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cypher = _make_store_capturing_init_cypher(monkeypatch, dimensions=8)
        assert "CREATE VECTOR INDEX trellis_node_embeddings IF NOT EXISTS" in cypher
        assert "`vector.dimensions`: 8" in cypher
        assert "`vector.similarity_function`: 'cosine'" in cypher
        assert "`vector.hnsw.m`: 16" in cypher
        assert "`vector.hnsw.ef_construction`: 100" in cypher
        # The headline assertion: Trellis ships quantization=False by
        # default to protect recall on low-dim vectors. Measured on
        # AuraDB Free 2026-04-27: recall@10 = 0.36 with quantization
        # enabled vs ~1.0 with it disabled (dim=16, 200 embeddings).
        assert "`vector.quantization.enabled`: false" in cypher

    def test_explicit_quantization_true_emits_true_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cypher = _make_store_capturing_init_cypher(
            monkeypatch, dimensions=8, quantization=True
        )
        assert "`vector.quantization.enabled`: true" in cypher

    def test_custom_hnsw_knobs_flow_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cypher = _make_store_capturing_init_cypher(
            monkeypatch,
            dimensions=8,
            m=32,
            ef_construction=200,
        )
        assert "`vector.hnsw.m`: 32" in cypher
        assert "`vector.hnsw.ef_construction`: 200" in cypher

    def test_similarity_choice_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cypher = _make_store_capturing_init_cypher(
            monkeypatch, dimensions=8, similarity="euclidean"
        )
        assert "`vector.similarity_function`: 'euclidean'" in cypher

    def test_index_name_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cypher = _make_store_capturing_init_cypher(
            monkeypatch, dimensions=8, index_name="custom_idx"
        )
        assert "CREATE VECTOR INDEX custom_idx IF NOT EXISTS" in cypher


class TestConstructorRejectsBadHnswKnobs:
    def test_zero_m_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match=r"^m must be > 0"):
            Neo4jVectorStore(
                "bolt://x",
                user="u",
                password=_DUMMY_PASSWORD,
                dimensions=8,
                m=0,
            )

    def test_negative_ef_construction_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="ef_construction must be > 0"):
            Neo4jVectorStore(
                "bolt://x",
                user="u",
                password=_DUMMY_PASSWORD,
                dimensions=8,
                ef_construction=-1,
            )

    def test_validation_runs_before_driver_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad knobs should raise before any network call to build_driver."""
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with patch("trellis.stores.neo4j.vector.build_driver") as mock_build:
            with pytest.raises(ValueError):
                Neo4jVectorStore(
                    "bolt://x",
                    user="u",
                    password=_DUMMY_PASSWORD,
                    dimensions=8,
                    m=0,
                )
            mock_build.assert_not_called()
