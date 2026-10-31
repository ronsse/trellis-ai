"""Capture checkpoints: a sweep keeps its progress as it goes.

The write, the supersessions, the training pairs and the watermark save run
every ``checkpoint_every`` judged sessions rather than once after the last, so a
sweep that dies, or whose store refuses a record, keeps what it had finished.
All tests are synchronous for the reason ``test_capture`` gives.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import trellis.ingest_corpus.sync as sync_module
from trellis.core.document_write import put_document
from trellis.retrieve.lifecycle import declared_successor
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.classification import Lifecycle
from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture import capture, reconcile_pass
from trellis_workers.session_capture.capture import run_capture
from trellis_workers.session_capture.watermark import WatermarkStore

from .conftest import (
    FakeLLMClient,
    assistant_turn,
    candidates_json,
    good_candidate,
    tool_result_turn,
    user_turn,
    write_transcript,
)

_SESSIONS = tuple(f"sess-ck-{i:04d}" for i in range(4))


def _registry(tmp_path: Path) -> MagicMock:
    from trellis.stores.sqlite.document import SQLiteDocumentStore
    from trellis.stores.sqlite.event_log import SQLiteEventLog
    from trellis.stores.sqlite.vector import SQLiteVectorStore

    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    return reg


def _session(root: Path, session_id: str, project: str = "proj") -> Path:
    """A capture-mandatory (has_error) transcript."""
    path = root / project / f"{session_id}.jsonl"
    write_transcript(
        path,
        [
            user_turn("run the deploy", session_id),
            assistant_turn("running the migration", "Bash", session_id),
            tool_result_turn(is_error=True, session_id=session_id),
        ],
    )
    return path


def _sessions(root: Path, count: int = len(_SESSIONS)) -> list[Path]:
    return [_session(root, session_id) for session_id in _SESSIONS[:count]]


def _candidate(n: int) -> dict:
    return good_candidate(title=f"Widget deploy needs flag {n}")


def _replies(count: int) -> list[str]:
    return [candidates_json(_candidate(n)) for n in range(count)]


def _stored(registry: MagicMock) -> list[dict]:
    docs = registry.knowledge.document_store.list_documents(limit=1000)
    return [d for d in docs if d["doc_id"].startswith("capture:claude-code:")]


def _documents(registry: MagicMock) -> list[tuple]:
    """Every stored row, metadata and lifecycle included, as comparable values."""
    docs = registry.knowledge.document_store.list_documents(limit=1000)
    return sorted(
        (
            d["doc_id"],
            d["content"],
            d.get("content_hash"),
            json.dumps(d["metadata"], sort_keys=True, default=str),
        )
        for d in docs
    )


def _stored_sessions(registry: MagicMock) -> list[str]:
    return sorted(d["metadata"]["session_id"] for d in _stored(registry))


def _cursors(wm: Path, paths: list[Path]) -> list[bool]:
    """Whether the watermark on disk covers each path."""
    store = WatermarkStore(wm)
    return [store.is_unchanged(path) for path in paths]


def _sweep_events(registry: MagicMock) -> list[dict]:
    events = registry.operational.event_log.get_events(
        event_type=EventType.CAPTURE_SWEEP_COMPLETED, limit=20
    )
    return [event.payload for event in events]


#: A second memory, unrelated to ``_candidate(n)`` so no reconcile judge is asked.
_OTHER = good_candidate(
    title="Billing cron drops rows",
    memory=(
        "The zebra billing cron drops rows whenever the ledger snapshot lags "
        "behind the nightly export, so rerun the export first."
    ),
)


def _distillation_decisions(registry: MagicMock) -> list[str]:
    """Every training-pair decision emitted so far, sorted."""
    events = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_OP_JUDGED, limit=200
    )
    return sorted(
        e.payload["decision"]
        for e in events
        if e.payload.get("op_type") == "distillation"
    )


class _Killed(BaseException):
    """Stands in for the process going away, which no ``except Exception`` sees."""


class _DiesOnCall(FakeLLMClient):
    def __init__(self, responses: list[str], die_at: int) -> None:
        super().__init__(responses)
        self._die_at = die_at

    async def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        if len(self.calls) + 1 == self._die_at:
            raise _Killed
        return await super().generate(**kwargs)


class _SyncSpy:
    """Records every ``SyncRecord`` the sweep hands the write seam, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.passes: list[list[tuple]] = []
        real = capture.sync_records

        def spy(registry, records, **kwargs):  # type: ignore[no-untyped-def]
            records = list(records)
            self.passes.append(
                [
                    (r.doc_id, r.source_key, r.content, r.handler_metadata)
                    for r in records
                ]
            )
            return real(registry, records, **kwargs)

        monkeypatch.setattr(capture, "sync_records", spy)

    @property
    def records(self) -> list[tuple]:
        return [record for one_pass in self.passes for record in one_pass]


