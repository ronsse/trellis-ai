"""Production prompt parity: parameterising the judge changed no default byte.

``build_distill_messages`` and ``distill_session`` gained keyword parameters
(salient window, context window, system prompt) so the offline judge eval can
vary them. The golden digest below was taken from the code **before** those
parameters existed; a default call must still produce exactly that prompt.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trellis_workers.session_capture import distill
from trellis_workers.session_capture.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    SessionDigest,
    ToolCall,
)

from .conftest import FakeLLMClient, candidates_json, good_candidate

#: sha256 of the default prompt for :func:`_golden_digest`, recorded on the
#: pre-change code. Changing it is a production prompt change.
_GOLDEN_SHA = "a1c92cb295c60d0b0e5d0b08418a6dd64604d79a886af8a389fcd608a8d989eb"


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(distill.ENV_MAX_SALIENT_CHARS, raising=False)
    monkeypatch.delenv(distill.ENV_JUDGE_CONTEXT_TOKENS, raising=False)


def _golden_digest() -> SessionDigest:
    """Long enough to be elided, with tools, so every prompt part is exercised."""
    digest = SessionDigest(session_id="sess-parity-0001", source_path="x")
    for index in range(120):
        digest.add_turn(ROLE_USER, f"user turn {index}: please look at widget {index}")
        digest.add_turn(
            ROLE_ASSISTANT, f"assistant turn {index}: the widget {index} is fine"
        )
    digest.tool_calls.extend(
        [ToolCall(name="Bash", is_error=False)] * 5
        + [ToolCall(name="Read", is_error=True)]
    )
    digest.has_error = True
    return digest


def _sha(messages: list[distill.Message]) -> str:
    rendered = json.dumps([[m.role, m.content] for m in messages])
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def test_default_prompt_is_byte_identical_to_the_pre_change_prompt() -> None:
    assert _sha(distill.build_distill_messages(_golden_digest())) == _GOLDEN_SHA


def test_explicit_defaults_build_the_same_prompt() -> None:
    explicit = distill.build_distill_messages(
        _golden_digest(),
        salient_chars=distill.DEFAULT_MAX_SALIENT_CHARS,
        system_prompt=distill.PROMPT_VARIANTS[distill.DEFAULT_PROMPT_VARIANT],
    )
    assert _sha(explicit) == _GOLDEN_SHA


def test_the_default_variant_is_the_production_prompt() -> None:
    assert distill.PROMPT_VARIANTS[distill.DEFAULT_PROMPT_VARIANT] is (
        distill._SYSTEM_PROMPT
    )


def test_default_distill_session_sends_the_golden_prompt() -> None:
    client = FakeLLMClient([candidates_json(good_candidate())])
    result = distill.distill_session(client, _golden_digest())
    assert result.outcome is distill.DistillOutcome.CANDIDATES
    assert _sha(client.calls[0]) == _GOLDEN_SHA


def test_the_operator_override_still_applies_when_no_parameter_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(distill.ENV_MAX_SALIENT_CHARS, "2000")
    overridden = distill.build_distill_messages(_golden_digest())
    assert _sha(overridden) != _GOLDEN_SHA
    assert overridden == distill.build_distill_messages(
        _golden_digest(), salient_chars=2000
    )


def test_a_parameter_wins_over_the_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(distill.ENV_MAX_SALIENT_CHARS, "2000")
    explicit = distill.build_distill_messages(
        _golden_digest(), salient_chars=distill.DEFAULT_MAX_SALIENT_CHARS
    )
    assert _sha(explicit) == _GOLDEN_SHA


def test_a_declared_context_window_parameter_refuses_an_oversize_prompt() -> None:
    client = FakeLLMClient([candidates_json(good_candidate())])
    result = distill.distill_session(client, _golden_digest(), context_tokens=1500)
    assert result.unavailable_reason == "prompt_too_large"
    assert client.calls == []
