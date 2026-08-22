"""Distillation: parsing and fail-closed behaviour (model mocked)."""

from __future__ import annotations

from trellis_workers.session_capture import distill
from trellis_workers.session_capture.models import SessionDigest

from .conftest import (
    BrokenLLMClient,
    FakeLLMClient,
    candidates_json,
    good_candidate,
)


def _digest() -> SessionDigest:
    d = SessionDigest(session_id="sess-fake-0001", source_path="x")
    d.user_texts.append("please fix the failing deploy step")
    d.assistant_texts.append("the migration must run first")
    d.has_error = True
    return d


def test_parse_candidates_happy_path() -> None:
    raw = candidates_json(good_candidate())
    cands = distill.parse_candidates(raw, "sess-fake-0001")
    assert len(cands) == 1
    assert cands[0].session_id == "sess-fake-0001"
    assert cands[0].non_derivable is True


def test_parse_candidates_tolerates_code_fence() -> None:
    raw = "```json\n" + candidates_json(good_candidate()) + "\n```"
    assert len(distill.parse_candidates(raw, "s")) == 1


def test_parse_candidates_malformed_returns_empty() -> None:
    assert distill.parse_candidates("not json at all", "s") == []


def test_parse_candidates_non_array_returns_empty() -> None:
    assert distill.parse_candidates('{"title": "x"}', "s") == []


def test_parse_candidates_skips_items_missing_fields() -> None:
    raw = candidates_json({"title": "only a title"}, good_candidate())
    cands = distill.parse_candidates(raw, "s")
    assert len(cands) == 1


def test_distill_no_client_returns_none() -> None:
    # None (not []) so the caller leaves the session un-watermarked.
    assert distill.distill_session(None, _digest()) is None


def test_distill_model_down_returns_none() -> None:
    assert distill.distill_session(BrokenLLMClient(), _digest()) is None


def test_distill_success_returns_candidates() -> None:
    client = FakeLLMClient([candidates_json(good_candidate())])
    result = distill.distill_session(client, _digest())
    assert result is not None
    assert len(result) == 1


def test_distill_empty_judgment_returns_empty_list_not_none() -> None:
    # Judge responded with an empty array — "nothing worthy", safe to advance.
    client = FakeLLMClient(["[]"])
    assert distill.distill_session(client, _digest()) == []


class TestSkipDisciplineInJudgePrompt:
    """#311: the judge prompt carries the skip-discipline rules on the wire.

    ``passes_worthiness`` only reads the model's three self-reported booleans
    plus an evidence string, so a self-certifying judge can land routine noise
    (a clean install, a bare listing) past the deterministic gate. The prompt
    is the only place that class is refused, which is why it is pinned here
    rather than left to the constant.
    """

    def _system(self) -> str:
        messages = distill.build_distill_messages(_digest())
        assert messages[0].role == "system"
        return " ".join(messages[0].content.split())

    def test_skip_criteria_present(self) -> None:
        system = self._system()
        assert "Skip discipline" in system
        assert "status check that found nothing notable" in system
        assert "dependency install or build that completed cleanly" in system
        assert "bare file or directory listing" in system
        assert "restatement of a finding" in system
        assert "research or a search that found nothing" in system

    def test_skips_are_silent(self) -> None:
        system = self._system()
        assert "return [] and nothing else" in system
        assert "never explain the skip in prose" in system
        # Non-schema output is discarded, so prose is not a storable artifact.
        assert "is not the JSON array is discarded" in system

    def test_anti_meta_guard_present(self) -> None:
        system = self._system()
        assert "NEVER what you or the capture process are doing" in system
        assert '"Analyzed the session and stored findings" is not a memory' in system
        # Scoped to the process, not the topic: a session ABOUT the capture
        # pipeline is ordinary subject matter.
        assert "ordinary subject matter" in system
