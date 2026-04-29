"""Scenario 5.4 — end-to-end agent loop convergence.

Simulates an agent using Trellis over N rounds: build pack → grade
ground-truth coverage → record feedback. Periodically runs the
effectiveness + advisory fitness loops so noise items get tagged and
under-performing advisories get suppressed. Tracks pack-quality and
useful-fraction over rounds; both should improve as the loops do their
work.

See ``scenario.py`` for the run entry point.
"""
