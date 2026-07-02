"""Skill-loop convergence — F-phase inner-loop scenario.

Wave 1, Unit D of Phase F, implemented as the reference-driver build
(issue #249). The scenario measures whether the inner agent loop
(curator + feedback + score-based evolver) converges over time:
under-populated nodes get enriched (axis P), pack quality improves as a
result (axis Q, measured on real PackBuilder / evaluate_pack /
EventLog), and score-based pruning retains the variants that produced
the lift (axis R — reference evolver; validates the measurement path,
not a production F5).

The F2 curator skill and F5 evolver are stood in for by deterministic
scenario-local drivers (``_ReferenceCurator`` / ``_ReferenceEvolver``
in :mod:`scenario`); when the F-phase machinery lands it replaces those
drivers and the seed helpers, panel, reducers, and report shape stay as
they are. ``run()`` returns ``status="skip"`` unless
``TRELLIS_EVAL_SKILL_LOOP`` is set — see the package README.
"""
