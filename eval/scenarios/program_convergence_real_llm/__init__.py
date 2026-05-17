"""Program-level convergence with a real LLM + real embedder (skeleton).

Skeleton placeholder for the real-LLM fork of the nine-axis
``program_convergence`` master scenario. The full implementation lands
in **E3 (Wave 5)** of
[`docs/design/plan-next-swarm-wave.md`](../../../docs/design/plan-next-swarm-wave.md);
this directory exists so the scenario name is discoverable by
:func:`eval.runner.list_scenarios` and so import / credential-gating
contracts can be locked in before the cost-bearing per-round work is
written.

What ships in **E3-prep (this unit):**

- Credential-gated entry point — without ``OPENAI_API_KEY`` or
  ``ANTHROPIC_API_KEY`` set, ``run()`` returns a ``status="skip"``
  :class:`~eval.runner.ScenarioReport`. No registry mutation, no API
  calls, no cost.
- Module docstring documenting the expected cost envelope, credential
  gating, budget audit hook, and which deferred work belongs to E3.
- Test coverage for the import-cleanly path and the no-credentials
  skip path.

What is **deferred to E3 (Wave 5):**

- The actual per-round nine-axis loop with real embeddings.
- Re-use of ``program_convergence._run_loop`` once C2 (loop dedup)
  lands; until then the skeleton flags the orchestration scaffold as a
  duplicate-and-dedupe-later.
- A ``BUDGET_CONSUMED`` :class:`~trellis.stores.base.event_log.EventType`
  value + per-round payload contract. The hook in ``scenario.py`` is
  stubbed as a no-op that documents the payload shape; adding the enum
  member is part of E3.
- Real-credentials integration test (cost-bearing, operator-gated).

See ``scenario.py`` for the credential-gating skeleton.
"""
