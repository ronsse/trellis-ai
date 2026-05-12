# ruff: noqa: E501, EM102, ERA001, PERF401, PLR0911, PLR0912, PLR0915, PLR2004
# ruff: noqa: SIM103, SIM108, TRY003, ARG001
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
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One ``except`` clause with classification metadata."""

    path: Path
    relpath: str
    line: int
    end_line: int
    except_text: str
    handler_excerpt: list[str]
    handler_pattern: (
        str  # one of: pass, return-empty, log-return-empty, log-only, other
    )
    catch_kind: str  # one of: bare, Exception, BaseException, specific
    function_name: str | None
    bucket: str
    note: str

    def is_silent(self) -> bool:
        return self.bucket != BUCKET_NOT_SILENT


@dataclass
class FileReport:
    """All findings for a single file (in source order)."""

    relpath: str
    findings: list[Finding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


class ExceptVisitor(ast.NodeVisitor):
    """Visits ``ExceptHandler`` nodes and records the enclosing function."""

    def __init__(self) -> None:
        self.handlers: list[tuple[ast.ExceptHandler, str | None]] = []
        self._function_stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._function_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._function_stack.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        enclosing = self._function_stack[-1] if self._function_stack else None
        self.handlers.append((node, enclosing))
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _format_except_clause(node: ast.ExceptHandler) -> str:
    """Render the ``except`` clause line text (without the body)."""
    if node.type is None:
        prefix = "except"
    else:
        prefix = "except " + ast.unparse(node.type)
    if node.name:
        prefix = f"{prefix} as {node.name}"
    return prefix + ":"


def _catch_kind(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "bare"
    text = ast.unparse(node.type)
    if text == "Exception":
        return "Exception"
    if text == "BaseException":
        return "BaseException"
    return "specific"


def _is_empty_return(stmt: ast.stmt) -> bool:
    """True if ``stmt`` is a ``return`` of an empty/falsy literal."""
    if not isinstance(stmt, ast.Return):
        return False
    if stmt.value is None:
        return True
    try:
        rendered = ast.unparse(stmt.value).strip()
    except (
        Exception
    ) as exc:  # pragma: no cover — ast.unparse raises only on malformed AST
        raise RuntimeError(f"ast.unparse failed on return value: {exc!r}") from exc
    return rendered in _EMPTY_RETURN_REPRS


def _handler_reraises(body: list[ast.stmt]) -> bool:
    """True if any statement (recursively) is a bare ``raise``."""
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
    return False


def _is_logging_call(stmt: ast.stmt) -> bool:
    """Heuristic: True if stmt is an expression-statement calling something
    like ``logger.warning(...)`` / ``log.error(...)`` / ``print(...)``."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute):
        name = func.attr.lower()
        return name in {
            "debug",
            "info",
            "warning",
            "warn",
            "error",
            "exception",
            "critical",
        }
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    return False


def _classify_handler_body(body: list[ast.stmt]) -> str:
    """Return one of: pass, return-empty, log-return-empty, log-only, other.

    ``pass``: body is exactly ``[Pass]``.
    ``return-empty``: body returns an empty sentinel with no other action.
    ``log-return-empty``: body is one logging call followed by an empty return.
    ``log-only``: body is only logging calls (no return).
    ``other``: anything else.
    """
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "pass"
    if len(body) == 1 and _is_empty_return(body[0]):
        return "return-empty"
    if len(body) == 2 and _is_logging_call(body[0]) and _is_empty_return(body[1]):
        return "log-return-empty"
    if body and all(_is_logging_call(s) for s in body):
        return "log-only"
    # Three+ statements that end with empty return preceded only by logging
    # also counts as log-return-empty (e.g. logger.warning(...); logger.debug(...); return None).
    if (
        len(body) >= 2
        and _is_empty_return(body[-1])
        and all(_is_logging_call(s) for s in body[:-1])
    ):
        return "log-return-empty"
    return "other"


