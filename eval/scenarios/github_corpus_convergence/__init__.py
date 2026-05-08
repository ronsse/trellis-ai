"""GitHub corpus convergence scenario — Phase B-2 of plan-real-corpus-eval.md.

Fork of ``dbt_corpus_convergence`` that swaps the Jaffle Shop dbt
corpus for the trellis-ai PR snapshot under
``eval/corpora/github_trellis/``.

Same retrieval shape: KeywordSearch + SemanticSearch + SeededGraphSearch
(with PR-aware seed extraction). Same telemetry, cost guards, and
convergence math. Same ``run(registry, ...)`` signature so the runner
can dispatch it identically.

See README.md for what the corpus contains and which skills the
queries exercise.
"""
