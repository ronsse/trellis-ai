"""Eval scenarios — one subpackage per named scenario.

Each subpackage defines ``scenario.py`` exposing
``run(registry) -> ScenarioReport``. The runner discovers them
automatically; nothing here re-exports them.
"""
