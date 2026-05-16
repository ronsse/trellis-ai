"""Program-level regression suite — CI gate for the nine convergence axes.

Runs the master :mod:`eval.scenarios.program_convergence` scenario at
50 rounds, calls every satellite for liveness, and asserts the nine
threshold lines from ``docs/design/plan-program-level-eval.md`` §4.2.
A regression in any axis flips the suite's status to ``regress``,
which the runner translates to exit code 1.

See ``scenario.py`` for the entry point.
"""
