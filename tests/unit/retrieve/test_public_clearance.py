"""A ``public`` caller (an ``external`` key) is served only documents labelled so.

It inverts the transparent default on purpose: an unlabelled document reads
``internal``, a pack item that cannot carry a label (a graph node, trace,
precedent, observation) is never shown, and neither is a chunk, whatever its
own row or its parent's row says. Every item kind the pack can hold has a
sentinel here, because the gate must not default open for the kind nobody listed.

The freeze class at the end is a weak check of the other half of the contract (a
caller that is *not* public-only takes no public branch); its docstring says what
it does and does not show.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from tests.unit.retrieve.test_sensitivity import _BODY, _strategy, label
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve import pack_builder as pack_builder_mod
from trellis.retrieve import sensitivity as sensitivity_mod
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.sensitivity import item_readable, partition_by_clearance
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PUB_CHUNK = chunk_doc_id("pub-parent", 1)
UNLABELLED_PUB_CHUNK = chunk_doc_id("pub-parent", 0)
PUB_CHUNK_OF_INTERNAL = chunk_doc_id("int-parent", 0)

#: Item ids a ``public`` caller may be served, and nothing else. No chunk is on
#: it: only a whole stored document labelled ``public`` is ever served.
SERVED = {"pub-doc", "pub-vec"}


def _typed(
    item_id: str, item_type: str, metadata: dict[str, Any] | None = None
) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type=item_type,
        excerpt=_BODY,
        relevance_score=0.5,
        metadata=metadata or {},
    )


@pytest.fixture
def doc_store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    store.put("pub-doc", "body", {"title": "p", **label("public")})
    store.put("plain-doc", "body", {"title": "u"})
    store.put("int-doc", "body", label("internal"))
    store.put("conf-doc", "body", label("confidential"))
    store.put("pub-vec", "body", label("public"))
    store.put("int-vec", "body", label("internal"))
    store.put("pub-entity-id", "body", label("public"))
    store.put("pub-parent", "body", label("public"))
    store.put(UNLABELLED_PUB_CHUNK, "chunk", {})
    store.put(PUB_CHUNK, "chunk", label("public"))
    store.put("int-parent", "body", label("internal"))
    store.put(PUB_CHUNK_OF_INTERNAL, "chunk", label("public"))
    yield store
    store.close()


def _pool() -> list[PackItem]:
    """One sentinel per kind a pack can hold; ``public`` is planted on every one."""
    planted = label("public")
    return [
        _typed("pub-doc", "document", planted),
        _typed("plain-doc", "document"),
        _typed("int-doc", "document", planted),  # metadata lies, row says internal
        _typed("conf-doc", "document"),
        _typed("ghost-doc", "document", planted),  # no stored row
        _typed("pub-vec", "vector", label("internal")),  # stale snapshot, row public
        _typed("int-vec", "vector", planted),  # stale snapshot, row internal
        _typed("pub-entity-id", "entity", planted),  # id of a public row, not a doc
        _typed("node-1", "entity", planted),
        _typed("trace-1", "trace", planted),
        _typed("precedent-1", "precedent", planted),
        _typed("evidence-1", "evidence", planted),
        _typed("obs-1", "observation", planted),
        _typed(PUB_CHUNK, "document"),
        _typed(UNLABELLED_PUB_CHUNK, "document"),
        _typed(PUB_CHUNK_OF_INTERNAL, "document", planted),
    ]


def _flat(builder: PackBuilder, clearance: Any = "public") -> list[str]:
    return [i.item_id for i in builder.build("intent", clearance=clearance).items]


def _sectioned(builder: PackBuilder, clearance: Any = "public") -> list[str]:
    pack = builder.build_sectioned(
        "intent", clearance=clearance, sections=[SectionRequest(name="All")]
    )
    return [i.item_id for s in pack.sections for i in s.items]


class TestEveryItemKind:
    @pytest.mark.parametrize("shape", [_flat, _sectioned])
    def test_a_public_caller_is_served_only_stored_public_documents(
        self, doc_store: SQLiteDocumentStore, shape: Any
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())], document_store=doc_store
        )
        assert set(shape(builder)) == SERVED

    @pytest.mark.parametrize("shape", [_flat, _sectioned])
    @pytest.mark.parametrize("kind", ["entity", "trace", "precedent", "observation"])
    def test_a_kind_that_cannot_carry_a_label_is_withheld_even_when_it_claims_one(
        self, doc_store: SQLiteDocumentStore, shape: Any, kind: str
    ) -> None:
        items = [
            _typed("pub-doc", "document"),
            _typed("pub-doc", kind, label("public")),
        ]
        builder = PackBuilder(
            strategies=[_strategy("graph", items[1:])], document_store=doc_store
        )
        assert shape(builder) == []

    @pytest.mark.parametrize("shape", [_flat, _sectioned])
    def test_without_a_document_store_nothing_is_served(self, shape: Any) -> None:
        builder = PackBuilder(strategies=[_strategy("keyword", _pool())])
        assert shape(builder) == []

    @pytest.mark.parametrize("shape", [_flat, _sectioned])
    def test_a_store_error_while_deciding_serves_nothing(
        self, doc_store: SQLiteDocumentStore, shape: Any
    ) -> None:
        broken = MagicMock(wraps=doc_store)
        broken.get.side_effect = RuntimeError("store down")
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())], document_store=broken
        )
        assert shape(builder) == []

    @pytest.mark.parametrize("shape", [_flat, _sectioned])
    @pytest.mark.parametrize("unknown", ["", "external", "PUBLIC", None])
    def test_an_unknown_clearance_is_served_no_more_than_public(
        self, doc_store: SQLiteDocumentStore, shape: Any, unknown: Any
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())], document_store=doc_store
        )
        assert set(shape(builder, unknown)) <= SERVED

    def test_a_chunk_is_never_served_whatever_its_row_or_its_parent_says(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        cache = ParentVerdictCache(doc_store)
        verdicts = {
            cid: item_readable("public", _typed(cid, "document"), cache)
            for cid in (PUB_CHUNK, UNLABELLED_PUB_CHUNK, PUB_CHUNK_OF_INTERNAL)
        }
        assert verdicts == {
            PUB_CHUNK: False,
            UNLABELLED_PUB_CHUNK: False,
            PUB_CHUNK_OF_INTERNAL: False,
        }

    def test_an_orphan_chunk_is_not_served(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        """A public chunk whose parent row is gone is the case a parent-label
        rule would have widened: with no parent there is nothing to inherit."""
        orphan = chunk_doc_id("deleted-parent", 0)
        doc_store.put(orphan, "chunk", label("public"))
        assert not item_readable(
            "public", _typed(orphan, "document"), ParentVerdictCache(doc_store)
        )

    def test_no_count_that_sees_past_the_filter_is_reported(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())], document_store=doc_store
        )
        pack = builder.build("intent", clearance="public")
        withholding = pack.metadata["withholding"]
        assert withholding["total"] == 0
        assert withholding["by_reason"] == {}
        assert withholding["withheld_item_ids"] == []
        assert withholding["served_count"] == len(SERVED)
        assert pack.retrieval_report.rejected_items == []
        assert pack.retrieval_report.candidates_found == len(SERVED)
        dumped = json.dumps(pack.model_dump(mode="json"))
        assert not re.search(r"node-1|trace-1|plain-doc|conf-doc|ghost-doc", dumped)

    def test_partition_withholds_what_item_readable_refuses(
        self, doc_store: SQLiteDocumentStore
    ) -> None:
        kept, withheld = partition_by_clearance(
            _pool(), "public", ParentVerdictCache(doc_store)
        )
        assert {i.item_id for i in kept} == SERVED
        assert len(kept) + len(withheld) == len(_pool())


class TestAdvisories:
    @staticmethod
    def _store(tmp_path: Path) -> AdvisoryStore:
        store = AdvisoryStore(tmp_path / "adv.json")
        store.put(
            Advisory(
                category=AdvisoryCategory.ENTITY,
                confidence=0.8,
                message="Test advisory naming an-entity-sentinel",
                evidence=AdvisoryEvidence(
                    sample_size=10,
                    success_rate_with=0.8,
                    success_rate_without=0.4,
                    effect_size=0.4,
                    evidence_confidence=1.0,
                ),
                scope="global",
                entity_id="an-entity-sentinel",
            )
        )
        return store

    def test_a_public_pack_carries_no_advisory_flat_or_sectioned(
        self, tmp_path: Path, doc_store: SQLiteDocumentStore
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())],
            document_store=doc_store,
            advisory_store=self._store(tmp_path),
        )
        flat = builder.build("intent", clearance="public")
        sectioned = builder.build_sectioned(
            "intent", clearance="public", sections=[SectionRequest(name="All")]
        )
        assert flat.advisories == []
        assert sectioned.advisories == []
        assert "an-entity-sentinel" not in flat.model_dump_json()
        assert "an-entity-sentinel" not in sectioned.model_dump_json()

    def test_a_read_key_still_receives_the_advisory(
        self, tmp_path: Path, doc_store: SQLiteDocumentStore
    ) -> None:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _pool())],
            document_store=doc_store,
            advisory_store=self._store(tmp_path),
        )
        assert len(builder.build("intent", clearance="internal").advisories) == 1


# ---------------------------------------------------------------------------
# SERVING FREEZE
# ---------------------------------------------------------------------------

#: Fields that differ between two runs of identical code (ids drawn per build,
#: clocks). Masked before comparing; nothing else is.
_MASKED = re.compile(r"pack_id|event_id|_at$|timestamp|duration_ms|latency|elapsed")


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<masked>" if _MASKED.search(k) else _mask(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _differences(a: Any, b: Any, path: str = "") -> list[str]:
    """Paths at which two captures differ, so a failure names the field."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [
            d
            for k in sorted(a.keys() | b.keys())
            for d in _differences(a.get(k), b.get(k), f"{path}/{k}")
        ]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in _differences(x, y, f"{path}[{i}]")
        ]
    return [] if a == b else [path]


