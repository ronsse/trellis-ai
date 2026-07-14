"""End-to-end capture sweep — writes go through the sanctioned seam.

All tests are synchronous (``def``): :func:`distill_session` and
:func:`judge_reconcile` call ``asyncio.run`` internally, which requires no
already-running event loop.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture import capture
from trellis_workers.session_capture.capture import run_capture

from .conftest import (
    BrokenLLMClient,
    FakeLLMClient,
    assistant_turn,
    candidates_json,
    good_candidate,
    tool_result_turn,
    user_turn,
    write_transcript,
)


def _error_session(path: Path, session_id: str = "sess-fake-0001") -> None:
    """A capture-mandatory (has_error) transcript."""
    write_transcript(
        path,
        [
            user_turn("run the deploy", session_id),
            assistant_turn("running the migration", "Bash", session_id),
            tool_result_turn(is_error=True, session_id=session_id),
        ],
    )


def _stored_captures(registry: MagicMock) -> list[dict]:
    docs = registry.knowledge.document_store.list_documents(limit=1000)
    return [d for d in docs if d["doc_id"].startswith("capture:claude-code:")]


def test_golden_transcript_writes_one_memory(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    client = FakeLLMClient([candidates_json(good_candidate())])

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
    )

    assert report.memories_written == 1
    stored = _stored_captures(registry)
    assert len(stored) == 1
    doc = stored[0]
    assert "migration" in doc["content"]
    assert doc["metadata"]["session_id"] == "sess-fake-0001"
    assert doc["metadata"]["distilled"] is True
    assert doc["metadata"]["reconciliation"] == capture.MARKER_PENDING


def test_memory_stored_and_distillation_events_emitted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    client = FakeLLMClient([candidates_json(good_candidate())])

    run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
    )

    stored = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_STORED
    )
    assert len(stored) == 1
    judged = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_OP_JUDGED
    )
    assert len(judged) == 1
    payload = judged[0].payload
    assert payload["op_type"] == "distillation"
    assert payload["decision"] == "keep"
    # Leak-safe: the training event carries a digest, never memory content.
    assert "memory" not in payload
    assert "content" not in payload


def test_model_down_writes_nothing_and_leaves_watermark(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    wm = tmp_path / "wm.json"

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=BrokenLLMClient(),
    )

    assert report.memories_written == 0
    assert _stored_captures(registry) == []
    assert any(w["kind"] == "distill_unavailable" for w in report.warnings)
    # Un-watermarked: nothing was recorded, so a later run retries the session.
    assert not wm.exists()


def test_rerun_is_idempotent_via_watermark(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    wm = tmp_path / "wm.json"
    client = FakeLLMClient([candidates_json(good_candidate())])

    first = run_capture(
        registry, transcripts_root=root, watermark_path=wm, llm_client=client
    )
    second = run_capture(
        registry, transcripts_root=root, watermark_path=wm, llm_client=client
    )

    assert first.memories_written == 1
    assert second.memories_written == 0
    assert second.sessions_skipped_watermark == 1
    assert len(_stored_captures(registry)) == 1


def test_rerun_is_idempotent_via_content_hash_even_without_watermark(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    client = FakeLLMClient([candidates_json(good_candidate())])

    run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm1.json",
        llm_client=client,
    )
    # Fresh watermark forces a re-parse + re-distill; sync_records' content
    # hash keeps it from duplicating the identical memory.
    second = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm2.json",
        llm_client=client,
    )
    assert second.memories_written == 0
    assert second.memories_skipped_unchanged == 1
    assert len(_stored_captures(registry)) == 1


def test_secret_bearing_candidate_is_blocked(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    leaky = good_candidate(
        memory=(
            "The service authenticates with api_key="
            "sk_live_ABCDEF1234567890 which must be rotated per the runbook."
        )
    )
    client = FakeLLMClient([candidates_json(leaky)])

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
    )

    assert report.memories_written == 0
    assert report.candidates_blocked_secret == 1
    assert report.secret_hits_by_class.get("key_value_secret") == 1
    assert _stored_captures(registry) == []


def test_unworthy_candidate_is_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    client = FakeLLMClient([candidates_json(good_candidate(non_derivable=False))])

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
    )

    assert report.memories_written == 0
    assert report.candidates_rejected_worthiness == 1


def test_clean_session_sampled_out(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    # No error, no correction → sampled. A huge denominator samples it out
    # with overwhelming probability (P(keep) ~ 1e-9).
    write_transcript(
        root / "proj" / "sess-fake-clean.jsonl",
        [user_turn("just a routine question", "sess-fake-clean")],
    )
    client = FakeLLMClient([candidates_json(good_candidate())])

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
        sample_denominator=1_000_000_000,
    )

    assert report.sessions_sampled_out == 1
    assert report.sessions_triggered == 0
    assert client.calls == []
    assert _stored_captures(registry) == []


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    wm = tmp_path / "wm.json"
    client = FakeLLMClient([candidates_json(good_candidate())])

    report = run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=client,
        dry_run=True,
    )

    # A dry run reports the plan (mirroring sync_records) but writes nothing
    # durable and never advances the watermark.
    assert report.dry_run is True
    assert _stored_captures(registry) == []
    assert not wm.exists()


def test_reconcile_flag_on_drops_near_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")
    registry = _registry(tmp_path)
    root = tmp_path / "projects"
    wm = tmp_path / "wm.json"

    # Session 1: writes the original memory.
    _error_session(root / "proj" / "sess-fake-0001.jsonl", "sess-fake-0001")
    client1 = FakeLLMClient([candidates_json(good_candidate())])
    run_capture(
        registry, transcripts_root=root, watermark_path=wm, llm_client=client1
    )
    assert len(_stored_captures(registry)) == 1

    # Session 2: distils a NEAR-duplicate; the reconcile judge returns NOOP.
    _error_session(root / "proj" / "sess-fake-0002.jsonl", "sess-fake-0002")
    near = good_candidate(memory=good_candidate()["memory"].replace("boots", "starts"))
    client2 = FakeLLMClient(
        [candidates_json(near), '{"decision": "noop", "confidence": 0.9}']
    )
    report = run_capture(
        registry, transcripts_root=root, watermark_path=wm, llm_client=client2
    )

    assert report.candidates_reconciled_noop == 1
    assert report.memories_written == 0
    # Still exactly one stored memory — the near-dup was suppressed.
    assert len(_stored_captures(registry)) == 1


def _registry(tmp_path: Path) -> MagicMock:
    from trellis.stores.sqlite.document import SQLiteDocumentStore
    from trellis.stores.sqlite.event_log import SQLiteEventLog
    from trellis.stores.sqlite.vector import SQLiteVectorStore

    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    return reg