class TestKeepsProgress:
    def test_a_killed_sweep_keeps_the_sessions_it_checkpointed(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        paths = _sessions(root)

        # Killed in the 4th judge call: sessions 0 and 1 had been checkpointed,
        # session 2 was judged and paid for but was not yet written.
        with pytest.raises(_Killed):
            run_capture(
                registry,
                transcripts_root=root,
                watermark_path=wm,
                llm_client=_DiesOnCall(_replies(4), die_at=4),
                checkpoint_every=2,
            )

        assert _stored_sessions(registry) == list(_SESSIONS[:2])
        assert _cursors(wm, paths) == [True, True, False, False]

        # The next sweep judges the two that are left, and only those.
        client = FakeLLMClient(_replies(4)[2:])
        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=client,
            checkpoint_every=2,
        )
        assert len(client.calls) == 2
        assert report.sessions_skipped_watermark == 2
        assert _stored_sessions(registry) == list(_SESSIONS)

    def test_a_cursor_is_saved_only_after_its_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At every save, each session in the file already has its memory stored."""
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        paths = _sessions(root)
        seen: list[tuple[int, int]] = []
        real_save = WatermarkStore.save

        def save(self: WatermarkStore) -> None:
            real_save(self)
            cursors = json.loads(wm.read_text())["cursors"]
            seen.append((len(cursors), len(_stored(registry))))

        monkeypatch.setattr(WatermarkStore, "save", save)

        run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(_replies(4)),
            checkpoint_every=1,
        )

        assert seen[:4] == [(1, 1), (2, 2), (3, 3), (4, 4)]
        assert all(_cursors(wm, paths))

    def test_checkpoints_fall_every_n_judged_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        root = tmp_path / "projects"
        _sessions(root)
        spy = _SyncSpy(monkeypatch)

        run_capture(
            registry,
            transcripts_root=root,
            watermark_path=tmp_path / "wm.json",
            llm_client=FakeLLMClient(_replies(4)),
            checkpoint_every=3,
        )

        assert [len(one_pass) for one_pass in spy.passes] == [3, 1]

    def test_checkpoint_every_must_be_positive(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="checkpoint_every"):
            run_capture(
                _registry(tmp_path),
                transcripts_root=tmp_path / "projects",
                watermark_path=tmp_path / "wm.json",
                llm_client=FakeLLMClient([]),
                checkpoint_every=0,
            )

    def test_a_dry_run_writes_one_pass_whatever_the_cadence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        _sessions(root)
        spy = _SyncSpy(monkeypatch)

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(_replies(4)),
            dry_run=True,
            checkpoint_every=1,
        )

        assert [len(one_pass) for one_pass in spy.passes] == [4]
        assert report.sessions_with_memory == 4
        assert _stored(registry) == []
        assert not wm.exists()


class TestStoreFault:
    @staticmethod
    def _fail_on(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
        real = sync_module.put_document

        def flaky(doc_store, vector_store, doc_id, content, *args, **kwargs):  # type: ignore[no-untyped-def]
            if marker in content:
                msg = "transient store error"
                raise RuntimeError(msg)
            return real(doc_store, vector_store, doc_id, content, *args, **kwargs)

        monkeypatch.setattr(sync_module, "put_document", flaky)

    def test_one_failed_record_leaves_the_other_sessions_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        paths = _sessions(root, 3)
        self._fail_on(monkeypatch, "flag 1")

        # The default cadence: all three sessions share one write pass.
        client = FakeLLMClient(_replies(3))
        report = run_capture(
            registry, transcripts_root=root, watermark_path=wm, llm_client=client
        )

        assert _stored_sessions(registry) == [_SESSIONS[0], _SESSIONS[2]]
        assert _cursors(wm, paths) == [True, False, True]
        assert report.sessions_errored == 1
        assert report.memories_written == 2
        assert report.sessions_with_memory == 2
        assert [w for w in report.warnings if w["kind"] == "session_error"] == [
            {
                "kind": "session_error",
                "session_id": _SESSIONS[1],
                "error_class": "RuntimeError",
            }
        ]

        # The sweep event still fires, and it reports the error.
        (event,) = _sweep_events(registry)
        assert event["sessions_errored"] == 1
        assert event["warning_kinds"]["session_error"] == 1

        # The retry judges the failed session and nothing else.
        monkeypatch.undo()
        retry = FakeLLMClient(_replies(3)[1:2])
        report = run_capture(
            registry, transcripts_root=root, watermark_path=wm, llm_client=retry
        )
        assert len(retry.calls) == 1
        assert report.sessions_errored == 0
        assert _stored_sessions(registry) == list(_SESSIONS[:3])

    @pytest.mark.parametrize(
        ("reconcile", "expected"),
        [(False, ["discard", "keep", "keep"]), (True, ["discard", "keep"])],
        ids=["reconcile_off", "reconcile_on"],
    )
    def test_a_failed_session_emits_no_pairs_and_its_retry_emits_each_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reconcile: bool,
        expected: list[str],
    ) -> None:
        """A half-written session has no watermark, so its pairs wait for the retry.

        Run 1 lands one of two memories and emits nothing. The retry judges the
        session afresh; with reconcile off the landed memory is skipped as
        unchanged but still paired, and it is paired once, not once per run.
        With reconcile on the landed memory is dropped as an exact duplicate, so
        it is never paired: the cost of a session that fails part way, bounded
        by that one session.
        """
        if reconcile:
            monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        (path,) = _sessions(root, 1)
        unworthy = good_candidate(
            durable=False,
            title="Ran the test suite once",
            memory=(
                "Ran the full test suite and it passed, which is what always "
                "happens and tells a future session nothing it could act on."
            ),
        )
        reply = candidates_json(_candidate(0), unworthy, _OTHER)
        self._fail_on(monkeypatch, "zebra")

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([reply]),
        )

        assert report.sessions_errored == 1
        assert _cursors(wm, [path]) == [False]
        assert len(_stored(registry)) == 1
        assert _distillation_decisions(registry) == []

        monkeypatch.undo()
        if reconcile:
            monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([reply]),
        )
        assert report.sessions_errored == 0
        assert _cursors(wm, [path]) == [True]
        assert len(_stored(registry)) == 2
        assert _distillation_decisions(registry) == expected

    def test_a_write_seam_that_raises_errors_the_batch_and_the_sweep_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        paths = _sessions(root, 3)

        def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
            msg = "store down"
            raise OSError(msg)

        monkeypatch.setattr(capture, "sync_records", refuse)

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(_replies(3)),
            checkpoint_every=2,
        )

        assert report.sessions_errored == 3
        assert _cursors(wm, paths) == [False, False, False]
        (event,) = _sweep_events(registry)
        assert event["sessions_errored"] == 3

    def test_a_failure_after_the_write_leaves_the_batch_to_be_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        paths = _sessions(root, 2)

        def refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
            msg = "event log down"
            raise OSError(msg)

        monkeypatch.setattr(capture, "_emit_training_pairs", refuse)

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(_replies(2)),
        )

        assert report.sessions_errored == 2
        assert len(_stored(registry)) == 2
        assert _cursors(wm, paths) == [False, False]
        assert len(_sweep_events(registry)) == 1


class TestNulInAReply:
    def test_a_nul_in_the_judge_reply_never_reaches_the_store(
        self, tmp_path: Path
    ) -> None:
        registry = _registry(tmp_path)
        root = tmp_path / "projects"
        _sessions(root, 1)
        candidate = good_candidate()
        candidate["memory"] += " nul\x00here"

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=tmp_path / "wm.json",
            llm_client=FakeLLMClient([candidates_json(candidate)]),
        )

        (doc,) = _stored(registry)
        assert report.memories_written == 1
        assert "\x00" not in doc["content"]
        assert "nulhere" in doc["content"]
        assert not any("\x00" in str(value) for value in doc["metadata"].values())


# ---------------------------------------------------------------------------
# A clean run is the same run, whatever the cadence
# ---------------------------------------------------------------------------

_NEAR_MEMORY = good_candidate()["memory"].replace("boots", "starts")


def _clean_scenario() -> list[str]:
    """Five judged sessions, built to cross every checkpoint boundary.

    Session 1's memory is a near-duplicate of session 0's, session 2's is
    identical to it, session 3 adds a memory and a judge refusal, and session 4
    yields nothing. Each reconciliation and each warning the single-pass sweep
    produced between these sessions is therefore one a checkpoint could change.
    """
    unworthy = good_candidate(
        durable=False,
        title="Ran the test suite once",
        memory=(
            "Ran the full test suite and it passed, which is what always "
            "happens and tells a future session nothing it could act on."
        ),
    )
    return [
        candidates_json(good_candidate()),
        candidates_json(good_candidate(memory=_NEAR_MEMORY)),
        candidates_json(good_candidate()),
        candidates_json(_candidate(3), unworthy),
        candidates_json(),
    ]


def _run_arm(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, every: int
) -> tuple[list[tuple], dict, list[dict], list[str], FakeLLMClient]:
    arm = tmp_path / name
    arm.mkdir()
    registry = _registry(arm)
    root = arm / "projects"
    spy = _SyncSpy(monkeypatch)
    sessions = tuple(f"sess-id-{i:04d}" for i in range(5))
    for session_id in sessions:
        _session(root, session_id)
    client = FakeLLMClient(_clean_scenario())
    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=arm / "wm.json",
        llm_client=client,
        checkpoint_every=every,
    )
    payload = report.to_payload()
    payload.pop("transcripts_root")
    stored = _documents(registry)
    judged = sorted(
        json.dumps(e.payload, sort_keys=True)
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED, limit=100
        )
    )
    return spy.records, payload, stored, judged, client


@pytest.mark.parametrize(
    "reconcile", [False, True], ids=["reconcile_off", "reconcile_on"]
)
@pytest.mark.parametrize("every", [1, 2, 4])
def test_a_clean_run_writes_the_same_records_in_the_same_order_at_any_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconcile: bool,
    every: int,
) -> None:
    if reconcile:
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    once = _run_arm(tmp_path, "once", monkeypatch, every=100)
    cadence = _run_arm(tmp_path, f"every{every}", monkeypatch, every=every)

    records_once, payload_once, stored_once, judged_once, client_once = once
    records_n, payload_n, stored_n, judged_n, client_n = cadence

    assert len(records_once) == 4  # sessions 0-3 write one each; 4 has none
    assert records_n == records_once  # content, order, metadata, source keys
    assert stored_n == stored_once
    assert payload_n == payload_once  # counts and every warning, near-duplicates too
    assert judged_n == judged_once
    assert len(client_n.calls) == len(client_once.calls)
    assert any(w["kind"] == "near_duplicate" for w in payload_once["warnings"])


def test_a_checkpoint_does_not_show_a_later_session_the_sweeps_own_memories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With reconcile on, session 1 must not be judged against session 0's memory.

    A sweep that writes once at its end never lets one session's adjudication
    see another's candidate; a checkpoint writes session 0 first, so without the
    hidden ids session 1 would be reconciled against it, a model call and a
    verdict the single pass never made.
    """
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _sessions(root, 2)
    client = FakeLLMClient(
        [
            candidates_json(good_candidate()),
            candidates_json(good_candidate(memory=_NEAR_MEMORY)),
            '{"decision": "noop", "confidence": 0.9}',
        ]
    )

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
        checkpoint_every=1,
    )

    assert len(client.calls) == 2  # two distillations, no reconcile judge
    assert report.candidates_reconciled_noop == 0
    assert report.memories_written == 2
    assert len(_stored(registry)) == 2


