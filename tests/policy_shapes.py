"""The degenerate shapes a damaged ``policies.json`` takes, in one place.

Two suites assert **opposite** things about this table, and that is the
point: the CRUD store (``tests/unit/stores/test_policy_store.py``) must
degrade on every shape and refuse every write, while the enforcement reader
(``tests/unit/mutate/test_policy_wiring.py``) must raise on every shape.
The asymmetry between the two readers is #413's whole design, so it has to
be a property over one table rather than a coincidence between two.

It lives here because it *was* two tables. Both files carried a verbatim
copy with a comment in each asserting they were the same, and nothing
enforced it — so they had already diverged: one had ``{"policies": null}``,
a bare scalar and a truncated document; the other did not. Adding a shape
to one copy silently left the other uncovered, on the exact code path where
#414's first attempt shipped the bug it was fixing.

The two shapes that matter most are ``{}`` and the typo'd key. They are
*valid JSON*, and both readers used to load them as **zero policies,
silently** — which for the enforcement reader is a fail-open reachable by a
one-character hand-edit.
"""

from __future__ import annotations

#: ``(id, file contents, the ``reason`` the CRUD store must record)``.
#: The enforcement reader ignores the third element: it raises on all of
#: them, and the *kind* of malformation is not a distinction it draws.
DEGENERATE_POLICY_FILES: list[tuple[str, str, str]] = [
    ("empty_json_object", "{}", "malformed_envelope"),
    ("null_policies_key", '{"policies": null}', "malformed_envelope"),
    ("typoed_key", '{"policys": [{"policy_id": "x"}]}', "malformed_envelope"),
    ("bare_list", "[]", "malformed_envelope"),
    ("scalar", '"not a policy file"', "malformed_envelope"),
    ("empty_file", "", "malformed_json"),
    ("truncated_json", '{"policies": [{"policy_i', "malformed_json"),
]

#: Parametrisation ids, so a failure names the shape rather than an index.
DEGENERATE_POLICY_IDS: list[str] = [s[0] for s in DEGENERATE_POLICY_FILES]