def _looks_like_try_or_maybe(function_name: str | None) -> bool:
    if not function_name:
        return False
    return function_name.startswith(("try_", "maybe_", "_try_", "_maybe_"))


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
    # First: handler that re-raises is not silent. Skip from report.
    if _handler_reraises(body):
        return BUCKET_NOT_SILENT, ""

    # Tests get their own bucket regardless of pattern.
    if "tests/" in relpath.replace("\\", "/") or relpath.startswith("tests"):
        return BUCKET_TEST_ONLY, ""

    note_bits: list[str] = []

    # Catch-too-broad is always at least a reviewer concern.
    if catch_kind in {"bare", "Exception", "BaseException"}:
        note_bits.append(f"broad catch ({catch_kind})")

    # Function naming hints intent ("try_X" / "maybe_Y" are by convention
    # allowed to swallow on failure).
    try_maybe = _looks_like_try_or_maybe(function_name)

    if handler_pattern == "pass":
        # Bare pass with no logging is almost always a defect, regardless
        # of catch breadth.
        bucket = BUCKET_DEFECT
        if try_maybe:
            note_bits.append("function name suggests intentional swallow")
            bucket = BUCKET_GRACEFUL
        return bucket, "; ".join(note_bits) or "silent pass — caller has no signal."

    if handler_pattern in {"return-empty", "log-return-empty"}:
        if try_maybe:
            return (
                BUCKET_GRACEFUL,
                "; ".join(note_bits)
                or "function named try_/maybe_ — empty return is the conventional signal.",
            )
        bucket = BUCKET_DEFECT
        if handler_pattern == "log-return-empty":
            note_bits.append(
                "logs warning then returns empty — classic silent-fallback shape"
            )
        else:
            note_bits.append("returns empty without logging — caller has no signal")
        return bucket, "; ".join(note_bits)

    if handler_pattern == "log-only":
        # Logging only, no return — control flow continues. If the catch
        # is broad, that's suspicious; if it's specific, this is often a
        # GRACEFUL-DEGRADATION shape (best-effort cache write, etc.).
        if catch_kind in {"bare", "Exception", "BaseException"}:
            return (
                BUCKET_DEFECT,
                "; ".join(note_bits)
                or "logs and swallows — control flow continues with possibly invalid state",
            )
        return (
            BUCKET_GRACEFUL,
            "; ".join(note_bits)
            or "logs and continues — likely best-effort; verify intent.",
        )

    # handler_pattern == "other"
    # The body does something non-trivial. Could still be silent if it
    # masks the exception, but we lack semantic context. Mark GUARD when
    # the catch is specific (most defensible) and DEFECT-with-note when
    # the catch is broad.
    if catch_kind in {"bare", "Exception", "BaseException"}:
        return (
            BUCKET_DEFECT,
            "; ".join(note_bits)
            or "broad catch with non-trivial body — verify the exception is not silently masked",
        )
    return (
        BUCKET_GUARD,
        "; ".join(note_bits)
        or "specific catch with non-trivial body — likely a guard; review on a case-by-case basis.",
    )


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_handler_excerpt(
    source_lines: list[str],
    node: ast.ExceptHandler,
    max_lines: int = 4,
) -> list[str]:
    """Return the handler body excerpt (up to ``max_lines`` lines)."""
    if not node.body:
        return []
    start = node.body[0].lineno
    # The end_lineno of the handler is the last line of the last stmt.
    end = node.body[-1].end_lineno or start
    end = min(end, start + max_lines - 1)
    return [
        source_lines[i - 1]
        for i in range(start, end + 1)
        if 1 <= i <= len(source_lines)
    ]


