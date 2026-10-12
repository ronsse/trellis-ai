"""No internal plan/task identifier leaks into the public repository tree.

This is a public repo: an internal work-item label (the kind used to
coordinate concurrent agents on a shared backlog) must never appear in
``src/``, ``tests/``, ``docs/agent-guide/`` or ``docs/ops/``. Unlike
``test_ui_operator_copy_rule.py`` (which polices only the strings
*rendered* by ``index.html``), this sweep reads every scannable file's
raw text wherever it sits in the tree -- a docstring, a code comment, a
markdown doc, a shell example.

Five shapes are policed, each a plain regex, not a shared mechanical
layer: ``tests/ast_rules.py`` resolves how a Python *call site* is
spelled or bound (aliasing, rebinding, subclassing) -- there is no call
site here, only literal text, so that module contributes nothing beyond
``assert_hand_read_floor`` (reused below, since it is documented as a
general-purpose floor, not a call-site concept).

1. A single letter, ``e`` or ``f``, immediately followed by exactly
   three digits and a word boundary -- the shape of this backlog's own
   work-item and subagent-worktree names.
2. A ``#`` immediately followed by shape 1's letter and one or more
   digits -- the shape used when such an id is cited inline like a
   GitHub issue.
3. The word naming a coordination document, a single space, then the
   two-character token for its first phase.
4. The hyphenated slug naming this UI work's control-plane doc.
5. The word for a hand-off document, a space, then shape 1's letter and
   digits.

The module docstring above deliberately describes shapes 1-5 in prose
rather than spelling out a contiguous example of any of them, and the
proof test below builds each one by string concatenation at runtime
rather than as a literal in this file's own source text -- otherwise
this file's own necessary fixture text would trip the very sweep it
proves, the same self-collision already found and fixed in
``test_ui_operator_copy_rule.py``'s plan-id proof (see that file's
docstring). The pattern-label strings used below for assertion messages
are worded the same way, for the same reason.

Three shapes are deliberately *not* flagged, structurally rather than
via an allow-list:

- A CSS/JS hex colour such as ``index.html``'s ``#e1e4ed``, ``#e0e7ff``
  or ``#e879f9``. Patterns 1 and 2 both require a trailing word
  boundary immediately after their digits; a hex colour's digits run
  straight into more hex letters with no boundary there, so neither
  pattern can match. Verified against those three real occurrences.
- Scientific notation (``1e100``). Patterns 1 and 2 both require a
  *leading* word boundary immediately before the ``e``/``f``; a
  preceding digit (as in ``1e100``) is a word character, so there is no
  boundary there either.
- A bare GitHub issue number (``#871``, or the ``#466`` cited in
  ``assert_hand_read_floor``'s own docstring). Pattern 2 requires a
  literal ``e`` right after ``#``, which a bare numeral does not have.

``_ALLOWED`` below is the explicit, short, file-scoped escape hatch for
any future legitimate match this reasoning does not anticipate. It is
empty today: the real tree has zero matches (see the proof test),
nothing needs to be allowed yet.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_DIRS = ("src", "tests", "docs/agent-guide", "docs/ops")
_SCAN_EXTENSIONS = frozenset(
    {
        ".py",
        ".md",
        ".rst",
        ".txt",
        ".html",
        ".cfg",
        ".ini",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".sh",
        ".js",
    }
)

# The two fixed-phrase patterns below build their own pattern *source*
# by concatenation rather than as one literal regex string: a single
# literal would itself contain the exact forbidden phrase as a
# contiguous substring, tripping this file's own sweep (and any other
# repo-wide label sweep) the moment it is read off disk. The
# runtime-compiled re.Pattern behaves identically either way.
_INTERNAL_E_OR_F_ID = re.compile(r"\b[ef][0-9]{3}\b")
_HASH_E_ID = re.compile(r"#e[0-9]+\b")
_PLAN_P1 = re.compile(r"\b" + "plan" + " p1" + r"\b", re.IGNORECASE)
_P1_UI_CONTROL_PLANE = re.compile(
    r"\b" + "p1" + "-ui-control-plane" + r"\b", re.IGNORECASE
)
_BRIEF_E_ID = re.compile(r"\bbrief e[0-9]+\b", re.IGNORECASE)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal e-/f- id", _INTERNAL_E_OR_F_ID),
    ("hash-prefixed e-id", _HASH_E_ID),
    ("internal multi-doc-plan reference", _PLAN_P1),
    ("internal UI-control-plane slug", _P1_UI_CONTROL_PLANE),
    ("internal brief-id reference", _BRIEF_E_ID),
)

# path (relative to repo root, POSIX-style) -> matched substrings allowed
# in that one file. See the module docstring's last paragraph.
_ALLOWED: dict[str, frozenset[str]] = {}


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCAN_DIRS:
        base = _REPO_ROOT / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in _SCAN_EXTENSIONS:
                continue
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _violations_in_text(text: str) -> list[tuple[str, str]]:
    """Return ``(pattern-label, matched-text)`` for every forbidden match."""
    hits: list[tuple[str, str]] = []
    for label, pattern in _PATTERNS:
        hits.extend((label, match.group(0)) for match in pattern.finditer(text))
    return hits


def _scan_repo() -> dict[Path, list[tuple[str, str]]]:
    violations: dict[Path, list[tuple[str, str]]] = {}
    for path in _scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        allowed = _ALLOWED.get(rel, frozenset())
        hits = [
            (label, matched)
            for label, matched in _violations_in_text(text)
            if matched not in allowed
        ]
        if hits:
            violations[path] = hits
    return violations


# ---------------------------------------------------------------------------
# Vacuity guard: the scan must actually be reading a tree this size.
# ---------------------------------------------------------------------------


def test_scan_population_is_above_a_hand_read_floor() -> None:
    # Hand count (2026-10-12): a find over src/, tests/, docs/agent-guide/
    # and docs/ops/ for the extensions in _SCAN_EXTENSIONS, excluding
    # __pycache__, read 1035 files. A floor of 900 catches a scan
    # narrowed to a single one
    # of the four directories (each is well under 900 alone) or to an
    # empty glob, while leaving room for ordinary file growth.
    files = _scan_files()
    assert_hand_read_floor(
        len(files),
        900,
        subject="scannable file(s) under src/, tests/, docs/agent-guide/, docs/ops/",
        hint="re-run the find command in this test's comment and compare.",
    )


# ---------------------------------------------------------------------------
# The real tree, today: zero violations.
# ---------------------------------------------------------------------------


def test_repo_carries_no_internal_plan_or_task_identifier() -> None:
    violations = _scan_repo()
    assert violations == {}, (
        "internal plan/task identifiers leaked into the public repo tree: "
        + "; ".join(f"{path}: {hits}" for path, hits in sorted(violations.items()))
    )


# ---------------------------------------------------------------------------
# The three structural exclusions, each pinned against a real shape.
# ---------------------------------------------------------------------------


def test_bare_github_issue_number_is_not_flagged() -> None:
    # The exact shape this file's own docstring cites as legitimate.
    assert _violations_in_text("see #871 and #466 for context") == []


def test_scientific_notation_numeral_is_not_flagged() -> None:
    assert _violations_in_text("a float literal like 1e100 or 2f100") == []


def test_hex_colour_is_not_flagged() -> None:
    # The three real swatches present in index.html today.
    for swatch in ("#e1e4ed", "#e0e7ff", "#e879f9"):
        assert _violations_in_text(f"background: {swatch};") == []


# ---------------------------------------------------------------------------
# Proof: each pattern catches a deliberately defective subject.
# ---------------------------------------------------------------------------


def test_each_pattern_catches_a_deliberately_defective_subject() -> None:
    # Every forbidden shape below is built by concatenation, never
    # written as a contiguous literal in this file's own source text --
    # see the module docstring for why.
    e_id = "e" + "180"
    f_id = "f" + "142"
    hash_e_id = "#" + "e" + "879"
    plan_p1 = "plan" + " " + "p1"
    p1_slug = "p1" + "-ui-control-plane"
    brief_e = "brief" + " " + "e" + "140"

    defective_subjects = {
        "internal e-/f- id": f"fixed by {e_id} and {f_id}",
        "hash-prefixed e-id": f"tracked as {hash_e_id} upstream",
        "internal multi-doc-plan reference": f"see the {plan_p1} doc",
        "internal UI-control-plane slug": f"branch {p1_slug}",
        "internal brief-id reference": f"read the {brief_e} write-up",
    }
    for label, text in defective_subjects.items():
        hits = _violations_in_text(text)
        assert hits, f"guard did not catch a {label} in: {text!r}"
