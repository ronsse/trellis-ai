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
