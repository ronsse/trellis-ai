"""Reading ``PACK_ASSEMBLED.injected_items`` goes through ``feedback/pack_rows.py``.

Every analysis that read the served rows by hand decided separately whether to
skip a sectioned pack and whether to drop a held-out one, and the copies
drifted: a reader that forgot a policy read as correct until a payload of the
forgotten shape arrived.
:func:`trellis.feedback.pack_rows.served_rows_from_payload` makes both policies
required keyword arguments, so a new reader has to state them.

This rule keeps the count of hand-written reads from growing. It walks the AST
of ``src/`` for a ``.get("injected_items")`` or ``["injected_items"]`` read, and
for a ``.get(<name>)`` whose name is one of the module constants that spell the
key, and allows only the sites below. Each is a read that is not "iterate the
rows under a policy", which is why it was left rather than migrated:

* ``pack_rows.py`` is the reader.
* ``attribution.py`` holds the membership helpers ``pack_rows`` itself imports,
  so it cannot import ``pack_rows`` back.
* ``pack_observations.derive_selection_efficiency`` takes the raw row count as
  a ratio numerator and treats a non-sequence as "unobserved", not as zero.
* ``advisory_generator`` copies the raw list into an analysis row.
* ``session_capture`` unions three differently-shaped records and returns
  ``None`` on any malformed one, which no per-row iterator expresses.

An allowlist entry is exact: a count above it fails (a new read), and a count
below it fails too, because the allowlist then names a read that is gone and
the number should come down with it. Writers build the key as a dict literal,
not a read, and are not matched.

The scan is checked against a synthetic corpus, and the population it found is
held to a number read off the tree by hand, so a scan that has stopped matching
cannot pass by finding nothing.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules

_SRC = Path(__file__).resolve().parents[2] / "src"
_KEY = "injected_items"
#: Module constants that spell the payload key, matched by their last name part.
_KEY_NAMES = frozenset({"_INJECTED_ITEMS", "INJECTED_ITEMS", "INJECTED_ITEMS_KEY"})

#: ``src``-relative path -> how many reads of the key it may hold. Hand-read.
_ALLOWED: dict[str, int] = {
    "trellis/feedback/pack_rows.py": 2,
    "trellis/feedback/attribution.py": 2,
    "trellis/learning/pack_observations.py": 1,
    "trellis/retrieve/advisory_generator.py": 1,
    "trellis_workers/session_capture/capture.py": 1,
}

#: The sum of ``_ALLOWED``, counted by hand off the tree.
_HAND_READ_TOTAL = 7


def _names_the_key(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == _KEY
    if isinstance(node, ast.Name):
        return node.id in _KEY_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _KEY_NAMES
    return False


def _is_read(node: ast.AST) -> bool:
    """``x.get(<key>)`` or a load-context ``x[<key>]``."""
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and bool(node.args)
            and _names_the_key(node.args[0])
        )
    if isinstance(node, ast.Subscript):
        return isinstance(node.ctx, ast.Load) and _names_the_key(node.slice)
    return False


def _key_reads(tree: ast.AST) -> list[int]:
    """Line numbers of every read of the payload key in *tree*."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call | ast.Subscript) and _is_read(node)
    ]


def _counts() -> Counter[str]:
    found: Counter[str] = Counter()
    for path, tree in iter_modules(_SRC):
        reads = _key_reads(tree)
        if reads:
            found[path.relative_to(_SRC).as_posix()] = len(reads)
    return found


class TestTheScan:
    """The scan sees every spelling, and nothing else."""

    @pytest.mark.parametrize(
        "source",
        [
            'payload.get("injected_items")',
            'payload.get("injected_items", [])',
            '(event.payload or {}).get("injected_items") or []',
            'payload["injected_items"]',
            "payload.get(_INJECTED_ITEMS)",
            "payload.get(INJECTED_ITEMS_KEY)",
            "payload.get(pack_rows.INJECTED_ITEMS_KEY)",
            'for raw in pack_payload.get("injected_items", []) or []: pass',
        ],
    )
    def test_a_read_is_found(self, source: str) -> None:
        assert len(_key_reads(ast.parse(source))) == 1

    @pytest.mark.parametrize(
        "source",
        [
            'payload = {"injected_items": rows}',
            'payload.get("injected_item_ids")',
            'payload.get("rejected_items")',
            '"""Reads ``payload.get("injected_items")`` by hand."""',
            '# payload.get("injected_items")',
            "payload.get(key)",
            'other["injected_items"] = rows',
        ],
    )
    def test_a_non_read_is_not(self, source: str) -> None:
        # A dict literal and a subscript assignment are writes (a Store-context
        # ``Subscript`` reads nothing), and a docstring or comment is no node.
        assert _key_reads(ast.parse(source)) == []

    def test_a_new_hand_written_loop_is_a_violation(self) -> None:
        """The defective subject the rule exists to refuse."""
        defective = ast.parse(
            "def f(payload):\n"
            '    for raw in payload.get("injected_items") or []:\n'
            "        pass\n"
        )
        assert _key_reads(defective) == [2]


class TestTheTree:
    def test_the_scan_has_not_stopped_matching(self) -> None:
        found = _counts()
        assert_hand_read_floor(
            sum(found[path] for path in _ALLOWED),
            _HAND_READ_TOTAL,
            subject="allowlisted injected_items read",
            hint="The allowlist names reads the scan no longer finds.",
        )

    def test_no_new_hand_written_read_of_the_key(self) -> None:
        found = _counts()
        unlisted = {path: n for path, n in found.items() if path not in _ALLOWED}
        grown = {
            path: (found[path], allowed)
            for path, allowed in _ALLOWED.items()
            if found[path] > allowed
        }
        assert not unlisted, (
            f"read(s) of payload['{_KEY}'] outside the allowlist: {unlisted}. "
            "Iterate the rows with trellis.feedback.pack_rows."
            "served_rows_from_payload (or iter_served_rows) and state "
            "include_sectioned / include_holdout, or test row presence with "
            "has_served_rows. If the read is not a per-row walk, add it to "
            "_ALLOWED with the reason in this module's docstring."
        )
        assert not grown, f"more reads than allowed (found, allowed): {grown}"

    def test_the_allowlist_only_shrinks(self) -> None:
        found = _counts()
        stale = {
            path: (found[path], allowed)
            for path, allowed in _ALLOWED.items()
            if found[path] < allowed
        }
        assert not stale, (
            f"allowlist entries name reads that are gone (found, allowed): {stale}. "
            "Lower the number, drop the entry, and update _HAND_READ_TOTAL."
        )
