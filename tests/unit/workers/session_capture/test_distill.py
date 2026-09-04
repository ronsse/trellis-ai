"""Distillation: parsing and fail-closed behaviour (model mocked)."""

from __future__ import annotations

from trellis_workers.session_capture import distill
from trellis_workers.session_capture.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    SessionDigest,
)

from .conftest import (
    BrokenLLMClient,
    FakeLLMClient,
    candidates_json,
    good_candidate,
)


def _digest() -> SessionDigest:
    d = SessionDigest(session_id="sess-fake-0001", source_path="x")
    d.add_turn(ROLE_USER, "please fix the failing deploy step")
    d.add_turn(ROLE_ASSISTANT, "the migration must run first")
    d.has_error = True
    return d


def test_parse_candidates_happy_path() -> None:
    raw = candidates_json(good_candidate())
    result = distill.parse_candidates(raw, "sess-fake-0001")
    assert result.outcome is distill.DistillOutcome.CANDIDATES
    assert len(result.candidates) == 1
    assert result.candidates[0].session_id == "sess-fake-0001"
    assert result.candidates[0].non_derivable is True


def test_parse_candidates_tolerates_code_fence() -> None:
    raw = "```json\n" + candidates_json(good_candidate()) + "\n```"
    result = distill.parse_candidates(raw, "s")
    assert result.outcome is distill.DistillOutcome.CANDIDATES
    assert len(result.candidates) == 1


def test_parse_candidates_malformed_is_distinct_from_empty() -> None:
    malformed = distill.parse_candidates("not json at all", "s")
    empty = distill.parse_candidates("[]", "s")

    assert malformed.outcome is distill.DistillOutcome.MALFORMED
    assert malformed.parse_error
    assert empty == distill.DistillResult(outcome=distill.DistillOutcome.EMPTY)


def test_parse_candidates_non_array_is_malformed() -> None:
    result = distill.parse_candidates('{"title": "x"}', "s")
    assert result.outcome is distill.DistillOutcome.MALFORMED


def test_parse_candidates_skips_items_missing_fields() -> None:
    raw = candidates_json({"title": "only a title"}, good_candidate())
    result = distill.parse_candidates(raw, "s")
    assert result.outcome is distill.DistillOutcome.CANDIDATES
    assert len(result.candidates) == 1


def test_build_messages_mark_an_oversize_cut() -> None:
    """A capped session announces the cut to the judge — size + reason (#310).

    A silent cut invites the model to confabulate the missing tail; the
    marker tells it material was removed and how much.
    """
    digest = _digest()
    digest.add_turn(ROLE_ASSISTANT, "y" * (distill._MAX_SALIENT_CHARS + 500))
    user = distill.build_distill_messages(digest)[1].content
    total = len(digest.salient_text)
    dropped = total - distill._MAX_SALIENT_CHARS
    assert (
        f'<elided chars="{dropped}" original_size_chars="{total}" reason="oversize" />'
    ) in user


def test_build_messages_under_cap_carry_no_elision_marker() -> None:
    user = distill.build_distill_messages(_digest())[1].content
    assert "<elided" not in user


def test_distill_no_client_is_unavailable() -> None:
    result = distill.distill_session(None, _digest())
    assert result.outcome is distill.DistillOutcome.UNAVAILABLE
    assert result.unavailable_reason == "no_client"


def test_distill_model_down_is_unavailable() -> None:
    result = distill.distill_session(BrokenLLMClient(), _digest())
    assert result.outcome is distill.DistillOutcome.UNAVAILABLE
    assert result.unavailable_reason == "model_error"


def test_distill_success_returns_candidates() -> None:
    client = FakeLLMClient([candidates_json(good_candidate())])
    result = distill.distill_session(client, _digest())
    assert result.outcome is distill.DistillOutcome.CANDIDATES
    assert len(result.candidates) == 1


def test_distill_empty_judgment_is_empty_not_unavailable() -> None:
    # Judge responded with an empty array — "nothing worthy", safe to advance.
    client = FakeLLMClient(["[]"])
    assert distill.distill_session(client, _digest()) == distill.DistillResult(
        outcome=distill.DistillOutcome.EMPTY
    )


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


