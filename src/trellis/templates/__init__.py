"""Shared markdown / text templates used by the CLI.

Templates here use :meth:`str.format` (no external templating engine).
Loaded as package-data via :mod:`importlib.resources`. Keep them
text-only — anything that needs Jinja-class control flow probably
belongs in code, not a template.
"""
