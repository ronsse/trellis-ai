"""The degenerate shapes a damaged ``DegradableJsonStore`` file takes.

One table, parameterised by envelope key, for every store that subclasses
:class:`~trellis.stores.degradable_json_store.DegradableJsonStore` and for
the strict enforcement reader that reads one of the same files.

It exists because there were two copies and they disagreed. ``policies``
carried ``{"policies": null}``, a bare scalar and a truncated document;
``advisories`` carried ``{"advisories": "not a list"}`` and ``{"version":
2, "items": []}``; neither was a superset of the other. Both were asserted
against **line-for-line identical code** — the divergence was in the tests,
not the behaviour, so adding a shape to one copy silently left the other
uncovered. That is the same duplication #426 removed from the source, and
the load ladder is the exact branch where #414's first attempt shipped the
``raw.get(key, [])`` bug it was written to fix.

Consumers assert **opposite** things about this table, and that is the
point:

* the CRUD stores (``tests/unit/stores/test_degradable_json_store.py``)
  must degrade on every shape and refuse every write;
* the enforcement reader (``tests/unit/mutate/test_policy_wiring.py``)
  must *raise* on every shape.

The asymmetry between the two readers is #413's whole design, so it has to
be a property over one table rather than a coincidence between two.

The two shapes that matter most are ``{}`` and the typo'd key. They are
*valid JSON*, and every reader used to load them as **zero rows,
silently** — which for the enforcement reader is a fail-open reachable by
a one-character hand-edit.
"""

from __future__ import annotations

import json


def _typo(key: str) -> str:
    """``key`` with its last two characters transposed.

    A hand-edit that reaches the missing-key branch while still looking,
    to a skim, like the file the store writes.
    """
    return key[:-2] + key[-1] + key[-2]


def degenerate_files(envelope_key: str) -> list[tuple[str, str, str]]:
    """``(id, file contents, the ``reason`` a degradable store must record)``.

    The enforcement reader ignores the third element: it raises on all of
    them, and the *kind* of malformation is not a distinction it draws.
    """
    return [
        # -- Valid JSON, wrong envelope. The dangerous half: nothing here
        # -- errors on its own, so a reader that shrugs loads zero rows.
        ("empty_json_object", "{}", "malformed_envelope"),
        (
            "typoed_key",
            json.dumps({_typo(envelope_key): [{"id": "x"}]}),
            "malformed_envelope",
        ),
        (
            "unrelated_envelope",
            json.dumps({"version": 2, "items": []}),
            "malformed_envelope",
        ),
        ("null_envelope_value", json.dumps({envelope_key: None}), "malformed_envelope"),
        (
            "envelope_value_not_a_list",
            json.dumps({envelope_key: "not a list"}),
            "malformed_envelope",
        ),
        ("bare_list", "[]", "malformed_envelope"),
        ("scalar", json.dumps("not a Trellis file"), "malformed_envelope"),
        # A *non-string* scalar, deliberately alongside the string one. Every
        # reader guards the envelope with an ``isinstance(data, dict)`` check,
        # and deleting that check leaves a JSON *string* still raising — ``in``
        # on a string is a substring test, so ``"policies" not in "not a
        # Trellis file"`` is True and the missing-key branch catches it by
        # accident. A number or a null has no ``in`` at all, so the same
        # deletion turns a ConfigError carrying the recovery advice into a bare
        # ``TypeError`` traceback: still failing closed, but as the unhandled
        # shape this module's whole point is to not produce. Measured: with
        # only the string scalar present, removing the check left all 266
        # targeted tests green.
        ("numeric_scalar", "3", "malformed_envelope"),
        # -- Not JSON at all.
        ("not_json", "not json at all", "malformed_json"),
        ("empty_file", "", "malformed_json"),
        ("truncated_json", '{"' + envelope_key + '": [{"id', "malformed_json"),
    ]


def degenerate_ids(envelope_key: str) -> list[str]:
    """Parametrisation ids, so a failure names the shape rather than an index."""
    return [shape[0] for shape in degenerate_files(envelope_key)]
