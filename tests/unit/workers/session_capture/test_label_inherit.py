"""A memory distilled from a session inherits the label of what the session was served.

The memory is written unlabelled, and an unlabelled document reads ``internal``,
so a memory distilled from a session served a ``confidential`` document was served
to a caller cleared only to ``internal``, and sent to a non-local enrichment or
mining route (#1227). The sweep now stamps the strictest label above ``internal``
among the served documents, on every route, from the same read that screens the
transcript. Only the sensitivity is inherited, never ``regulatory_tags``.

Each test runs the real sweep. The serving test builds the pack through the real
builder factory over real SQLite stores, so it asks the read gate rather than the
metadata the sweep wrote.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.structlog_isolation import IsolatedCliRunner
from tests.unit.workers.session_capture.conftest import (
    FakeLLMClient,
    candidates_json,
    good_candidate,
)
from tests.unit.workers.session_capture.test_capture import (
    A,
    B,
    _pack_session,
    _registry,
    _stored_captures,
    _sweep,
)
from tests.unit.workers.session_capture.test_distill_egress import (
    _declare_local,
    _emit_pack,
    _serve,
)
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.builder_factory import build_pack_builder
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    DataClassification,
    sensitivity_of,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_cli import worker
from trellis_cli.main import app
from trellis_workers.session_capture import capture
from trellis_workers.session_capture import sweep as capture_sweep
from trellis_workers.session_capture.models import CaptureReport

runner = IsolatedCliRunner()

#: A word only the distilled memory contains, so a pack on it finds nothing else.
INTENT = "frobnicator schema migration"


def _label(level: str, **extra: Any) -> dict[str, Any]:
    return {DATA_CLASSIFICATION_KEY: {"sensitivity": level, **extra}}


def _client() -> FakeLLMClient:
    return FakeLLMClient([candidates_json(good_candidate())])


def _session(root: Path, *pack_ids: str) -> None:
    _pack_session(
        root / "proj" / "sess-inherit-0001.jsonl",
        "sess-inherit-0001",
        *[("get_context", p) for p in pack_ids],
        error=True,
    )


def _run(
    registry: Any,
    tmp_path: Path,
    *,
    dry_run: bool = False,
    wm: str = "wm.json",
    client: FakeLLMClient | None = None,
) -> capture.CaptureReport:
    return _sweep(
        registry,
        tmp_path / "projects",
        tmp_path / wm,
        client or _client(),
        dry_run=dry_run,
    )


def _memory(registry: Any) -> dict[str, Any]:
    docs = _stored_captures(registry)
    assert len(docs) == 1
    row = registry.knowledge.document_store.get(docs[0]["doc_id"])
    assert row is not None
    return row


def _stamp(registry: Any) -> Any:
    """What the stored memory carries under ``data_classification``, or ``None``."""
    return _memory(registry)["metadata"].get(DATA_CLASSIFICATION_KEY)


class TestAMemoryInheritsTheStrictestServedLabel:
    def test_a_confidential_document_labels_the_memory_confidential(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.memories_written == 1
        assert sensitivity_of(_memory(registry)["metadata"]) == "confidential"
        stamp = _stamp(registry)
        assert stamp["classified_by"] == [capture.SESSION_INHERIT_CLASSIFIER]
        assert stamp["classified_by"] == ["session-inherit"]
        assert report.memories_label_inherited == 1
        assert report.memories_label_inherited_by_level == {"confidential": 1}

    @pytest.mark.parametrize("local", [True, False], ids=["local", "cloud"])
    @pytest.mark.parametrize("served", [None, "internal", "public"])
    def test_nothing_above_internal_leaves_no_key_at_all(
        self, tmp_path: Path, local: bool, served: str | None
    ) -> None:
        registry = _registry(tmp_path)
        if local:
            _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-plain", "body", _label(served) if served else {}
        )
        _serve(registry, A, "doc-plain")
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.memories_written == 1
        assert DATA_CLASSIFICATION_KEY not in _memory(registry)["metadata"]
        assert report.memories_label_inherited == 0
        assert report.memories_label_inherited_by_level == {}

    def test_a_session_served_nothing_writes_an_unlabelled_memory(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.memories_written == 1
        assert DATA_CLASSIFICATION_KEY not in _memory(registry)["metadata"]

    def test_a_chunk_of_a_labelled_parent_takes_the_parents_label(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put("parent", "body", _label("restricted"))
        _serve(registry, A, chunk_doc_id("parent", 0))
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"
        assert report.memories_label_inherited_by_level == {"restricted": 1}

    @pytest.mark.parametrize("first", ["pack-a", "pack-b"])
    def test_the_strictest_of_several_wins_across_documents_and_packs(
        self, tmp_path: Path, first: str
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-int", "body", _label("internal"))
        docs.put("doc-restricted", "body", _label("restricted"))
        docs.put("doc-conf", "body", _label("confidential"))
        pack_a = ["doc-int", "doc-conf"] if first == "pack-a" else ["doc-restricted"]
        pack_b = ["doc-restricted"] if first == "pack-a" else ["doc-int", "doc-conf"]
        _serve(registry, A, *pack_a)
        _serve(registry, B, *pack_b)
        _session(tmp_path / "projects", A, B)

        report = _run(registry, tmp_path)

        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"
        assert report.memories_label_inherited_by_level == {"restricted": 1}

    def test_only_the_sensitivity_is_inherited_not_the_regulatory_tags(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-phi",
            "body",
            _label("confidential", regulatory_tags=["phi", "pii"], jurisdiction="us"),
        )
        _serve(registry, A, "doc-phi")
        _session(tmp_path / "projects", A)

        _run(registry, tmp_path)

        stamp = DataClassification.model_validate(_stamp(registry))
        assert stamp.sensitivity == "confidential"
        assert stamp.regulatory_tags == []
        assert stamp.jurisdiction == []

    def test_a_cloud_route_still_withholds_and_stamps_nothing(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        _session(tmp_path / "projects", A)
        client = _client()

        report = _run(registry, tmp_path, client=client)

        assert client.calls == []
        assert _stored_captures(registry) == []
        assert report.sessions_skipped_egress_withheld == 1
        assert report.memories_label_inherited == 0


class TestTheLabelServesAsALabel:
    def test_a_pack_at_internal_clearance_no_longer_serves_the_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        monkeypatch.setattr(
            registry, "llm_route", lambda _c=None: SimpleNamespace(local=True)
        )
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        _session(tmp_path / "projects", A)

        _run(registry, tmp_path)

        memory_id = _stored_captures(registry)[0]["doc_id"]

        def pack(clearance: str) -> tuple[set[str], set[str]]:
            built = build_pack_builder(registry, surface="test").build(
                INTENT, clearance=clearance
            )
            return (
                {item.item_id for item in built.items},
                {
                    item.item_id
                    for item in built.retrieval_report.rejected_items
                    if item.reason == "sensitivity"
                },
            )

        served, withheld = pack("internal")
        assert memory_id not in served
        assert memory_id in withheld
        assert memory_id in pack("confidential")[0]

    def test_an_unlabelled_session_still_serves_its_memory_at_internal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(stores_dir=stores)
        monkeypatch.setattr(
            registry, "llm_route", lambda _c=None: SimpleNamespace(local=True)
        )
        registry.knowledge.document_store.put("doc-plain", "body", {})
        _serve(registry, A, "doc-plain")
        _session(tmp_path / "projects", A)

        _run(registry, tmp_path)

        memory_id = _stored_captures(registry)[0]["doc_id"]
        built = build_pack_builder(registry, surface="test").build(
            INTENT, clearance="internal"
        )
        assert memory_id in {item.item_id for item in built.items}


class TestAReadThatCannotAnswerFailsClosed:
    def test_an_event_log_outage_holds_the_session_then_a_retry_stamps_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        log = registry.operational.event_log
        real = log.get_events

        def down(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("event_type") is EventType.PACK_ASSEMBLED:
                msg = "log down"
                raise RuntimeError(msg)
            return real(*args, **kwargs)

        monkeypatch.setattr(log, "get_events", down)
        _session(tmp_path / "projects", A)
        client = _client()

        held = _run(registry, tmp_path, client=client)

        assert client.calls == []
        assert _stored_captures(registry) == []
        assert held.memories_written == 0
        assert held.sessions_skipped_egress_unreadable == 1
        assert held.sessions_skipped_egress_withheld == 0
        assert [w["kind"] for w in held.warnings] == ["egress_unreadable"]

        monkeypatch.setattr(log, "get_events", real)
        retried = _run(registry, tmp_path, client=client)

        assert retried.sessions_skipped_egress_unreadable == 0
        assert sensitivity_of(_memory(registry)["metadata"]) == "confidential"

    def test_a_document_store_outage_holds_the_session_then_a_retry_stamps_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-secret", "body", _label("confidential"))
        _serve(registry, A, "doc-secret")
        real = docs.get

        def down(doc_id: str) -> Any:
            if doc_id == "doc-secret":
                msg = "store down"
                raise sqlite3.OperationalError(msg)
            return real(doc_id)

        monkeypatch.setattr(docs, "get", down)
        _session(tmp_path / "projects", A)
        client = _client()

        held = _run(registry, tmp_path, client=client)

        assert client.calls == []
        assert _stored_captures(registry) == []
        assert held.sessions_skipped_egress_unreadable == 1

        monkeypatch.setattr(docs, "get", real)
        _run(registry, tmp_path, client=client)

        assert sensitivity_of(_memory(registry)["metadata"]) == "confidential"

    def test_an_outage_never_stamps_less_than_what_a_readable_item_gave(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One document readable (``restricted``), another not: held, not written."""
        registry = _registry(tmp_path)
        _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-readable", "body", _label("restricted"))
        docs.put("doc-down", "body", {})
        _serve(registry, A, "doc-down", "doc-readable")
        real = docs.get

        def down(doc_id: str) -> Any:
            if doc_id == "doc-down":
                msg = "store down"
                raise sqlite3.OperationalError(msg)
            return real(doc_id)

        monkeypatch.setattr(docs, "get", down)
        _session(tmp_path / "projects", A)

        held = _run(registry, tmp_path)

        assert _stored_captures(registry) == []
        assert held.sessions_skipped_egress_unreadable == 1

    def test_an_orphan_chunk_stamps_restricted(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        _serve(registry, A, chunk_doc_id("gone", 0))
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.sessions_skipped_egress_unreadable == 0
        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"
        assert report.memories_label_inherited_by_level == {"restricted": 1}

    def test_an_unparseable_document_row_stamps_restricted(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-bad", "body", {})
        docs._conn.execute(
            "UPDATE documents SET metadata_json = ? WHERE doc_id = ?",
            ("{broken", "doc-bad"),
        )
        docs._conn.commit()
        _serve(registry, A, "doc-bad")
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        # Repaired so the listing helper can read past it to the memory.
        docs.put("doc-bad", "body", {})
        assert report.sessions_skipped_egress_unreadable == 0
        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"

    @pytest.mark.parametrize(
        "payload",
        [{}, {"injected_item_ids": "doc-x"}, {"injected_item_ids": [""]}],
        ids=["no-item-record", "ids-not-a-list", "id-is-empty"],
    )
    def test_a_pack_event_with_no_readable_item_list_stamps_restricted(
        self, tmp_path: Path, payload: dict[str, Any]
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        _emit_pack(registry, A, payload)
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.sessions_skipped_egress_unreadable == 0
        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"

    def test_a_pack_event_whose_stored_payload_does_not_parse_stamps_restricted(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        _serve(registry, A, "doc-ok")
        conn = registry.operational.event_log._conn
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE entity_id = ?", ("{broken", A)
        )
        conn.commit()
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        assert report.sessions_skipped_egress_unreadable == 0
        assert sensitivity_of(_memory(registry)["metadata"]) == "restricted"

    def test_a_pack_the_log_no_longer_holds_names_nothing_and_stamps_nothing(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        _session(tmp_path / "projects", A)

        _run(registry, tmp_path)

        assert DATA_CLASSIFICATION_KEY not in _memory(registry)["metadata"]

    def test_a_stored_row_that_cannot_be_read_errors_the_session_not_a_bare_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-secret", "body", _label("confidential"))
        _serve(registry, A, "doc-secret")
        real = docs.get

        def down(doc_id: str) -> Any:
            if doc_id.startswith("capture:"):
                msg = "store down"
                raise sqlite3.OperationalError(msg)
            return real(doc_id)

        monkeypatch.setattr(docs, "get", down)
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path)

        monkeypatch.setattr(docs, "get", real)
        assert _stored_captures(registry) == []
        assert report.memories_written == 0
        assert report.sessions_errored == 1


class TestALabelIsNeverLowered:
    def _seed_memory(
        self, registry: Any, tmp_path: Path, stored_label: dict[str, Any]
    ) -> str:
        """Capture the memory unlabelled, then give its row other text and a label."""
        _session(tmp_path / "projects", A)
        _run(registry, tmp_path, wm="wm-first.json")
        doc_id = _stored_captures(registry)[0]["doc_id"]
        registry.knowledge.document_store.put(doc_id, "older wording", stored_label)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        return doc_id

    def test_a_stricter_stored_label_stands(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        doc_id = self._seed_memory(registry, tmp_path, _label("restricted"))

        report = _run(registry, tmp_path, wm="wm-second.json")

        assert report.memories_written == 1
        row = registry.knowledge.document_store.get(doc_id)
        assert sensitivity_of(row["metadata"]) == "restricted"
        assert report.memories_label_inherited == 0

    def test_a_laxer_stored_label_is_raised(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        doc_id = self._seed_memory(registry, tmp_path, _label("internal"))

        report = _run(registry, tmp_path, wm="wm-second.json")

        row = registry.knowledge.document_store.get(doc_id)
        assert sensitivity_of(row["metadata"]) == "confidential"
        assert report.memories_label_inherited_by_level == {"confidential": 1}

    @pytest.mark.parametrize(
        ("stored", "inherited", "expected"),
        [
            pytest.param(None, "confidential", "confidential", id="no-row"),
            pytest.param("public", "confidential", "confidential", id="lower-row"),
            pytest.param("confidential", "confidential", None, id="equal-row"),
            pytest.param("restricted", "confidential", None, id="stricter-row"),
            pytest.param("restricted", None, None, id="nothing-to-stamp"),
        ],
    )
    def test_label_to_stamp(
        self, stored: str | None, inherited: str | None, expected: str | None
    ) -> None:
        registry = MagicMock()
        row = None if stored is None else {"metadata": _label(stored)}
        registry.knowledge.document_store.get.return_value = row

        assert capture._label_to_stamp(registry, "doc", inherited) == expected  # type: ignore[arg-type]


class TestTheCountIsOfWhatWasWritten:
    def test_an_unchanged_memory_is_not_counted_as_labelled(
        self, tmp_path: Path
    ) -> None:
        """The same text captured again is skipped, so no label was written for it."""
        registry = _registry(tmp_path)
        _declare_local(registry)
        _session(tmp_path / "projects", A)
        _run(registry, tmp_path, wm="wm-first.json")
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")

        again = _run(registry, tmp_path, wm="wm-second.json")

        assert again.memories_skipped_unchanged == 1
        assert again.memories_written == 0
        assert again.memories_label_inherited == 0
        assert again.memories_label_inherited_by_level == {}


class TestADryRunReportsTheLabelAndWritesNothing:
    def test_dry_run_counts_the_stamp_and_stores_no_memory(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        _session(tmp_path / "projects", A)

        report = _run(registry, tmp_path, dry_run=True)

        assert _stored_captures(registry) == []
        assert report.memories_label_inherited == 1
        assert report.memories_label_inherited_by_level == {"confidential": 1}
        payload = report.to_payload()
        assert payload["memories_label_inherited"] == 1
        assert payload["memories_label_inherited_by_level"] == {"confidential": 1}

    def test_a_live_rerun_after_a_dry_run_writes_the_memory_stamped(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        _declare_local(registry)
        registry.knowledge.document_store.put(
            "doc-secret", "body", _label("confidential")
        )
        _serve(registry, A, "doc-secret")
        _session(tmp_path / "projects", A)
        _run(registry, tmp_path, dry_run=True)

        live = _run(registry, tmp_path)

        assert sensitivity_of(_memory(registry)["metadata"]) == "confidential"
        assert live.memories_label_inherited == 1


class TestTheServedReadIsShared:
    @pytest.mark.parametrize("local", [True, False], ids=["local", "cloud"])
    def test_a_served_document_is_read_once_per_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bool
    ) -> None:
        registry = _registry(tmp_path)
        if local:
            _declare_local(registry)
        docs = registry.knowledge.document_store
        docs.put("doc-plain", "body", {})
        _serve(registry, A, "doc-plain")
        _serve(registry, B, "doc-plain")
        reads: list[str] = []
        real = docs.get

        def counting(doc_id: str) -> Any:
            reads.append(doc_id)
            return real(doc_id)

        monkeypatch.setattr(docs, "get", counting)
        _session(tmp_path / "projects", A, B)

        _run(registry, tmp_path)

        assert reads.count("doc-plain") == 1


class TestTheCaptureReport:
    """``trellis worker capture-sessions`` shows the count in both formats."""

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        report: CaptureReport,
        *extra: str,
    ) -> Any:
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        monkeypatch.setattr(
            worker, "_get_registry", lambda: SimpleNamespace(stores_dir=tmp_path)
        )
        monkeypatch.setattr(capture_sweep, "run_sweep", lambda **_k: report)
        return runner.invoke(app, ["worker", "capture-sessions", *extra])

    def test_both_formats_carry_the_count_and_exit_alike(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report = CaptureReport(
            transcripts_root="x",
            memories_label_inherited=3,
            memories_label_inherited_by_level={"confidential": 2, "restricted": 1},
        )

        as_json = self._invoke(monkeypatch, tmp_path, report, "--format", "json")
        as_text = self._invoke(monkeypatch, tmp_path, report)

        payload = json.loads(as_json.output)
        assert payload["memories_label_inherited"] == 3
        assert payload["memories_label_inherited_by_level"] == {
            "confidential": 2,
            "restricted": 1,
        }
        assert "labels inherited: 3 memories" in as_text.output
        assert "2 confidential, 1 restricted" in as_text.output
        assert as_json.exit_code == as_text.exit_code == 0

    def test_a_sweep_that_inherited_nothing_prints_no_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report = CaptureReport(transcripts_root="x")

        as_json = self._invoke(monkeypatch, tmp_path, report, "--format", "json")
        as_text = self._invoke(monkeypatch, tmp_path, report)

        assert json.loads(as_json.output)["memories_label_inherited"] == 0
        assert "labels inherited" not in as_text.output
