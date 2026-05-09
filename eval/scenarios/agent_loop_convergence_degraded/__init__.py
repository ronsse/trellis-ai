"""Degraded-retrieval convergence scenario — proves the dual loop earns its keep.

The baseline ``agent_loop_convergence`` scenario starts from precise
retrieval (only 2 distractors per domain, pack budget of 8) and
measures whether the loop holds up that quality. The honest answer:
``useful_delta`` ≈ 0 because the loop has nothing to learn — the agent
hits the right entities on round 1.

This scenario starts from *deliberately degraded* retrieval — many
distractors that match the query tokens, a tight pack budget that
forces them to compete with real entities — and runs long enough for
the dual loop to demote them. ``useful_delta`` should rise from a low
starting point as noise tagging accumulates and packs become precise.

That climb is the load-bearing claim Trellis makes ("improves with
use"); this scenario is what proves it.

See ``scenario.py`` for the run entry point.
"""
