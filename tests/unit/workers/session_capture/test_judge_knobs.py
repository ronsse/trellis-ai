"""``TRELLIS_CAPTURE_PROMPT_VARIANT`` / ``TRELLIS_CAPTURE_COMPLETION_TOKENS``.

Defaults must be byte-identical to the sweep before the knobs existed: the
production prompt and a 1200-token completion cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.errors import ConfigError
from trellis.llm import LLMResponse
from trellis_workers.session_capture import distill
from trellis_workers.session_capture import sweep as capture_sweep
from trellis_workers.session_capture.capture import run_capture
from trellis_workers.session_capture.models import CaptureReport, SessionDigest

from .conftest import assistant_turn, tool_result_turn, user_turn, write_transcript


class RecordingClient:
    """Records each call's ``max_tokens`` and messages; answers ``[]``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        messages: list[Any],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return LLMResponse(content="[]", model="fake")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        distill.ENV_PROMPT_VARIANT,
        distill.ENV_COMPLETION_TOKENS,
        distill.ENV_MAX_SALIENT_CHARS,
        distill.ENV_JUDGE_CONTEXT_TOKENS,
    ):
        monkeypatch.delenv(var, raising=False)


def _digest() -> SessionDigest:
    digest = SessionDigest(session_id="sess-fake-0001", source_path="x")
    digest.add_turn("user", "deploy failed on the migration step")
    return digest


class TestResolution:
    def test_unset_resolves_to_the_production_defaults(self) -> None:
        assert distill.capture_system_prompt({}) is None
        assert (
            distill.completion_tokens({}) == distill.DEFAULT_COMPLETION_TOKENS == 1200
        )

    def test_a_known_variant_resolves_to_its_prompt_text(self) -> None:
        env = {distill.ENV_PROMPT_VARIANT: distill.VERBATIM_EVIDENCE_PROMPT_VARIANT}
        assert (
            distill.capture_system_prompt(env)
            is distill.PROMPT_VARIANTS[distill.VERBATIM_EVIDENCE_PROMPT_VARIANT]
        )

    def test_an_unknown_variant_raises_naming_the_setting(self) -> None:
        with pytest.raises(ConfigError) as exc_info:
            distill.capture_system_prompt({distill.ENV_PROMPT_VARIANT: "nope"})
        assert exc_info.value.setting == distill.ENV_PROMPT_VARIANT
        assert "verbatim_evidence" in exc_info.value.message

    def test_completion_tokens_override_and_fallback(self) -> None:
        key = distill.ENV_COMPLETION_TOKENS
        assert distill.completion_tokens({key: "8000"}) == 8000
        assert distill.completion_tokens({key: "lots"}) == 1200
        assert distill.completion_tokens({key: "0"}) == 1200


class TestDistillCall:
    def test_defaults_are_byte_identical(self) -> None:
        client = RecordingClient()
        distill.distill_session(client, _digest())
        (call,) = client.calls
        assert call["max_tokens"] == 1200
        expected = distill.build_distill_messages(_digest())
        assert [(m.role, m.content) for m in call["messages"]] == [
            (m.role, m.content) for m in expected
        ]
        assert call["messages"][0].content == distill._SYSTEM_PROMPT


def _error_session(path: Path) -> None:
    write_transcript(
        path,
        [
            user_turn("run the deploy", "sess-fake-0001"),
            assistant_turn("running the migration", "Bash", "sess-fake-0001"),
            tool_result_turn(is_error=True, session_id="sess-fake-0001"),
        ],
    )


def _registry(tmp_path: Path) -> MagicMock:
    from trellis.stores.sqlite.document import SQLiteDocumentStore
    from trellis.stores.sqlite.event_log import SQLiteEventLog
    from trellis.stores.sqlite.vector import SQLiteVectorStore

    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    return reg


def _run(tmp_path: Path, client: RecordingClient, **kwargs: Any) -> CaptureReport:
    root = tmp_path / "projects"
    _error_session(root / "proj" / "sess-fake-0001.jsonl")
    return run_capture(
        _registry(tmp_path),
        transcripts_root=root,
        watermark_path=tmp_path / "wm.json",
        llm_client=client,
        **kwargs,
    )


class TestCapturePath:
    def test_sweep_defaults_reach_the_judge_unchanged(self, tmp_path: Path) -> None:
        client = RecordingClient()
        _run(tmp_path, client)
        (call,) = client.calls
        assert call["max_tokens"] == 1200
        assert call["messages"][0].content == distill._SYSTEM_PROMPT

    def test_configured_prompt_and_budget_reach_the_judge(self, tmp_path: Path) -> None:
        prompt = distill.PROMPT_VARIANTS[distill.VERBATIM_EVIDENCE_PROMPT_VARIANT]
        client = RecordingClient()
        _run(
            tmp_path,
            client,
            judge_system_prompt=prompt,
            judge_completion_tokens=3000,
        )
        (call,) = client.calls
        assert call["max_tokens"] == 3000
        assert call["messages"][0].content == prompt

    def test_window_check_reserves_the_configured_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A window that fits this prompt plus the default 1200, but not 3000.
        monkeypatch.setenv(distill.ENV_JUDGE_CONTEXT_TOKENS, "2500")
        client = RecordingClient()
        report = _run(tmp_path, client, judge_completion_tokens=3000)
        assert client.calls == []
        assert report.sessions_judge_unavailable == 1

        fits = RecordingClient()
        _run(tmp_path / "again", fits)
        assert len(fits.calls) == 1


class TestRunSweep:
    @pytest.fixture
    def registry(self) -> MagicMock:
        return MagicMock()

    def test_unset_env_passes_the_defaults(self, monkeypatch, registry) -> None:
        spy = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(capture_sweep, "run_capture", spy)
        capture_sweep.run_sweep(registry=registry)
        kwargs = spy.call_args[1]
        assert kwargs["judge_system_prompt"] is None
        assert kwargs["judge_completion_tokens"] == 1200

    def test_env_reaches_run_capture(self, monkeypatch, registry) -> None:
        monkeypatch.setenv(
            distill.ENV_PROMPT_VARIANT, distill.VERBATIM_EVIDENCE_PROMPT_VARIANT
        )
        monkeypatch.setenv(distill.ENV_COMPLETION_TOKENS, "6000")
        spy = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(capture_sweep, "run_capture", spy)
        capture_sweep.run_sweep(registry=registry)
        kwargs = spy.call_args[1]
        assert (
            kwargs["judge_system_prompt"]
            == distill.PROMPT_VARIANTS[distill.VERBATIM_EVIDENCE_PROMPT_VARIANT]
        )
        assert kwargs["judge_completion_tokens"] == 6000

    def test_unknown_variant_fails_before_any_session_is_judged(
        self, monkeypatch, registry
    ) -> None:
        monkeypatch.setenv(distill.ENV_PROMPT_VARIANT, "not-a-variant")
        spy = MagicMock()
        monkeypatch.setattr(capture_sweep, "run_capture", spy)
        with pytest.raises(ConfigError):
            capture_sweep.run_sweep(registry=registry)
        spy.assert_not_called()
        registry.build_llm_client.assert_not_called()