class TestPromptWindowPreflight:
    """A prompt that cannot fit the judge's window must not be sent.

    Ollama truncates an oversize prompt server-side, returns 200, and
    hermes3:8b answers from the remnant rather than declining -- inventing
    memories that appear nowhere in the transcript. ``passes_worthiness``
    cannot catch that: a fabrication carries confident booleans and
    plausible-looking evidence.

    The check is a pre-flight against a *declared* window rather than a
    post-hoc read of ``usage.prompt_tokens``, because Ollama reports that
    field as tokens *newly evaluated* -- an identical prompt returns 1 on a
    cache hit (measured: 1212, then 1, then 1). A ratio test on it would fire
    hardest on a retry, the one path the fail-closed contract guarantees.
    """

    def test_oversize_prompt_returns_none_without_calling_the_model(
        self, monkeypatch
    ) -> None:
        # Window smaller than the reserve alone: nothing can fit.
        monkeypatch.setenv(distill.ENV_JUDGE_CONTEXT_TOKENS, "64")
        client = FakeLLMClient([candidates_json(good_candidate())])
        result = distill.distill_session(client, _digest())
        assert result.outcome is distill.DistillOutcome.UNAVAILABLE
        assert result.unavailable_reason == "prompt_too_large"
        # The model is never consulted: a prompt that cannot fit buys nothing.
        assert client.calls == []

    def test_default_config_fits_the_default_window(self) -> None:
        """The shipped cap must not trip the guard it ships with."""
        messages = distill.build_distill_messages(_digest())
        estimated = sum(len(m.content) for m in messages) // distill._CHARS_PER_TOKEN
        assert (
            estimated + distill._COMPLETION_RESERVE_TOKENS
            <= distill.DEFAULT_JUDGE_CONTEXT_TOKENS
        )

    def test_fitting_prompt_is_sent(self) -> None:
        client = FakeLLMClient([candidates_json(good_candidate())])
        result = distill.distill_session(client, _digest())
        assert result.outcome is distill.DistillOutcome.CANDIDATES
        assert len(result.candidates) == 1
        assert len(client.calls) == 1

    def test_declared_larger_window_admits_a_larger_prompt(self, monkeypatch) -> None:
        """Raising the declared window is what unlocks a raised cap."""
        big = "y" * 60_000
        digest = _digest()
        digest.add_turn(ROLE_ASSISTANT, big)
        monkeypatch.setenv(distill.ENV_MAX_SALIENT_CHARS, "60000")

        monkeypatch.setenv(distill.ENV_JUDGE_CONTEXT_TOKENS, "4096")
        result = distill.distill_session(FakeLLMClient(["[]"]), digest)
        assert result.outcome is distill.DistillOutcome.UNAVAILABLE

        monkeypatch.setenv(distill.ENV_JUDGE_CONTEXT_TOKENS, "32768")
        assert distill.distill_session(
            FakeLLMClient(["[]"]), digest
        ) == distill.DistillResult(outcome=distill.DistillOutcome.EMPTY)


class TestJudgeContextTokens:
    def test_default_when_unset(self) -> None:
        assert distill.judge_context_tokens({}) == distill.DEFAULT_JUDGE_CONTEXT_TOKENS

    def test_operator_declaration_applies(self) -> None:
        assert (
            distill.judge_context_tokens({distill.ENV_JUDGE_CONTEXT_TOKENS: "32768"})
            == 32768
        )

    def test_unparseable_falls_back_rather_than_raising(self) -> None:
        assert (
            distill.judge_context_tokens({distill.ENV_JUDGE_CONTEXT_TOKENS: "big"})
            == distill.DEFAULT_JUDGE_CONTEXT_TOKENS
        )


class TestMaxSalientCharsOverride:
    def test_default_when_unset(self) -> None:
        assert distill.max_salient_chars({}) == distill._MAX_SALIENT_CHARS

    def test_operator_override_applies(self) -> None:
        assert (
            distill.max_salient_chars({distill.ENV_MAX_SALIENT_CHARS: "16000"}) == 16000
        )

    def test_unparseable_falls_back_rather_than_raising(self) -> None:
        # A typo in one env var must not take out the nightly sweep.
        assert (
            distill.max_salient_chars({distill.ENV_MAX_SALIENT_CHARS: "lots"})
            == distill._MAX_SALIENT_CHARS
        )

    def test_non_positive_falls_back(self) -> None:
        assert (
            distill.max_salient_chars({distill.ENV_MAX_SALIENT_CHARS: "0"})
            == distill._MAX_SALIENT_CHARS
        )
