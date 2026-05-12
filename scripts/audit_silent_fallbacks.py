# ruff: noqa: EM102, TRY003, PLR0911, PLR0915, PLR2004
"""Silent-fallback audit script (Cleanup C2 Phase 0).

Walks a source tree, locates every ``except`` clause, and classifies each
based on the handler body. Produces a deterministic Markdown report so
re-runs are diffable.

Per the POC directive in ``docs/design/plan-self-improvement-program.md``
§2, this script does **not** silently skip files it cannot parse — parse
errors raise with the filename in the message. The script never modifies
source files; it is strictly read-only.

Usage
-----

::

    python scripts/audit_silent_fallbacks.py --src src/ \\
        --output audit/silent_fallbacks_2026-05.md

The four classification buckets follow the cleanup plan:

* ``DEFECT`` — hides a failure the caller would want to see.
* ``GRACEFUL-DEGRADATION`` — documented best-effort fallback.
* ``GUARD`` — boundary validation; raising is correct.
* ``TEST-ONLY`` — fixture cleanup in tests; acceptable.

The bucket is a *suggestion* — the script does syntactic analysis only,
and humans must classify ambiguous cases. Where the bucket is not
obvious, the script also emits a one-line reviewer note.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Subdirectories of ``src/`` the report breaks out separately.
KNOWN_SUBPACKAGES = (
    "trellis",
    "trellis_cli",
    "trellis_api",
    "trellis_sdk",
    "trellis_wire",
    "trellis_workers",
)

# Empty-return literals the heuristic treats as silent fallback values.
_EMPTY_RETURN_REPRS = frozenset(
    {"[]", "None", "{}", "0", "0.0", "False", '""', "''", "()"}
)

# Bucket labels.
BUCKET_DEFECT = "DEFECT"
BUCKET_GRACEFUL = "GRACEFUL-DEGRADATION"
BUCKET_GUARD = "GUARD"
BUCKET_TEST_ONLY = "TEST-ONLY"
BUCKET_NOT_SILENT = "NOT-SILENT"  # filtered out before report — handler re-raises

BUCKET_ORDER = (BUCKET_DEFECT, BUCKET_GRACEFUL, BUCKET_GUARD, BUCKET_TEST_ONLY)

# Catch kinds the heuristic treats as "broad".
_BROAD_CATCH = frozenset({"bare", "Exception", "BaseException"})

# Logging-call attribute names recognised by ``_is_logging_call``.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)

# Function-name prefixes that signal an intentional swallow on failure.
_TRY_MAYBE_PREFIXES = ("try_", "maybe_", "_try_", "_maybe_")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One ``except`` clause with classification metadata."""

    relpath: str
    line: int
    except_text: str
    handler_excerpt: tuple[str, ...]
    handler_pattern: str  # pass | return-empty | log-return-empty | log-only | other
    catch_kind: str  # bare | Exception | BaseException | specific
    function_name: str | None
    bucket: str
    note: str


# ---------------------------------------------------------------------------
# AST walker
# ---------------------------------------------------------------------------


