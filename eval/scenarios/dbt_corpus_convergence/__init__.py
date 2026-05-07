"""dbt corpus convergence scenario — Phase B-1 of plan-real-corpus-eval.md.

A fork of ``agent_loop_convergence_real_llm`` that swaps the synthetic
domain-templated corpus for the Jaffle Shop dbt manifest, and the
round-robin domain queries for 12 hand-authored ground-truth queries
covering column-level transformations, multi-hop lineage, change-impact
analysis, and test cross-referencing.

See ``README.md`` for what this exercises and what it deliberately
defers (notably: ``GraphSearch`` is not in the strategy list — see
the note in scenario.py).
"""