# ---------------------------------------------------------------------------
# A stored parent is not a landed record
# ---------------------------------------------------------------------------


def _big_candidate() -> dict:
    """A memory long enough to be stored as a parent with chunk documents."""
    paragraph = "The frobnicator migration must run first because the probe fails. "
    return good_candidate(memory="\n\n".join([paragraph * 20] * 12))


def _chunk_rows(registry: MagicMock) -> list[dict]:
    docs = registry.knowledge.document_store.list_documents(limit=1000)
    return [d for d in docs if "#chunk-" in d["doc_id"]]


def _fail_chunks(patch: pytest.MonkeyPatch) -> None:
    real = sync_module.put_document

    def flaky(doc_store, vector_store, doc_id, content, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "#chunk-" in doc_id:
            msg = "transient store error"
            raise RuntimeError(msg)
        return real(doc_store, vector_store, doc_id, content, *args, **kwargs)

    patch.setattr(sync_module, "put_document", flaky)


@pytest.mark.parametrize(
    "reconcile", [False, True], ids=["reconcile_off", "reconcile_on"]
)
def test_a_record_whose_chunk_fails_is_written_again_by_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconcile: bool
) -> None:
    """A parent stored without its chunks must not read as landed to the retry.

    Run 1 dies on a chunk write, after the parent is stored. The retry finds the
    parent's chunk rows missing, so neither the write's same-hash skip nor
    reconcile's exact-duplicate drop applies; it stores the whole record, in
    either reconcile mode, and only then watermarks the session.
    """
    if reconcile:
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    registry = _registry(tmp_path)
    root, wm = tmp_path / "projects", tmp_path / "wm.json"
    (path,) = _sessions(root, 1)
    reply = candidates_json(_big_candidate())

    with monkeypatch.context() as patch:
        _fail_chunks(patch)
        first = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([reply]),
        )

    assert first.sessions_errored == 1
    assert _cursors(wm, [path]) == [False]
    assert len(_stored(registry)) == 1  # the parent, with no chunk behind it
    assert _chunk_rows(registry) == []

    second = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=FakeLLMClient([reply]),
    )

    assert second.sessions_errored == 0
    assert second.memories_written == 1
    assert second.candidates_reconciled_noop == 0
    assert _cursors(wm, [path]) == [True]
    (parent,) = [d for d in _stored(registry) if "#chunk-" not in d["doc_id"]]
    assert parent["metadata"]["chunk_count"] == len(_chunk_rows(registry)) >= 2
    stored_events = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_STORED, limit=10
    )
    assert len(stored_events) == 1