class ExceptVisitor(ast.NodeVisitor):
    """Visits ``ExceptHandler`` nodes and records the enclosing function."""

    def __init__(self) -> None:
        self.handlers: list[tuple[ast.ExceptHandler, str | None]] = []
        self._function_stack: list[str] = []

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    visit_FunctionDef = _visit_function  # noqa: N815
    visit_AsyncFunctionDef = _visit_function  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        enclosing = self._function_stack[-1] if self._function_stack else None
        self.handlers.append((node, enclosing))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _catch_kind(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "bare"
    text = ast.unparse(node.type)
    if text in {"Exception", "BaseException"}:
        return text
    return "specific"


def _is_empty_return(stmt: ast.stmt) -> bool:
    """True if ``stmt`` is a ``return`` of an empty/falsy literal."""
    if not isinstance(stmt, ast.Return):
        return False
    if stmt.value is None:
        return True
    return ast.unparse(stmt.value).strip() in _EMPTY_RETURN_REPRS


def _handler_reraises(body: list[ast.stmt]) -> bool:
    """True if any statement (recursively) is a ``raise``."""
    return any(
        isinstance(sub, ast.Raise) for stmt in body for sub in ast.walk(stmt)
    )


def _is_logging_call(stmt: ast.stmt) -> bool:
    """Heuristic: True if stmt is a logging-method or print expression-stmt."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    func = stmt.value.func
    if isinstance(func, ast.Attribute):
        return func.attr.lower() in _LOG_METHODS
    return isinstance(func, ast.Name) and func.id == "print"


def _classify_handler_body(body: list[ast.stmt]) -> str:
    """Return one of: pass, return-empty, log-return-empty, log-only, other."""
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "pass"
    if len(body) == 1 and _is_empty_return(body[0]):
        return "return-empty"
    if (
        len(body) >= 2
        and _is_empty_return(body[-1])
        and all(_is_logging_call(s) for s in body[:-1])
    ):
        return "log-return-empty"
    if body and all(_is_logging_call(s) for s in body):
        return "log-only"
    return "other"


def _looks_like_try_or_maybe(function_name: str | None) -> bool:
    return function_name is not None and function_name.startswith(_TRY_MAYBE_PREFIXES)


def _bucket_for(
    *,
    relpath: str,
    handler_pattern: str,
    catch_kind: str,
    body: list[ast.stmt],
    function_name: str | None,
) -> tuple[str, str]:
    """Decide a bucket suggestion plus reviewer note.

    Returns (bucket, note). ``bucket`` may be ``BUCKET_NOT_SILENT`` to
    indicate the handler should be filtered out of the report (it
    re-raises somewhere).
    """
    if _handler_reraises(body):
        return BUCKET_NOT_SILENT, ""

    # Tests get their own bucket regardless of pattern.
    if "tests/" in relpath or relpath.startswith("tests"):
        return BUCKET_TEST_ONLY, ""

    broad = catch_kind in _BROAD_CATCH
    try_maybe = _looks_like_try_or_maybe(function_name)
    broad_prefix = f"broad catch ({catch_kind})" if broad else ""

    # The original behaviour: only the return-empty/log-return-empty DEFECT
    # branch appends its default to the broad-catch prefix. Every other
    # branch uses the prefix when present and the default otherwise.
    def fallback(default: str) -> str:
        return broad_prefix or default

    if handler_pattern == "pass":
        if try_maybe:
            return BUCKET_GRACEFUL, fallback(
                "function name suggests intentional swallow"
            )
        return BUCKET_DEFECT, fallback("silent pass — caller has no signal.")

    if handler_pattern in {"return-empty", "log-return-empty"}:
        if try_maybe:
            return BUCKET_GRACEFUL, fallback(
                "function named try_/maybe_ — empty return is the conventional signal."
            )
        default = (
            "logs warning then returns empty — classic silent-fallback shape"
            if handler_pattern == "log-return-empty"
            else "returns empty without logging — caller has no signal"
        )
        return BUCKET_DEFECT, f"{broad_prefix}; {default}" if broad_prefix else default

    if handler_pattern == "log-only":
        if broad:
            return BUCKET_DEFECT, fallback(
                "logs and swallows — control flow continues with possibly invalid state"
            )
        return BUCKET_GRACEFUL, (
            "logs and continues — likely best-effort; verify intent."
        )

    # handler_pattern == "other": non-trivial body.
    if broad:
        return BUCKET_DEFECT, fallback(
            "broad catch with non-trivial body —"
            " verify the exception is not silently masked"
        )
    return BUCKET_GUARD, (
        "specific catch with non-trivial body —"
        " likely a guard; review on a case-by-case basis."
    )


# ---------------------------------------------------------------------------
# Known-critical-site rules (cleanup plan §4)
# ---------------------------------------------------------------------------


def _classify_known_critical(finding: Finding) -> str | None:
    """If this finding is one of the §4 known-critical sites, return a label."""
    rp = finding.relpath
    fn = finding.function_name or ""

    if rp == "trellis/extract/llm.py" and fn in {
        "_parse_candidates",
        "_try_json_loads",
        "_parse_json_tolerant",
    }:
        return "§4.1 LLMExtractor parse swallow"
    if rp == "trellis_workers/learning/miner.py" and fn == "_parse_candidates":
        return "§4.2 worker miner parse swallow"
    if rp.startswith("trellis/llm/providers/"):
        return "§4.5 embedder/LLM provider swallow"
    if (
        rp.startswith("trellis/mutate/policies/")
        or rp == "trellis/mutate/policy_gate.py"
    ):
        return "§4.6 policy gate deny-on-error"
    if rp == "trellis/mutate/executor.py" and finding.catch_kind in _BROAD_CATCH:
        return "§4.7 MutationExecutor broad-catch / event-log swallow"
    return None


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def _extract_handler_excerpt(
    source_lines: list[str],
    node: ast.ExceptHandler,
    max_lines: int = 4,
) -> tuple[str, ...]:
    """Return the handler body excerpt (up to ``max_lines`` lines)."""
    if not node.body:
        return ()
    start = node.body[0].lineno
    end = min(node.body[-1].end_lineno or start, start + max_lines - 1)
    return tuple(source_lines[i - 1] for i in range(start, end + 1))


def _except_clause_text(node: ast.ExceptHandler, source_lines: list[str]) -> str:
    """Render the ``except`` clause line text (without the body)."""
    idx = node.lineno - 1
    if 0 <= idx < len(source_lines):
        return source_lines[idx].strip()
    # Reconstruct from AST as fallback (shouldn't happen on valid inputs).
    prefix = "except" if node.type is None else f"except {ast.unparse(node.type)}"
    if node.name:
        prefix = f"{prefix} as {node.name}"
    return prefix + ":"


def _relpath_of(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def scan_file(path: Path, root: Path) -> list[Finding]:
    """Parse ``path`` and return one ``Finding`` per silent ``except`` clause.

    Parse and read errors raise with the filename in the message — silent
    skipping would violate the POC directive this audit enforces.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"audit_silent_fallbacks: cannot read {path}: {exc!r}"
        ) from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(
            f"audit_silent_fallbacks: parse failed for {path}: {exc!r}"
        ) from exc

    source_lines = source.splitlines()
    relpath = _relpath_of(path, root)

    visitor = ExceptVisitor()
    visitor.visit(tree)

    findings: list[Finding] = []
    for handler, fn_name in visitor.handlers:
        catch_kind = _catch_kind(handler)
        pattern = _classify_handler_body(handler.body)
        bucket, note = _bucket_for(
            relpath=relpath,
            handler_pattern=pattern,
            catch_kind=catch_kind,
            body=handler.body,
            function_name=fn_name,
        )
        if bucket == BUCKET_NOT_SILENT:
            continue
        findings.append(
            Finding(
                relpath=relpath,
                line=handler.lineno,
                except_text=_except_clause_text(handler, source_lines),
                handler_excerpt=_extract_handler_excerpt(source_lines, handler),
                handler_pattern=pattern,
                catch_kind=catch_kind,
                function_name=fn_name,
                bucket=bucket,
                note=note,
            )
        )
    return findings


