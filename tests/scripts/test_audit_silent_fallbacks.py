"""Tests for ``scripts/audit_silent_fallbacks.py``.

Followup 2 (helper-call-chain awareness): the audit script now recognises
helper-call indirection as a "raise" instead of flagging it as a DEFECT.
This module covers the recognition rules plus the ``--literal-only`` flag
that restores the legacy behavior.

The scripts/ directory does not have an ``__init__.py``, so the audit
script is loaded via ``importlib.util`` and exposed as the ``audit``
fixture. Each test writes a small synthetic ``.py`` file into ``tmp_path``
and runs ``scan_file`` against it — there is no dependence on the real
repo source tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_silent_fallbacks.py"


def _load_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_silent_fallbacks", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        msg = f"could not load spec for {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_silent_fallbacks"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> ModuleType:
    return _load_audit_module()


def _write_file(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.write_text(source, encoding="utf-8")
    return path


def _bucket_set(findings):
    return {(f.relpath, f.line, f.bucket) for f in findings}


def _defect_lines(findings):
    return sorted(f.line for f in findings if f.bucket == "DEFECT")


# ---------------------------------------------------------------------------
# Pattern recognition — helper-aware mode (default)
# ---------------------------------------------------------------------------


def test_literal_raise_is_recognised(audit, tmp_path):
    """A direct ``raise`` in the handler body filters the finding out."""
    source = (
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    path = _write_file(tmp_path, "literal.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_underscore_raise_helper_convention_is_recognised(audit, tmp_path):
    """A call to ``_raise_*`` is treated as a raise (regex convention)."""
    source = (
        "from typing import NoReturn\n"
        "\n"
        "def _raise_invalid(msg: str) -> NoReturn:\n"
        "    raise ValueError(msg)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError as e:\n"
        "        _raise_invalid(str(e))\n"
    )
    path = _write_file(tmp_path, "helper_convention.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_raise_prefix_without_underscore_is_recognised(audit, tmp_path):
    """``raise_foo`` (no leading underscore) also matches the convention."""
    source = (
        "def raise_internal(msg: str):\n"
        "    raise RuntimeError(msg)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except RuntimeError as e:\n"
        "        raise_internal(str(e))\n"
    )
    path = _write_file(tmp_path, "raise_prefix.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_intra_module_function_ending_in_raise_is_recognised(audit, tmp_path):
    """A helper that doesn't match the regex still wins via AST inspection.

    The function name ``boom`` doesn't match ``_raise_*`` but its body
    provably ends in ``raise`` — the pre-pass picks it up.
    """
    source = (
        "def boom(msg: str):\n"
        "    raise ValueError(msg)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError as e:\n"
        "        boom(str(e))\n"
    )
    path = _write_file(tmp_path, "intra_module.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_noreturn_annotation_is_recognised(audit, tmp_path):
    """A helper annotated ``-> NoReturn`` is recognised even if body shape varies.

    The helper here has a conditional branch — ``_function_provably_raises``
    is conservative and would not flag it, but the ``NoReturn`` annotation
    promises termination, so the pre-pass records it.
    """
    source = (
        "from typing import NoReturn\n"
        "\n"
        "def abort_now(code: int) -> NoReturn:\n"
        "    if code < 0:\n"
        "        raise ValueError(code)\n"
        "    raise SystemExit(code)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError as e:\n"
        "        abort_now(1)\n"
    )
    path = _write_file(tmp_path, "noreturn.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_typing_noreturn_qualified_annotation_is_recognised(audit, tmp_path):
    """``typing.NoReturn`` (qualified) is also accepted as a promise to abort."""
    source = (
        "import typing\n"
        "\n"
        "def abort_now(code: int) -> typing.NoReturn:\n"
        "    raise SystemExit(code)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        abort_now(2)\n"
    )
    path = _write_file(tmp_path, "noreturn_qualified.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_sys_exit_is_recognised_as_stack_abort(audit, tmp_path):
    """``sys.exit(N)`` aborts the call stack → not silent."""
    source = (
        "import sys\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        sys.exit(1)\n"
    )
    path = _write_file(tmp_path, "sys_exit.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_typer_exit_is_recognised_as_stack_abort(audit, tmp_path):
    """``typer.Exit(code=N)`` aborts the call stack → not silent."""
    source = (
        "import typer\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        typer.Exit(code=2)\n"
    )
    path = _write_file(tmp_path, "typer_exit.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_click_abort_is_recognised_as_stack_abort(audit, tmp_path):
    """``click.Abort()`` aborts the call stack → not silent."""
    source = (
        "import click\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        click.Abort()\n"
    )
    path = _write_file(tmp_path, "click_abort.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


def test_cross_module_raise_helper_is_recognised_by_convention(audit, tmp_path):
    """``mod.raise_internal(...)`` matches the regex on the trailing segment.

    The script deliberately does NOT walk imports — the convention regex
    catches cross-module helpers without cross-file resolution. This test
    locks that behavior in.
    """
    source = (
        "from .errors import _raise_internal\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError as e:\n"
        "        _raise_internal(str(e))\n"
    )
    path = _write_file(tmp_path, "cross_module.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# Silent except still flagged
# ---------------------------------------------------------------------------


def test_truly_silent_except_is_flagged_as_defect(audit, tmp_path):
    """No raise, no helper, no exit → DEFECT."""
    source = (
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        return []\n"
    )
    path = _write_file(tmp_path, "silent.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert len(findings) == 1
    assert findings[0].bucket == "DEFECT"
    assert findings[0].handler_pattern == "return-empty"


def test_log_only_broad_catch_is_flagged_as_defect(audit, tmp_path):
    """log-only + broad catch is the classic silent-fallback shape."""
    source = (
        "import logging\n"
        "\n"
        "log = logging.getLogger(__name__)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        log.warning('failed')\n"
    )
    path = _write_file(tmp_path, "log_only.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert len(findings) == 1
    assert findings[0].bucket == "DEFECT"


def test_helper_call_that_does_not_match_is_still_flagged(audit, tmp_path):
    """A function call to a non-raising helper does NOT mark as raise."""
    source = (
        "def log_and_continue(msg: str) -> None:\n"
        "    print(msg)\n"
        "\n"
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception as e:\n"
        "        log_and_continue(str(e))\n"
    )
    path = _write_file(tmp_path, "non_raising_helper.py", source)
    findings = audit.scan_file(path, tmp_path)
    assert len(findings) == 1
    assert findings[0].bucket == "DEFECT"


# ---------------------------------------------------------------------------
# Mixed-content file
# ---------------------------------------------------------------------------


def test_mixed_file_separates_recognised_and_silent_excepts(audit, tmp_path):
    """A single file with both helper-routed and silent excepts."""
    source = (
        "from typing import NoReturn\n"
        "import sys\n"
        "\n"
        "def _raise_bad(msg: str) -> NoReturn:\n"
        "    raise ValueError(msg)\n"
        "\n"
        "def good_a():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError as e:\n"
        "        _raise_bad(str(e))\n"
        "\n"
        "def good_b():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        sys.exit(1)\n"
        "\n"
        "def bad_a():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        return None\n"
        "\n"
        "def bad_b():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    path = _write_file(tmp_path, "mixed.py", source)
    findings = audit.scan_file(path, tmp_path)
    # Two silent excepts remain; the two helper-routed ones are filtered out.
    assert len(findings) == 2
    assert {f.function_name for f in findings} == {"bad_a", "bad_b"}
    assert all(f.bucket == "DEFECT" for f in findings)


# ---------------------------------------------------------------------------
# --literal-only flag — legacy behavior
# ---------------------------------------------------------------------------


def test_literal_only_mode_flags_helper_calls_as_findings(audit, tmp_path):
    """``literal_only=True`` restores the legacy behavior.

    In helper-aware mode the helper-routed and exit-routed excepts are
    filtered out entirely (BUCKET_NOT_SILENT). In legacy mode they fall
    through to the regular classifier, which produces findings — the
    exact bucket depends on catch breadth and body shape (a broad ``pass``
    catch would be DEFECT, a specific ``other``-pattern catch would be
    GUARD). The point of this test is that the helper sites *show up*
    in literal-only output but *not* in helper-aware output.
    """
    source = (
        "from typing import NoReturn\n"
        "import sys\n"
        "\n"
        "def _raise_bad(msg: str) -> NoReturn:\n"
        "    raise ValueError(msg)\n"
        "\n"
        "def helper_routed():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"  # broad catch → DEFECT in legacy mode
        "        _raise_bad('boom')\n"
        "\n"
        "def exit_routed():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"  # broad catch → DEFECT in legacy mode
        "        sys.exit(1)\n"
    )
    path = _write_file(tmp_path, "legacy.py", source)

    helper_aware = audit.scan_file(path, tmp_path, literal_only=False)
    literal_only = audit.scan_file(path, tmp_path, literal_only=True)

    # Helper-aware mode filters both out.
    assert helper_aware == []
    # Legacy mode surfaces both as findings.
    assert len(literal_only) == 2
    # Broad-catch + non-trivial body → DEFECT under the existing classifier.
    assert all(f.bucket == "DEFECT" for f in literal_only)


def test_literal_only_mode_still_recognises_literal_raise(audit, tmp_path):
    """Even in legacy mode, an actual ``raise`` keeps the finding out."""
    source = (
        "def do_work():\n"
        "    try:\n"
        "        risky()\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    path = _write_file(tmp_path, "legacy_raise.py", source)
    findings = audit.scan_file(path, tmp_path, literal_only=True)
    assert findings == []


# ---------------------------------------------------------------------------
# Lower-level helpers — direct exercise
# ---------------------------------------------------------------------------


def test_raise_helper_name_regex_matches_expected_forms(audit):
    re = audit._RAISE_HELPER_NAME_RE
    assert re.match("_raise_invalid_params")
    assert re.match("raise_internal")
    assert re.match("_raise_x")
    assert not re.match("raised")
    assert not re.match("raisedex")
    assert not re.match("Raise_x")  # case-sensitive
    assert not re.match("_raise")  # requires trailing word


def test_dotted_name_handles_attribute_chains(audit):
    import ast as _ast

    tree = _ast.parse("a.b.c", mode="eval")
    assert audit._dotted_name(tree.body) == "a.b.c"
    tree = _ast.parse("foo", mode="eval")
    assert audit._dotted_name(tree.body) == "foo"
    tree = _ast.parse("f().x", mode="eval")
    assert audit._dotted_name(tree.body) is None


def test_call_is_stack_abort_recognises_known_aborts(audit):
    import ast as _ast

    def _call(expr: str) -> _ast.Call:
        node = _ast.parse(expr, mode="eval").body
        assert isinstance(node, _ast.Call)
        return node

    assert audit._call_is_stack_abort(_call("sys.exit(1)"))
    assert audit._call_is_stack_abort(_call("typer.Exit(code=2)"))
    assert audit._call_is_stack_abort(_call("click.Abort()"))
    assert audit._call_is_stack_abort(_call("os._exit(0)"))
    # Bare ``exit(1)`` is deliberately NOT matched — too overloaded.
    assert not audit._call_is_stack_abort(_call("exit(1)"))
    # A non-abort call does not match.
    assert not audit._call_is_stack_abort(_call("foo.bar(1)"))