@pytest.mark.parametrize(
    "reconcile", [False, True], ids=["reconcile_off", "reconcile_on"]
)
def test_a_record_with_all_its_chunks_is_still_dropped_as_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reconcile: bool
) -> None:
    """The check does not turn every re-judged chunked memory into a rewrite."""
    if reconcile:
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    registry = _registry(tmp_path)
    root, wm = tmp_path / "projects", tmp_path / "wm.json"
    _sessions(root, 1)
    reply = candidates_json(_big_candidate())
    run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=FakeLLMClient([reply]),
    )
    rows = _documents(registry)

    wm.unlink()  # a reset cursor judges the session again
    second = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=FakeLLMClient([reply]),
    )

    assert second.memories_written == 0
    assert (
        second.candidates_reconciled_noop,
        second.memories_skipped_unchanged,
    ) == ((1, 0) if reconcile else (0, 1))
    assert _documents(registry) == rows
    stored = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_STORED, limit=10
    )
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# Supersessions survive a fault, and a retry repairs one that was lost
# ---------------------------------------------------------------------------

_SUPERSEDE = '{"decision": "supersede", "confidence": 0.9}'
_UPDATE = '{"decision": "update", "confidence": 0.9}'
_NOOP = '{"decision": "noop", "confidence": 0.9}'