def iter_python_files(root: Path) -> Iterable[Path]:
    """Yield every ``*.py`` file under ``root`` in deterministic order."""
    yield from sorted(root.rglob("*.py"))


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _subpackage_for(relpath: str) -> str:
    head, _, _ = relpath.partition("/")
    return head or "<root>"


def _bucket_counts(findings: Iterable[Finding]) -> Counter[str]:
    return Counter(f.bucket for f in findings)


def _ordered_subpackages(present: set[str]) -> list[str]:
    """KNOWN_SUBPACKAGES first (in declared order), then any extras sorted."""
    known = [s for s in KNOWN_SUBPACKAGES if s in present]
    extras = sorted(present - set(known))
    return known + extras


def render_report(findings: list[Finding], *, src_root_label: str) -> str:
    """Render the full Markdown report deterministically."""
    findings_sorted = sorted(findings, key=lambda f: (f.relpath, f.line))
    total = len(findings_sorted)
    overall = _bucket_counts(findings_sorted)

    by_subpackage: dict[str, list[Finding]] = defaultdict(list)
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings_sorted:
        by_subpackage[_subpackage_for(f.relpath)].append(f)
        by_file[f.relpath].append(f)

    known_critical = sorted(
        (
            (label, f)
            for f in findings_sorted
            if (label := _classify_known_critical(f)) is not None
        ),
        key=lambda item: (item[0], item[1].relpath, item[1].line),
    )

    out: list[str] = []
    emit = out.append

    def blank() -> None:
        out.append("")

    emit("# Silent-fallback audit — 2026-05")
    blank()
    emit(
        "Generated by `scripts/audit_silent_fallbacks.py`. Re-run to refresh; "
        "the script is deterministic so diffs are meaningful."
    )
    blank()
    emit(f"- **Source root scanned:** `{src_root_label}`")
    emit(f"- **Total candidate `except` sites flagged:** **{total}**")
    blank()

    # Overall bucket summary
    emit("## Bucket totals")
    blank()
    for bucket in BUCKET_ORDER:
        count = overall.get(bucket, 0)
        pct = (100.0 * count / total) if total else 0.0
        emit(f"- **{bucket}**: {count} ({pct:.1f}%)")
    blank()
    emit(
        "> The script flags every `except` clause that doesn't `raise` somewhere in "
        "its body. Bucket assignment is heuristic (AST shape + catch breadth + "
        "function name) and must be confirmed by a human before any code change."
    )
    blank()

    # Per-subpackage breakdown
    emit("## Per-directory breakdown")
    blank()
    emit("| Subpackage | Total | DEFECT | GRACEFUL | GUARD | TEST-ONLY |")
    emit("|---|---:|---:|---:|---:|---:|")
    for s in _ordered_subpackages(set(by_subpackage)):
        sub_findings = by_subpackage[s]
        counts = _bucket_counts(sub_findings)
        cells = " | ".join(str(counts.get(b, 0)) for b in BUCKET_ORDER)
        emit(f"| `{s}/` | {len(sub_findings)} | {cells} |")
    blank()

    # Known-critical highlights
    emit("## DEFECT — known critical")
    blank()
    if not known_critical:
        emit(
            "_No known-critical sites detected._ (Expected sites: LLMExtractor "
            "parse swallow, worker miner parse swallow, embedder/LLM provider "
            "swallow, policy-gate deny-on-error, MutationExecutor event-log "
            "swallow. If the audit returns zero here, verify the scanner "
            "patterns are still correct.)"
        )
    else:
        for label, f in known_critical:
            emit(
                f"- **{label}** — `{f.relpath}:{f.line}` in "
                f"`{f.function_name or '<module>'}` — `{f.except_text}` "
                f"(pattern=`{f.handler_pattern}`, catch=`{f.catch_kind}`)"
            )
    blank()

    # Per-file detailed listing
    emit("## Per-file findings")
    blank()
    if not findings_sorted:
        emit("_No findings._")
        blank()

    current_subpackage: str | None = None
    for relpath in sorted(by_file):
        sub = _subpackage_for(relpath)
        if sub != current_subpackage:
            emit(f"### `{sub}/`")
            blank()
            current_subpackage = sub

        file_findings = by_file[relpath]
        counts = _bucket_counts(file_findings)
        summary = ", ".join(
            f"{b}={counts[b]}" for b in BUCKET_ORDER if counts.get(b, 0)
        )
        emit(f"#### `{relpath}` ({len(file_findings)} sites — {summary})")
        blank()

        for f in file_findings:
            emit(
                f"- **line {f.line}** — bucket=**{f.bucket}**, "
                f"pattern=`{f.handler_pattern}`, catch=`{f.catch_kind}`, "
                f"fn=`{f.function_name or '<module>'}`"
            )
            blank()
            emit("  ```python")
            emit(f"  {f.except_text}")
            for excerpt_line in f.handler_excerpt:
                emit(f"  {excerpt_line}")
            emit("  ```")
            if f.note:
                blank()
                emit(f"  _Reviewer note:_ {f.note}")
            blank()

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit silent-fallback patterns in a source tree. Read-only; "
            "produces a deterministic Markdown report."
        )
    )
    parser.add_argument(
        "--src", type=Path, required=True, help="Root directory to scan (e.g. src/)."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Markdown output path."
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary to stdout, do not write report. Useful for spot checks.",
    )
    return parser.parse_args(argv)


def _render_root_label(src_root: Path) -> str:
    """Render src_root relative to cwd when possible, for portable reports."""
    try:
        return str(src_root.resolve().relative_to(Path.cwd())).replace("\\", "/")
    except ValueError:
        return str(src_root).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    src_root: Path = args.src.resolve()
    if not src_root.is_dir():
        raise SystemExit(
            f"audit_silent_fallbacks: --src is not a directory: {src_root}"
        )

    all_findings: list[Finding] = []
    for py_path in iter_python_files(src_root):
        all_findings.extend(scan_file(py_path, src_root))

    counter = _bucket_counts(all_findings)
    print(
        f"[audit] scanned {src_root} — {len(all_findings)} silent-fallback candidates"
    )
    for bucket in BUCKET_ORDER:
        print(f"[audit]   {bucket}: {counter.get(bucket, 0)}")

    if args.summary_only:
        return 0

    output: Path = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(all_findings, src_root_label=_render_root_label(src_root))
    output.write_text(report, encoding="utf-8")
    print(f"[audit] wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