def _freeze_pool() -> list[PackItem]:
    return [*_pool(), _typed("open-doc", "document")]


_RUNS = itertools.count()


def _capture(
    tmp_path: Path, doc_store: SQLiteDocumentStore, clearance: str, holdout: float
) -> dict[str, Any]:
    """The flat pack, sectioned pack and both assembled-pack events for one arm."""
    log = SQLiteEventLog(tmp_path / f"events-{next(_RUNS)}.db")
    try:
        builder = PackBuilder(
            strategies=[_strategy("keyword", _freeze_pool())],
            document_store=doc_store,
            event_log=log,
            holdout_rate=holdout,
        )
        flat = builder.build("intent", clearance=clearance)  # type: ignore[arg-type]
        sectioned = builder.build_sectioned(
            "intent",
            clearance=clearance,  # type: ignore[arg-type]
            sections=[SectionRequest(name="All")],
        )
        events = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
        return _mask(
            {
                "flat": flat.model_dump(mode="json"),
                "sectioned": sectioned.model_dump(mode="json"),
                "events": sorted(
                    (e.payload for e in events), key=lambda p: json.dumps(_mask(p))
                ),
            }
        )
    finally:
        log.close()


class TestServingFreeze:
    """A non-public caller never takes the public branch.

    This is a weak check and is named as one. The baseline patches
    ``is_public_only`` to always return False, and for these callers it already
    returns False, so both arms run the same code: the test shows the dispatch
    predicate is false for ``internal``, ``confidential`` and ``restricted``, and
    that two runs of one build agree once ids and clocks are masked. It does not
    compare the code against the code before the branch existed, so a change
    made inside the non-public path would pass it. That comparison needs the old
    build beside the new one (the flat pack, the sectioned pack and every
    ``PACK_ASSEMBLED`` payload, on the served and the held-out arm) and is made
    against a base checkout, outside this suite. Masked: ids drawn per build and
    clocks or durations (see ``_MASKED``), and nothing else.
    """

    @pytest.mark.parametrize("holdout", [0.0, 1.0])
    @pytest.mark.parametrize("clearance", ["internal", "confidential", "restricted"])
    def test_a_non_public_caller_runs_the_same_code_with_and_without_the_branch(
        self,
        tmp_path: Path,
        doc_store: SQLiteDocumentStore,
        monkeypatch: pytest.MonkeyPatch,
        clearance: str,
        holdout: float,
    ) -> None:
        with_branch = _capture(tmp_path, doc_store, clearance, holdout)

        monkeypatch.setattr(sensitivity_mod, "is_public_only", lambda _c: False)
        monkeypatch.setattr(pack_builder_mod, "is_public_only", lambda _c: False)
        without_branch = _capture(tmp_path, doc_store, clearance, holdout)

        assert _differences(with_branch, without_branch) == []
        assert json.dumps(with_branch, sort_keys=True) == json.dumps(
            without_branch, sort_keys=True
        )
        # Not a vacuous comparison: the arm either served items or was held out.
        if holdout == 0.0:
            assert with_branch["flat"]["items"]

    @pytest.mark.parametrize("clearance", ["internal", "confidential", "restricted"])
    def test_no_new_key_appears_in_a_non_public_payload(
        self, tmp_path: Path, doc_store: SQLiteDocumentStore, clearance: str
    ) -> None:
        captured = _capture(tmp_path, doc_store, clearance, 0.0)
        for payload in captured["events"]:
            assert not [k for k in payload if "public" in k or "external" in k]
        assert not [k for k in captured["flat"]["metadata"] if "public" in k]

    def test_the_public_arm_does_differ(
        self, tmp_path: Path, doc_store: SQLiteDocumentStore
    ) -> None:
        """The freeze compares something: the public arm serves fewer items."""
        public = _capture(tmp_path, doc_store, "public", 0.0)
        internal = _capture(tmp_path, doc_store, "internal", 0.0)
        assert len(public["flat"]["items"]) < len(internal["flat"]["items"])
