# ruff: noqa: EM102, TRY003
"""Audit fields copied at several construction sites that no assertion pins.

#447 and #456 are one defect twice: **eleven of twelve** ``item_type`` /
``relevance_score`` mutants survived the full default selection, across six
sites each hand-copying four fields off a ``PackItem``. #447 diagnosed the
cause as two *fixture* shapes — a population sized 1, and a pool too uniform
to distinguish the field under test.

**That diagnosis does not generalise into a detector.** A sweep for uniform
fixture pools returns 122 hits on the pre-#456 tree and 122 on the post-#456
tree, flagging both ``PackItem`` fields on each — because #456 changed the
*source* (six copies to one constructor) and added tests, and changed no
fixture at all. A check green on both sources tells them apart only if it can,
which is the equivalence argument #456 itself rested on.

What does generalise is the arithmetic one layer over. Four assertions in the
suite pinned those two fields; they reached **one** of the six sites, so a
constant-fold at the other five changed nothing observable. Uniformity is the
*enabler*; the *detector* is ``sites - pins``.

So this script counts, per ``Class.field``:

``sites``
    ``src/`` call sites constructing ``Class(..., field=<obj>.<attr>, ...)``.
    Only a read off a lower-case binding counts — ``operation=Operation.X`` is
    an enum member, where a constant-fold *is* the constant and can diverge
    from nothing.
``same``
    How many of those are same-name copies (``field=obj.field``), the shape
    #456 found: an identical hand-written copy repeated per branch.
``pins``
    Assertions under ``tests/unit/`` comparing ``.field`` to a concrete value.

**Read ``gap = sites - pins`` as a lower bound, never as a coverage figure.**
``pins`` is keyed on the bare attribute name, so it cannot separate
``Command.operation`` (the input) from ``CommandResult.operation`` (the copy)
and **over-counts**. The error runs one way by construction: a high gap is
trustworthy, a low gap is not evidence of coverage. Only a mutant settles it,
which is why the report ends by naming the mutants to run rather than a verdict.

The script is read-only, never modifies a source file, and emits deterministic
Markdown so re-runs are diffable.

Validated by discriminating the two sides of the defect it generalises. Against
the pre-#456 tree (``76bf565``) ``RejectedItem.item_type`` ranks **5** — 7 sites,
7 same-name copies, 5 pins, gap +2 — and against ``origin/main`` the whole
``RejectedItem`` family is **absent**, the consolidation into
``RejectedItem.from_pack_item`` having left one construction site in ``src/``
(the class definition), below the two-site floor.

It caught one of #456's two fields and missed the other, which is the sharper
result. ``RejectedItem.relevance_score`` scored **-15** on that same pre-#456
tree: 22 assertions read a bare ``.relevance_score`` and exactly one has a
receiver that could be a ``RejectedItem``. #456 then proved that field uncovered
at five of its six sites. The column over-counted by ~21x, in the direction that
hides a defect — which is what the lower-bound caveat above is about, measured
on a case whose ground truth is known.

Usage
-----

::

    python scripts/audit_replicated_field_copies.py \\
        --output audit/replicated_field_copies.md
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections import Counter, defaultdict

#: A field copied at fewer sites than this is not the shape #456 found: one
#: site cannot disagree with itself, so there is no silent divergence to hide.
DEFAULT_MIN_SITES = 2


def _construction_name(call: ast.Call) -> str | None:
    """Class name of a constructor call, or ``None`` if this is not one."""
    func = call.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else (func.id if isinstance(func, ast.Name) else None)
    )
    return name if name and name[0].isupper() else None


def collect_sites(
    src: pathlib.Path,
) -> dict[tuple[str, str], list[tuple[str, int, bool, str]]]:
    """Every ``Class(field=<obj>.<attr>)`` construction site under ``src``."""
    sites: dict[tuple[str, str], list[tuple[str, int, bool, str]]] = defaultdict(list)
    for path in sorted(src.rglob("*.py")):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:  # never skip silently — name the file
            raise SystemExit(f"parse error in {path}: {exc}") from exc
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            class_name = _construction_name(node)
            if class_name is None:
                continue
            for kw in node.keywords:
                value = kw.value
                if not kw.arg or not isinstance(value, ast.Attribute):
                    continue
                if not isinstance(value.ctx, ast.Load):
                    continue
                base = value.value
                # `operation=Operation.ENTITY_CREATE` names an enum member, not
                # an instance read: folding it to a constant IS the constant.
                if isinstance(base, ast.Name) and base.id[:1].isupper():
                    continue
                sites[(class_name, kw.arg)].append(
                    (
                        str(path),
                        value.lineno,
                        value.attr == kw.arg,
                        lines[value.lineno - 1].strip()[:80],
                    )
                )
    return sites


def collect_pins(
    tests: pathlib.Path,
) -> tuple[Counter, dict[str, list[tuple[str, int, str]]]]:
    """Assertions comparing ``.field`` to a concrete value, by field name."""
    pins: Counter = Counter()
    where: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for path in sorted(tests.rglob("test_*.py")):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise SystemExit(f"parse error in {path}: {exc}") from exc
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            for cmp_node in ast.walk(node.test):
                if not isinstance(cmp_node, ast.Compare) or not cmp_node.comparators:
                    continue
                pair = (cmp_node.left, cmp_node.comparators[0])
                for lhs, rhs in (pair, pair[::-1]):
                    if not isinstance(lhs, ast.Attribute):
                        continue
                    if not isinstance(lhs.ctx, ast.Load):
                        continue
                    if not isinstance(rhs, ast.Constant | ast.Attribute | ast.Name):
                        continue
                    pins[lhs.attr] += 1
                    where[lhs.attr].append(
                        (str(path), node.lineno, lines[node.lineno - 1].strip()[:80])
                    )
    return pins, where


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", default="src", type=pathlib.Path)
    parser.add_argument("--tests", default="tests/unit", type=pathlib.Path)
    parser.add_argument("--min-sites", default=DEFAULT_MIN_SITES, type=int)
    parser.add_argument("--top", default=25, type=int)
    parser.add_argument("--output", type=pathlib.Path, help="write Markdown here")
    args = parser.parse_args(argv)

    sites = collect_sites(args.src)
    pins, pin_where = collect_pins(args.tests)

    rows = []
    for (class_name, field), occurrences in sites.items():
        n_sites = len({(p, ln) for p, ln, _, _ in occurrences})
        if n_sites < args.min_sites:
            continue
        n_same = sum(1 for _, _, same, _ in occurrences if same)
        n_pins = pins.get(field, 0)
        rows.append(
            (n_sites - n_pins, n_sites, n_same, n_pins, class_name, field, occurrences)
        )
    rows.sort(key=lambda r: (-r[0], -r[1], r[4], r[5]))

    out = [
        "# Replicated field copies with no pinning assertion",
        "",
        (
            f"`{len(rows)}` `Class.field` pairs constructed at >= {args.min_sites} "
            f"sites in `{args.src}`, ranked by `gap = sites - pins`."
        ),
        "",
        (
            "`gap` is a **lower bound**: `pins` is keyed on the bare attribute name, "
            "so it cannot separate one class's field from another's and over-counts. "
            "A high gap is trustworthy; a low gap is not evidence of coverage. "
            "Confirm with a mutant."
        ),
        "",
        "| Class.field | sites | same-name | pins | gap |",
        "|---|---:|---:|---:|---:|",
    ]
    for gap, n_sites, n_same, n_pins, class_name, field, _ in rows[: args.top]:
        out.append(
            f"| `{class_name}.{field}` | {n_sites} | {n_same} | {n_pins} | {gap:+d} |"
        )

    for gap, n_sites, _n_same, n_pins, class_name, field, occurrences in rows[:3]:
        heading = (
            f"## `{class_name}.{field}` — {n_sites} sites, {n_pins} pins, gap {gap:+d}"
        )
        out += ["", heading, ""]
        for path, lineno, same, text in sorted(set(occurrences)):
            marker = " *(same-name copy)*" if same else ""
            out.append(f"- `{path}:{lineno}`{marker} — `{text}`")
        for path, lineno, text in pin_where.get(field, [])[:6]:
            out.append(f"- pin: `{path}:{lineno}` — `{text}`")

    report = "\n".join(out) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
