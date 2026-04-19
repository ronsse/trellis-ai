"""Structural boundary test: trellis_sdk must not import from trellis core.

The HTTP-only SDK story depends on this invariant.  If a contributor
accidentally slips a ``from trellis.stores ...`` into an SDK module,
client packages that depend only on ``trellis_sdk`` + ``trellis_wire``
would suddenly pull core at import time — which defeats the point of
the boundary.

Enforced the same way as ``trellis_wire``'s isolation test: AST-walk
every ``.py`` in ``src/trellis_sdk/`` and fail on any ``trellis.*``
import.  Allowlist for ``trellis_sdk``, ``trellis_wire``, and the
stdlib.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_trellis_sdk_has_no_core_imports() -> None:
    sdk_dir = Path(__file__).parent.parent.parent.parent / "src" / "trellis_sdk"
    assert sdk_dir.is_dir(), f"SDK package not found at {sdk_dir}"

    offenders: list[tuple[str, int, str]] = []
    for py_file in sdk_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root == "trellis":
                    offenders.append((str(py_file), node.lineno, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root == "trellis":
                        offenders.append((str(py_file), node.lineno, alias.name))
    assert not offenders, (
        "trellis_sdk must not import from trellis.*  Offenders:\n"
        + "\n".join(f"  {p}:{ln}  {mod}" for p, ln, mod in offenders)
    )
