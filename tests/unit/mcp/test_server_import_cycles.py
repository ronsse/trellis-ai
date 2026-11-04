"""Every module of the split MCP server imports cleanly in a fresh process.

The tool modules need the ``mcp`` instance and ``server.py`` has to import the
tool modules so they register, which is a cycle waiting to happen. The layering
(boundary -> wiring -> rejections -> app -> tools -> server) is what prevents
it, and an in-process import cannot prove it: by the time a test runs, the
whole package is already in ``sys.modules`` and any ordering is satisfied.
Each case below starts an interpreter, imports exactly one module first, and
checks that ``trellis`` came from this checkout rather than an installed copy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.integration._live_server import repo_src_pythonpath

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
_MCP = _SRC / "trellis" / "mcp"

#: Hand-read off ``src/trellis/mcp`` when the server was split: ``server`` plus
#: ``app``, ``boundary``, ``rejections``, ``wiring``, the ``tools`` package and
#: its ten modules, ``trellis_api.app`` and ``trellis_cli.main``.
_EXPECTED_IMPORT_CASES = 18

_PROBE = """
import importlib, sys
import trellis
from pathlib import Path
src = Path(sys.argv[2]).resolve()
loaded = Path(trellis.__file__).resolve()
assert src in loaded.parents, f"trellis loaded from {loaded}, not {src}"
importlib.import_module(sys.argv[1])
print("imported", sys.argv[1])
"""


def _module_names() -> list[str]:
    flat = ["app", "boundary", "rejections", "wiring"]
    tools = sorted(
        path.stem for path in (_MCP / "tools").glob("*.py") if path.stem != "__init__"
    )
    return [
        "trellis.mcp.server",
        *(f"trellis.mcp.{name}" for name in flat),
        "trellis.mcp.tools",
        *(f"trellis.mcp.tools.{name}" for name in tools),
        "trellis_api.app",
        "trellis_cli.main",
    ]


def test_the_case_list_covers_every_module() -> None:
    names = _module_names()
    assert len(names) == _EXPECTED_IMPORT_CASES, names
    for name in names:
        top, _, rest = name.partition(".")
        relative = Path(top, *rest.split(".")) if rest else Path(top)
        assert (_SRC / relative).with_suffix(".py").exists() or (
            _SRC / relative / "__init__.py"
        ).exists(), name


@pytest.mark.parametrize("module", _module_names())
def test_module_imports_first_in_a_fresh_process(module: str) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        env = {
            **os.environ,
            "PYTHONPATH": repo_src_pythonpath(),
            "TRELLIS_CONFIG_DIR": scratch,
            "TRELLIS_DATA_DIR": scratch,
        }
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", _PROBE, module, str(_SRC)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert done.returncode == 0, done.stderr[-2000:]
    assert f"imported {module}" in done.stdout
