"""What naming a precedent in a node's pointers costs to judge (#1232).

A pointer to a promoted precedent is judged by the gate the read doors use
(:class:`~trellis.retrieve.precedents.PrecedentClearance`): the precedent's own
row, the document its ``source_item_id`` names, and the graph node that names
the source. These tests pin the bill: it is per distinct precedent, never per
node that names it, and a request that already judged a precedent pays nothing
more for it. An id outside the precedent namespace costs what it always did.
Real SQLite stores and real promotions; reads are counted at the stores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.unit.retrieve.test_precedent_listing_clearance import BODY, promote, relabel
from trellis.retrieve.precedents import PrecedentClearance
from trellis.retrieve.sensitivity import conceal_graph_pointers, conceal_node_pointers
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

OPEN = "doc-open"
PRECEDENTS = 3


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    reg.knowledge.document_store.put(OPEN, "an open note", {})
    yield reg
    reg.close()


@pytest.fixture
def withheld(registry: StoreRegistry) -> list[str]:
    """``PRECEDENTS`` precedents, each over its own source labelled above a reader."""
    ids = []
    for n in range(PRECEDENTS):
        source = f"doc-source-{n}"
        registry.knowledge.document_store.put(source, BODY, {"title": f"S{n}"})
        ids.append(promote(registry, f"cand:{n}", source, title=f"Title {n}"))
        relabel(registry, source, "confidential")
    return ids


class Reads:
    """Counts document-store ``get`` and graph-store ``get_node`` calls."""

    def __init__(self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch):
        self.docs = 0
        self.nodes = 0
        docs = registry.knowledge.document_store
        graph = registry.knowledge.graph_store
        real_get, real_node = docs.get, graph.get_node

        def get(doc_id: str, *a: Any, **k: Any) -> Any:
            self.docs += 1
            return real_get(doc_id, *a, **k)

        def get_node(node_id: str, *a: Any, **k: Any) -> Any:
            self.nodes += 1
            return real_node(node_id, *a, **k)

        monkeypatch.setattr(docs, "get", get)
        monkeypatch.setattr(graph, "get_node", get_node)

    def measure(self, call: Callable[[], Any]) -> tuple[int, int]:
        """``(document reads, graph reads)`` ``call`` made."""
        before = (self.docs, self.nodes)
        call()
        return self.docs - before[0], self.nodes - before[1]


def _holders(count: int, ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "node_id": f"holder-{n}",
            "node_type": "entity",
            "properties": {"name": f"holder {n}"},
            "document_ids": [OPEN, *ids],
        }
        for n in range(count)
    ]


def _gate(registry: StoreRegistry) -> PrecedentClearance:
    return PrecedentClearance(
        registry.knowledge.document_store,
        "internal",
        registry.knowledge.graph_store,
    )


def test_the_plain_judgement_alone_names_every_withheld_precedent(registry, withheld):
    """The premise: each precedent's own row is readable, so none is dropped."""
    (out,) = conceal_node_pointers(
        _holders(1, withheld), registry.knowledge.document_store, "internal"
    )

    assert set(withheld) <= set(out["document_ids"])


def test_every_withheld_precedent_is_dropped_and_a_readable_pointer_kept(
    registry, withheld
):
    out = conceal_node_pointers(
        _holders(4, withheld),
        registry.knowledge.document_store,
        "internal",
        precedents=_gate(registry),
    )

    assert [n["document_ids"] for n in out] == [[OPEN]] * 4


@pytest.mark.parametrize("nodes", [1, 6])
def test_the_bill_is_per_distinct_precedent_not_per_node(
    registry, withheld, monkeypatch, nodes
):
    reads = Reads(registry, monkeypatch)
    store = registry.knowledge.document_store

    baseline = reads.measure(
        lambda: conceal_node_pointers(_holders(nodes, withheld), store, "internal")
    )
    gated = reads.measure(
        lambda: conceal_node_pointers(
            _holders(nodes, withheld), store, "internal", precedents=_gate(registry)
        )
    )

    # Before: one document read per distinct id (the open note and each
    # precedent's own row). After: the precedent's own row and its source's, and
    # the graph node that names the source, per precedent; the open note as before.
    assert baseline == (1 + PRECEDENTS, 0)
    assert gated == (1 + 2 * PRECEDENTS, PRECEDENTS)


def test_a_precedent_the_request_already_judged_costs_nothing_more(
    registry, withheld, monkeypatch
):
    gate = _gate(registry)
    for precedent_id in withheld:
        assert gate.verdict_of(precedent_id) is not None
    reads = Reads(registry, monkeypatch)

    cost = reads.measure(
        lambda: conceal_node_pointers(
            _holders(3, withheld),
            registry.knowledge.document_store,
            "internal",
            precedents=gate,
        )
    )

    assert cost == (1, 0), "only the open note is read"


def test_an_id_outside_the_precedent_namespace_is_judged_as_before(
    registry, withheld, monkeypatch
):
    reads = Reads(registry, monkeypatch)
    store = registry.knowledge.document_store

    cost = reads.measure(
        lambda: conceal_node_pointers(
            _holders(3, []), store, "internal", precedents=_gate(registry)
        )
    )

    assert cost == (1, 0)


def test_restricted_reads_nothing(registry, withheld, monkeypatch):
    reads = Reads(registry, monkeypatch)

    nodes, edges = conceal_graph_pointers(
        _holders(2, withheld),
        [],
        registry.knowledge.document_store,
        "restricted",
        precedents=_gate(registry),
    )

    assert (reads.docs, reads.nodes) == (0, 0)
    assert all(set(withheld) <= set(n["document_ids"]) for n in nodes)
    assert edges == []
