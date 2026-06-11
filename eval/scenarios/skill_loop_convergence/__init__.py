"""Skill-loop convergence — F-phase inner-loop scenario (skeleton).

Wave 1, Unit D of Phase F. Scaffolded so F1-F5 can plug their
implementations in without renegotiating the file layout.

The scenario measures whether the inner agent loop (graph-skill harness
+ curator skill + feedback + score-based evolver) converges over time:
under-populated nodes get enriched, pack quality improves as a result,
and the F5 evolver retains the prompt variants that produced the lift.

Every callable here raises :class:`NotImplementedError` with a docstring
naming the F-phase that fills it in. The runner discovers the scenario
by name, but :func:`run` returns ``status="skip"`` until the F-phase
machinery lands — see :mod:`scenario` for the gating contract.

See the package README for the F-phase wiring table and the references
to ``adr-graph-skill-harness.md`` / ``adr-inner-curation-loop.md``.
"""