def _seed(registry: MagicMock, doc_id: str, memory: str) -> None:
    """A capture stored before the sweep, as an earlier sweep would have left it."""
    put_document(
        registry.knowledge.document_store,
        registry.knowledge.vector_store,
        doc_id,
        (
            "# Widget deploy needs the migrate flag first\n\n"
            f"{memory}\n\n**Signal:** failure\n"
            "**Evidence:** deploy/playbook.yml step order; observed boot-probe error\n"
            "**Source:** Claude Code session `old`\n"
        ),
        {"session_id": "old"},
        preserve_updated_at=False,
    )


def _superseded_by(registry: MagicMock, doc_id: str) -> str | None:
    doc = registry.knowledge.document_store.get(doc_id)
    return declared_successor(doc["metadata"])


def _doc_of_session(registry: MagicMock, session_id: str) -> dict:
    (doc,) = [
        d for d in _stored(registry) if d["metadata"].get("session_id") == session_id
    ]
    return doc


class TestSupersessionIsRepaired:
    _OLD = "capture:claude-code:old-y"

    def _setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[MagicMock, Path, Path, list[Path]]:
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        _seed(registry, self._OLD, _NEAR_MEMORY)
        paths = [_session(root, "sess-a"), _session(root, "sess-b")]
        return registry, root, wm, paths

    @staticmethod
    def _refuse(monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]) -> None:
        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            msg = "store blip"
            raise exc(msg)

        monkeypatch.setattr(reconcile_pass, "mark_document_superseded", boom)

    def test_a_raise_keeps_the_pairs_and_the_retry_applies_the_supersession(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, paths = self._setup(tmp_path, monkeypatch)
        replies = [
            candidates_json(good_candidate()),
            _SUPERSEDE,
            candidates_json(_OTHER),
        ]
        with monkeypatch.context() as patch:
            self._refuse(patch, RuntimeError)
            first = run_capture(
                registry,
                transcripts_root=root,
                watermark_path=wm,
                llm_client=FakeLLMClient(list(replies)),
            )

        # Only the session that held the supersession is left for the retry.
        assert first.sessions_errored == 1
        assert _cursors(wm, paths) == [False, True]
        assert _superseded_by(registry, self._OLD) is None
        # The raise did not take the batch's training pairs down with it.
        assert _distillation_decisions(registry) == ["keep", "keep"]

        retry = FakeLLMClient([replies[0]])
        second = run_capture(
            registry, transcripts_root=root, watermark_path=wm, llm_client=retry
        )

        assert len(retry.calls) == 1  # no reconcile judge: an exact duplicate
        assert second.sessions_errored == 0
        assert second.candidates_reconciled_noop == 1
        assert _cursors(wm, paths) == [True, True]
        successor = _doc_of_session(registry, "sess-a")
        assert _superseded_by(registry, self._OLD) == successor["doc_id"]
        # Still two pairs: the retry's duplicate is dropped, not paired again.
        assert _distillation_decisions(registry) == ["keep", "keep"]

    def test_a_kill_loses_the_batchs_pairs_but_the_retry_applies_the_supersession(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, paths = self._setup(tmp_path, monkeypatch)
        replies = [
            candidates_json(good_candidate()),
            _SUPERSEDE,
            candidates_json(_OTHER),
        ]
        with monkeypatch.context() as patch:
            self._refuse(patch, _Killed)
            with pytest.raises(_Killed):
                run_capture(
                    registry,
                    transcripts_root=root,
                    watermark_path=wm,
                    llm_client=FakeLLMClient(list(replies)),
                )

        assert _cursors(wm, paths) == [False, False]
        assert _superseded_by(registry, self._OLD) is None

        second = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([replies[0], replies[2]]),
        )

        assert second.sessions_errored == 0
        assert second.candidates_reconciled_noop == 2
        assert _cursors(wm, paths) == [True, True]
        successor = _doc_of_session(registry, "sess-a")
        assert _superseded_by(registry, self._OLD) == successor["doc_id"]
        # The documented limit: the killed batch's keep pairs are not recovered.
        assert _distillation_decisions(registry) == []

    def test_a_repair_that_is_already_applied_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, paths = self._setup(tmp_path, monkeypatch)
        run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(
                [candidates_json(good_candidate()), _SUPERSEDE, candidates_json(_OTHER)]
            ),
        )
        before = registry.knowledge.document_store.get(self._OLD)
        assert _superseded_by(registry, self._OLD) is not None

        # Re-judge both sessions: every candidate is an exact duplicate.
        for path in paths:
            path.touch()
        calls: list[tuple] = []
        real = reconcile_pass.mark_document_superseded

        def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(reconcile_pass, "mark_document_superseded", spy)
        second = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(
                [candidates_json(good_candidate()), candidates_json(_OTHER)]
            ),
        )

        assert second.candidates_reconciled_noop == 2
        assert calls == []
        assert registry.knowledge.document_store.get(self._OLD) == before

    def test_a_landed_candidate_of_a_failed_session_still_gets_its_supersession(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run 1, before any retry: the stale-mark is not left for the repair."""
        registry, root, wm, paths = self._setup(tmp_path, monkeypatch)
        TestStoreFault._fail_on(monkeypatch, "zebra")

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient(
                [
                    candidates_json(good_candidate(), _OTHER),
                    _SUPERSEDE,
                    candidates_json(_candidate(7)),
                ]
            ),
        )

        assert report.sessions_errored == 1
        assert _cursors(wm, paths) == [False, True]
        successor = _doc_of_session(registry, "sess-a")
        assert _superseded_by(registry, self._OLD) == successor["doc_id"]


# ---------------------------------------------------------------------------
# A clean run is the same run with documents already in the store
# ---------------------------------------------------------------------------


def _store_arm(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, every: int
) -> dict:
    arm = tmp_path / name
    arm.mkdir()
    registry = _registry(arm)
    root = arm / "projects"
    _seed(registry, "capture:claude-code:old-y1", _NEAR_MEMORY)
    _seed(
        registry, "capture:claude-code:old-y2", _NEAR_MEMORY.replace("first", "before")
    )
    for index in range(5):
        _session(root, f"sess-st-{index}")
    spy = _SyncSpy(monkeypatch)
    replies = []
    for verdict in (_SUPERSEDE, _SUPERSEDE, _UPDATE, _NOOP, _SUPERSEDE):
        replies += [candidates_json(good_candidate()), verdict]
    client = FakeLLMClient(replies)
    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=arm / "wm.json",
        llm_client=client,
        checkpoint_every=every,
    )
    payload = report.to_payload()
    payload.pop("transcripts_root")
    return {
        "records": spy.records,
        "report": payload,
        "documents": _documents(registry),
        "prompts": [repr(call) for call in client.calls],
        "events": sorted(
            json.dumps(e.payload, sort_keys=True)
            for e in registry.operational.event_log.get_events(
                event_type=EventType.MEMORY_OP_JUDGED, limit=200
            )
        ),
    }


@pytest.mark.parametrize("every", [1, 2])
def test_supersessions_against_stored_documents_are_the_same_at_any_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, every: int
) -> None:
    """Each batch's supersessions land in the one place a single pass put them."""
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    once = _store_arm(tmp_path, "once", monkeypatch, every=100)
    cadence = _store_arm(tmp_path, f"every{every}", monkeypatch, every=every)

    assert once["report"]["candidates_reconciled_supersede"] == 3
    assert once["report"]["supersessions_failed"] == 0
    old = [d for d in once["documents"] if d[0].endswith(("old-y1", "old-y2"))]
    assert any('"state": "superseded"' in d[3] for d in old)
    for key in once:
        assert cadence[key] == once[key], key


def test_two_transcripts_with_one_session_id_are_the_same_run_at_any_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one way a later session can render exactly an earlier one's memory.

    Two transcripts sharing a session id and a reply produce one document id and
    one text. The single pass adjudicates both against the store as it was and
    writes the second as unchanged; a checkpoint must not instead drop it as an
    exact duplicate of the first, which is what the hidden ids prevent.
    """
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    arms = {}
    for name, every in (("once", 100), ("each", 1)):
        arm = tmp_path / name
        arm.mkdir()
        registry = _registry(arm)
        root = arm / "projects"
        _session(root, "sess-twin", project="proj-one")
        _session(root, "sess-twin", project="proj-two")
        spy = _SyncSpy(monkeypatch)
        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=arm / "wm.json",
            llm_client=FakeLLMClient([candidates_json(good_candidate())] * 2),
            checkpoint_every=every,
        )
        payload = report.to_payload()
        payload.pop("transcripts_root")
        arms[name] = (
            len(spy.records),
            payload,
            _documents(registry),
            _distillation_decisions(registry),
        )

    assert arms["once"][0] == 2
    assert arms["once"][1]["candidates_reconciled_noop"] == 0
    assert arms["once"][3] == ["keep", "keep"]
    assert arms["each"] == arms["once"]


class TestRepairLeavesATargetAlone:
    """The repair stamps a supersession; it must not undo what an operator did."""

    _OLD = "capture:claude-code:old-y"

    def _first(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        refuse: bool = False,
    ) -> tuple[MagicMock, Path, Path, Path]:
        """A sweep whose capture supersedes the seeded memory, or fails to."""
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        _seed(registry, self._OLD, _NEAR_MEMORY)
        path = _session(root, "sess-a")
        replies = [candidates_json(good_candidate()), _SUPERSEDE]
        with monkeypatch.context() as patch:
            if refuse:

                def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
                    msg = "store blip"
                    raise RuntimeError(msg)

                patch.setattr(reconcile_pass, "mark_document_superseded", boom)
            run_capture(
                registry,
                transcripts_root=root,
                watermark_path=wm,
                llm_client=FakeLLMClient(list(replies)),
            )
        return registry, root, wm, path

    @staticmethod
    def _set_lifecycle(registry: MagicMock, doc_id: str, record: dict) -> None:
        docs = registry.knowledge.document_store
        doc = docs.get(doc_id)
        put_document(
            docs,
            registry.knowledge.vector_store,
            doc_id,
            doc["content"],
            {**doc["metadata"], "lifecycle": record},
            preserve_updated_at=True,
        )

    @staticmethod
    def _resweep(registry, root, wm, *, dry_run=False):  # type: ignore[no-untyped-def]
        wm.unlink(missing_ok=True)  # a reset cursor judges the session again
        return run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            dry_run=dry_run,
            llm_client=FakeLLMClient([candidates_json(good_candidate())]),
        )

    @staticmethod
    def _pack(registry: MagicMock, query: str) -> list[str]:
        docs = registry.knowledge.document_store
        builder = PackBuilder(strategies=[KeywordSearch(docs)], document_store=docs)
        return [i.item_id for i in builder.build(query, clearance="restricted").items]

    def test_repair_does_not_unarchive_an_archived_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, _ = self._first(tmp_path, monkeypatch)
        self._set_lifecycle(
            registry,
            self._OLD,
            {"state": "archived", "deprecation_reason": "operator retired it"},
        )
        self._resweep(registry, root, wm)
        stored = registry.knowledge.document_store.get(self._OLD)
        assert stored["metadata"]["lifecycle"]["state"] == "archived"

    def test_archived_target_does_not_return_to_a_pack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The query matches only the target, so its successor withholds nothing."""
        registry, root, wm, _ = self._first(tmp_path, monkeypatch)
        archived = Lifecycle(state="archived", deprecation_reason="retention")
        self._set_lifecycle(registry, self._OLD, archived.model_dump(mode="json"))
        assert self._OLD not in self._pack(registry, "starts")

        self._resweep(registry, root, wm)

        assert self._OLD not in self._pack(registry, "starts")

    def test_a_target_deleted_after_a_good_supersession_is_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, _ = self._first(tmp_path, monkeypatch)
        doc = _doc_of_session(registry, "sess-a")
        assert doc["metadata"]["supersedes_doc_id"] == self._OLD
        registry.knowledge.document_store.delete(self._OLD)

        report = self._resweep(registry, root, wm)

        after = registry.knowledge.document_store.get(doc["doc_id"])["metadata"]
        assert report.supersessions_failed == 0
        assert after["supersedes_doc_id"] == self._OLD
        assert after["reconciliation"] == "supersede"

    def test_dry_run_stamps_nothing_then_live_repairs_and_keeps_the_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, root, wm, path = self._first(tmp_path, monkeypatch, refuse=True)
        docs = registry.knowledge.document_store
        before = docs.get(self._OLD)
        assert _superseded_by(registry, self._OLD) is None
        raw = wm.read_bytes() if wm.exists() else None

        dry = run_capture(
            registry,
            transcripts_root=tmp_path / "projects",
            watermark_path=wm,
            dry_run=True,
            llm_client=FakeLLMClient([candidates_json(good_candidate())]),
        )

        assert dry.candidates_reconciled_noop == 1
        assert docs.get(self._OLD) == before
        assert (wm.read_bytes() if wm.exists() else None) == raw

        run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([candidates_json(good_candidate())]),
        )

        after = docs.get(self._OLD)
        assert (
            _superseded_by(registry, self._OLD)
            == (_doc_of_session(registry, "sess-a")["doc_id"])
        )
        assert after["updated_at"] == before["updated_at"]
        assert _cursors(wm, [path]) == [True]


