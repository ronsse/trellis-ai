"""Temporary CI diagnostic: report why a Rich console renders at width 80 with COLUMNS set."""
import os
import sys

import pytest
from rich.console import Console

OUT = "/tmp/width_det.txt"
_prop = Console.size
_count = {"n": 0}


def _log(text):
    with open(OUT, "a") as f:
        f.write(f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')} {text}\n")


def _size(self):
    result = _prop.fget(self)
    columns = os.environ.get("COLUMNS")
    if columns and columns.isdigit() and result.width != int(columns) and _count["n"] < 40:
        _count["n"] += 1
        _log(
            f"MISMATCH test={os.environ.get('PYTEST_CURRENT_TEST')} width={result.width} COLUMNS={columns} "
            f"_width={self._width} _height={self._height} is_terminal={self.is_terminal} "
            f"is_dumb={self.is_dumb_terminal} same_environ={self._environ is os.environ} "
            f"TERM={os.environ.get('TERM')} FORCE_COLOR={os.environ.get('FORCE_COLOR')} "
            f"TTY_COMPATIBLE={os.environ.get('TTY_COMPATIBLE')} "
            f"LINES={os.environ.get('LINES')} force_terminal={self._force_terminal} jupyter={self.is_jupyter}"
        )
    return result


Console.size = property(_size, _prop.fset)


def _env():
    keys = ("COLUMNS", "LINES", "TERM", "FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "PY_COLORS", "CI")
    return {k: os.environ.get(k) for k in keys}


_log(f"PLUGIN-LOAD env={_env()} stdout_tty={sys.stdout.isatty()}")


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session):
    rows = []
    for name, mod in sorted(sys.modules.items()):
        c = getattr(mod, "console", None) if name.startswith("trellis_cli") else None
        if c is not None and hasattr(c, "_width"):
            rows.append(f"{name}={c._width}/{c._height}")
    _log(f"COLLECTED env={_env()} consoles={' '.join(rows)}")
