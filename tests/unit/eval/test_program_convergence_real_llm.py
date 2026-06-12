"""Tests for ``program_convergence_real_llm`` (E3 — full implementation).

Coverage:

1. The scenario module imports cleanly + is discoverable.
2. The credential-gated skip path returns ``status="skip"`` without
   touching the registry or making API calls — both the no-credentials
   case and the Anthropic-only-no-OpenAI case.
3. With credentials AND the ``TRELLIS_EVAL_REAL_LLM_MOCK`` hatch set,
   a 3-round smoke run emits every axis metric, fires the
   BUDGET_CONSUMED event, and returns ``status="pass"``.
4. The per-run hard cost cap raises ``RunBudgetError`` after the
   BUDGET_CONSUMED event has been emitted (operators always see the
   bill, even on abort).

Real-credentials exercise is operator-gated, not CI: this module
NEVER calls a real provider. All "real-key" paths in the tests use
either ``monkeypatch.setenv("OPENAI_API_KEY", ...)`` with the mock
hatch on, or assert the credential-gating logic alone.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from eval.runner import list_scenarios
from eval.scenarios._convergence_common import NINE_AXIS_LABELS
from eval.scenarios.program_convergence_real_llm import scenario as real_llm_scenario

from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_scenario_module_imports_cleanly() -> None:
    """Module import side-effects must not break; discovery must see it."""
    assert real_llm_scenario.SCENARIO_NAME == "program_convergence_real_llm"
    assert callable(real_llm_scenario.run)
    assert "program_convergence_real_llm" in list_scenarios()


# ---------------------------------------------------------------------------
# Credential-gated skip paths
# ---------------------------------------------------------------------------


def test_run_skips_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OPENAI / ANTHROPIC keys, ``run`` returns skip — no calls fire."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, raising=False)

    registry = MagicMock()
    report = real_llm_scenario.run(registry)

    assert report.status == "skip"
    assert report.name == "program_convergence_real_llm"
    info_findings = [f for f in report.findings if f.severity == "info"]
    assert info_findings, "skip path must emit an info finding"
    assert any(
        "OPENAI_API_KEY" in f.message and "ANTHROPIC_API_KEY" in f.message
        for f in info_findings
    )
    # The skip path must not touch the registry — otherwise the scenario
    # is doing work it has no credentials to bill against.
    registry.assert_not_called()


def test_run_skips_when_only_anthropic_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTHROPIC_API_KEY without OPENAI_API_KEY → skip (embedder is OpenAI-only)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, raising=False)

    registry = MagicMock()
    report = real_llm_scenario.run(registry)

    assert report.status == "skip"
    info_findings = [f for f in report.findings if f.severity == "info"]
    assert any("OPENAI_API_KEY required" in f.message for f in info_findings)
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

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert real_llm_scenario._has_credentials() is False


def test_mock_enabled_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_mock_enabled`` returns True for any non-empty value of the hatch."""
    monkeypatch.delenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, raising=False)
    assert real_llm_scenario._mock_enabled() is False

    monkeypatch.setenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, "1")
    assert real_llm_scenario._mock_enabled() is True

    monkeypatch.setenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, "")
    assert real_llm_scenario._mock_enabled() is False


# ---------------------------------------------------------------------------
# Mock-API smoke test — full nine-axis loop without billing real tokens
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    """In-memory SQLite registry sufficient for the nine-axis loop."""
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


def test_mock_smoke_emits_all_nine_axes_and_budget_event(
    sqlite_registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock-hatch smoke: 3 rounds, every axis lands, BUDGET_CONSUMED fires.

    Asserts:

    * Status is ``"pass"``.
    * Every axis surfaces ``first_quarter_mean`` / ``last_quarter_mean``
      / ``delta`` metrics.
    * Exactly one ``BUDGET_CONSUMED`` event lands in the operational
      EventLog with the required payload keys.
    * Cost metrics surface in the report.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-mock")
    monkeypatch.setenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, "1")

    report = real_llm_scenario.run(
        sqlite_registry,
        seed=0,
        rounds=3,
        feedback_batch_size=3,
        analyzer_cadence=3,
        traces_per_domain=2,
    )

    assert report.status == "pass", f"unexpected status; findings={report.findings}"
    assert report.name == "program_convergence_real_llm"

    # Nine axes x {first_quarter_mean, last_quarter_mean, delta}.
    for label in NINE_AXIS_LABELS:
        for suffix in ("first_quarter_mean", "last_quarter_mean", "delta"):
            key = f"axis.{label}.{suffix}"
            assert key in report.metrics, f"missing metric {key!r}"

    # Cost metrics surfaced.
    assert "embedder.calls_total" in report.metrics
    assert "embedder.input_tokens_total" in report.metrics
    assert "cost.total_usd" in report.metrics
    assert report.metrics["mock_enabled"] == 1.0

    # Exactly one BUDGET_CONSUMED event lands.
    events = sqlite_registry.operational.event_log.get_events(
        event_type=EventType.BUDGET_CONSUMED,
        limit=100,
    )
    assert len(events) == 1, f"expected one BUDGET_CONSUMED, got {len(events)}"
    event = events[0]
    assert event.source == "eval.program_convergence_real_llm.mock"
    assert event.entity_id == "program_convergence_real_llm_0000"
    assert event.entity_type == "ProgramConvergenceRealLLMRun"
    payload = event.payload
    for required in ("tokens_consumed", "dollars_estimated", "provider", "model"):
        assert required in payload, f"BUDGET_CONSUMED missing {required!r}"
    assert payload["provider"] == "openai"
    assert isinstance(payload["tokens_consumed"], int)
    assert payload["tokens_consumed"] > 0
    assert isinstance(payload["dollars_estimated"], float)
    assert payload["dollars_estimated"] >= 0.0


def test_hard_cost_cap_trips_and_emits_budget_event(
    sqlite_registry: StoreRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap=$0 → RunBudgetError, but BUDGET_CONSUMED still fires first.

    Operators must always see the bill, even on abort. The cap=$0 case
    is the cleanest forcing function — any non-zero token total trips.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-mock")
    monkeypatch.setenv(real_llm_scenario.MOCK_HATCH_ENV_VAR, "1")

    with pytest.raises(real_llm_scenario.RunBudgetError):
        real_llm_scenario.run(
            sqlite_registry,
            seed=0,
            rounds=3,
            feedback_batch_size=3,
            analyzer_cadence=3,
            traces_per_domain=2,
            run_hard_cost_cap_usd=0.0,
        )

    # The audit event must have fired before the raise.
    events = sqlite_registry.operational.event_log.get_events(
        event_type=EventType.BUDGET_CONSUMED,
        limit=100,
    )
    assert len(events) == 1, "BUDGET_CONSUMED must fire even on cap abort"
    payload = events[0].payload
    assert payload["tokens_consumed"] > 0
