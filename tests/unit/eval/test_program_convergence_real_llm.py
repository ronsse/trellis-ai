"""Skeleton-level tests for ``program_convergence_real_llm`` (E3-prep).

This module covers the E3-prep contract only:

1. The scenario module imports cleanly (registers itself with the
   runner's discovery path).
2. The credential-gated skip path works without API keys set — no
   registry mutation, no API calls, ``status="skip"``.

Real-credentials integration testing (cost-bearing) is deferred to
E3 (Wave 5) per ``docs/design/plan-next-swarm-wave.md`` §8.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from eval.runner import list_scenarios
from eval.scenarios.program_convergence_real_llm import scenario as real_llm_scenario

if TYPE_CHECKING:
    import pytest


def test_scenario_module_imports_cleanly() -> None:
    """Module import side-effects must not break; discovery must see it.

    The scenario package being importable + showing up under
    :func:`eval.runner.list_scenarios` is the prerequisite for E3's
    full implementation slot. If this regresses, E3 cannot land.
    """
    assert real_llm_scenario.SCENARIO_NAME == "program_convergence_real_llm"
    assert callable(real_llm_scenario.run)
    assert "program_convergence_real_llm" in list_scenarios()


def test_run_skips_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OPENAI / ANTHROPIC keys, ``run`` returns skip — no calls fire.

    The registry argument is a :class:`MagicMock` because the skip
    path returns before touching any store. If a future refactor
    accidentally exercises the registry before the credential check,
    this test surfaces it via ``mock.assert_not_called`` semantics.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    registry = MagicMock()
    report = real_llm_scenario.run(registry)

    assert report.status == "skip"
    assert report.name == "program_convergence_real_llm"
    # Exactly one info finding pointing the operator at the env vars.
    info_findings = [f for f in report.findings if f.severity == "info"]
    assert info_findings, "skip path must emit an info finding"
    assert any(
        "OPENAI_API_KEY" in f.message and "ANTHROPIC_API_KEY" in f.message
        for f in info_findings
    )
    # The skip path must not touch the registry — otherwise the scenario
    # is doing work it has no credentials to bill against.
    registry.assert_not_called()


def test_has_credentials_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_has_credentials`` returns True iff either env var is non-empty."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert real_llm_scenario._has_credentials() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert real_llm_scenario._has_credentials() is True

    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert real_llm_scenario._has_credentials() is True

    # Empty string must not count as credentials present.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert real_llm_scenario._has_credentials() is False
