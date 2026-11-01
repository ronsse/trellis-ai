"""A pack is the same whether or not a document's metadata is readable as an object.

The direct-read gate (``DocumentClearance``) stopped raising on a row whose stored
``metadata`` is JSON that is not an object. A pack is under a measurement freeze,
so it must behave as it did. Two things are pinned, each with literals produced on
the tree before the gate changed, which this file also passes on.

An item's own metadata is judged by ``effective_sensitivity`` and never reaches
that gate; ``test_the_pack_is_the_one_built_before_the_gate_changed`` builds packs
through ``build_pack_builder`` for each clearance a caller who is not public-only
holds (``internal`` is the clearance of a caller with no scope that raises it),
over one store holding a row of every shape, and pins the items served.

A served item that *points* at another row (``supersedes_doc_id``,
``updates_doc_id``, ``evidence_ref``, ``source_item_id``) does reach it: the pack
asks whether the caller may read what is named
(``conceal_unreadable_successors`` -> ``readable_ids`` -> ``DocumentClearance``).
When the named row's metadata is not an object, that question RAISED at
``internal`` and ``confidential`` and kept the pointer at ``restricted``, and it
still does: the pack path keeps the older judgement on purpose, and only the direct
reads judge such a row ``restricted``.
``test_a_pointer_at_an_unreadable_row_is_judged_as_it_was`` pins it for every key,
and ``test_the_pack_path_alone_keeps_the_older_judgement`` pins that the gate's
default is the other one.

``_source_is_labelled`` (the learning-side caller of ``DocumentClearance``, for a
promoted precedent's source) is pinned the same way: a shape the gate used to
raise on was answered by its ``except`` as labelled, and is still labelled.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.unit.retrieve.public_corpus import EMBED_FN_PATH, _put
from trellis.learning.scoring import _source_is_labelled
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.retrieve.sensitivity import (
    DocumentClearance,
    conceal_node_pointers,
    readable_ids,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

RAW_METADATA: dict[str, str] = {
    "null": "null",
    "list": "[1]",
    "number": "5",
    "string": '"str"',
    "true": "true",
    "empty-list": "[]",
    "empty-string": '""',
    "zero": "0",
    "false": "false",
    "pairs-public": json.dumps([["data_classification", {"sensitivity": "public"}]]),
    "pairs-restricted": json.dumps(
        [["data_classification", {"sensitivity": "restricted"}]]
    ),
    "restricted": json.dumps({"data_classification": {"sensitivity": "restricted"}}),
}

CLEARANCES = ("internal", "confidential", "restricted")
INTENT = "failover runbook"


def _registry(root: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    config_dir = root / "config"
    data_dir = root / "data"
    (data_dir / "stores").mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
    return StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)


def _world(root: Path, raw: str, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """A store with an unlabelled document and one whose metadata is ``raw``.

    Both are embedded, the second with its own label when ``raw`` is an object, so
    the semantic axis serves them as well as the keyword one; the keyword axis
    raises on some of these rows on its own (it reads ``metadata.get``), which is
    the pack's own business and not touched here.
    """
    reg = _registry(root, monkeypatch)
    _put(reg, "doc-plain", "failover runbook for the plain document", {})
    parsed = json.loads(raw)
    snapshot = parsed if isinstance(parsed, dict) else {}
    _put(reg, "doc-row", "failover runbook for the row under test", snapshot)
    conn = reg.knowledge.document_store._get_conn()
    conn.execute(
        "UPDATE documents SET metadata_json = ? WHERE doc_id = 'doc-row'", (raw,)
    )
    conn.commit()
    return reg


def _served(registry: StoreRegistry, clearance: str) -> list[str]:
    pack = build_pack_builder(registry, surface="test").build(
        INTENT,
        clearance=clearance,  # type: ignore[arg-type]
    )
    return sorted(i.item_id for i in pack.items)


#: ``shape -> clearance -> item ids served``, as the tree before the gate changed
#: built it.
EXPECTED: dict[str, dict[str, list[str]]] = {
    "null": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "list": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "number": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "string": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "true": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "empty-list": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "empty-string": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "zero": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "false": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "pairs-public": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "pairs-restricted": {
        "internal": ["doc-plain", "doc-row"],
        "confidential": ["doc-plain", "doc-row"],
        "restricted": ["doc-plain", "doc-row"],
    },
    "restricted": {
        "internal": ["doc-plain"],
        "confidential": ["doc-plain"],
        "restricted": ["doc-plain", "doc-row"],
    },
}

#: ``shape -> whether a precedent's source reads as labelled above internal``.
SOURCE_LABELLED: dict[str, bool] = {
    "null": False,
    "list": True,
    "number": True,
    "string": True,
    "true": True,
    "empty-list": False,
    "empty-string": False,
    "zero": False,
    "false": False,
    "pairs-public": False,
    "pairs-restricted": True,
    "restricted": True,
}


@pytest.mark.parametrize("shape", sorted(RAW_METADATA))
def test_the_pack_is_the_one_built_before_the_gate_changed(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _world(tmp_path, RAW_METADATA[shape], monkeypatch)
    try:
        assert {c: _served(reg, c) for c in CLEARANCES} == EXPECTED[shape]
    finally:
        reg.close()


@pytest.mark.parametrize("shape", sorted(RAW_METADATA))
def test_a_precedent_source_is_judged_as_it_was(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _world(tmp_path, RAW_METADATA[shape], monkeypatch)
    try:
        store = reg.knowledge.document_store
        assert _source_is_labelled("doc-row", store) is SOURCE_LABELLED[shape]
    finally:
        reg.close()


POINTER_KEYS = ("supersedes_doc_id", "updates_doc_id", "evidence_ref", "source_item_id")

#: A document that names ``doc-row`` under each pointer key.
POINTER_INTENT = "failover runbook"

#: What a pack at ``internal`` and ``confidential`` did on a pointer at a row of
#: this shape before the gate changed: raise this, or serve both documents with the
#: pointer kept (``True``) or dropped (``False``) on ``doc-ptr``. Identical for
#: every one of the four keys.
RAISES: dict[str, str] = {
    "list": "TypeError",
    "number": "TypeError",
    "true": "TypeError",
    "string": "ValueError",
}
KEPT = {"doc-ptr": True, "doc-row": False}
DROPPED = {"doc-ptr": False, "doc-row": False}
POINTER_EXPECTED: dict[str, dict[str, object]] = {
    **{
        shape: {
            "internal": exc,
            "confidential": exc,
            "restricted": KEPT,
        }
        for shape, exc in RAISES.items()
    },
    # ``dict()`` converts these, so they never raised and are judged as they were.
    "null": {"internal": KEPT, "confidential": KEPT, "restricted": KEPT},
    "empty-list": {"internal": KEPT, "confidential": KEPT, "restricted": KEPT},
    "pairs-public": {"internal": KEPT, "confidential": KEPT, "restricted": KEPT},
    "pairs-restricted": {
        "internal": DROPPED,
        "confidential": DROPPED,
        "restricted": KEPT,
    },
}


def _pointer_world(
    root: Path, key: str, raw: str, monkeypatch: pytest.MonkeyPatch
) -> StoreRegistry:
    """``doc-ptr`` names ``doc-row`` under ``key``; ``doc-row`` holds ``raw``."""
    reg = _registry(root, monkeypatch)
    _put(reg, "doc-row", "archive note about an unrelated subject", {})
    _put(reg, "doc-ptr", "failover runbook for the pointer document", {key: "doc-row"})
    conn = reg.knowledge.document_store._get_conn()
    conn.execute(
        "UPDATE documents SET metadata_json = ? WHERE doc_id = 'doc-row'", (raw,)
    )
    conn.commit()
    return reg


def _pack_outcome(registry: StoreRegistry, clearance: str, key: str) -> object:
    try:
        pack = build_pack_builder(registry, surface="test").build(
            POINTER_INTENT,
            clearance=clearance,  # type: ignore[arg-type]
        )
    except Exception as exc:
        return type(exc).__name__
    return {item.item_id: key in item.metadata for item in pack.items}


@pytest.mark.parametrize("key", POINTER_KEYS)
@pytest.mark.parametrize("shape", sorted(POINTER_EXPECTED))
def test_a_pointer_at_an_unreadable_row_is_judged_as_it_was(
    shape: str, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _pointer_world(tmp_path, key, RAW_METADATA[shape], monkeypatch)
    try:
        got = {c: _pack_outcome(reg, c, key) for c in CLEARANCES}
        assert got == POINTER_EXPECTED[shape]
    finally:
        reg.close()


@pytest.mark.parametrize("shape", sorted(RAISES))
def test_the_pack_path_alone_keeps_the_older_judgement(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's default judges the row ``restricted``; the pack's call does not."""
    reg = _pointer_world(
        tmp_path, "supersedes_doc_id", RAW_METADATA[shape], monkeypatch
    )
    try:
        store = reg.knowledge.document_store
        assert readable_ids(store, ["doc-row"], "internal") == []
        with pytest.raises((TypeError, ValueError)):
            readable_ids(
                store, ["doc-row"], "internal", fail_closed_on_unreadable_label=False
            )
        gate = DocumentClearance(store, "internal")
        assert gate.sensitivity("doc-row", store.get("doc-row")["metadata"]) == (
            "restricted"
        )
    finally:
        reg.close()


@pytest.mark.parametrize("shape", sorted(RAISES))
def test_a_graph_read_drops_a_pointer_at_an_unreadable_row(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct read fails closed: the pointer is dropped, not raised on."""
    reg = _pointer_world(
        tmp_path, "supersedes_doc_id", RAW_METADATA[shape], monkeypatch
    )
    try:
        node = {"node_id": "n1", "properties": {"evidence_ref": "doc-row"}}
        store = reg.knowledge.document_store
        (shown,) = conceal_node_pointers([node], store, "internal")
        assert "evidence_ref" not in shown["properties"]
        (kept,) = conceal_node_pointers([node], store, "restricted")
        assert kept["properties"] == {"evidence_ref": "doc-row"}
    finally:
        reg.close()
