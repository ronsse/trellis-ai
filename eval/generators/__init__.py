"""Synthetic data generators.

Each scenario that needs synthetic input drops its generator here so
generators can be reused across scenarios. Output is never committed —
generators must be deterministic given a seed.
"""
