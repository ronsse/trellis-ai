"""Capture checkpoints: a sweep keeps its progress as it goes.

The write, the supersessions, the training pairs and the watermark save run
every ``checkpoint_every`` judged sessions rather than once after the last, so a
sweep that dies, or whose store refuses a record, keeps what it had finished.
All tests are synchronous for the reason ``test_capture`` gives.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import trellis.ingest_corpus.sync as sync_module
from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture import capture
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


def _session(root: Path, session_id: str) -> Path:
    """A capture-mandatory (has_error) transcript."""
    path = root / "proj" / f"{session_id}.jsonl"
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

    def test_the_candidates_of_a_failed_session_that_landed_keep_their_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-written session: the landed memory is paired, its discard is not.

        The retry judges the session again, so it emits the discard itself, and
        with reconcile on it would drop the landed memory as a duplicate and
        never pair it.
        """
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
        reply = candidates_json(_candidate(0), unworthy, _candidate(1))
        self._fail_on(monkeypatch, "flag 1")

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([reply]),
        )

        assert report.sessions_errored == 1
        assert _cursors(wm, [path]) == [False]
        assert len(_stored(registry)) == 1
        judged = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED, limit=20
        )
        decisions = [e.payload.get("decision") for e in judged]
        assert decisions == ["keep"]

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
    stored = sorted((d["doc_id"], d["content"]) for d in _stored(registry))
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
