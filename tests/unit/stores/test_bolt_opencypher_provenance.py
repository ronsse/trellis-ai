"""Unit-shape tests for the bolt_opencypher provenance write path.

These run unconditionally — no live Neo4j or ArcadeDB instance is
required. We mock the Bolt driver / session so the test exercises the
Python side of the write path: validator runs before any I/O, and the
edge property payload includes the five provenance fields in the
``$base_props`` / ``$rows`` parameters that ship over Bolt.

The full round-trip tests (write → read back → assert equality) live in
:file:`test_neo4j_graph.py` and :file:`test_arcadedb_graph.py`, gated
on real backends. This file is the fast feedback loop that catches
regressions in the shared base class without needing a server.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

pytest.importorskip("neo4j")

from trellis.stores.base.edge_provenance import EDGE_PROVENANCE_FIELDS
from trellis.stores.bolt_opencypher.graph import (
    BoltOpenCypherGraphStore,
    _edge_props_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_with_mocked_driver() -> BoltOpenCypherGraphStore:
    """Construct a BoltOpenCypherGraphStore wired to a mock driver.

    ``init_schema=False`` skips the DDL run so the constructor doesn't
    need a working session. The returned store has a ``MagicMock`` for
    ``self._driver`` — individual tests reach into it to inspect what
    Cypher / parameters got shipped.
    """
    store = BoltOpenCypherGraphStore.__new__(BoltOpenCypherGraphStore)
    store._driver = MagicMock()  # type: ignore[attr-defined]
    store._database = "test"
    store._owns_driver = False
    return store


def _capture_write_calls(
    store: BoltOpenCypherGraphStore,
    *,
    known_endpoint_ids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Wire the mock driver to record every (cypher, params) tx.run call.

    Returns the same list the test can inspect after invoking
    ``upsert_edge``. The mock's ``execute_write`` runs the supplied
    closure against a fake ``tx`` whose ``run`` records the call and
    returns a result whose ``single()`` returns a ``{"edge_id": ...}``
    mapping (so ``upsert_edge`` can return a string).

    ``known_endpoint_ids`` controls what
    :func:`_fetch_current_node_id_set` returns — bulk paths run an
    endpoint-validation read trip first, and tests that exercise the
    write path need that read to pretend the endpoints exist.
    """
    captured: list[dict[str, object]] = []
    endpoint_ids = known_endpoint_ids or set()

    fake_session = MagicMock()
    store._driver.session.return_value.__enter__.return_value = fake_session

    def _run(cypher, **params):
        captured.append({"cypher": cypher, "params": params})
        result = MagicMock()
        # ``upsert_edge`` reads ``record["edge_id"]`` off ``.single()``;
        # supply a mapping that satisfies subscript access.
        result.single.return_value = {"edge_id": "edge-xyz"}
        result.consume.return_value = None
        # ``_fetch_current_node_id_set`` materializes the result via
        # ``list(tx.run(...))`` and reads ``node_id`` off each row.
        # Return mappings for every id in ``known_endpoint_ids``.
        if "n.node_id IN $ids" in cypher and "RETURN n.node_id" in cypher:
            rows = [{"node_id": nid} for nid in endpoint_ids]
            result.__iter__ = lambda self_: iter(rows)
        else:
            result.__iter__ = lambda self_: iter([])
        return result

    def _execute(fn):
        fake_tx = MagicMock()
        fake_tx.run.side_effect = _run
        return fn(fake_tx)

    fake_session.execute_write.side_effect = _execute
    fake_session.execute_read.side_effect = _execute
    return captured


# ---------------------------------------------------------------------------
# upsert_edge — single-row Cypher carries provenance
# ---------------------------------------------------------------------------


