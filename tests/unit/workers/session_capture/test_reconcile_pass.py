"""The capture sweep's reconcile pass — verdicts, and when they are applied.

This module had **zero** tests before #407/#408, which is how two silent
failures lived in it: a ``--dry-run`` that stale-marked pre-existing documents
and emitted audit events, and a ``mark_document_superseded`` return value that
both callers discarded.

Everything here is synthetic (this repo is public) and the local model is a
canned :class:`~.conftest.FakeLLMClient`; no test makes a network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest

from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture import reconcile_pass
from trellis_workers.session_capture.capture import run_capture
from trellis_workers.session_capture.models import CandidateMemory, CaptureReport

from .conftest import (
    FakeLLMClient,
    assistant_turn,
    candidates_json,
    good_candidate,
    tool_result_turn,
    user_turn,
    write_transcript,
)

_SUPERSEDE = '{"decision": "supersede", "confidence": 0.9}'


@pytest.fixture(autouse=True)
def _reconcile_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here is about the flag-gated verdict tier."""
    monkeypatch.setenv("TRELLIS_ENABLE_RECONCILE_ON_WRITE", "1")


def _registry(tmp_path: Path) -> MagicMock:
    from trellis.stores.sqlite.document import SQLiteDocumentStore
    from trellis.stores.sqlite.event_log import SQLiteEventLog
    from trellis.stores.sqlite.vector import SQLiteVectorStore

    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    return reg


def _error_session(path: Path, session_id: str) -> None:
    """A capture-mandatory (has_error) transcript."""
    write_transcript(
        path,
        [
            user_turn("run the deploy", session_id),
            assistant_turn("running the migration", "Bash", session_id),
            tool_result_turn(is_error=True, session_id=session_id),
        ],
    )


def _near_duplicate() -> dict[str, Any]:
    """A candidate ~0.9 Jaccard from :func:`good_candidate` — near, not exact."""
    return good_candidate(memory=good_candidate()["memory"].replace("boots", "starts"))


def _candidate(
    doc_id: str, *, content: str = "", supersedes: str | None = None
) -> CandidateMemory:
    """A post-gating candidate: only the writer-populated fields matter here."""
    candidate = CandidateMemory(
        title="t",
        memory="m",
        memory_type="factual",
        signal="success",
        evidence="e",
        non_derivable=True,
        durable=True,
        actionable=True,
        confidence=0.9,
    )
    candidate.doc_id = doc_id
    candidate.content = content
    candidate.supersedes_doc_id = supersedes
    return candidate


def _captures(registry: MagicMock) -> dict[str, dict[str, Any]]:
    docs = registry.knowledge.document_store.list_documents(limit=1000)
    return {
        d["doc_id"]: d for d in docs if d["doc_id"].startswith("capture:claude-code:")
    }


def _seed_one_memory(registry: MagicMock, root: Path, wm: Path) -> str:
    """Run a live sweep that stores exactly one capture, and return its id."""
    _error_session(root / "proj" / "sess-fake-0001.jsonl", "sess-fake-0001")
    run_capture(
        registry,
        transcripts_root=root,
        watermark_path=wm,
        llm_client=FakeLLMClient([candidates_json(good_candidate())]),
    )
    stored = _captures(registry)
    assert len(stored) == 1
    return next(iter(stored))


