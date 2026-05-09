"""Shared parser for ``content_tags`` filter operators.

Both the SQLite and Postgres document stores translate
``filters["content_tags"][<facet>]`` into a SQL condition. The accepted
shape — and the operators it supports — must be identical across
backends so callers see the same semantics regardless of substrate.
This module is the single source of truth for that parse.

There is exactly one accepted shape: a single-key operator dict.

* ``{"in": [...]}`` — facet value matches any of the listed values.
* ``{"not_in": [...]}`` — facet value matches none. Strictly more
  expressive than ``in`` against an enumerated allowlist: robust to
  new facet values being added later.
* ``{"eq": x}`` — scalar equality. Sugar over ``{"in": [x]}``.
* ``{"ne": x}`` — scalar inequality. Sugar over ``{"not_in": [x]}``.

A bare list (legacy implicit-``in``) and a bare scalar are rejected
loudly. Silent no-ops were the failure mode this DSL was designed to
eliminate; permissive parsing brings them back.
"""

from __future__ import annotations

from typing import Any

# Operators that must always be paired with a list payload. ``in`` and
# ``not_in`` are the set-membership operators; ``eq`` / ``ne`` are
# scalar sugar that this module re-writes into single-element ``in`` /
# ``not_in`` so backends only have to handle the two list-shaped forms.
_LIST_OPERATORS = {"in", "not_in"}
_SCALAR_OPERATORS = {"eq", "ne"}
_ALL_OPERATORS = _LIST_OPERATORS | _SCALAR_OPERATORS


def normalize_facet_filter(value: Any) -> tuple[str, list[Any]] | None:
    """Normalize a single facet's filter payload into ``(operator, values)``.

    Accepts exactly one shape: a single-key operator dict where the key
    is ``in`` / ``not_in`` / ``eq`` / ``ne``. ``eq`` and ``ne`` are
    rewritten into single-element ``in`` / ``not_in`` so the backend
    SQL generators only deal with two cases.

    Returns ``None`` when the operator's value list is empty — that's
    the documented way to opt out of a facet at runtime (e.g. the
    PackBuilder default uses an empty ``not_in`` to mean "no
    exclusion"). An empty list is the only condition that produces
    ``None``; everything else either returns a tuple or raises.

    Raises ``ValueError`` for:

    * Any non-dict input — including the legacy bare-list form
      (``["a", "b"]``) and bare scalars (``"high"``, ``42``). Callers
      must spell the operator explicitly: ``{"in": ["a", "b"]}``,
      ``{"eq": "high"}``.
    * Operator dicts with zero or more than one key.
    * Unknown operator names.
    * Wrong payload type for the chosen operator (list payload on
      ``eq`` / ``ne``; non-list payload on ``in`` / ``not_in``).
    """
    if not isinstance(value, dict):
        # All malformed inputs raise the same exception type so callers
        # only have to catch one. The TRY004 "prefer TypeError for wrong
        # type" lint is reasonable in general but adds caller burden
        # without buying anything for an internal parser.
        msg = (
            "tag_filters facet value must be a single-key operator dict "
            f"(got {type(value).__name__}); use one of "
            f"{sorted(_ALL_OPERATORS)}"
        )
        raise ValueError(msg)  # noqa: TRY004
    if len(value) != 1:
        msg = (
            "tag_filters facet operator dict must have exactly one key "
            f"(got {sorted(value)})"
        )
        raise ValueError(msg)
    op, payload = next(iter(value.items()))
    if op in _LIST_OPERATORS:
        if not isinstance(payload, list):
            msg = (
                f"tag_filters '{op}' requires a list payload, got "
                f"{type(payload).__name__}"
            )
            raise ValueError(msg)
        return (op, list(payload)) if payload else None
    if op in _SCALAR_OPERATORS:
        # Reject list / dict payloads on eq / ne — callers wanting set
        # semantics should spell the list operators directly.
        if isinstance(payload, list | dict):
            msg = (
                f"tag_filters '{op}' requires a scalar payload, got "
                f"{type(payload).__name__}"
            )
            raise ValueError(msg)
        return ("in" if op == "eq" else "not_in", [payload])
    msg = (
        f"tag_filters unknown operator '{op}'; expected one of: "
        f"{', '.join(sorted(_ALL_OPERATORS))}"
    )
    raise ValueError(msg)
