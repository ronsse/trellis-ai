"""Program-level convergence with a real LLM + real embedder — SKELETON.

E3-prep (Wave 3) deliverable: discoverable scenario module with
credential gating and a stubbed per-round loop. The full
implementation is **E3 (Wave 5)** in
[`docs/design/plan-next-swarm-wave.md`](../../../docs/design/plan-next-swarm-wave.md)
§8.

Cost envelope (E3 full impl, for forward planning):
    ~$0.50 per run at 50 rounds * N seeds * 1 embed/seed * $0.00002/token
    (text-embedding-3-small at 1536 dim; assumes ~1K tokens per
    summary + 30 distractors + per-round query embedding). The
    setup-time corpus embed dominates; per-round embed cost is the
    tail. Final figure pinned in E3 once the regime-shift /
    advisory-eviction passes are real-credentials-exercised.

Credential gating:
    Requires ``OPENAI_API_KEY`` *or* ``ANTHROPIC_API_KEY`` to be
    present. If neither is set, :func:`run` returns a
    :class:`~eval.runner.ScenarioReport` with ``status="skip"`` and a
    single info finding telling the operator how to enable the
    scenario. This mirrors the contract operators get from any
    backend-conditional scenario in the suite. The full E3 impl will
    layer the OpenAI / Moonshot split documented in
    :mod:`eval._real_llm` on top — the skip semantics stay identical.

Budget audit hook:
    E3 will emit a ``BUDGET_CONSUMED`` event after each round (or
    batch) carrying ``{tokens_consumed, dollars_estimated, provider}``.
    The :class:`eval.scenarios.agent_loop_convergence_real_llm.scenario._Telemetry`
    accumulator already tracks per-call token totals; the skeleton
    documents this hook without instantiating the helper (no API
    calls happen at skeleton time). Adding the ``BUDGET_CONSUMED``
    :class:`~trellis.stores.base.event_log.EventType` enum member +
    payload-schema docstring is part of E3 — the enum must not gain
    members until at least one production emit-site exists, per the
    project's "no dormant enum values" hygiene.

What's still stubbed:
    1. The real per-round nine-axis loop. Marked with
       ``# TODO(E3): ...`` in the body. E3 reuses the synthetic
       master's ``_run_loop`` once C2 (Wave 3) lands the extraction;
       until then the comment flags the orchestration scaffold as a
       duplicate to be deduped during E3 implementation.
    2. The ``_Telemetry`` integration. The agent-loop fork's
       accumulator is reusable but threading it through the
       program-convergence axis-grading path is non-trivial; deferred.
    3. The real ``BUDGET_CONSUMED`` event emission. Documented above.
    4. Anthropic embedder path. The reference implementation today is
       OpenAI-only; if E3 wants Anthropic for chat it can layer that
       on without touching this scenario's credential-gating contract.
"""

from __future__ import annotations

import os

from eval.runner import Finding, ScenarioReport
from trellis.stores.registry import StoreRegistry

SCENARIO_NAME = "program_convergence_real_llm"

#: Environment variables the scenario looks for. Either is sufficient.
#: ``OPENAI_API_KEY`` is the primary path (OpenAI embedder, optional
#: Moonshot/OpenAI chat). ``ANTHROPIC_API_KEY`` is the fallback path
#: (Anthropic chat; embeddings still go via OpenAI in E3's design).
_CREDENTIAL_ENV_VARS: tuple[str, ...] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _has_credentials() -> bool:
    """Return ``True`` iff at least one credential env var is non-empty."""
    return any(os.environ.get(name) for name in _CREDENTIAL_ENV_VARS)


def run(
    registry: StoreRegistry,
    *,
    seed: int = 0,
    rounds: int = 50,
) -> ScenarioReport:
    """Skeleton entry point. Returns ``skip`` without credentials; raises otherwise.

    The E3 full implementation will mirror the synthetic master's
    :func:`eval.scenarios.program_convergence.scenario.run` signature
    (9-axis dataclasses, regression-shift kwargs, chart rendering)
    but route the embedder through
    :func:`eval._real_llm.build_openai_embedder` (or the Anthropic
    equivalent) and emit a per-round ``BUDGET_CONSUMED`` event.

    Today the body only honours the credential-gated skip contract.
    The ``registry``, ``seed`` and ``rounds`` parameters are accepted
    but ignored at skeleton time — they lock the signature E3 will
    fill in.
    """
    # Skeleton silences "unused argument" by binding to ``_`` — these
    # parameters lock the E3 signature shape but the skeleton has no
    # work to delegate them to yet.
    _ = (registry, seed, rounds)

    if not _has_credentials():
        return ScenarioReport(
            name=SCENARIO_NAME,
            status="skip",
            findings=[
                Finding(
                    severity="info",
                    message=("set OPENAI_API_KEY or ANTHROPIC_API_KEY to run"),
                )
            ],
            decision=(
                "Scenario skipped — real-LLM credentials not configured. "
                "See eval/scenarios/program_convergence_real_llm/scenario.py "
                "module docstring for the expected cost envelope (~$0.50/run "
                "at 50 rounds) and the E3 (Wave 5) implementation plan."
            ),
        )

    # TODO(E3): implement real-embed per-round work.
    # Re-use eval.scenarios.program_convergence.scenario._run_loop once
    # C2 (Wave 3) lands the orchestration extraction; until then the
    # full impl will need to duplicate that scaffold with a clear
    # comment marking it for dedup during E3.
    #
    # TODO(E3): instantiate
    # eval.scenarios.agent_loop_convergence_real_llm.scenario._Telemetry
    # at run start so per-round budget audit has a token / cost source.
    #
    # TODO(E3): emit BUDGET_CONSUMED event per round with payload
    # {tokens_consumed: int, dollars_estimated: float, provider: str}.
    # The EventType enum member must land in the same PR as the first
    # emit-site — no dormant values.
    msg = (
        "program_convergence_real_llm full implementation is deferred to "
        "E3 (Wave 5) — see module docstring. Re-running with credentials "
        "set is intentionally a hard error in the skeleton so operators "
        "cannot accidentally trigger a partial run that bills tokens "
        "without surfacing the nine-axis output the scenario name promises."
    )
    raise NotImplementedError(msg)