def _count_puts(store: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``put`` against *store* for the rest of the test."""
    seen: list[str] = []
    real = store.put

    def _spy(doc_id: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(doc_id))
        return real(doc_id, *args, **kwargs)

    monkeypatch.setattr(store, "put", _spy)
    return seen


# ---------------------------------------------------------------------------
# #408 — a dry run plans; it does not write and does not emit
# ---------------------------------------------------------------------------


class _DryRun(NamedTuple):
    """What one dry-run sweep left behind, for the assertions below."""

    registry: MagicMock
    report: CaptureReport
    seed_id: str
    puts: list[str]
    new_events: list[Any]


class TestDryRunSupersede:
    """A ``--dry-run`` sweep whose judge returns SUPERSEDE.

    The target is a *pre-existing* document, not one of the sweep's own
    candidates, which is what made the un-fixed behaviour worse than a
    stray write: an operator previewing a sweep silently changed the
    lifecycle state of memories already in the store.
    """

    @staticmethod
    def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _DryRun:
        registry = _registry(tmp_path)
        root = tmp_path / "projects"
        wm = tmp_path / "wm.json"
        seed_id = _seed_one_memory(registry, root, wm)

        _error_session(root / "proj" / "sess-fake-0002.jsonl", "sess-fake-0002")
        puts = _count_puts(registry.knowledge.document_store, monkeypatch)
        log = registry.operational.event_log
        before = {e.event_id for e in log.get_events(limit=1000)}
        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=wm,
            llm_client=FakeLLMClient([candidates_json(_near_duplicate()), _SUPERSEDE]),
            dry_run=True,
        )
        return _DryRun(
            registry=registry,
            report=report,
            seed_id=seed_id,
            puts=puts,
            new_events=[
                e for e in log.get_events(limit=1000) if e.event_id not in before
            ],
        )

    def test_writes_nothing_to_the_document_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted against the store, not against a returned plan.

        Un-fixed, ``_apply_verdict`` reached ``mark_document_superseded`` →
        ``document_store.put`` for the seeded doc. Since #397 that write
        preserves ``updated_at``, so the row is byte-identical apart from the
        lifecycle key — the stray write got *less* visible, not less real.
        """
        run = self._run(tmp_path, monkeypatch)

        assert run.puts == []
        stored = _captures(run.registry)
        assert set(stored) == {run.seed_id}
        assert "lifecycle" not in (stored[run.seed_id]["metadata"] or {})

    def test_emits_no_audit_events_but_the_flagged_funnel_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``MEMORY_OP_JUDGED`` — the event the un-fixed dry run appended.

        The whole set of new events is asserted, not just the absence of the
        judged one, so "a dry run writes no history" stays a claim about a
        known list rather than about the one type this fix touched. Two types
        survive and both are pre-existing plan records that carry
        ``dry_run=True`` in their own payload: ``CORPUS_SYNCED`` from the
        write seam, and ``CAPTURE_SWEEP_COMPLETED``, which is emitted
        unconditionally so that a sweep judging sessions and keeping none
        cannot look like a sweep that never ran.
        """
        run = self._run(tmp_path, monkeypatch)

        assert {e.event_type for e in run.new_events} == {
            EventType.CORPUS_SYNCED,
            EventType.CAPTURE_SWEEP_COMPLETED,
        }
        assert all(e.payload["dry_run"] is True for e in run.new_events)

    def test_still_reports_the_verdict_it_would_have_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half of the fix that is easy to lose.

        Gating the *model call* instead of the two side-effect sites would
        also make the two assertions above pass — and would make a dry run
        preview a different sweep from the one it claims to be previewing.
        """
        report = self._run(tmp_path, monkeypatch).report

        assert report.dry_run is True
        assert report.candidates_reconciled_supersede == 1
        assert report.supersessions_failed == 0
        assert report.to_payload()["candidates_reconciled_supersede"] == 1


# ---------------------------------------------------------------------------
# #407 — the same-sweep supersession is ordinary operation, not a race
# ---------------------------------------------------------------------------


class TestSameSweepSupersession:
    def test_second_candidate_supersedes_the_first_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """Two candidates of one session, the second superseding the first.

        ``adjudicate`` adds each survivor to its own ``index`` inside the
        candidate loop while nothing is persisted until ``_write_records``
        runs afterwards, so the target of this supersession does not exist
        yet at verdict time. Un-fixed, ``mark_document_superseded`` returned
        ``False``, the bool was discarded, and the sweep wrote the earlier doc
        with no lifecycle marker while the later one carried a
        ``supersedes_doc_id`` pointing at it — a claim contradicted by the
        very row it named.
        """
        registry = _registry(tmp_path)
        root = tmp_path / "projects"
        _error_session(root / "proj" / "sess-fake-0001.jsonl", "sess-fake-0001")
        client = FakeLLMClient(
            [candidates_json(good_candidate(), _near_duplicate()), _SUPERSEDE]
        )

        report = run_capture(
            registry,
            transcripts_root=root,
            watermark_path=tmp_path / "wm.json",
            llm_client=client,
        )

        stored = _captures(registry)
        assert report.memories_written == 2
        assert report.candidates_reconciled_supersede == 1
        assert report.supersessions_failed == 0
        # The write seam's own near-duplicate notice is expected here and is
        # not a supersession failure; nothing from apply_supersessions fires.
        assert [w for w in report.warnings if w["kind"].startswith("supersede")] == []

        successors = [
            d for d in stored.values() if d["metadata"].get("supersedes_doc_id")
        ]
        assert len(successors) == 1
        successor = successors[0]
        target_id = successor["metadata"]["supersedes_doc_id"]
        # Not dangling: the named row exists...
        assert target_id in stored
        # ...and it agrees that it was superseded, by this successor.
        lifecycle = stored[target_id]["metadata"]["lifecycle"]
        assert lifecycle["state"] == "superseded"
        assert lifecycle["superseded_by"] == successor["doc_id"]

    def test_adjudicate_writes_nothing_to_the_document_store(
        self, tmp_path: Path
    ) -> None:
        """The structural half of the fix, pinned as a property.

        Ordering is what makes the two issues go away, so it is asserted
        directly: with a store that refuses every ``put``, adjudication still
        completes. A future side effect added inside the candidate loop fails
        here whether or not whoever added it remembered ``dry_run``.
        """
        registry = _registry(tmp_path)
        docs = registry.knowledge.document_store
        seed_id = _seed_one_memory(
            registry, tmp_path / "projects", tmp_path / "wm.json"
        )

        def _refuse(*_args: Any, **_kwargs: Any) -> str:
            msg = "adjudicate must not write to the document store"
            raise AssertionError(msg)

        seed_content = _captures(registry)[seed_id]["content"]
        docs.put = _refuse  # type: ignore[method-assign]
        candidate = _candidate(
            "capture:claude-code:deadbeefdeadbeef",
            content=seed_content.replace("boots", "starts"),
        )
        report = CaptureReport(transcripts_root="/nowhere")

        survivors = reconcile_pass.adjudicate(
            registry,
            [candidate],
            client=FakeLLMClient([_SUPERSEDE]),
            id_prefix="capture:claude-code:",
            report=report,
            dry_run=False,
        )

        assert survivors == [candidate]
        assert candidate.supersedes_doc_id == seed_id
        assert report.candidates_reconciled_supersede == 1


# ---------------------------------------------------------------------------
# #407 — apply_supersessions reads the bool it is handed
# ---------------------------------------------------------------------------


class TestApplySupersessions:
    def test_vanished_target_is_counted_and_warned(self, tmp_path: Path) -> None:
        from trellis.stores.sqlite.document import SQLiteDocumentStore

        docs = SQLiteDocumentStore(tmp_path / "docs.db")
        docs.put("new-doc", "successor", {"supersedes_doc_id": "gone-doc"})
        report = CaptureReport(transcripts_root="/nowhere")

        reconcile_pass.apply_supersessions(
            docs, [_candidate("new-doc", supersedes="gone-doc")], report
        )

        assert report.supersessions_failed == 1
        assert report.warnings == [
            {
                "kind": "supersede_target_missing",
                "old_doc_id": "gone-doc",
                "new_doc_id": "new-doc",
            }
        ]
        # The successor stops claiming it — the counter records the failure,
        # it does not excuse leaving a pointer to a document that is not
        # there, which is #407's title.
        meta = docs.get("new-doc")["metadata"]
        assert "supersedes_doc_id" not in meta
        assert meta["reconciliation"] == "stale_recheck"

    def test_missing_successor_does_not_stale_mark_anything(
        self, tmp_path: Path
    ) -> None:
        """The mirror image, and the reason the successor is checked first.

        If the write seam dropped the successor, stale-marking the target
        would point ``superseded_by`` at a document that does not exist — the
        same defect as #407, aimed the other way.
        """
        from trellis.stores.sqlite.document import SQLiteDocumentStore

        docs = SQLiteDocumentStore(tmp_path / "docs.db")
        docs.put("old-doc", "target", {})
        report = CaptureReport(transcripts_root="/nowhere")

        reconcile_pass.apply_supersessions(
            docs, [_candidate("never-written", supersedes="old-doc")], report
        )

        assert report.supersessions_failed == 1
        assert report.warnings[0]["kind"] == "supersede_successor_missing"
        assert "lifecycle" not in (docs.get("old-doc")["metadata"] or {})

    def test_candidates_without_a_verdict_are_untouched(self, tmp_path: Path) -> None:
        from trellis.stores.sqlite.document import SQLiteDocumentStore

        docs = SQLiteDocumentStore(tmp_path / "docs.db")
        report = CaptureReport(transcripts_root="/nowhere")

        reconcile_pass.apply_supersessions(docs, [_candidate("plain-add")], report)

        assert report.supersessions_failed == 0
        assert report.warnings == []