class TestBoltUpsertEdgeShape:
    def test_provenance_keys_in_base_props(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        store.upsert_edge(
            "a",
            "b",
            "depends_on",
            source_trace_id="tr_1",
            agent_id="agent-1",
            confidence=0.42,
            evidence_ref="doc-1",
            extractor_tier="HYBRID",
        )
        # Find the write call (the upsert path runs exactly one).
        write_call = captured[-1]
        base_props = write_call["params"]["base_props"]
        for field in EDGE_PROVENANCE_FIELDS:
            assert field in base_props, f"missing {field} in base_props"
        assert base_props["source_trace_id"] == "tr_1"
        assert base_props["agent_id"] == "agent-1"
        assert base_props["confidence"] == 0.42
        assert base_props["evidence_ref"] == "doc-1"
        assert base_props["extractor_tier"] == "HYBRID"

    def test_none_provenance_keys_still_present_as_none(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        store.upsert_edge("a", "b", "depends_on", {"weight": 1.0})
        write_call = captured[-1]
        base_props = write_call["params"]["base_props"]
        # All five keys present, all five None — the Bolt driver
        # serialises None as a missing property server-side, which
        # both Neo4j and ArcadeDB treat as "not set".
        for field in EDGE_PROVENANCE_FIELDS:
            assert field in base_props
            assert base_props[field] is None

    def test_bad_confidence_raises_before_any_round_trip(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        with pytest.raises(ValueError, match="confidence must be in"):
            store.upsert_edge("a", "b", "depends_on", confidence=2.0)
        # No Cypher was shipped — validator fired before the driver
        # session opened.
        assert captured == []

    def test_bad_tier_raises_before_any_round_trip(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        with pytest.raises(ValueError, match="extractor_tier must be one of"):
            store.upsert_edge("a", "b", "depends_on", extractor_tier="BOGUS")
        assert captured == []

    def test_bad_confidence_type_raises_before_round_trip(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        with pytest.raises(TypeError, match="confidence must be"):
            store.upsert_edge(
                "a",
                "b",
                "depends_on",
                confidence=True,  # type: ignore[arg-type]
            )
        assert captured == []


# ---------------------------------------------------------------------------
# upsert_edges_bulk — UNWIND row carries provenance per spec
# ---------------------------------------------------------------------------


class TestBoltUpsertEdgesBulkShape:
    def test_bulk_rows_carry_provenance(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store, known_endpoint_ids={"a", "b", "c"})
        store.upsert_edges_bulk(
            [
                {
                    "source_id": "a",
                    "target_id": "b",
                    "edge_type": "links_to",
                    "confidence": 0.5,
                    "extractor_tier": "DETERMINISTIC",
                },
                {
                    "source_id": "a",
                    "target_id": "c",
                    "edge_type": "links_to",
                },
            ]
        )
        # The bulk path does:
        # 1. _fetch_current_node_id_set (read)
        # 2. UNWIND $rows (write)
        # — we want the second call.
        write_call = next(c for c in captured if "UNWIND $rows" in str(c["cypher"]))
        rows = write_call["params"]["rows"]
        assert len(rows) == 2
        row0_props = rows[0]["props"]
        assert row0_props["confidence"] == 0.5
        assert row0_props["extractor_tier"] == "DETERMINISTIC"
        # Unspecified fields default to None in the props payload —
        # ``extract_edge_provenance`` fills every key.
        assert row0_props["source_trace_id"] is None
        # Row 1 has zero provenance specified.
        row1_props = rows[1]["props"]
        for field in EDGE_PROVENANCE_FIELDS:
            assert row1_props[field] is None

    def test_bulk_bad_row_raises_with_row_index(self) -> None:
        store = _make_store_with_mocked_driver()
        captured = _capture_write_calls(store)
        with pytest.raises(ValueError, match=r"upsert_edges_bulk\[1\]"):
            store.upsert_edges_bulk(
                [
                    {
                        "source_id": "a",
                        "target_id": "b",
                        "edge_type": "links_to",
                    },
                    {
                        "source_id": "a",
                        "target_id": "c",
                        "edge_type": "links_to",
                        "extractor_tier": "INVALID",
                    },
                ]
            )
        # Bad row aborts before any Cypher ships.
        assert captured == []


# ---------------------------------------------------------------------------
# _edge_props_to_dict — read path surfaces provenance fields
# ---------------------------------------------------------------------------


class TestEdgePropsToDict:
    def test_includes_all_provenance_fields_when_set(self) -> None:
        props = {
            "edge_id": "e1",
            "source_id": "a",
            "target_id": "b",
            "edge_type": "depends_on",
            "properties_json": json.dumps({"w": 1}),
            "created_at": "2026-01-01T00:00:00",
            "valid_from": "2026-01-01T00:00:00",
            "valid_to": None,
            "source_trace_id": "tr_1",
            "agent_id": "agent-7",
            "confidence": 0.42,
            "evidence_ref": "doc-1",
            "extractor_tier": "DETERMINISTIC",
        }
        result = _edge_props_to_dict(props)
        assert result["source_trace_id"] == "tr_1"
        assert result["agent_id"] == "agent-7"
        assert result["confidence"] == 0.42
        assert result["evidence_ref"] == "doc-1"
        assert result["extractor_tier"] == "DETERMINISTIC"

    def test_missing_provenance_reads_back_as_none(self) -> None:
        # Bolt drivers omit missing properties from the returned dict —
        # mirror that here.
        props = {
            "edge_id": "e1",
            "source_id": "a",
            "target_id": "b",
            "edge_type": "depends_on",
            "properties_json": json.dumps({}),
            "created_at": "2026-01-01T00:00:00",
            "valid_from": "2026-01-01T00:00:00",
            "valid_to": None,
        }
        result = _edge_props_to_dict(props)
        for field in EDGE_PROVENANCE_FIELDS:
            assert result[field] is None