# ---------------------------------------------------------------------------
# An unlanded record keeps the verdict it was stored with
# ---------------------------------------------------------------------------


def _varied_memory(seed: int, *, drop_every: int = 0) -> str:
    """A chunked memory whose shingles are distinct, so near-duplicates match.

    :func:`_big_candidate` repeats one paragraph, which gives MinHash almost
    nothing to compare. ``drop_every`` removes words, for the near-duplicate.
    """
    rng = random.Random(seed)  # noqa: S311 - a fixture, not a secret
    words = [f"w{rng.randrange(100_000)}" for _ in range(1900)]
    if drop_every:
        words = [w for i, w in enumerate(words) if i % drop_every]
    return "\n\n".join(" ".join(words[i : i + 150]) for i in range(0, len(words), 150))


class TestAnUnlandedRecordKeepsItsVerdict:
    """The retry of a half-written record is not judged again.

    Run 1 stores the parent and dies before its chunks. Its record already
    carries the verdict that run reached; a second judgement of the same record
    can only contradict it, and the contradictions both lose something.
    """

    _NEIGHBOUR = "capture:claude-code:old-near"

    def _half_written(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        replies: list[str],
        seed_before: bool,
    ) -> tuple[MagicMock, Path, Path, Path]:
        monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
        registry = _registry(tmp_path)
        root, wm = tmp_path / "projects", tmp_path / "wm.json"
        (path,) = _sessions(root, 1)
        if seed_before:
            _seed(registry, self._NEIGHBOUR, _varied_memory(1, drop_every=40))
        with monkeypatch.context() as patch:
            _fail_chunks(patch)
            first = run_capture(
                registry,
                transcripts_root=root,
                watermark_path=wm,
                llm_client=FakeLLMClient(replies),
            )
        assert first.sessions_errored == 1
        assert _cursors(wm, [path]) == [False]
        assert _chunk_rows(registry) == []
        return registry, root, wm, path

    @classmethod
    def _parent_of_the_capture(cls, registry: MagicMock) -> dict[str, Any]:
        (parent,) = [
            d
            for d in _stored(registry)
            if d["doc_id"] != cls._NEIGHBOUR and "#chunk-" not in d["doc_id"]
        ]
        return parent

    @classmethod
    def _assert_the_neighbour_is_a_near_duplicate(cls, registry: MagicMock) -> None:
        """The canary: without it the tests below could pass by never judging."""
        from trellis.classify.dedup.minhash import MinHashIndex

        docs = registry.knowledge.document_store
        index = MinHashIndex()
        index.add(cls._NEIGHBOUR, docs.get(cls._NEIGHBOUR)["content"])
        parent = cls._parent_of_the_capture(registry)
        hits = dict(index.query(docs.get(parent["doc_id"])["content"]))
        assert hits.get(cls._NEIGHBOUR, 0.0) > 0.5
        assert hits[cls._NEIGHBOUR] < reconcile_pass._NEAR_CEILING

    def test_a_judge_that_says_noop_cannot_drop_a_record_whose_chunks_are_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reply = candidates_json(good_candidate(memory=_varied_memory(1)))
        registry, root, wm, path = self._half_written(
            tmp_path, monkeypatch, replies=[reply], seed_before=False
        )
        # The neighbour arrives between the runs, so this run would judge.
        _seed(registry, self._NEIGHBOUR, _varied_memory(1, drop_every=40))
        self._assert_the_neighbour_is_a_near_duplicate(registry)

        retry = FakeLLMClient([reply, _NOOP])
        second = run_capture(
            registry, transcripts_root=root, watermark_path=wm, llm_client=retry
        )

        assert len(retry.calls) == 1  # the distillation; no reconcile judge
        assert second.sessions_errored == 0
        assert second.candidates_reconciled_noop == 0
        assert second.memories_written == 1
        assert _cursors(wm, [path]) == [True]
        parent = self._parent_of_the_capture(registry)
        assert parent["metadata"]["chunk_count"] == len(_chunk_rows(registry)) >= 2

    def test_a_retry_that_cannot_judge_keeps_the_supersession_the_first_run_claimed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reply = candidates_json(good_candidate(memory=_varied_memory(1)))
        registry, root, wm, path = self._half_written(
            tmp_path, monkeypatch, replies=[reply, _SUPERSEDE], seed_before=True
        )
        self._assert_the_neighbour_is_a_near_duplicate(registry)
        parent = self._parent_of_the_capture(registry)
        # What run 1 left: a claim on the stored row, and an unmarked target.
        assert parent["metadata"]["supersedes_doc_id"] == self._NEIGHBOUR
        assert parent["metadata"]["reconciliation"] == "supersede"
        assert _superseded_by(registry, self._NEIGHBOUR) is None

        # The model is unreachable, which reconcile reads as "add, skipped".
        retry = FakeLLMClient([reply])
        second = run_capture(
            registry, transcripts_root=root, watermark_path=wm, llm_client=retry
        )

        assert len(retry.calls) == 1
        assert second.sessions_errored == 0
        assert _cursors(wm, [path]) == [True]
        stored = registry.knowledge.document_store.get(parent["doc_id"])
        assert stored["metadata"]["reconciliation"] == "supersede"
        assert stored["metadata"]["supersedes_doc_id"] == self._NEIGHBOUR
        assert _superseded_by(registry, self._NEIGHBOUR) == parent["doc_id"]
        assert stored["metadata"]["chunk_count"] == len(_chunk_rows(registry)) >= 2
