"""Program-level convergence with a real LLM-backed embedder.

Real-embedding fork of the nine-axis ``program_convergence`` master
scenario. Lands as **E3 (Wave 5)** of
[`docs/design/plan-next-swarm-wave.md`](../../../docs/design/plan-next-swarm-wave.md);
the synthetic master in ``eval.scenarios.program_convergence`` stays the
substrate so per-seed diffs against it remain meaningful.

What this scenario does:

- Builds the same nine-axis ``_RoundResult`` curve as the synthetic
  master, but layers ``SemanticSearch`` (backed by OpenAI's
  ``text-embedding-3-small``) on top of the keyword retrieval path.
- Embeds every seed-entity summary at setup time and every per-round
  query at retrieval time, accumulating per-call telemetry.
- Emits ONE
  :attr:`~trellis.stores.base.event_log.EventType.BUDGET_CONSUMED`
  event per run carrying ``{tokens_consumed, dollars_estimated,
  provider, model}`` — operators reconcile spend by joining this event
  type on ``source="eval.program_convergence_real_llm"`` and
  ``entity_id=<run_id>``.
- Enforces a per-run hard cost cap (``run_hard_cost_cap_usd``, default
  $2.00). Mid-loop overruns raise :class:`RunBudgetError`; the budget
  event is emitted before the raise so the bill is always visible.

Credential gating:

- No ``OPENAI_API_KEY`` and no ``ANTHROPIC_API_KEY`` →
  ``status="skip"`` with an info finding pointing operators at both
  env vars.
- Only ``ANTHROPIC_API_KEY`` set → ``status="skip"`` with an info
  finding noting that embeddings require OpenAI today
  (``eval/_real_llm.py`` only ships an OpenAI factory).
- ``OPENAI_API_KEY`` set → runs end-to-end against the real OpenAI
  embedding endpoint.

CI exercise:

- ``TRELLIS_EVAL_REAL_LLM_MOCK=1`` swaps the real embedder for a
  deterministic in-memory mock. The same nine-axis loop +
  BUDGET_CONSUMED emit path runs; no API calls are made. CI uses
  this hatch.

Cost calibration: see the ``scenario.py`` module docstring for the
formula. At the default 50 rounds against the synthetic corpus the
expected token total is ~1160 tokens (~$0.0000232) — two orders of
magnitude below the $2 cap. The cap exists to absorb pricing
regressions or larger operator-supplied corpora without surprise.
"""
