# `eval/generators/`

Synthetic data generators. Two rules:

1. **Deterministic given a seed.** Same `seed` argument, same output.
   No clock, no `random.random()` without an explicit seeded
   `random.Random` instance.
2. **Output is never committed.** Generators are *the* source of
   truth — datasets emitted by them go to a tmp dir or
   `eval/reports/` and stay there.

Add a generator here when more than one scenario uses it. Single-use
generators can live alongside the scenario for now and graduate when a
second consumer appears.