def scan_file(path: Path, root: Path) -> list[Finding]:
    """Parse ``path`` and return one ``Finding`` per silent ``except`` clause.

    Parse errors raise with the filename in the message — silent skipping
    would violate the POC directive this audit enforces.
    """
    try:
        source = _read_source(path)
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

    visitor = ExceptVisitor()
    visitor.visit(tree)

    findings: list[Finding] = []
    for handler, fn_name in visitor.handlers:
        catch_kind = _catch_kind(handler)
        pattern = _classify_handler_body(handler.body)
        excerpt = _extract_handler_excerpt(source_lines, handler)
        except_line_idx = handler.lineno - 1
        except_text = (
            source_lines[except_line_idx].strip()
            if 0 <= except_line_idx < len(source_lines)
            else _format_except_clause(handler)
        )

        try:
            relpath = str(path.relative_to(root))
        except ValueError:
            relpath = str(path)
        relpath = relpath.replace("\\", "/")

        bucket, note = _bucket_for(
            relpath=relpath,
            handler_pattern=pattern,
            catch_kind=catch_kind,
            body=handler.body,
            function_name=fn_name,
        )

        if bucket == BUCKET_NOT_SILENT:
            continue

        end_line = handler.end_lineno or handler.lineno
        findings.append(
            Finding(
                path=path,
                relpath=relpath,
                line=handler.lineno,
                end_line=end_line,
                except_text=except_text,
                handler_excerpt=excerpt,
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
# Subpackage / known-DEFECT detection
# ---------------------------------------------------------------------------


def _subpackage_for(relpath: str) -> str:
    parts = relpath.split("/")
    if not parts:
        return "<root>"
    return parts[0]


def _classify_known_critical(finding: Finding) -> str | None:
    """If this finding is one of the §4 known-critical sites, return a label."""
    rp = finding.relpath
    fn = finding.function_name or ""

    # §4.1 — LLMExtractor _parse_candidates (actually lives in _try_json_loads)
    if rp == "trellis/extract/llm.py" and fn in {
        "_parse_candidates",
        "_try_json_loads",
        "_parse_json_tolerant",
    }:
        return "§4.1 LLMExtractor parse swallow"

    # §4.2 — Worker miner _parse_candidates
    if rp == "trellis_workers/learning/miner.py" and fn == "_parse_candidates":
        return "§4.2 worker miner parse swallow"

    # §4.5 — embedder / LLM provider error swallowing
    if rp.startswith("trellis/llm/providers/"):
        return "§4.5 embedder/LLM provider swallow"

    # §4.6 — policy gate "deny on error"
    if (
        rp.startswith("trellis/mutate/policies/")
        or rp == "trellis/mutate/policy_gate.py"
    ):
        return "§4.6 policy gate deny-on-error"

    # §4.7 — EventLog write swallowing / broad-catch in MutationExecutor
    if rp == "trellis/mutate/executor.py" and finding.catch_kind in {
        "bare",
        "Exception",
        "BaseException",
    }:
        return "§4.7 MutationExecutor broad-catch / event-log swallow"

    return None


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _bucket_counter(findings: list[Finding]) -> Counter[str]:
    return Counter(f.bucket for f in findings)


def _format_count_line(label: str, count: int, total: int) -> str:
    pct = (100.0 * count / total) if total else 0.0
    return f"- **{label}**: {count} ({pct:.1f}%)"


def render_report(
    findings: list[Finding],
    *,
    src_root: Path,
    generated_for: str,
) -> str:
    """Render the full Markdown report deterministically."""
    findings_sorted = sorted(findings, key=lambda f: (f.relpath, f.line))
    total = len(findings_sorted)
    overall_counter = _bucket_counter(findings_sorted)

    by_subpackage: dict[str, list[Finding]] = defaultdict(list)
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings_sorted:
        by_subpackage[_subpackage_for(f.relpath)].append(f)
        by_file[f.relpath].append(f)

    known_critical: list[tuple[str, Finding]] = []
    for f in findings_sorted:
        label = _classify_known_critical(f)
        if label is not None:
            known_critical.append((label, f))
    # Stable sort by section label so re-runs produce identical reports.
    known_critical.sort(key=lambda item: (item[0], item[1].relpath, item[1].line))

    lines: list[str] = []
    lines.append("# Silent-fallback audit — 2026-05")
    lines.append("")
    lines.append(
        "Generated by `scripts/audit_silent_fallbacks.py`. Re-run to refresh; "
        "the script is deterministic so diffs are meaningful."
    )
    lines.append("")
    # Render the source root relative to the cwd when possible so the
    # report is portable across worktrees.
    try:
        rendered_root = str(Path(generated_for).resolve().relative_to(Path.cwd()))
    except ValueError:
        rendered_root = generated_for
    rendered_root = rendered_root.replace("\\", "/")
    lines.append(f"- **Source root scanned:** `{rendered_root}`")
    lines.append(f"- **Total candidate `except` sites flagged:** **{total}**")
    lines.append("")

    # Overall bucket summary
    lines.append("## Bucket totals")
    lines.append("")
    for bucket in (BUCKET_DEFECT, BUCKET_GRACEFUL, BUCKET_GUARD, BUCKET_TEST_ONLY):
        lines.append(_format_count_line(bucket, overall_counter.get(bucket, 0), total))
    lines.append("")
    lines.append(
        "> The script flags every `except` clause that doesn't `raise` somewhere in "
        "its body. Bucket assignment is heuristic (AST shape + catch breadth + "
        "function name) and must be confirmed by a human before any code change."
    )
    lines.append("")

    # Per-subpackage breakdown
    lines.append("## Per-directory breakdown")
    lines.append("")
    lines.append("| Subpackage | Total | DEFECT | GRACEFUL | GUARD | TEST-ONLY |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    # Stable order: KNOWN_SUBPACKAGES first, then any extras alphabetical.
    seen: set[str] = set()
    ordered_subpkgs: list[str] = []
    for s in KNOWN_SUBPACKAGES:
        if s in by_subpackage:
            ordered_subpkgs.append(s)
            seen.add(s)
    for s in sorted(by_subpackage):
        if s not in seen:
            ordered_subpkgs.append(s)
    for s in ordered_subpkgs:
        sub_findings = by_subpackage[s]
        counter = _bucket_counter(sub_findings)
        lines.append(
            f"| `{s}/` | {len(sub_findings)} | "
            f"{counter.get(BUCKET_DEFECT, 0)} | "
            f"{counter.get(BUCKET_GRACEFUL, 0)} | "
            f"{counter.get(BUCKET_GUARD, 0)} | "
            f"{counter.get(BUCKET_TEST_ONLY, 0)} |"
        )
    lines.append("")

    # Known-critical highlights
    lines.append("## DEFECT — known critical")
    lines.append("")
    if not known_critical:
        lines.append(
            "_No known-critical sites detected._ (Expected sites: LLMExtractor "
            "parse swallow, worker miner parse swallow, embedder/LLM provider "
            "swallow, policy-gate deny-on-error, MutationExecutor event-log "
            "swallow. If the audit returns zero here, verify the scanner "
            "patterns are still correct.)"
        )
    else:
        for label, f in known_critical:
            lines.append(
                f"- **{label}** — `{f.relpath}:{f.line}` in "
                f"`{f.function_name or '<module>'}` — `{f.except_text}` "
                f"(pattern=`{f.handler_pattern}`, catch=`{f.catch_kind}`)"
            )
    lines.append("")

    # Per-file detailed listing
    lines.append("## Per-file findings")
    lines.append("")
    if not findings_sorted:
        lines.append("_No findings._")
        lines.append("")

    current_subpackage: str | None = None
    for relpath in sorted(by_file):
        sub = _subpackage_for(relpath)
        if sub != current_subpackage:
            lines.append(f"### `{sub}/`")
            lines.append("")
            current_subpackage = sub

        file_findings = by_file[relpath]
        counter = _bucket_counter(file_findings)
        bucket_summary = ", ".join(
            f"{b}={counter.get(b, 0)}"
            for b in (BUCKET_DEFECT, BUCKET_GRACEFUL, BUCKET_GUARD, BUCKET_TEST_ONLY)
            if counter.get(b, 0)
        )
        lines.append(
            f"#### `{relpath}` ({len(file_findings)} sites — {bucket_summary})"
        )
        lines.append("")

        for f in file_findings:
            lines.append(
                f"- **line {f.line}** — bucket=**{f.bucket}**, "
                f"pattern=`{f.handler_pattern}`, catch=`{f.catch_kind}`, "
                f"fn=`{f.function_name or '<module>'}`"
            )
            lines.append("")
            lines.append("  ```python")
            lines.append(f"  {f.except_text}")
            for excerpt_line in f.handler_excerpt:
                lines.append(f"  {excerpt_line}")
            lines.append("  ```")
            if f.note:
                lines.append("")
                lines.append(f"  _Reviewer note:_ {f.note}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


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
        "--src",
        type=Path,
        required=True,
        help="Root directory to scan (e.g. src/).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Markdown output path.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print summary to stdout, do not write report. Useful for spot checks.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    src_root: Path = args.src.resolve()
    if not src_root.exists():
        msg = f"audit_silent_fallbacks: --src does not exist: {src_root}"
        raise SystemExit(msg)
    if not src_root.is_dir():
        msg = f"audit_silent_fallbacks: --src is not a directory: {src_root}"
        raise SystemExit(msg)

    all_findings: list[Finding] = []
    for py_path in iter_python_files(src_root):
        all_findings.extend(scan_file(py_path, src_root))

    total = len(all_findings)
    counter = _bucket_counter(all_findings)
    print(f"[audit] scanned {src_root} — {total} silent-fallback candidates")
    for bucket in (BUCKET_DEFECT, BUCKET_GRACEFUL, BUCKET_GUARD, BUCKET_TEST_ONLY):
        print(f"[audit]   {bucket}: {counter.get(bucket, 0)}")

    if args.summary_only:
        return 0

    output: Path = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(
        all_findings,
        src_root=src_root,
        generated_for=str(src_root),
    )
    output.write_text(report, encoding="utf-8")
    print(f"[audit] wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
