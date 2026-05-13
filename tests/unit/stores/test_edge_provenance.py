"""Tests for the shared edge-provenance validator and SQLite round-trip.

The validator (``trellis.stores.base.edge_provenance``) is the single
source of truth that every graph backend defers to at the store
boundary. The schema layer (:class:`trellis.schemas.graph.Edge`) is the
Pydantic equivalent; this module pins down the identical contract for
backends that bypass Pydantic on the hot path.

SQLite round-trip tests live in this file too because the WIP commit
landed the SQLite write path without dedicated coverage — adding it
here keeps the per-backend pattern consistent with Neo4j and ArcadeDB.
Postgres + Neo4j + ArcadeDB live tests are env-gated in their own files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.stores.base.edge_provenance import (
    EDGE_PROVENANCE_FIELDS,
    extract_edge_provenance,
    validate_edge_provenance,
)
from trellis.stores.sqlite.graph import SQLiteGraphStore

# ---------------------------------------------------------------------------
# Helper module
# ---------------------------------------------------------------------------


class TestValidateEdgeProvenance:
    """The five-field validator — confidence range + extractor_tier allowlist."""

    def test_all_none_passes(self) -> None:
        # The happy path: callers that don't supply provenance opt
        # every field into ``None`` and the validator returns silently.
        validate_edge_provenance()

    def test_all_fields_populated_passes(self) -> None:
        validate_edge_provenance(
            source_trace_id="tr_abc",
            agent_id="agent-1",
            confidence=0.75,
            evidence_ref="ev-9",
            extractor_tier="DETERMINISTIC",
        )

    @pytest.mark.parametrize("conf", [0.0, 0.5, 1.0])
    def test_confidence_boundaries_accepted(self, conf: float) -> None:
        validate_edge_provenance(confidence=conf)

    @pytest.mark.parametrize("conf", [-0.0001, 1.0001, 1.5, -1.0])
    def test_confidence_out_of_range_raises(self, conf: float) -> None:
        with pytest.raises(ValueError, match="confidence must be in"):
            validate_edge_provenance(confidence=conf)

    def test_confidence_bool_rejected(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python; the validator
        # explicitly rejects it so a ``confidence=True`` typo doesn't
        # silently land as 1.0.
        with pytest.raises(TypeError, match="confidence must be"):
            validate_edge_provenance(confidence=True)  # type: ignore[arg-type]

    def test_confidence_non_numeric_rejected(self) -> None:
        with pytest.raises(TypeError, match="confidence must be"):
            validate_edge_provenance(confidence="0.5")  # type: ignore[arg-type]

    @pytest.mark.parametrize("tier", ["DETERMINISTIC", "HYBRID", "LLM"])
    def test_extractor_tier_allowlist_accepts_canonical(self, tier: str) -> None:
        validate_edge_provenance(extractor_tier=tier)

    def test_extractor_tier_outside_allowlist_raises(self) -> None:
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            validate_edge_provenance(extractor_tier="MAGIC")

    def test_extractor_tier_case_sensitive(self) -> None:
        # The validator is intentionally case-sensitive — retrieval
        # queries compare strings, and lowercasing 'llm' would silently
        # diverge from canonical normalization elsewhere.
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            validate_edge_provenance(extractor_tier="deterministic")

    def test_extractor_tier_non_string_rejected(self) -> None:
        with pytest.raises(TypeError, match="extractor_tier must be"):
            validate_edge_provenance(extractor_tier=42)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field",
        ["source_trace_id", "agent_id", "evidence_ref"],
    )
    def test_opaque_str_fields_reject_non_str(self, field: str) -> None:
        with pytest.raises(TypeError, match=f"{field} must be"):
            validate_edge_provenance(**{field: 99})  # type: ignore[arg-type]


class TestExtractEdgeProvenance:
    """Pulling provenance kwargs out of a bulk spec dict."""

    def test_full_spec_extracted(self) -> None:
        spec = {
            "source_trace_id": "tr_1",
            "agent_id": "a-1",
            "confidence": 0.42,
            "evidence_ref": "ev-1",
            "extractor_tier": "HYBRID",
            "ignored_extra": "value",  # extra keys silently dropped
        }
        result = extract_edge_provenance(spec)
        assert result == {
            "source_trace_id": "tr_1",
            "agent_id": "a-1",
            "confidence": 0.42,
            "evidence_ref": "ev-1",
            "extractor_tier": "HYBRID",
        }

    def test_partial_spec_fills_nones(self) -> None:
        result = extract_edge_provenance({"confidence": 0.5})
        assert result["confidence"] == 0.5
        for field in EDGE_PROVENANCE_FIELDS:
            if field != "confidence":
                assert result[field] is None

    def test_none_spec_returns_all_nones(self) -> None:
        result = extract_edge_provenance(None)
        assert result == dict.fromkeys(EDGE_PROVENANCE_FIELDS)


# ---------------------------------------------------------------------------
# SQLite round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path: Path):
    store = SQLiteGraphStore(tmp_path / "graph.db")
    yield store
    store.close()


def _seed_endpoints(store: SQLiteGraphStore) -> None:
    store.upsert_node("a", "service", {})
    store.upsert_node("b", "service", {})


class TestSQLiteProvenanceRoundTrip:
    def test_upsert_edge_with_full_provenance_roundtrips(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        _seed_endpoints(graph_store)
        graph_store.upsert_edge(
            "a",
            "b",
            "depends_on",
            source_trace_id="tr_42",
            agent_id="agent-7",
            confidence=0.83,
            evidence_ref="doc-9",
            extractor_tier="HYBRID",
        )
        edges = graph_store.get_edges("a", direction="outgoing")
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source_trace_id"] == "tr_42"
        assert edge["agent_id"] == "agent-7"
        assert edge["confidence"] == pytest.approx(0.83)
        assert edge["evidence_ref"] == "doc-9"
        assert edge["extractor_tier"] == "HYBRID"

    def test_upsert_edge_without_provenance_reads_back_none(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        _seed_endpoints(graph_store)
        graph_store.upsert_edge("a", "b", "depends_on", {"w": 1.0})
        edges = graph_store.get_edges("a", direction="outgoing")
        assert len(edges) == 1
        edge = edges[0]
        for field in EDGE_PROVENANCE_FIELDS:
            assert edge[field] is None, (
                f"expected None for {field}, got {edge[field]!r}"
            )

    def test_upsert_edge_bad_confidence_raises_before_write(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        _seed_endpoints(graph_store)
        with pytest.raises(ValueError, match="confidence must be in"):
            graph_store.upsert_edge(
                "a",
                "b",
                "depends_on",
                confidence=1.5,
            )
        # Edge was not written.
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_upsert_edge_bad_tier_raises_before_write(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        _seed_endpoints(graph_store)
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            graph_store.upsert_edge(
                "a",
                "b",
                "depends_on",
                extractor_tier="MAGIC",
            )
        assert graph_store.get_edges("a", direction="outgoing") == []

    def test_partial_provenance_other_fields_remain_none(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        # Confidence alone — the other four columns stay NULL and read
        # back as None. Real callers in the extraction pipeline mostly
        # populate a subset of fields (e.g. tier + confidence from a
        # deterministic rule), so this is the common shape.
        _seed_endpoints(graph_store)
        graph_store.upsert_edge(
            "a",
            "b",
            "depends_on",
            confidence=0.9,
            extractor_tier="DETERMINISTIC",
        )
        edge = graph_store.get_edges("a", direction="outgoing")[0]
        assert edge["confidence"] == pytest.approx(0.9)
        assert edge["extractor_tier"] == "DETERMINISTIC"
        assert edge["source_trace_id"] is None
        assert edge["agent_id"] is None
        assert edge["evidence_ref"] is None

    def test_upsert_edge_update_carries_new_provenance(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        # Re-upserting the same (source, target, type) triplet closes
        # the prior version and starts a new one with the supplied
        # provenance. Re-reading returns the most recent values.
        _seed_endpoints(graph_store)
        graph_store.upsert_edge(
            "a", "b", "depends_on", confidence=0.5, extractor_tier="DETERMINISTIC"
        )
        graph_store.upsert_edge(
            "a", "b", "depends_on", confidence=0.95, extractor_tier="LLM"
        )
        edges = graph_store.get_edges("a", direction="outgoing")
        assert len(edges) == 1  # latest only
        edge = edges[0]
        assert edge["confidence"] == pytest.approx(0.95)
        assert edge["extractor_tier"] == "LLM"


class TestSQLiteBulkProvenanceRoundTrip:
    def test_bulk_writes_provenance_per_row(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        graph_store.upsert_node("c", "s", {})
        graph_store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "edge_type": "links_to",
                    "confidence": 0.4,
                    "extractor_tier": "DETERMINISTIC",
                    "agent_id": "agent-x",
                },
                {
                    "source_id": "a",
                    "target_id": "c",
                    "edge_type": "links_to",
                    # No provenance on this row.
                },
            ]
        )
        edges = sorted(
            graph_store.get_edges("a", direction="outgoing"),
            key=lambda e: e["target_id"],
        )
        assert edges[0]["target_id"] == "b"
        assert edges[0]["confidence"] == pytest.approx(0.4)
        assert edges[0]["agent_id"] == "agent-x"
        assert edges[1]["target_id"] == "c"
        assert edges[1]["confidence"] is None
        assert edges[1]["extractor_tier"] is None

    def test_bulk_bad_confidence_raises_with_row_index(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        with pytest.raises(ValueError, match=r"upsert_edges_bulk\[0\]"):
            graph_store.upsert_edges_bulk(
                [
                    {
                        "source_id": "a",
                        "target_id": "b",
                        "edge_type": "links_to",
                        "confidence": 2.0,
                    }
                ]
            )

    def test_bulk_bad_tier_raises_with_row_index(
        self, graph_store: SQLiteGraphStore
    ) -> None:
        graph_store.upsert_node("a", "s", {})
        graph_store.upsert_node("b", "s", {})
        with pytest.raises(ValueError, match=r"upsert_edges_bulk\[0\]"):
            graph_store.upsert_edges_bulk(
                [
                    {
                        "source_id": "a",
                        "target_id": "b",
                        "edge_type": "links_to",
                        "extractor_tier": "INVALID",
                    }
                ]
            )
