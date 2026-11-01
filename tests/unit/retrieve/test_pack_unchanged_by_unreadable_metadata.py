"""A pack is the same whether or not a document's metadata is readable as an object.

The direct-read gate (``DocumentClearance``) stopped raising on a row whose stored
``metadata`` is JSON that is not an object. A pack is not a direct read: it judges
an item by ``effective_sensitivity`` on the item's own metadata and never asks that
gate. This builds packs through ``build_pack_builder`` for each clearance a caller
who is not public-only holds (``internal`` is the clearance of a caller with
no scope that raises it), over one store holding a row of every shape, and pins
the items served. The literals were produced on the tree
before the gate changed, and the same file passes on it; if a later change moves
what a pack serves, this fails and says which clearance.

``_source_is_labelled`` (the one pack-side caller of ``DocumentClearance``, for a
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


def _world(root: Path, raw: str, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """A store with an unlabelled document and one whose metadata is ``raw``.

    Both are embedded, the second with its own label when ``raw`` is an object, so
    the semantic axis serves them as well as the keyword one; the keyword axis
    raises on some of these rows on its own (it reads ``metadata.get``), which is
    the pack's own business and not touched here.
    """
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    config_dir = root / "config"
    data_dir = root / "data"
    (data_dir / "stores").mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(f"data_dir: {data_dir}\n")
    reg = StoreRegistry.from_config_dir(config_dir=config_dir, data_dir=data_dir)
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
